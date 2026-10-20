/**
 * One AudioContext for the whole app.
 *
 * Browsers refuse to start audio before a user gesture, so nothing is created
 * until `unlockAudio()` runs from a click. Everything that makes sound (the TTS
 * player) or captures it (the microphone worklet) hangs off this context, which
 * keeps sample-rate conversions and device switching consistent.
 */

type AudioContextCtor = typeof AudioContext;

let context: AudioContext | null = null;
let master: GainNode | null = null;
/**
 * A pass-through tap on the master output, used by the lip-sync meter.
 *
 * `AnalyserNode` does not modify the signal it passes, so it is inserted *in*
 * the chain (`master -> analyser -> destination`) rather than hung off the side:
 * a node with no output connection is not guaranteed to be pulled by the
 * renderer, and a mouth that works in one browser and not another is not worth
 * the saved node.
 */
let analyser: AnalyserNode | null = null;

function ctor(): AudioContextCtor {
  const legacy = (window as Window & { webkitAudioContext?: AudioContextCtor }).webkitAudioContext;
  const Ctor = window.AudioContext ?? legacy;
  if (!Ctor) throw new Error('This browser has no Web Audio support.');
  return Ctor;
}

export function getAudioContext(): AudioContext {
  if (context) return context;
  const Ctor = ctor();
  const ctx = new Ctor({ latencyHint: 'interactive' });
  context = ctx;

  master = ctx.createGain();
  master.gain.value = 1;

  analyser = ctx.createAnalyser();
  // ~21 ms at 48 kHz: long enough for a stable RMS, short enough to catch the
  // onset of a syllable. No extra smoothing here - the lip-sync controller owns
  // attack/release, in one place, driven by the settings drawer's sliders.
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0;

  master.connect(analyser);
  analyser.connect(ctx.destination);
  return ctx;
}

/** The node every audio source must connect to (volume lives here). */
export function getMasterGain(): GainNode {
  getAudioContext();
  return master as GainNode;
}

/**
 * The lip-sync tap, or `null` before the audio graph exists.
 *
 * Deliberately does **not** create the context: "is there anything to measure?"
 * must never be the thing that unlocks audio.
 */
export function getAnalyserNode(): AnalyserNode | null {
  return analyser;
}

/** Call from a click handler: creates + resumes the context. */
export async function unlockAudio(): Promise<AudioContext> {
  const ctx = getAudioContext();
  if (ctx.state !== 'running') {
    try {
      await ctx.resume();
    } catch {
      /* the next gesture will retry */
    }
  }
  return ctx;
}

export function isAudioUnlocked(): boolean {
  return context !== null && context.state === 'running';
}

export function setOutputVolume(volume: number): void {
  const gain = getMasterGain();
  gain.gain.value = Math.min(1, Math.max(0, volume));
}

export async function closeAudio(): Promise<void> {
  const ctx = context;
  context = null;
  master = null;
  analyser = null;
  if (ctx && ctx.state !== 'closed') {
    try {
      await ctx.close();
    } catch {
      /* already closing */
    }
  }
}
