/**
 * Procedural body motion: idle life and gestures.
 *
 * three-vrm exposes a *normalized* humanoid rig whose bone nodes are world-axis
 * aligned at rest (see `VRMHumanoidRig._setupTransforms`), and `vrm.update()`
 * copies that rig back onto the model's real bones. So we can speak in plain
 * world axes: `+X` is the avatar's left, `+Y` is up, `+Z` is the direction the
 * avatar faces - for both VRM 0.x and 1.0 models.
 *
 * Every frame we:
 *   1. reset the tracked bones to their rest rotation,
 *   2. add the idle offsets (breathing, sway, head drift),
 *   3. add the active gesture clip (and any clip still fading out),
 *   4. add the arm rest pose (unless the model already rests its arms down),
 *   5. compose the result onto the rig.
 *
 * ## Gestures interrupt, they do not queue
 *
 * A gesture that arrives while another is playing *replaces* it: the old clip
 * keeps running but its gain ramps to zero while the new one ramps in, so the
 * body cross-fades instead of snapping. Queueing was a real bug - the backend
 * asks for a gesture once per *sentence*, so a four-sentence happy reply used to
 * sit through four sequential `nod` clips, which reads as an avatar that will
 * not sit still. Each gesture also has a cooldown so the same one cannot machine
 * gun, and there is a floor on how fast *any* two gestures may follow.
 *
 * ## Sharing a skeleton with real animation clips
 *
 * `AnimationController` plays authored clips (Mixamo / `.vrma`) through a
 * `THREE.AnimationMixer` on top of this layer. Because the mixer *sets* the
 * bones it animates, this controller has to hand them over explicitly - that is
 * what `setOwnership` is for:
 *
 *   * ownership `0` (the default) - this layer writes the bone outright,
 *   * ownership `1` - this layer does not touch it at all (the clip owns it),
 *   * anything in between - this layer *slerps* its own pose over whatever the
 *     mixer just wrote, which turns a clip's fade-in into a smooth blend instead
 *     of the snap you get from two writers fighting over one quaternion.
 */

import * as THREE from 'three';
import type { VRM, VRMHumanBoneName } from '@pixiv/three-vrm';

import { DEFAULT_MOTION, type MotionOptions } from './constants';
import { clamp01, smoothTowards, smoothstep } from './smooth';
import type { FrameController } from './types';
import type { GestureName } from '../types/config';

const TRACKED_BONES = [
  'hips',
  'spine',
  'chest',
  'upperChest',
  'neck',
  'head',
  'leftShoulder',
  'leftUpperArm',
  'leftLowerArm',
  'leftHand',
  'rightShoulder',
  'rightUpperArm',
  'rightLowerArm',
  'rightHand',
] as const;

export type TrackedBone = (typeof TRACKED_BONES)[number];

/** If the rest pose already points the arms down, do not rotate them further. */
const ARM_DOWN_ANGLE = THREE.MathUtils.degToRad(45);

/**
 * Time constants for a gesture taking over and letting go, in seconds.
 *
 * These are passed to `smoothTowards` as *tau*, not as a total duration: the
 * old code divided them by 2.5 "to land in `fade` seconds", which turned every
 * gesture into a 70 ms snap - the exact jerkiness that makes an avatar look
 * puppeteered. A 0.14 s attack means the movement is committed within a couple
 * of frames but still eases, and a slower 0.26 s release lets it dissolve.
 */
const GESTURE_FADE_OUT = 0.26;
const GESTURE_FADE_IN = 0.14;
/** No two gestures may start closer together than this. */
const GLOBAL_GESTURE_GAP = 0.6;

/**
 * Gestures this controller has a clip for. `smile` and `blush` are facial hints
 * (see `FACIAL_ACTIONS`) and are owned by the expression controller instead.
 */
export type MotionGesture = Exclude<GestureName, 'smile' | 'blush'>;

/** Everything a gesture curve is allowed to touch. */
export interface GestureContext {
  add(bone: TrackedBone, x: number, y: number, z: number): void;
  /** Slide the whole body along +Z (towards the camera), in metres. */
  lean(z: number): void;
  /** The current effective arm-down angle in radians (0 when arms rest low). */
  armAngle: number;
}

/**
 * Every number a gesture is built from, in one place.
 *
 * Angles are radians and are deliberately small: 0.12 rad is already a
 * deliberate nod, and a head that rotates 20 degrees per word reads as broken
 * rather than expressive.
 */
export const GESTURE_TUNING = {
  nod: { head: 0.13, neck: 0.05 },
  shake: { head: 0.15, neck: 0.06, cycles: 3 },
  tilt: { head: 0.2, neck: 0.06, yaw: 0.05 },
  wave: { lift: 0.3, lower: 0.22, elbow: 0.34, hand: 0.24, cycles: 5 },
  // `think` is deliberately arm-free. The old version tried to raise a hand to
  // the chin with additive euler offsets on top of the arm-rest pose; without
  // IK the elbow landed somewhere in her chest, which read as a broken arm.
  think: { head: 0.055, tilt: 0.11, yaw: 0.06, turn: 0.02 },
  sigh: { shoulder: 0.075, chest: 0.075, spine: 0.03, head: 0.1 },
  laugh: { chest: 0.035, head: 0.06, spine: 0.025, shoulder: 0.05, cycles: 5 },
  lean_in: { hips: 0.045, chest: 0.045, head: 0.035 },
} as const;

/** 0 -> 1 -> 0 across `p`, peaking at `peak`. Smooth at both ends. */
function bell(p: number, peak = 0.5): number {
  if (p <= 0 || p >= 1) return 0;
  return p < peak ? smoothstep(0, peak, p) : smoothstep(1, peak, p);
}

/** Ease in, hold, ease out - the shape of a deliberate movement. */
function hold(p: number, inAt = 0.28, outAt = 0.72): number {
  return smoothstep(0, inAt, p) * (1 - smoothstep(outAt, 1, p));
}

/** `[pitch (+ = down), yaw, tilt]` in radians, added to a bone's rest pose. */
type PostureOffset = readonly [number, number, number];

/**
 * How a mood changes her *posture*, not just her face.
 *
 * This is the layer that was missing: expressions moved the brows and
 * lip-sync moved the jaw, but her body stayed in exactly the same neutral
 * stance, which is why a happy sentence and a sad one looked like the same
 * animation with a different mouth. The offsets are small (2-5 degrees), slow,
 * and additive with gestures, so they read as body language rather than as a
 * pose being played. `listening` is a phase, not an emotion - a tiny tilt that
 * says she is paying attention.
 */
const MOOD_POSTURE: Readonly<Record<string, Partial<Record<TrackedBone, PostureOffset>>>> = {
  neutral: {},
  relaxed: {},
  listening: {
    head: [-0.008, 0.015, 0.028],
    neck: [0, 0.008, 0.012],
  },
  happy: {
    head: [-0.014, 0, 0.02],
    neck: [-0.006, 0, 0.008],
    chest: [-0.01, 0, 0],
  },
  sad: {
    head: [0.05, 0.015, -0.02],
    neck: [0.02, 0, -0.008],
    chest: [0.022, 0, 0],
    spine: [0.014, 0, 0],
  },
  angry: {
    head: [0.024, 0, 0],
    neck: [0.012, 0, 0],
    chest: [0.018, 0, 0],
  },
  surprised: {
    head: [-0.03, 0, 0],
    neck: [-0.012, 0, 0],
    chest: [-0.012, 0, 0],
  },
  thinking: {
    head: [-0.006, -0.035, 0.06],
    neck: [0, -0.012, 0.02],
    chest: [0, 0, 0.01],
  },
  shy: {
    head: [0.04, 0.02, 0.06],
    neck: [0.016, 0, 0.02],
    chest: [0.01, 0, 0],
  },
};

/**
 * The gesture library.
 *
 * `pose` receives an eased `p` in `0..1` and adds its offsets for the frame.
 * Each one is a *single* movement with a beginning, a hold and a return: the
 * old library used raw `sin(2*PI*p)` envelopes, which silently produced two or
 * three repetitions per clip.
 */
export const GESTURES: Record<
  MotionGesture,
  { seconds: number; cooldown: number; pose: (p: number, ctx: GestureContext) => void }
> = {
  nod: {
    seconds: 0.85,
    cooldown: 1.7,
    pose: (p, ctx) => {
      const e = bell(p, 0.42);
      ctx.add('head', GESTURE_TUNING.nod.head * e, 0, 0);
      ctx.add('neck', GESTURE_TUNING.nod.neck * e, 0, 0);
    },
  },
  shake: {
    seconds: 1.1,
    cooldown: 2.1,
    pose: (p, ctx) => {
      const w = bell(p, 0.5);
      const swing = Math.sin(p * Math.PI * GESTURE_TUNING.shake.cycles);
      ctx.add('head', 0, GESTURE_TUNING.shake.head * swing * w, 0);
      ctx.add('neck', 0, GESTURE_TUNING.shake.neck * swing * w, 0);
    },
  },
  tilt: {
    seconds: 1.2,
    cooldown: 2.4,
    pose: (p, ctx) => {
      const e = hold(p, 0.32, 0.66);
      ctx.add('head', 0, GESTURE_TUNING.tilt.yaw * e, GESTURE_TUNING.tilt.head * e);
      ctx.add('neck', 0, 0, GESTURE_TUNING.tilt.neck * e);
    },
  },
  wave: {
    seconds: 1.9,
    cooldown: 3.5,
    pose: (p, ctx) => {
      const t = GESTURE_TUNING.wave;
      const raise = hold(p, 0.22, 0.8);
      // Lift to roughly horizontal, then swing the forearm from the elbow.
      ctx.add('rightUpperArm', 0, 0, -raise * Math.max(0, ctx.armAngle - t.lift));
      ctx.add('rightUpperArm', -0.12 * raise, 0, 0);
      ctx.add('rightLowerArm', -t.lower * raise, 0, Math.sin(p * Math.PI * t.cycles) * t.elbow * raise);
      ctx.add('rightHand', 0, Math.sin(p * Math.PI * t.cycles + 0.5) * t.hand * raise, 0);
      ctx.add('head', -0.03 * raise, 0.05 * raise, 0);
    },
  },
  think: {
    seconds: 2.0,
    cooldown: 4.0,
    pose: (p, ctx) => {
      const t = GESTURE_TUNING.think;
      // A held, slightly asymmetrical tilt with a small turn away: the body
      // language of "let me think", with no arm to clip through the torso.
      const e = hold(p, 0.28, 0.7);
      ctx.add('head', -t.head * e, t.yaw * e, t.tilt * e);
      ctx.add('neck', 0, t.yaw * 0.3 * e, t.tilt * 0.4 * e);
      ctx.add('chest', 0, -t.turn * e, 0);
      ctx.add('spine', 0, -t.turn * 0.4 * e, 0);
    },
  },
  sigh: {
    seconds: 1.9,
    cooldown: 3.0,
    pose: (p, ctx) => {
      const t = GESTURE_TUNING.sigh;
      // Shoulders rise with the breath, then everything drops on the exhale.
      const rise = smoothstep(0, 0.34, p) * (1 - smoothstep(0.36, 0.66, p));
      const drop = smoothstep(0.4, 0.8, p) * (1 - smoothstep(0.86, 1, p));
      ctx.add('leftShoulder', 0, 0, -t.shoulder * rise);
      ctx.add('rightShoulder', 0, 0, t.shoulder * rise);
      ctx.add('chest', -t.chest * drop - 0.02 * rise, 0, 0);
      ctx.add('spine', -t.spine * drop, 0, 0);
      ctx.add('head', t.head * drop, 0, 0);
      ctx.add('neck', t.head * 0.5 * drop, 0, 0);
    },
  },
  laugh: {
    seconds: 1.35,
    cooldown: 2.5,
    pose: (p, ctx) => {
      const t = GESTURE_TUNING.laugh;
      // A bell window decays the oscillation on both ends, so the bounce reads
      // as a laugh rather than a vibration.
      const w = bell(p, 0.45);
      const bounce = Math.sin(p * Math.PI * t.cycles) * w;
      ctx.add('chest', t.chest * bounce, 0, 0);
      ctx.add('head', t.head * bounce, 0, 0);
      ctx.add('spine', -t.spine * w, 0, 0);
      ctx.add('leftShoulder', 0, 0, -t.shoulder * Math.abs(bounce));
      ctx.add('rightShoulder', 0, 0, t.shoulder * Math.abs(bounce));
    },
  },
  lean_in: {
    seconds: 1.6,
    cooldown: 4.0,
    pose: (p, ctx) => {
      const t = GESTURE_TUNING.lean_in;
      const e = bell(p, 0.45);
      ctx.lean(t.hips * e);
      ctx.add('chest', t.chest * e, 0, 0);
      ctx.add('head', t.head * e, 0, 0);
    },
  },
};

function isMotionGesture(name: GestureName): name is MotionGesture {
  return Object.prototype.hasOwnProperty.call(GESTURES, name);
}

interface Clip {
  name: MotionGesture;
  duration: number;
  elapsed: number;
  /** Caller-supplied strength, 0..1. */
  intensity: number;
  /** Ramped 0..1 as the gesture takes over, back to 0 as it is replaced. */
  gain: number;
  releasing: boolean;
}

export class MotionController implements FrameController {
  private vrm: VRM | null = null;
  private options: MotionOptions = { ...DEFAULT_MOTION };

  private readonly nodes = new Map<TrackedBone, THREE.Object3D>();
  private readonly rest = new Map<TrackedBone, THREE.Quaternion>();
  private readonly restHips = new THREE.Vector3();
  private readonly offsets = new Map<TrackedBone, THREE.Vector3>();
  /** Offsets produced by the gesture clip currently being evaluated. */
  private readonly clipOffsets = new Map<TrackedBone, THREE.Vector3>();
  private readonly hipsOffset = new THREE.Vector3();
  private clipLean = 0;

  private readonly scratchEuler = new THREE.Euler();
  private readonly scratchQuat = new THREE.Quaternion();
  private readonly scratchPose = new THREE.Quaternion();

  /**
   * How much the clip layer owns each tracked bone, 0..1.
   * Bones absent from the map are owned entirely by this controller.
   */
  private ownership: ReadonlyMap<string, number> = new Map();

  private clips: Clip[] = [];
  /** Wall clock, in seconds, used for the per-gesture cooldowns. */
  private time = 0;
  private nextGestureAt = 0;
  private readonly cooldownUntil = new Map<MotionGesture, number>();

  /** Effective arm-down angle; 0 when the model already rests arms down. */
  private armAngle = 0;
  private armsAlreadyDown = false;

  /** Outreach to the lip-sync controller, so speech nudges the body. */
  private energy: () => number = () => 0;
  /** Low-passed energy: raw mouth openness is a 50 Hz signal, not a motion. */
  private smoothedEnergy = 0;

  /** The posture currently requested by the face (`thinking`, `shy`, ...). */
  private moodName = 'neutral';
  private readonly posture = new Map<TrackedBone, THREE.Vector3>();
  private readonly postureTarget = new Map<TrackedBone, THREE.Vector3>();

  private readonly gestureContext: GestureContext = {
    add: (bone, x, y, z) => this.addClip(bone, x, y, z),
    lean: (z) => {
      this.clipLean += z;
    },
    armAngle: 0,
  };

  setEnergySource(source: () => number): void {
    this.energy = source;
  }

  /**
   * Ask for the body language that belongs to an emotion. Unknown names (and
   * `null`) mean "neutral": the posture eases back out rather than snapping.
   */
  setMood(emotion: string | null | undefined): void {
    const key = (emotion ?? 'neutral').trim().toLowerCase();
    this.moodName = key in MOOD_POSTURE ? key : 'neutral';
    this.postureTarget.clear();
    for (const bone of TRACKED_BONES) {
      const offset = MOOD_POSTURE[this.moodName]?.[bone];
      if (!offset) continue;
      this.postureTarget.set(bone, new THREE.Vector3(offset[0], offset[1], offset[2]));
    }
  }

  /** The posture currently requested (for the debug overlay). */
  get mood(): string {
    return this.moodName;
  }

  /**
   * Declare which bones an external clip layer is currently driving.
   * Called every frame by `AnimationController`; an empty map means "all mine".
   */
  setOwnership(bones: ReadonlyMap<string, number>): void {
    this.ownership = bones.size > 0 ? bones : EMPTY_OWNERSHIP;
  }

  /** How much of `bone` is currently handed over to the clip layer (0..1). */
  private owned(bone: string): number {
    return this.ownership.get(bone) ?? 0;
  }

  get detectedArmsDown(): boolean {
    return this.armsAlreadyDown;
  }

  setOptions(options: Partial<MotionOptions>): void {
    this.options = { ...this.options, ...options };
    // A model that rests its arms down must not be rotated again by default.
    this.armAngle = this.armsAlreadyDown && this.options.armRest ? 0 : THREE.MathUtils.degToRad(this.options.armAngle);
    if (!this.options.armRest) this.armAngle = 0;
    this.gestureContext.armAngle = this.armAngle;
  }

  get config(): MotionOptions {
    return { ...this.options };
  }

  attach(vrm: VRM | null): void {
    this.nodes.clear();
    this.rest.clear();
    this.clips = [];
    this.cooldownUntil.clear();
    this.nextGestureAt = 0;
    this.posture.clear();
    this.postureTarget.clear();
    this.moodName = 'neutral';
    this.ownership = EMPTY_OWNERSHIP;
    this.vrm = vrm;
    if (!vrm) return;

    vrm.scene.updateWorldMatrix(true, true);
    for (const name of TRACKED_BONES) {
      const node = vrm.humanoid.getNormalizedBoneNode(name as VRMHumanBoneName);
      if (!node) continue;
      this.nodes.set(name, node);
      this.rest.set(name, node.quaternion.clone());
    }

    const hips = this.nodes.get('hips');
    if (hips) this.restHips.copy(hips.position);

    this.armsAlreadyDown = this.detectArmsDown();
    this.setOptions(this.options);
  }

  /** True when the model's rest pose is already an A-pose / relaxed stance. */
  private detectArmsDown(): boolean {
    const upper = this.nodes.get('leftUpperArm');
    const lower = this.nodes.get('leftLowerArm');
    if (!upper || !lower) return true; // no arms to worry about

    const from = upper.getWorldPosition(new THREE.Vector3());
    const to = lower.getWorldPosition(new THREE.Vector3());
    const direction = to.sub(from);
    if (direction.lengthSq() < 1e-8) return false;
    direction.normalize();
    const down = new THREE.Vector3(0, -1, 0);
    return direction.angleTo(down) < ARM_DOWN_ANGLE;
  }

  /**
   * Start a gesture. A new gesture **replaces** the current one rather than
   * queueing behind it, and a repeat of the same gesture is refused until its
   * cooldown has passed.
   */
  play(name: GestureName, intensity = 1): void {
    if (!this.vrm) return;
    // Facial hints never reach `MotionController` from the conversation layer,
    // but a stray one must not start a no-op clip that blocks the next gesture.
    if (!isMotionGesture(name)) return;

    const now = this.time;
    if (now < this.nextGestureAt) return;
    if (now < (this.cooldownUntil.get(name) ?? 0)) return;
    if (this.activeClip === name) return;

    this.nextGestureAt = now + GLOBAL_GESTURE_GAP;
    this.cooldownUntil.set(name, now + GESTURES[name].cooldown);

    // Demote whatever is playing: it keeps its phase, loses its gain.
    for (const clip of this.clips) clip.releasing = true;

    this.clips.push({
      name,
      duration: GESTURES[name].seconds,
      elapsed: 0,
      intensity: clamp01(intensity),
      gain: 0,
      releasing: false,
    });
    // `releasing` clips are dropped as soon as they fade out; the cap is only
    // a guard against a pathological stream of hints.
    if (this.clips.length > 3) this.clips.splice(0, this.clips.length - 3);
  }

  get activeClip(): GestureName | null {
    for (const clip of this.clips) {
      if (!clip.releasing) return clip.name;
    }
    return null;
  }

  /* ------------------------------------------------------------------ */
  /*  Frame                                                              */
  /* ------------------------------------------------------------------ */

  update(delta: number, elapsed: number): void {
    if (!this.vrm || this.nodes.size === 0) return;

    this.time = elapsed;
    this.offsets.clear();
    this.hipsOffset.set(0, 0, 0);

    const intensity = Math.max(0, this.options.intensity);
    if (this.options.idle && intensity > 0) this.applyIdle(elapsed, intensity, delta);
    if (intensity > 0) this.advancePosture(delta, intensity);
    this.advanceClips(delta, intensity);
    this.applyArmRest();

    this.commit();
  }

  private applyIdle(elapsed: number, intensity: number, delta: number): void {
    // Mouth openness is a per-frame signal. Feeding it straight into a head
    // rotation made the head bob at syllable rate; a slow low-pass plus a much
    // smaller gain turns it into a chest that moves when she speaks.
    this.smoothedEnergy = smoothTowards(this.smoothedEnergy, clamp01(this.energy()), delta, 0.4);

    if (this.options.breathing) {
      // Chest and spine inhale at ~10 breaths per minute, slightly out of phase.
      const breath = Math.sin(elapsed * 1.05);
      this.add('chest', (breath * 0.01 + this.smoothedEnergy * 0.006) * intensity, 0, 0);
      this.add('spine', Math.sin(elapsed * 1.05 - 0.5) * 0.006 * intensity, 0, 0);
      this.add('upperChest', Math.sin(elapsed * 1.05 - 0.9) * 0.005 * intensity, 0, 0);
    }

    if (this.options.sway) {
      // A slow figure-of-eight around the hips. The amplitudes used to be twice
      // this: on a bust/face camera a 1.5-degree rock is not "alive", it is a
      // visible wobble that never stops.
      this.add('hips', Math.sin(elapsed * 0.19) * 0.006 * intensity, 0, Math.sin(elapsed * 0.29) * 0.012 * intensity);
      this.add('spine', 0, 0, -Math.sin(elapsed * 0.23) * 0.006 * intensity);
      this.add('chest', 0, Math.sin(elapsed * 0.17) * 0.008 * intensity, 0);
    }

    // Head drift: three slow, incommensurate axes, deliberately under half a
    // degree each. The old version was louder than the gestures it accompanied.
    this.add(
      'neck',
      Math.sin(elapsed * 0.43) * 0.006 * intensity,
      Math.sin(elapsed * 0.31) * 0.009 * intensity,
      0,
    );
    this.add(
      'head',
      Math.sin(elapsed * 0.67) * 0.004 * intensity,
      Math.sin(elapsed * 0.47) * 0.005 * intensity,
      Math.sin(elapsed * 0.37) * 0.005 * intensity,
    );
  }

  /**
   * Ease the posture towards the current mood. Slow on purpose: a person does
   * not re-pose their shoulders the instant they feel something, and an
   * instant posture change is exactly what reads as "unconscious" motion.
   */
  private advancePosture(delta: number, intensity: number): void {
    for (const bone of TRACKED_BONES) {
      const target = this.postureTarget.get(bone);
      let current = this.posture.get(bone);

      if (!target) {
        if (!current) continue;
        current.x = smoothTowards(current.x, 0, delta, 0.6);
        current.y = smoothTowards(current.y, 0, delta, 0.6);
        current.z = smoothTowards(current.z, 0, delta, 0.6);
        if (Math.abs(current.x) + Math.abs(current.y) + Math.abs(current.z) < 1e-4) {
          this.posture.delete(bone);
          continue;
        }
      } else {
        if (!current) {
          current = new THREE.Vector3();
          this.posture.set(bone, current);
        }
        current.x = smoothTowards(current.x, target.x, delta, 0.45);
        current.y = smoothTowards(current.y, target.y, delta, 0.45);
        current.z = smoothTowards(current.z, target.z, delta, 0.45);
      }

      this.add(bone, current.x * intensity, current.y * intensity, current.z * intensity);
    }
  }

  private advanceClips(delta: number, intensity: number): void {
    if (this.clips.length === 0) return;

    // The user's global motion slider scales gestures exactly like it scales
    // the idle, so "less motion" means less of all of it.
    const gain = intensity;
    const survivors: Clip[] = [];

    for (const clip of this.clips) {
      clip.elapsed += delta;
      const progress = clamp01(clip.elapsed / clip.duration);

      if (clip.releasing) {
        clip.gain = smoothTowards(clip.gain, 0, delta, GESTURE_FADE_OUT);
        if (clip.gain <= 0.01) continue; // gone: stop evaluating it entirely
      } else {
        clip.gain = smoothTowards(clip.gain, 1, delta, GESTURE_FADE_IN);
      }

      // The curve writes into its own map, then we blend the whole thing in at
      // one weight. That is what makes a cross-fade possible at all: the old
      // clip and the new one are both evaluated and summed by their own gains.
      this.clipOffsets.clear();
      this.clipLean = 0;
      GESTURES[clip.name].pose(progress, this.gestureContext);
      this.blendClip(clip.gain * clip.intensity * gain);

      if (progress >= 1 && !clip.releasing) clip.releasing = true;
      survivors.push(clip);
    }

    this.clips = survivors;
  }

  /** Add the current clip's pose into the frame's offsets, scaled by `factor`. */
  private blendClip(factor: number): void {
    if (factor <= 0) return;
    for (const [bone, offset] of this.clipOffsets) {
      let target = this.offsets.get(bone);
      if (!target) {
        target = new THREE.Vector3();
        this.offsets.set(bone, target);
      }
      target.x += offset.x * factor;
      target.y += offset.y * factor;
      target.z += offset.z * factor;
    }
    this.hipsOffset.z += this.clipLean * factor;
  }

  private addClip(bone: TrackedBone, x: number, y: number, z: number): void {
    if (!this.nodes.has(bone)) return;
    let offset = this.clipOffsets.get(bone);
    if (!offset) {
      offset = new THREE.Vector3();
      this.clipOffsets.set(bone, offset);
    }
    offset.x += x;
    offset.y += y;
    offset.z += z;
  }

  private applyArmRest(): void {
    if (this.armAngle <= 0) return;
    const angle = this.armAngle;
    // Mirrored: the left arm hangs at -Z rotation, the right at +Z.
    this.add('leftUpperArm', 0, -0.12, -angle);
    this.add('rightUpperArm', 0, 0.12, angle);
    // Relaxed elbows, bending forward once the arms hang down.
    this.add('leftLowerArm', -0.3, 0, 0);
    this.add('rightLowerArm', -0.3, 0, 0);
  }

  private add(bone: TrackedBone, x: number, y: number, z: number): void {
    if (!this.nodes.has(bone)) return;
    let offset = this.offsets.get(bone);
    if (!offset) {
      offset = new THREE.Vector3();
      this.offsets.set(bone, offset);
    }
    offset.x += x;
    offset.y += y;
    offset.z += z;
  }

  private commit(): void {
    for (const [name, node] of this.nodes) {
      const owned = this.owned(name);
      if (owned >= HANDOVER_DONE) continue; // the clip layer owns this bone
      const base = this.rest.get(name) ?? IDENTITY;
      const offset = this.offsets.get(name);
      if (!offset) {
        if (owned <= HANDOVER_IDLE) node.quaternion.copy(base);
        continue;
      }
      this.scratchEuler.set(offset.x, offset.y, offset.z, 'XYZ');
      this.scratchQuat.setFromEuler(this.scratchEuler);
      this.scratchPose.copy(base).multiply(this.scratchQuat);
      if (owned <= HANDOVER_IDLE) {
        node.quaternion.copy(this.scratchPose);
      } else {
        // Mid hand-over: the mixer has already written its clip pose, so we
        // slerp our own pose *over* it instead of overwriting it.
        node.quaternion.slerp(this.scratchPose, 1 - owned);
      }
    }

    const hips = this.nodes.get('hips');
    if (hips && this.owned('hips') < 0.5 && this.hipsOffset.lengthSq() > 0) {
      hips.position.copy(this.restHips).add(this.hipsOffset);
    }
  }

  dispose(): void {
    this.clips = [];
    this.cooldownUntil.clear();
  }
}

const IDENTITY = new THREE.Quaternion();

/** Ownership thresholds: below IDLE the procedural layer writes outright, above
 *  DONE it does not write at all. */
const HANDOVER_IDLE = 0.001;
const HANDOVER_DONE = 0.999;

const EMPTY_OWNERSHIP: ReadonlyMap<string, number> = new Map();
