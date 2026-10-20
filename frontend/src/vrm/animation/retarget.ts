/**
 * Retargeting: turn a humanoid clip authored for *another* rig into a clip that
 * drives a VRM's normalized humanoid rig.
 *
 * The maths
 * ---------
 * three-vrm's normalized rig is, by construction, a rig whose bones sit at the
 * world-aligned rest orientation - every normalized bone's rest rotation is the
 * identity (see `VRMHumanoidRig._setupTransforms`), and `VRMHumanoidRig.update`
 * conjugates that rotation back into each raw bone's own frame:
 *
 *     q_raw = P⁻¹ ⊗ q_normalized ⊗ P ⊗ q_rawRest          (P = parent's rest world rotation)
 *
 * So a normalized bone's local rotation *is* "the delta from the rest pose,
 * expressed in the parent's frame". And that is exactly what you get from any
 * other rig with the same topology by removing its own rest pose:
 *
 *     q_normalized(t) = q_source(t) ⊗ q_sourceRest⁻¹
 *
 * Consequence: **no per-model calibration is needed** for rotation, which is why
 * a single Mixamo clip drives every T-pose VRM. What does *not* transfer is
 * translation (the two rigs have different bone lengths and different hip
 * heights), so the hips track is converted into a *delta from its own first
 * frame* and scaled - and can be switched off entirely (`positionStrength: 0`).
 */

import { PropertyBinding, Quaternion, QuaternionKeyframeTrack, VectorKeyframeTrack, AnimationClip, Euler, MathUtils } from 'three';
import type { Object3D, KeyframeTrack, Vector3Tuple } from 'three';
import type { VRM } from '@pixiv/three-vrm';

import { buildBoneIndex, canonicalBoneName, type BoneIndex } from './bones';

export interface RetargetOptions {
  /** Multiplier on the hips translation (Mixamo FBX is often in cm). */
  positionScale?: number;
  /** How much of the hips translation to keep, 0..1. `0` disables root motion. */
  positionStrength?: number;
  /** Extra rotation (degrees, XYZ) applied to the hips rotation delta. */
  rootRotation?: Vector3Tuple | null;
}

export interface RetargetReport {
  clip: AnimationClip;
  /** Canonical VRM bone names the clip drives. */
  bones: string[];
  /** Names of the normalized nodes it drives (used for the procedural mask). */
  nodeNames: string[];
  /** Source nodes with no VRM counterpart (twist bones, IK, props, ...). */
  unmapped: string[];
  /** False when the clip only carries rotations. */
  positionApplied: boolean;
}

/** Sanitised source-node-name -> node, mirroring how tracks are written. */
function indexSourceNodes(root: Object3D): Map<string, Object3D> {
  const index = new Map<string, Object3D>();
  root.traverse((node) => {
    const sanitized = PropertyBinding.sanitizeNodeName(node.name);
    if (sanitized && !index.has(sanitized)) index.set(sanitized, node);
    if (node.name && !index.has(node.name)) index.set(node.name, node);
  });
  return index;
}

function trackNodeName(track: KeyframeTrack): string {
  const dot = track.name.lastIndexOf('.');
  const raw = dot >= 0 ? track.name.slice(0, dot) : track.name;
  // `path/NodeName` and `NodeName[0]` are both legal in the format.
  const path = raw.lastIndexOf('/');
  const last = path >= 0 ? raw.slice(path + 1) : raw;
  const bracket = last.indexOf('[');
  return bracket >= 0 ? last.slice(0, bracket) : last;
}

function trackProperty(track: KeyframeTrack): string {
  const dot = track.name.lastIndexOf('.');
  if (dot < 0) return '';
  return track.name.slice(dot + 1).replace(/\[.*\]$/, '');
}

/**
 * `PropertyBinding` splits a track name on dots, so a node whose *own* name
 * contains one could never be addressed reliably. Such models are vanishingly
 * rare; skipping the bone is better than binding to the wrong node.
 */
function isBindable(nodeName: string): boolean {
  return nodeName.length > 0 && !nodeName.includes('.');
}

/** Keep a quaternion track continuous: no 180 degree flips between keyframes. */
function makeQuaternionContinuous(values: Float32Array): void {
  for (let i = 4; i < values.length; i += 4) {
    const dot =
      values[i] * values[i - 4] +
      values[i + 1] * values[i - 3] +
      values[i + 2] * values[i - 2] +
      values[i + 3] * values[i - 1];
    if (dot < 0) {
      values[i] = -values[i];
      values[i + 1] = -values[i + 1];
      values[i + 2] = -values[i + 2];
      values[i + 3] = -values[i + 3];
    }
  }
}

/**
 * Retarget one clip authored for `sourceRoot` onto `vrm`'s normalized rig.
 *
 * `sourceRoot` must be the object hierarchy the clip came from, in its **rest
 * pose** (load the file, do not play it): its current rotations are used as the
 * reference pose to subtract.
 */
export function retargetClip(
  source: AnimationClip,
  vrm: VRM,
  sourceRoot: Object3D,
  options: RetargetOptions = {},
): RetargetReport {
  const index: BoneIndex = buildBoneIndex(vrm);
  const sourceNodes = indexSourceNodes(sourceRoot);

  const positionStrength = options.positionStrength ?? 1;
  const positionScale = options.positionScale ?? 1;
  const correction = options.rootRotation
    ? new Quaternion().setFromEuler(
        new Euler(
          MathUtils.degToRad(options.rootRotation[0]),
          MathUtils.degToRad(options.rootRotation[1]),
          MathUtils.degToRad(options.rootRotation[2]),
          'XYZ',
        ),
      )
    : null;

  const tracks: KeyframeTrack[] = [];
  const bones = new Set<string>();
  const nodeNames = new Set<string>();
  const unmapped = new Set<string>();
  let positionApplied = false;

  const scratch = new Quaternion();
  const restInverse = new Quaternion();

  for (const track of source.tracks) {
    const sourceName = trackNodeName(track);
    const property = trackProperty(track);
    if (!sourceName || !property) continue;

    const bone = canonicalBoneName(sourceName);
    const target = bone ? index.byCanonical.get(bone) : undefined;
    if (!bone || !target || !isBindable(target.name)) {
      if (property !== 'scale') unmapped.add(sourceName);
      continue;
    }

    if (property === 'quaternion') {
      const sourceNode = sourceNodes.get(sourceName);
      if (!sourceNode) {
        unmapped.add(sourceName);
        continue;
      }
      restInverse.copy(sourceNode.quaternion).invert();
      const values = Float32Array.from(track.values);
      const isHips = bone === 'hips';

      for (let i = 0; i < values.length; i += 4) {
        scratch.set(values[i], values[i + 1], values[i + 2], values[i + 3]);
        // delta = q(t) * rest⁻¹  -> the rotation in the parent's frame
        scratch.multiply(restInverse);
        if (isHips && correction) scratch.premultiply(correction);
        scratch.normalize();
        values[i] = scratch.x;
        values[i + 1] = scratch.y;
        values[i + 2] = scratch.z;
        values[i + 3] = scratch.w;
      }
      makeQuaternionContinuous(values);

      tracks.push(new QuaternionKeyframeTrack(`${target.name}.quaternion`, Array.from(track.times), Array.from(values)));
      bones.add(bone);
      nodeNames.add(target.name);
      continue;
    }

    if (property === 'position') {
      // Only the hips: other bones' translations are rig-specific.
      if (bone !== 'hips' || positionStrength <= 0) continue;
      const values = Float32Array.from(track.values);
      if (values.length < 3) continue;
      const base = target.position;
      const sourceBase = [values[0], values[1], values[2]] as const;
      const gain = positionScale * positionStrength;
      for (let i = 0; i < values.length; i += 3) {
        values[i] = base.x + (values[i] - sourceBase[0]) * gain;
        values[i + 1] = base.y + (values[i + 1] - sourceBase[1]) * gain;
        values[i + 2] = base.z + (values[i + 2] - sourceBase[2]) * gain;
      }
      tracks.push(
        new VectorKeyframeTrack(`${target.name}.position`, Array.from(track.times), Array.from(values)),
      );
      positionApplied = true;
      continue;
    }

    // `scale` tracks are dropped: a Mixamo rig's bone scales are meaningless on
    // a VRM and would squash limbs.
  }

  const clip = new AnimationClip(source.name, source.duration, tracks);
  clip.optimize();

  return {
    clip,
    bones: [...bones],
    nodeNames: [...nodeNames],
    unmapped: [...unmapped],
    positionApplied,
  };
}

/**
 * Adopt a clip that is *already* VRM-native (`.vrma`, or a clip this module
 * produced earlier): it only needs its unmapped tracks dropped.
 */
export function adoptVrmClip(source: AnimationClip, vrm: VRM): RetargetReport {
  const index = buildBoneIndex(vrm);
  const tracks: KeyframeTrack[] = [];
  const bones = new Set<string>();
  const nodeNames = new Set<string>();
  const unmapped = new Set<string>();

  for (const track of source.tracks) {
    const sourceName = trackNodeName(track);
    const property = trackProperty(track);
    if (!sourceName || property === 'scale') continue;
    if (!index.nodeNames.has(sourceName) || !isBindable(sourceName)) {
      unmapped.add(sourceName);
      continue;
    }
    tracks.push(track.clone());
    nodeNames.add(sourceName);
    const bone = canonicalBoneName(sourceName);
    if (bone) bones.add(bone);
  }

  return {
    clip: new AnimationClip(source.name, source.duration, tracks),
    bones: [...bones],
    nodeNames: [...nodeNames],
    unmapped: [...unmapped],
    positionApplied: tracks.some((track) => trackProperty(track) === 'position'),
  };
}

export { trackNodeName, trackProperty };
