/**
 * Client-side speech recognition (Web Speech API).
 *
 * This is the "zero install" path: `hello.client_side_asr = true` tells the
 * backend to expect typed `chat` messages instead of `audio.*` frames, and the
 * browser does the transcription itself. Final results are forwarded to the
 * conversation controller, which sends them as a normal `chat` message.
 *
 * The Web Speech API is the flakiest part of the whole app, and almost all of
 * the flakiness is one error: ``network``. In Chrome that does **not** mean the
 * user's connection is down - it means the browser could not reach *its own*
 * speech service (offline, behind a proxy/firewall, a VPN, or a corporate
 * policy). The previous implementation reported it as a generic error, then let
 * `onend` restart 250 ms later, forever: the log filled up and the mic never
 * worked. So this version:
 *
 *  * classifies every error code and only retries the transient ones,
 *  * backs off exponentially (1 s -> 30 s) instead of hammering, and gives up
 *    with an actionable message after a few consecutive network failures,
 *  * exposes a state callback so the UI can show *why* the mic is not
 *    listening, and
 *  * can be paused while the avatar speaks, because the recogniser happily
 *    transcribes the TTS output coming out of the speakers.
 */

export type BrowserASRState =
  | 'idle'
  | 'starting'
  | 'listening'
  | 'retrying'
  | 'paused'
  | 'stopped'
  | 'unavailable'
  | 'error';

export interface BrowserASREvents {
  onPartial: (text: string) => void;
  onFinal: (text: string) => void;
  /** A message worth showing; `fatal` means "stop offering this engine". */
  onError: (message: string, fatal?: boolean) => void;
  onState: (state: BrowserASRState, detail?: string) => void;
  onEnd: () => void;
}

/* The Web Speech API is not in lib.dom.d.ts: declare only what we use. */
interface SpeechRecognitionAlternativeLike {
  transcript: string;
  confidence: number;
}

interface SpeechRecognitionResultLike {
  readonly length: number;
  isFinal: boolean;
  item(index: number): SpeechRecognitionAlternativeLike;
  [index: number]: SpeechRecognitionAlternativeLike;
}

interface SpeechRecognitionResultListLike {
  readonly length: number;
  item(index: number): SpeechRecognitionResultLike;
  [index: number]: SpeechRecognitionResultLike;
}

interface SpeechRecognitionEventLike extends Event {
  resultIndex: number;
  results: SpeechRecognitionResultListLike;
}

interface SpeechRecognitionErrorEventLike extends Event {
  error: string;
  message: string;
}

interface SpeechRecognitionLike extends EventTarget {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): SpeechRecognitionCtor | null {
  const scope = window as Window & {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null;
}

/** First retry delay, and the ceiling. Backoff doubles between the two. */
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30_000;
/** Consecutive `network` failures before we stop trying and say so. */
const NETWORK_FAILURE_LIMIT = 4;

/**
 * Errors that will never get better by trying again. Everything not listed
 * here is treated as transient.
 */
const FATAL_ERRORS: Record<string, string> = {
  'not-allowed': 'Microphone permission was blocked. Allow the mic for this site and start again.',
  'service-not-allowed':
    'This browser refused speech recognition (service-not-allowed). A secure context (https or localhost) is required.',
  'audio-capture': 'No microphone was found. Plug one in, or switch to the server-side engine.',
  'language-not-supported': 'This browser cannot recognise that language. Pick another voice language.',
  'bad-grammar': 'The browser rejected the recognition settings; the server-side engine still works.',
};

const TRANSIENT_ERRORS: Record<string, string> = {
  network:
    'The browser could not reach its speech service. This is usually Chrome being offline or blocked by a proxy/VPN - the server-side engine works without it.',
  'no-speech': 'Did not catch anything.',
  aborted: 'Recognition was interrupted.',
};

export class BrowserASR {
  private recognition: SpeechRecognitionLike | null = null;
  private readonly events: BrowserASREvents;
  private wantRunning = false;
  private paused = false;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private lang = 'en-US';
  private state: BrowserASRState = 'idle';

  constructor(events: BrowserASREvents) {
    this.events = events;
  }

  static get available(): boolean {
    return recognitionCtor() !== null;
  }

  get isRunning(): boolean {
    return this.wantRunning;
  }

  get currentState(): BrowserASRState {
    return this.state;
  }

  /** How many consecutive transient failures we have seen (0 = healthy). */
  get errorStreak(): number {
    return this.failures;
  }

  start(lang = 'en-US'): void {
    this.lang = lang;
    const Ctor = recognitionCtor();
    if (!Ctor) {
      this.setState('unavailable');
      this.events.onError(
        'This browser has no built-in speech recognition. Use the server-side engine instead.',
        true,
      );
      return;
    }

    this.wantRunning = true;
    this.paused = false;
    this.failures = 0;
    this.clearRestart();
    this.open();
  }

  stop(): void {
    this.wantRunning = false;
    this.paused = false;
    this.clearRestart();
    this.teardown();
    this.setState('stopped');
  }

  /**
   * Stop feeding transcripts while the avatar is talking, without tearing the
   * recogniser down: the network stays warm, so resuming is instant.
   */
  pause(): void {
    if (!this.wantRunning || this.paused) return;
    this.paused = true;
    this.clearRestart();
    this.teardown();
    this.setState('paused');
  }

  resume(): void {
    if (!this.wantRunning || !this.paused) return;
    this.paused = false;
    this.failures = 0;
    this.open();
  }

  /* ------------------------------------------------------------------ */
  /*  Internals                                                          */
  /* ------------------------------------------------------------------ */

  private open(): void {
    if (!this.wantRunning || this.paused || this.recognition) return;
    const Ctor = recognitionCtor();
    if (!Ctor) return;

    const recognition = new Ctor();
    recognition.lang = this.lang;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      this.failures = 0;
      this.setState('listening');
    };

    recognition.onresult = (event) => {
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const text = result[0]?.transcript ?? '';
        if (result.isFinal) {
          const final = text.trim();
          if (final) this.events.onFinal(final);
        } else {
          interim += text;
        }
      }
      this.events.onPartial(interim.trim());
    };

    recognition.onerror = (event) => {
      const code = event.error || 'unknown';
      const fatal = FATAL_ERRORS[code];
      if (fatal) {
        // Retrying cannot help: stop wanting to run so we do not loop.
        this.wantRunning = false;
        this.events.onError(fatal, true);
        this.setState('error', fatal);
        return;
      }
      const transient = TRANSIENT_ERRORS[code];
      if (transient) {
        if (code === 'no-speech' || code === 'aborted') {
          // Normal churn: Chrome fires these on every pause in speech.
          return;
        }
        this.failures += 1;
        const giveUp = this.failures >= NETWORK_FAILURE_LIMIT;
        this.events.onError(
          giveUp
            ? `${transient} Switched off after ${this.failures} attempts - use the server-side engine (Input tab).`
            : transient,
          giveUp,
        );
        this.setState(giveUp ? 'error' : 'retrying', transient);
        if (giveUp) this.wantRunning = false;
        return;
      }
      this.events.onError(`Speech recognition error: ${code || event.message}`);
      this.setState('error', code);
    };

    recognition.onend = () => {
      if (this.recognition !== recognition) return; // superseded by a teardown
      this.recognition = null;
      this.events.onEnd();

      if (!this.wantRunning || this.paused) {
        this.setState(this.wantRunning ? 'paused' : 'stopped');
        return;
      }
      // Chrome ends the session after a pause in speech; that is expected.
      // A *failure* backed off instead, so we do not spin on a dead service.
      const backoff = this.failures
        ? Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** (this.failures - 1))
        : 250;
      const delay = backoff * (0.8 + Math.random() * 0.4);
      if (this.failures) this.setState('retrying');
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        this.open();
      }, delay);
    };

    this.recognition = recognition;
    try {
      recognition.start();
    } catch {
      // `start()` throws when a previous session has not released the mic yet.
      this.recognition = null;
      this.scheduleReopen(500);
    }
  }

  private scheduleReopen(delayMs: number): void {
    if (!this.wantRunning || this.paused || this.restartTimer !== null) return;
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      this.open();
    }, delayMs);
  }

  private teardown(): void {
    const recognition = this.recognition;
    this.recognition = null;
    if (!recognition) return;
    recognition.onend = null;
    recognition.onerror = null;
    recognition.onresult = null;
    recognition.onstart = null;
    try {
      recognition.abort();
    } catch {
      /* already stopped */
    }
  }

  private clearRestart(): void {
    if (this.restartTimer !== null) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
  }

  private setState(state: BrowserASRState, detail?: string): void {
    if (this.state === state && !detail) return;
    this.state = state;
    this.events.onState(state, detail);
  }
}
