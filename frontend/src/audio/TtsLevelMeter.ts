/**
 * A live loudness meter on the audio that is actually coming out of the
 * speakers.
 *
 * This is the "measure it" half of the lip-sync design. There are two legitimate
 * ways to know how open the mouth should be, and the controller uses both in
 * order of preference:
 *
 *  1. **The server's `volumes` envelope.** The backend synthesises the audio,
 *     decodes it, and measures a normalised RMS per `lipsync.slice_ms` before
 *     sending it (`pipeline/speech.py` -> `rms_envelope`). That is already a
 *     measurement of the real waveform, it is exactly aligned to the sentence's
 *     own clock, and it costs the browser nothing - so when it is there, it
 *     wins.
 *  2. **This meter.** Web Audio gives us a tap on the master gain, so anything
 *     the *browser* schedules can be measured directly. It covers the cases the
 *     envelope does not: an engine that returns audio without one, a server
 *     configured with `lipsync.enabled=false`, a truncated payload - and it is
 *     the honest answer to "drive the mouth from the sound, not from a guess".
 *
 * What it cannot do: the `browser` TTS transport speaks through the operating
 * system's speech synthesiser, which never touches Web Audio, so there is
 * legitimately nothing to measure there and the controller keeps its
 * speech-shaped fallback for it. `available` reports that state rather than
 * pretending otherwise.
 *
 * The window is `fftSize` samples (~21 ms at 48 kHz). Speech RMS is a small
 * fraction of full scale, so the value is gated (below the gate: definitely
 * silence) and normalised against a reference loudness before the controller
 * sees it; attack/release smoothing lives in the controller so that one set of
 * sliders tunes every path.
 */

import { getAnalyserNode } from './engine';

/** Below this RMS the output is treated as silence (room tone, dither, tail). */
const GATE = 0.006;

/** RMS that reads as a fully open mouth. Conversational TTS lands near it. */
const REFERENCE = 0.28;

export class TtsLevelMeter {
  /**
   * Reused analysis window. The `ArrayBuffer` type argument (rather than the
   * default `ArrayBufferLike`) is what `getFloatTimeDomainData` accepts.
   */
  private buffer: Float32Array<ArrayBuffer> | null = null;

  /** True once there is an audio graph to measure (i.e. audio was unlocked). */
  get available(): boolean {
    return getAnalyserNode() !== null;
  }

  /**
   * Current loudness 0..1, or `null` when there is nothing to measure.
   *
   * `null` is not `0`: it means "no meter", which the controller must not
   * confuse with "silence", because those two want different fallbacks.
   */
  read(): number | null {
    const analyser = getAnalyserNode();
    if (!analyser) return null;

    if (!this.buffer || this.buffer.length !== analyser.fftSize) {
      this.buffer = new Float32Array(analyser.fftSize);
    }
    analyser.getFloatTimeDomainData(this.buffer);

    let sum = 0;
    for (let i = 0; i < this.buffer.length; i += 1) {
      const sample = this.buffer[i];
      sum += sample * sample;
    }
    const rms = Math.sqrt(sum / this.buffer.length);

    if (rms <= GATE) return 0;
    const normalised = (rms - GATE) / (REFERENCE - GATE);
    // Loudness perception tracks the square root of amplitude far better than
    // amplitude itself, and it keeps quiet syllables visible instead of leaving
    // the mouth twitching between shut and slightly-less-shut.
    return Math.min(1, Math.max(0, Math.sqrt(normalised)));
  }
}
