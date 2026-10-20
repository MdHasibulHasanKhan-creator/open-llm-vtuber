/**
 * Types for the external animation layer (`AnimationController`).
 *
 * The idea is that the *procedural* motion in `MotionController` is always the
 * floor - it needs no assets and always works - and real, authored animation
 * clips are the ceiling. A manifest (`public/animations/manifest.json`) lists
 * clips; when it is empty everything degrades gracefully to procedural motion.
 */

/** What a clip is for. Idle clips loop; gestures play once and blend out. */
export type ClipKind = 'idle' | 'gesture';

/** One clip in the manifest. Every field beyond the first three is optional. */
export interface AnimationClipEntry {
  /**
   * Logical name. For `gesture` clips this should match a backend gesture name
   * (`nod`, `shake`, `tilt`, `wave`, `think`, `sigh`, `laugh`, `lean_in`) so the
   * LLM's hints play *your* animation; any other name is playable from the UI.
   */
  name: string;
  /** `.glb` / `.gltf` / `.fbx` / `.vrma`, absolute or relative to the site root. */
  url: string;
  kind: ClipKind;
  /** Loop the clip. Defaults to `kind === 'idle'`. */
  loop?: boolean;
  /** Cross-fade duration in seconds when this clip takes over (default 0.35). */
  fadeIn?: number;
  fadeOut?: number;
  /**
   * The clip plays at this weight. Use a little less than 1 for a *gentle*
   * guesture that should not dominate the idle pose.
   */
  intensity?: number;
  /**
   * Mixamo's FBX/GLB translation channels are often in centimetres while VRM
   * works in metres. Scales the hips translation track only.
   */
  positionScale?: number;
  /**
   * How much of the hips translation to keep, 0..1. `0` disables root motion
   * entirely, which is the safest choice for a talking avatar that must stay
   * centred in frame.
   */
  positionStrength?: number;
  /**
   * Extra rotation (degrees, XYZ) applied to the hips track. Fixes the axis
   * convention of an export that was not normalised to Y-up - see
   * `docs/ANIMATIONS.md`.
   */
  rootRotation?: [number, number, number];
}

export interface AnimationManifest {
  version?: number;
  /** Seconds between random idle changes (0 keeps the first idle forever). */
  idleSeconds?: number;
  clips: AnimationClipEntry[];
}

/** Which layer is currently producing the pose (for the settings UI). */
export type AnimationMode = 'procedural' | 'clips';

export interface AnimationStatus {
  mode: AnimationMode;
  loading: boolean;
  /** Non-fatal problems, one line each (bad URL, unreadable file, ...). */
  problems: string[];
  clips: number;
  idleClips: number;
  gestureClips: number;
  /** Bone names the active clips are driving, for the debug panel. */
  activeIdle: string | null;
  activeGesture: string | null;
  drivenBones: number;
}

/** Client-side animation preferences (persisted in the store). */
export interface AnimationOptions {
  /** Use clips at all. Off = pure procedural motion. */
  enabled: boolean;
  /** Where the manifest lives. */
  manifestUrl: string;
  /** Overrides the manifest's `idleSeconds` (0 = never change). */
  idleSeconds: number;
  /** Global weight on clip gestures, on top of each entry's `intensity`. */
  gestureIntensity: number;
}

export const DEFAULT_ANIMATION: AnimationOptions = {
  enabled: true,
  manifestUrl: '/animations/manifest.json',
  idleSeconds: 0,
  gestureIntensity: 1,
};
