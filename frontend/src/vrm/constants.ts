/** Domain constants shared by the VRM controllers. */

import type { CameraPreset, GestureName, LipsyncSettings, VisemeName } from '../types/config';
import { EMOTIONS, type EmotionName } from './expressionRecipes';

/** VRM 1.0 blendshape presets the backend can drive. */
export const MOUTH_PRESETS: readonly VisemeName[] = ['aa', 'ih', 'ou', 'ee', 'oh'];

/**
 * Emotions the expression controller owns.
 *
 * `thinking` and `shy` are first class: the model's VRM preset list has no such
 * expression, but the *recipe* layer can build them from brow/lid morphs, which
 * is a real face rather than `relaxed` with a comment.
 */
export const EMOTION_PRESETS = EMOTIONS;

export type { EmotionName };

/** Mouth shape -> expression preset. `nn` means "closed": no preset at all. */
export const VISEME_TO_PRESET: Record<string, string | null> = {
  aa: 'aa',
  ih: 'ih',
  ou: 'ou',
  ee: 'ee',
  oh: 'oh',
  nn: null,
};

/** Skeletal gestures `MotionController` implements. */
export const MOTION_ACTIONS: readonly GestureName[] = [
  'nod',
  'shake',
  'tilt',
  'wave',
  'think',
  'sigh',
  'laugh',
  'lean_in',
];

/**
 * Gesture notes that are really *expressions*, routed to the expression
 * controller instead of the skeleton: `(smiles)` moves a face, not a shoulder.
 */
export const FACIAL_ACTIONS: Record<
  string,
  { emotion: EmotionName; weight: number; ms: number; overrideBlink?: boolean }
> = {
  smile: { emotion: 'happy', weight: 0.6, ms: 1600 },
  laugh: { emotion: 'happy', weight: 0.9, ms: 1300 },
  blush: { emotion: 'shy', weight: 0.8, ms: 2200 },
};

/**
 * Camera framing, expressed as a fraction of the avatar's own height so it
 * works for a 1.4 m chibi and a 1.9 m adult alike.
 */
export const CAMERA_FRAMING: Record<CameraPreset, { height: number; distance: number; fov: number }> = {
  /** Eyes and mouth: closest, best for lip-sync watching. */
  face: { height: 0.93, distance: 0.5, fov: 26 },
  /** Head and torso: the default talking-head framing. */
  bust: { height: 0.78, distance: 1.05, fov: 30 },
  /** Whole body, including gesture animations. */
  full: { height: 0.5, distance: 2.0, fov: 34 },
};

export const DEFAULT_LIPSYNC: LipsyncSettings = {
  mode: 'mouth',
  enabled: true,
  attack_ms: 40,
  release_ms: 120,
  viseme_gain: 1,
  amplitude_modulation: true,
  amplitude_floor: 0.45,
  slice_ms: 20,
  mouth_threshold: 0.12,
  mouth_hysteresis: 0.05,
  mouth_scale: 0.72,
  mouth_max: 0.68,
  mouth_min_dwell_ms: 60,
};

/** Client-side idle motion preferences (nothing here changes the backend). */
export interface MotionOptions {
  /** Procedural breathing, sway and head drift. */
  idle: boolean;
  breathing: boolean;
  sway: boolean;
  /** Rotate the arms down from the rest T-pose into a natural stance. */
  armRest: boolean;
  /** How far down, in degrees. */
  armAngle: number;
  /** Global multiplier on every idle/gesture amplitude. */
  intensity: number;
}

export const DEFAULT_MOTION: MotionOptions = {
  idle: true,
  breathing: true,
  sway: true,
  armRest: true,
  armAngle: 68,
  intensity: 1,
};
