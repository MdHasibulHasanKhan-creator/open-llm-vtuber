/**
 * Browser speech synthesis ("speak it on the client" mode).
 *
 * Used when the backend reports `tts.format === "browser"`: the server still
 * computes the viseme timeline but ships no audio, and we voice the sentence
 * here with the Web Speech API.
 *
 * Two details matter:
 *
 *  * `speechSynthesis.speak()` *queues* utterances and plays them in order, so
 *    a whole multi-sentence reply is handed over as it arrives and still sounds
 *    like one continuous answer.
 *  * the API exposes no playback position, so each utterance estimates its own
 *    clock from `onboundary` events (which carry the character index being
 *    spoken) and re-anchors on every one of them, smoothing the correction so
 *    the mouth never jumps.
 */

import type { Voice } from '../types/config';

export interface SpeechHandle {
  id: number;
  /** Server-side estimate of the sentence length, in seconds. */
  duration: number;
  /** Seconds since *this* sentence started (negative before it starts). */
  clock: () => number;
  /** Resolves when the sentence finishes (or is cancelled). */
  done: Promise<void>;
  stop: () => void;
}

export interface SpeakOptions {
  id: number;
  text: string;
  /** Seconds - the backend's estimate for this sentence. */
  duration: number;
  /** BCP-47 tag, e.g. `en-US` or `ja-JP`. */
  lang: string;
  /** Voice hint, e.g. `en-US-AriaNeural`; matched loosely against local voices. */
  voice?: string;
  /** edge-tts style `"+10%"`. */
  rate?: string;
  pitch?: string;
  volume?: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** edge-tts style percentage -> a `SpeechSynthesisUtterance.rate` multiplier. */
export function parseRate(rate: string | undefined): number {
  const percent = Number.parseFloat((rate ?? '').replace('%', ''));
  if (!Number.isFinite(percent)) return 1;
  return clamp(1 + percent / 100, 0.5, 2);
}

function parsePitch(pitch: string | undefined): number {
  const hz = Number.parseFloat((pitch ?? '').replace(/hz/i, ''));
  if (!Number.isFinite(hz)) return 1;
  return clamp(1 + hz / 100, 0, 2);
}

export class SpeechSynthesisPlayer {
  private pending = new Set<() => void>();

  get available(): boolean {
    return typeof window !== 'undefined' && 'speechSynthesis' in window;
  }

  /** Voices for the settings drawer, filtered by language when given. */
  getVoices(lang?: string): Voice[] {
    if (!this.available) return [];
    const primary = (lang ?? '').toLowerCase().split('-')[0];
    return window.speechSynthesis
      .getVoices()
      .filter((voice) => !primary || voice.lang.toLowerCase().startsWith(primary))
      .map((voice) => ({ name: voice.name, locale: voice.lang }));
  }

  /** Queue one sentence. Returns a handle whose clock tracks its playback. */
  speak(options: SpeakOptions): SpeechHandle {
    let anchor = Number.POSITIVE_INFINITY;
    const startedAt = (): number => performance.now() / 1000;

    if (!this.available) {
      anchor = startedAt();
      return {
        id: options.id,
        duration: options.duration,
        clock: () => startedAt() - anchor,
        done: Promise.resolve(),
        stop: () => undefined,
      };
    }

    const utterance = new SpeechSynthesisUtterance(options.text);
    utterance.lang = options.lang;
    utterance.rate = parseRate(options.rate);
    utterance.pitch = parsePitch(options.pitch);
    if (typeof options.volume === 'number') utterance.volume = clamp(options.volume, 0, 1);

    const voice = this.pickVoice(options.lang, options.voice);
    if (voice) utterance.voice = voice;

    const estimate = Math.max(0.2, options.duration);
    const length = Math.max(1, options.text.length);

    let finish: () => void = () => undefined;
    const done = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const settle = (): void => {
      this.pending.delete(settle);
      finish();
    };
    this.pending.add(settle);

    utterance.onstart = () => {
      // Playback actually began: this is t = 0 for the viseme timeline.
      anchor = startedAt();
    };
    utterance.onboundary = (event: SpeechSynthesisEvent) => {
      if (!Number.isFinite(anchor)) return;
      if (event.name && event.name !== 'word' && event.name !== 'sentence') return;
      const estimated = estimate * clamp(event.charIndex / length, 0, 1);
      const actual = startedAt() - anchor;
      // Correct only a fraction of the error: the estimate is coarse and a hard
      // snap would visibly flicker the mouth.
      const correction = clamp(estimated - actual, -0.25, 0.25) * 0.6;
      anchor = startedAt() - (actual + correction);
    };
    utterance.onend = settle;
    utterance.onerror = settle;

    window.speechSynthesis.speak(utterance);

    return {
      id: options.id,
      duration: estimate,
      clock: () => (Number.isFinite(anchor) ? startedAt() - anchor : -1),
      done,
      stop: () => {
        try {
          window.speechSynthesis.cancel();
        } catch {
          /* nothing queued */
        }
        settle();
      },
    };
  }

  /** Barge-in: drop everything queued or playing. */
  cancelAll(): void {
    if (!this.available) return;
    try {
      window.speechSynthesis.cancel();
    } catch {
      /* nothing queued */
    }
    for (const settle of [...this.pending]) settle();
    this.pending.clear();
  }

  get isSpeaking(): boolean {
    return this.pending.size > 0;
  }

  private pickVoice(lang: string, hint?: string): SpeechSynthesisVoice | null {
    const voices = window.speechSynthesis.getVoices();
    if (voices.length === 0) return null;

    const wanted = lang.toLowerCase();
    const primary = wanted.split('-')[0];
    const hintLower = (hint ?? '').toLowerCase();

    if (hintLower) {
      // `en-US-AriaNeural` -> `aria`, which local voice names do contain.
      const stem = hintLower.replace(/neural$/, '').replace(/^[a-z]{2}-[a-z]{2}-/, '');
      const named = voices.find(
        (voice) => voice.lang.toLowerCase().startsWith(primary) && voice.name.toLowerCase().includes(stem),
      );
      if (named) return named;
    }

    return (
      voices.find((voice) => voice.lang.toLowerCase() === wanted) ??
      voices.find((voice) => voice.lang.toLowerCase().startsWith(primary)) ??
      null
    );
  }
}
