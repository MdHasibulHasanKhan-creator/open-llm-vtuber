/**
 * Real-time lip-sync.
 *
 * Two modes, chosen per sentence by the backend (`tts.start.lipsync_mode`):
 *
 * **`mouth`** (the default) - the simplest thing that looks right. The backend
 * ships a normalised RMS envelope (`volumes`, one value per `lipsync.slice_ms`)
 * and we turn it into a *single* number: how far the mouth is open. Below
 * `mouth_threshold` the mouth is shut; above it, the envelope above the
 * threshold is shaped, scaled by `mouth_scale` and capped at `mouth_max`. The
 * cap matters because the envelope is normalised to its own peak, so a plain
 * linear gain turns every stressed vowel into a fully open jaw - a shout, not a
 * sentence. A hysteresis band and a minimum dwell time stop a level that hovers
 * on the threshold from making the mouth flutter, and the usual attack/release
 * smoothing keeps every transition soft. Only the `aa` blendshape is ever
 * written, so there is nothing to jitter.
 *
 * **`visemes`** - the full mouth-shape timeline (`[[start, end, "aa", w], ...]`),
 * snapped to the TTS engine's word boundaries when it provided them, optionally
 * modulated by the same envelope. More expressive on a model with well authored
 * mouth shapes; more fragile everywhere else. `nn` means "closed".
 *
 * Sentences are *queued* rather than replaced, because the backend synthesises
 * and sends the whole reply as fast as it can - the second sentence's frames
 * arrive while the first is still being spoken. Each sentence carries its own
 * clock that starts at zero when *its* audio does, and we simply pick whichever
 * sentence is currently playing.
 *
 * ## Where the loudness comes from
 *
 * Three sources, in order, so that the mouth is never driven by a guess when a
 * measurement is available:
 *
 * 1. the sentence's own `volumes` envelope (measured by the backend from the
 *    real waveform, and aligned to this sentence's clock);
 * 2. a live `AnalyserNode` tap on the master output (`audio/TtsLevelMeter.ts`)
 *    for any audio the browser is playing through Web Audio;
 * 3. a speech-shaped synthesis for the `browser` TTS transport, whose audio the
 *    page never gets to see (the OS speech synthesiser plays it).
 */

import type { VRM } from '@pixiv/three-vrm';

import { DEFAULT_LIPSYNC, MOUTH_PRESETS, VISEME_TO_PRESET } from './constants';
import { clamp01, smoothTowards } from './smooth';
import type { Clock, FrameController } from './types';
import { TtsLevelMeter } from '../audio/TtsLevelMeter';
import type { LipsyncMode, LipsyncSettings } from '../types/config';
import type { TtsStartMessage, VisemeFrame } from '../types/protocol';

/** How long a finished sentence is kept before it is forgotten. */
const TAIL_SECONDS = 0.3;

/** Smallest opening a "speaking" frame produces, so quiet syllables still read. */
const MOUTH_FLOOR = 0.04;

/**
 * Shape applied to the normalised envelope before the gain.
 *
 * A raw RMS envelope spends most of a syllable near its top, so a linear map
 * opens the jaw the same amount for "ah" and "ee". A mild power curve lifts the
 * quiet part (so whispers still move the lips) while leaving headroom at the
 * top, which is what makes the mouth look like it is speaking rather than
 * flapping open and shut.
 */
const MOUTH_CURVE = 0.8;

/**
 * Time constant for closing the mouth after a barge-in (seconds).
 *
 * Reusing the normal `release_ms` here reads as a lag: the audio has already
 * stopped dead, so a mouth that keeps drifting shut for another ~200 ms looks
 * disconnected from it. 60 ms is short enough to be an "instant" interrupt and
 * still a fade rather than a snap.
 */
const INTERRUPT_RELEASE = 0.06;

interface Utterance {
  frames: VisemeFrame[];
  volumes: number[];
  clock: Clock;
  duration: number;
  pointer: number;
  text: string;
  /** Set from the sentence's own header, so a mid-reply patch cannot strand it. */
  mode: LipsyncMode | null;
  /**
   * True when this sentence's audio is scheduled through Web Audio (so the
   * `AnalyserNode` can see it). False for the `browser` transport, whose sound
   * the page never touches.
   */
  audio: boolean;
}

export class LipSyncController implements FrameController {
  private vrm: VRM | null = null;
  private tuning: LipsyncSettings = { ...DEFAULT_LIPSYNC };
  private items: Utterance[] = [];
  private readonly values = new Map<string, number>();
  private readonly meter = new TtsLevelMeter();
  private openness = 0;
  /** True while the mouth is closing because of a barge-in / stop. */
  private interrupting = false;

  /** Frame clock in seconds, used for hysteresis / dwell timing. */
  private time = 0;
  private mouthOpen = false;
  private mouthSince = 0;
  private mouthUtterance: Utterance | null = null;

  constructor(tuning: Partial<LipsyncSettings> = {}) {
    this.configure(tuning);
  }

  attach(vrm: VRM | null): void {
    this.vrm = vrm;
    this.values.clear();
    this.clear();
  }

  /** Live-updated from the backend `lipsync` section (settings drawer). */
  configure(tuning: Partial<LipsyncSettings>): void {
    const previous = this.tuning.mode;
    this.tuning = { ...this.tuning, ...tuning };
    if (this.tuning.mode !== previous) this.resetMouth();
    if (!this.tuning.enabled) this.clear();
  }

  get config(): LipsyncSettings {
    return { ...this.tuning };
  }

  get mode(): LipsyncMode {
    return this.tuning.mode;
  }

  get active(): boolean {
    return this.items.length > 0;
  }

  /** Text of the sentence currently being spoken, if any. */
  get activeText(): string {
    return this.activeUtterance?.text ?? '';
  }

  /** How far the mouth is open right now, 0..1 (drives secondary motion). */
  get mouthOpenness(): number {
    return this.openness;
  }

  /**
   * Queue a sentence. `clock` returns seconds since *that sentence's* audio
   * began - negative while it is still queued behind another one.
   */
  enqueue(header: TtsStartMessage, clock: Clock): void {
    if (!this.tuning.enabled) return;
    this.items.push({
      frames: normaliseFrames(header.visemes),
      volumes: header.volumes ?? [],
      clock,
      duration: header.duration > 0 ? header.duration : 0,
      pointer: 0,
      text: header.text,
      mode: normaliseMode(header.lipsync_mode),
      audio: header.format !== 'browser',
    });
    if (this.items.length > 8) this.items.shift();
  }

  /** Relax the mouth (barge-in, reset, or audio failure). */
  clear(): void {
    this.items = [];
    this.interrupting = this.openness > 0.01;
    this.resetMouth();
  }

  /** Alias so the controller can call `stop()` uniformly. */
  stop(): void {
    this.clear();
  }

  update(delta: number): void {
    this.time += delta;
    const manager = this.vrm?.expressionManager;

    this.prune();
    const active = this.activeUtterance;
    if (active !== this.mouthUtterance) {
      // A new sentence starts from a clean hysteresis state.
      this.resetMouth();
      this.mouthUtterance = active;
    }

    const targets = new Map<string, number>();
    for (const preset of MOUTH_PRESETS) targets.set(preset, 0);

    if (manager && active) {
      const time = active.clock();
      if (Number.isFinite(time) && time >= 0) {
        if (this.modeFor(active) === 'mouth') {
          const open = this.mouthTarget(active, time);
          if (open > 0) targets.set('aa', open);
        } else {
          const frame = frameAt(active, time);
          if (frame) {
            const preset = VISEME_TO_PRESET[frame[2]] ?? null;
            if (preset) {
              // The same ceiling the `mouth` mode uses: `aa` at 1.0 is a shout
              // on a VRoid face, and a viseme timeline is not a reason to make
              // every vowel a shout too.
              const ceiling = clamp01(this.tuning.mouth_max);
              const weight = Math.min(
                ceiling,
                clamp01(frame[3] * this.tuning.viseme_gain * this.envelopeAt(active, time)),
              );
              if (weight > 0) targets.set(preset, Math.max(targets.get(preset) ?? 0, weight));
            }
          }
        }
      }
    }

    const attack = Math.max(0.001, this.tuning.attack_ms / 1000);
    const release = Math.max(0.001, this.tuning.release_ms / 1000);
    // A barge-in closes faster than a sentence ending naturally.
    const closing = this.interrupting ? Math.min(release, INTERRUPT_RELEASE) : release;

    let openness = 0;
    for (const preset of MOUTH_PRESETS) {
      const target = targets.get(preset) ?? 0;
      const previous = this.values.get(preset) ?? 0;
      const tau = target > previous ? attack : closing;
      const next = smoothTowards(previous, target, delta, tau);
      this.values.set(preset, next === 0 ? 0 : next);
      manager?.setValue(preset, next);
      if (next > openness) openness = next;
    }
    this.openness = openness;
    if (this.interrupting && openness <= 0.005) this.interrupting = false;
  }

  /* ------------------------------------------------------------------ */
  /*  Internals                                                          */
  /* ------------------------------------------------------------------ */

  private modeFor(utterance: Utterance): LipsyncMode {
    return utterance.mode ?? this.tuning.mode;
  }

  private resetMouth(): void {
    this.mouthOpen = false;
    // `-Infinity` so the very first syllable may open the mouth immediately
    // instead of waiting out the minimum dwell time.
    this.mouthSince = -Infinity;
    this.mouthUtterance = null;
  }

  /**
   * `mouth` mode: one target for the `aa` blendshape.
   *
   * The open/closed *decision* is hysteretic and rate limited, but the amount is
   * continuous, so what the viewer sees is a mouth that follows the loudness of
   * the voice instead of snapping.
   */
  private mouthTarget(utterance: Utterance, time: number): number {
    const threshold = clamp01(this.tuning.mouth_threshold);
    const hysteresis = Math.max(0, this.tuning.mouth_hysteresis);
    const dwell = Math.max(0, this.tuning.mouth_min_dwell_ms) / 1000;
    const level = this.levelAt(utterance, time);

    const canSwitch = this.time - this.mouthSince >= dwell;
    if (!this.mouthOpen) {
      if (level >= threshold && canSwitch) {
        this.mouthOpen = true;
        this.mouthSince = this.time;
      }
    } else if (level <= threshold - hysteresis && canSwitch) {
      this.mouthOpen = false;
      this.mouthSince = this.time;
    }

    if (!this.mouthOpen) return 0;

    // Work in the part of the envelope that is actually speech (threshold..1).
    const span = Math.max(1e-3, 1 - threshold);
    const normalised = clamp01((level - threshold) / span);
    const gain = Math.max(0.05, this.tuning.mouth_scale);
    const open = MOUTH_FLOOR + Math.pow(normalised, MOUTH_CURVE) * gain;
    return clamp01(Math.min(clamp01(this.tuning.mouth_max), open));
  }

  /**
   * The loudness (0..1) driving the mouth at `time`.
   *
   * Measurement beats guesswork: the sentence's own envelope if there is one,
   * otherwise the live analyser tap, otherwise a speech-shaped synthetic level.
   */
  private levelAt(utterance: Utterance, time: number): number {
    const volumes = utterance.volumes;
    if (volumes.length > 0) return sampleEnvelope(volumes, time, this.tuning.slice_ms);
    if (utterance.audio) {
      const live = this.meter.read();
      if (live !== null) return live;
    }
    // Browser speech: the OS plays it, so there is nothing in the audio graph to
    // analyse. A speech-shaped synthetic level keeps one code path.
    return syntheticLevel(time, utterance.duration);
  }

  /** Drop sentences that have played out. */
  private prune(): void {
    while (this.items.length > 0) {
      const head = this.items[0];
      const time = head.clock();
      if (!Number.isFinite(time)) break; // clock not live yet
      // With more sentences waiting the head can go immediately; the last one
      // is kept a little longer so a jittery clock cannot clip its final viseme.
      const grace = this.items.length > 1 ? 0 : 1.2;
      const expireAt = Math.max(head.duration, 0) + TAIL_SECONDS + grace;
      if (time < expireAt) break;
      this.items.shift();
    }
  }

  /** The most recently started sentence; `null` when nothing is playing yet. */
  private get activeUtterance(): Utterance | null {
    let active: Utterance | null = null;
    for (const item of this.items) {
      const time = item.clock();
      if (Number.isFinite(time) && time >= 0) active = item;
    }
    return active;
  }

  /** Envelope multiplier, 1 when modulation is off. */
  private envelopeAt(utterance: Utterance, time: number): number {
    if (!this.tuning.amplitude_modulation || utterance.volumes.length === 0) return 1;
    const floor = clamp01(this.tuning.amplitude_floor);
    return floor + (1 - floor) * this.levelAt(utterance, time);
  }
}

/**
 * The normalised envelope value (0..1) at `time`, **linearly interpolated**
 * between slices.
 *
 * The backend measures one value per `lipsync.slice_ms` (20 ms by default),
 * which is a 50 Hz staircase - slower than the frame rate, so nearest-slice
 * lookup makes the mouth step rather than move. Interpolating is what turns the
 * envelope back into the continuous curve it was sampled from. Outside the
 * envelope the level is 0, so the mouth closes rather than freezing open.
 */
function sampleEnvelope(volumes: number[], time: number, sliceMs: number): number {
  if (time < 0) return 0;
  const slice = Math.max(0.005, sliceMs / 1000);
  const position = time / slice;
  const index = Math.floor(position);
  if (index >= volumes.length) return 0;
  const current = clamp01(volumes[index] ?? 0);
  const next = index + 1 < volumes.length ? clamp01(volumes[index + 1] ?? 0) : current;
  const fraction = position - index;
  return current + (next - current) * fraction;
}

/**
 * A speech-shaped level used when there is no audio to measure
 * (`format: "browser"`). Two incommensurate frequencies at roughly syllable
 * rate, faded in and out across the sentence.
 */
export function syntheticLevel(time: number, duration: number): number {
  if (duration <= 0 || time < 0 || time > duration) return 0;
  const attack = Math.min(0.12, duration * 0.1);
  const release = Math.min(0.2, duration * 0.15);
  const rise = Math.min(1, time / Math.max(0.001, attack));
  const fall = Math.min(1, (duration - time) / Math.max(0.001, release));
  const syllable = 0.5 + 0.5 * Math.sin(time * Math.PI * 9.0);
  const slow = 0.5 + 0.5 * Math.sin(time * Math.PI * 1.4 + 1.1);
  return clamp01(rise * fall * (0.35 + 0.65 * syllable) * (0.6 + 0.4 * slow));
}

/** The frame covering `time`, or `null` during a gap. Frames are ordered. */
function frameAt(utterance: Utterance, time: number): VisemeFrame | null {
  const frames = utterance.frames;
  if (frames.length === 0) return null;

  let index = utterance.pointer;
  if (index >= frames.length) index = frames.length - 1;
  while (index < frames.length && frames[index][1] <= time) index += 1;
  utterance.pointer = index;

  if (index >= frames.length) return null;
  const frame = frames[index];
  return time >= frame[0] ? frame : null;
}

/** Sort + drop malformed frames so the runtime never has to defend itself. */
function normaliseFrames(raw: VisemeFrame[] | undefined): VisemeFrame[] {
  if (!raw || raw.length === 0) return [];
  const frames: VisemeFrame[] = [];
  for (const entry of raw) {
    if (!Array.isArray(entry) || entry.length < 4) continue;
    const start = Number(entry[0]);
    const end = Number(entry[1]);
    const viseme = String(entry[2]);
    const weight = Number(entry[3]);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    if (!(viseme in VISEME_TO_PRESET)) continue;
    frames.push([start, end, viseme, Number.isFinite(weight) ? weight : 1]);
  }
  frames.sort((a, b) => a[0] - b[0]);
  return frames;
}

function normaliseMode(raw: string | undefined): LipsyncMode | null {
  if (raw === 'mouth' || raw === 'visemes') return raw;
  return null;
}
