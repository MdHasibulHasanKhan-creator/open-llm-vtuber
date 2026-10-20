/**
 * Microphone capture with voice-activity detection.
 *
 * Pipeline: `getUserMedia` -> `pcm-capture` AudioWorklet (16 kHz Int16, 100 ms
 * blocks) -> energy VAD -> WebSocket.
 *
 * Two things make barge-in feel right:
 *   * a **preroll ring buffer**, so the first phoneme (which arrives *before*
 *     the energy threshold is crossed) is still sent, and
 *   * a **higher threshold while the assistant is talking** (on top of the
 *     browser's own echo cancellation), so the avatar's own voice does not
 *     trigger the microphone.
 */

import { getAudioContext } from './engine';

export type MicMode = 'off' | 'vad' | 'ptt';

export interface MicrophoneEvents {
  /** A 100 ms block of 16 kHz Int16 PCM. */
  onChunk: (bytes: Uint8Array) => void;
  onSpeechStart: () => void;
  onSpeechEnd: () => void;
  /** Smoothed input level, 0..1 - drives the UI meter. */
  onLevel: (level: number) => void;
  onError: (message: string) => void;
}

export interface MicrophoneOptions {
  mode: MicMode;
  /** RMS threshold for normal speech. */
  threshold: number;
  /** RMS threshold while the assistant is speaking (barge-in). */
  bargeInThreshold: number;
  /** Energy must stay above the threshold this long before we commit. */
  attackMs: number;
  /** Silence this long ends the utterance. */
  releaseMs: number;
  /** How much audio to keep from *before* speech was detected. */
  prerollMs: number;
}

export const MIC_DEFAULTS: MicrophoneOptions = {
  mode: 'vad',
  threshold: 0.014,
  bargeInThreshold: 0.032,
  attackMs: 180,
  releaseMs: 750,
  prerollMs: 350,
};

/** One worklet block is 100 ms (see `public/pcm-worklet.js`). */
const BLOCK_MS = 100;
const CAPTURE_RATE = 16000;

let workletLoaded: Promise<void> | null = null;

function loadWorklet(ctx: AudioContext): Promise<void> {
  if (!workletLoaded) {
    workletLoaded = ctx.audioWorklet.addModule('/pcm-worklet.js').catch((error: unknown) => {
      workletLoaded = null;
      throw error;
    });
  }
  return workletLoaded;
}

function rmsOfInt16(bytes: Uint8Array): number {
  const count = bytes.byteLength >> 1;
  if (count === 0) return 0;
  let sum = 0;
  for (let i = 0; i < count; i++) {
    let value = (bytes[i * 2 + 1] << 8) | bytes[i * 2];
    if (value & 0x8000) value -= 0x10000;
    sum += value * value;
  }
  return Math.sqrt(sum / count) / 32768;
}

export class MicrophoneCapture {
  private readonly events: MicrophoneEvents;
  private options: MicrophoneOptions;

  private node: AudioWorkletNode | null = null;
  private sink: GainNode | null = null;
  private running = false;

  /**
   * The last `getUserMedia` stream, kept across `stop()`.
   *
   * Re-requesting it takes 100-400 ms and, in Chrome, re-opens the microphone
   * indicator - which made toggling voice input feel broken. It is only really
   * released by `release()` (mic switched off, or the app is disposed).
   */
  private static sharedStream: MediaStream | null = null;

  /** Preroll ring of recent blocks (oldest first). */
  private ring: Uint8Array[] = [];
  private ringLimit = 1;

  private speaking = false;
  private pushing = false;
  private loudBlocks = 0;
  private quietBlocks = 0;
  private bargeIn = false;
  private smoothedLevel = 0;

  constructor(events: MicrophoneEvents, options: Partial<MicrophoneOptions> = {}) {
    this.events = events;
    this.options = { ...MIC_DEFAULTS };
    this.setOptions(options);
  }

  get isRunning(): boolean {
    return this.running;
  }

  get isSpeaking(): boolean {
    return this.speaking;
  }

  get mode(): MicMode {
    return this.options.mode;
  }

  setOptions(options: Partial<MicrophoneOptions>): void {
    this.options = { ...this.options, ...options } as MicrophoneOptions;
    this.ringLimit = Math.max(1, Math.round(this.options.prerollMs / BLOCK_MS));
    while (this.ring.length > this.ringLimit) this.ring.shift();
  }

  /** Raise/lower the threshold while the avatar is speaking (barge-in mode). */
  setAssistantSpeaking(speaking: boolean): void {
    this.bargeIn = speaking;
  }

  async start(): Promise<void> {
    if (this.running) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      this.events.onError('This browser cannot access the microphone (getUserMedia is unavailable).');
      return;
    }

    try {
      const ctx = getAudioContext();
      // A suspended context produces no callbacks at all, which looks exactly
      // like "the mic is on but nothing is heard".
      if (ctx.state === 'suspended') await ctx.resume().catch(() => undefined);
      await loadWorklet(ctx);

      const stream = await this.acquireStream();

      const source = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, 'pcm-capture', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        channelCount: 1,
        channelCountMode: 'explicit',
      });
      node.port.onmessage = (event: MessageEvent<{ type: string; buffer?: ArrayBuffer }>) => {
        if (event.data.type !== 'pcm' || !event.data.buffer) return;
        this.handleBlock(new Uint8Array(event.data.buffer));
      };

      // A muted sink keeps the graph pulling the worklet on every browser.
      const sink = ctx.createGain();
      sink.gain.value = 0;
      source.connect(node);
      node.connect(sink);
      sink.connect(ctx.destination);

      this.node = node;
      this.sink = sink;
      this.running = true;
    } catch (error) {
      this.events.onError(
        error instanceof DOMException && error.name === 'NotAllowedError'
          ? 'Microphone permission was denied.'
          : `Could not open the microphone: ${error instanceof Error ? error.message : String(error)}`,
      );
      this.stop();
    }
  }

  /** Release the cached stream (mic switched off, or the app is going away). */
  release(): void {
    this.stop();
    const stream = MicrophoneCapture.sharedStream;
    MicrophoneCapture.sharedStream = null;
    for (const track of stream?.getTracks() ?? []) {
      track.onended = null;
      track.stop();
    }
  }

  stop(): void {
    this.node?.port.postMessage('flush');
    if (this.node) {
      this.node.port.onmessage = null;
      try {
        this.node.disconnect();
      } catch {
        /* already detached */
      }
    }
    try {
      this.sink?.disconnect();
    } catch {
      /* already detached */
    }

    // The stream itself is intentionally *not* stopped - see `sharedStream`.
    this.node = null;
    this.sink = null;
    this.running = false;
    this.finishUtterance(false);
    this.ring = [];
    this.smoothedLevel = 0;
  }

  /** The cached stream if it is still live, otherwise a fresh one. */
  private async acquireStream(): Promise<MediaStream> {
    const cached = MicrophoneCapture.sharedStream;
    if (cached && cached.getAudioTracks().some((track) => track.readyState === 'live')) {
      return cached;
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    MicrophoneCapture.sharedStream = stream;

    for (const track of stream.getTracks()) {
      track.onended = () => {
        // Unplugged, revoked, or taken over (often another tab): say so instead
        // of going silently deaf.
        if (MicrophoneCapture.sharedStream === stream) MicrophoneCapture.sharedStream = null;
        if (this.running) {
          this.stop();
          this.events.onError('The microphone stopped (unplugged, or another app took it).');
        }
      };
    }
    return stream;
  }

  /* ------------------------------------------------------------------ */
  /*  Push to talk                                                       */
  /* ------------------------------------------------------------------ */

  beginPushToTalk(): void {
    if (this.options.mode !== 'ptt' || !this.running) return;
    this.pushing = true;
    this.ring = [];
    this.beginUtterance();
  }

  endPushToTalk(): void {
    if (this.options.mode !== 'ptt') return;
    this.pushing = false;
    this.node?.port.postMessage('flush');
    this.finishUtterance(true);
  }

  /* ------------------------------------------------------------------ */
  /*  Block handling                                                     */
  /* ------------------------------------------------------------------ */

  private handleBlock(bytes: Uint8Array): void {
    const level = rmsOfInt16(bytes);
    this.smoothedLevel = this.smoothedLevel * 0.7 + level * 0.3;
    this.events.onLevel(Math.min(1, this.smoothedLevel * 6));

    if (this.options.mode === 'off') return;

    if (this.options.mode === 'ptt') {
      if (this.pushing) this.emit(bytes);
      return;
    }

    // Voice activity mode: remember the block first (it may *be* the onset).
    this.ring.push(bytes);
    while (this.ring.length > this.ringLimit) this.ring.shift();

    const threshold = this.bargeIn ? this.options.bargeInThreshold : this.options.threshold;
    const attackBlocks = Math.max(1, Math.round(this.options.attackMs / BLOCK_MS));
    const releaseBlocks = Math.max(1, Math.round(this.options.releaseMs / BLOCK_MS));

    if (!this.speaking) {
      if (level > threshold) {
        this.loudBlocks += 1;
        if (this.loudBlocks >= attackBlocks) {
          // The ring already holds the attack *and* this block.
          this.beginUtterance();
        }
      } else {
        this.loudBlocks = 0;
      }
      return;
    }

    this.emit(bytes);
    if (level > threshold * 0.65) {
      this.quietBlocks = 0;
    } else {
      this.quietBlocks += 1;
      if (this.quietBlocks >= releaseBlocks) this.finishUtterance(true);
    }
  }

  private beginUtterance(): void {
    if (this.speaking) return;
    this.speaking = true;
    this.quietBlocks = 0;
    this.loudBlocks = 0;
    this.events.onSpeechStart();
    for (const block of this.ring) this.events.onChunk(block);
    this.ring = [];
  }

  private finishUtterance(notify: boolean): void {
    const was = this.speaking;
    this.speaking = false;
    this.loudBlocks = 0;
    this.quietBlocks = 0;
    if (was && notify) this.events.onSpeechEnd();
  }

  private emit(bytes: Uint8Array): void {
    if (this.options.mode === 'off') return;
    this.events.onChunk(bytes);
  }
}

export { CAPTURE_RATE };
