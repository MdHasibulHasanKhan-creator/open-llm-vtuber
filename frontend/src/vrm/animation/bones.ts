/**
 * Bone name mapping: *any* humanoid rig -> VRM human bone names.
 *
 * A Mixamo skeleton calls it `mixamorig:LeftForeArm`; VRoid calls it
 * `J_Bip_L_LowerArm`; Blender's auto-rigger calls it `DEF-forearm.L`; a game
 * engine might call it `Bip01 L Forearm`. They all mean the same joint.
 *
 * Instead of shipping a huge table, both sides of the mapping are pushed through
 * one canonicaliser and compared as *words*: `left` + `forearm`/`lowerarm` are
 * the same tokens whatever the punctuation was. That makes the mapping work for
 * rigs we have never seen, which is exactly what a "drop your Mixamo file in"
 * feature needs.
 */

import { VRMHumanBoneList, type VRM, type VRMHumanBoneName } from '@pixiv/three-vrm';
import type { Object3D } from 'three';

/** Tokens that carry no meaning and are simply dropped. */
const NOISE_TOKENS = new Set([
  'j', 'bip', 'bip01', 'bip001', 'def', 'org', 'mch', 'bn', 'bone', 'joint',
  'jnt', 'scene', 'root', 'armature', 'skel', 'skeleton', 'mixamorig', 'obj',
  'geo', 'node', 'c', 't', 'n',
  // three-vrm names its normalized rig nodes `Normalized_<raw name>`.
  'normalized',
]);

/**
 * Helper joints (twist bones, IK targets, end tags) share a name with a real
 * joint but must never drive it: a "forearm twist" rotating the forearm itself
 * looks like a broken elbow.
 */
const HELPER_TOKENS = new Set([
  'twist', 'roll', 'helper', 'ik', 'pole', 'nub', 'end', 'tip', 'tag',
  'ctrl', 'ctrlref', 'target', 'locator', 'null', 'offset', 'adj',
]);

const SIDE_TOKENS: Record<string, 'left' | 'right'> = {
  l: 'left',
  left: 'left',
  lt: 'left',
  r: 'right',
  right: 'right',
  rt: 'right',
};

/** Token -> VRM bone "part", without the side. */
const PART_TOKENS: Record<string, string> = {
  hips: 'hips',
  pelvis: 'hips',
  spine: 'spine',
  chest: 'chest',
  upperchest: 'upperChest',
  neck: 'neck',
  head: 'head',
  // arms
  clavicle: 'shoulder',
  shoulder: 'shoulder',
  arm: 'upperArm',
  upperarm: 'upperArm',
  uparm: 'upperArm',
  forearm: 'lowerArm',
  lowerarm: 'lowerArm',
  loarm: 'lowerArm',
  elbow: 'lowerArm',
  hand: 'hand',
  wrist: 'hand',
  // legs
  thigh: 'upperLeg',
  upleg: 'upperLeg',
  upperleg: 'upperLeg',
  leg: 'lowerLeg',
  lowerleg: 'lowerLeg',
  shin: 'lowerLeg',
  calf: 'lowerLeg',
  knee: 'lowerLeg',
  foot: 'foot',
  ankle: 'foot',
  toebase: 'toes',
  toe: 'toes',
  toes: 'toes',
  // face
  eye: 'eye',
  jaw: 'jaw',
};

/** Finger name -> VRM finger part. */
const FINGER_TOKENS: Record<string, string> = {
  thumb: 'Thumb',
  index: 'Index',
  pointer: 'Index',
  middle: 'Middle',
  ring: 'Ring',
  little: 'Little',
  pinky: 'Little',
};

/** Digit index / segment word -> VRM finger segment. */
const SEGMENT_TOKENS: Record<string, string> = {
  '0': 'Metacarpal',
  '1': 'Proximal',
  '2': 'Intermediate',
  '3': 'Distal',
  '4': 'Distal',
  proximal: 'Proximal',
  intermediate: 'Intermediate',
  medial: 'Intermediate',
  distal: 'Distal',
  metacarpal: 'Metacarpal',
};

/** Centre bones never take a side prefix. */
const CENTRE_PARTS = new Set(['hips', 'spine', 'chest', 'upperChest', 'neck', 'head', 'jaw']);

const VRM_BONE_SET: ReadonlySet<string> = new Set<string>(VRMHumanBoneList);

/**
 * Split an arbitrary bone name into lowercase word tokens.
 *
 * Handles `mixamorig:LeftForeArm`, `J_Bip_L_LowerArm`, `DEF-forearm.L`,
 * `Bip01 L Forearm` and `LeftForeArm_02` alike.
 */
export function tokeniseBoneName(raw: string): string[] {
  const cleaned = raw
    // Namespacing and engine-style numeric suffixes.
    .replace(/^(mixamorig|armature|rig|skeleton)[:_.|-]*/i, '')
    .replace(/[:|]/g, ' ')
    .replace(/\.\d+$/, '')
    .replace(/_\d+$/, '');
  // camelCase -> spaced, then split on every non-alphanumeric run.
  const spaced = cleaned
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .toLowerCase();
  return spaced
    .split(/[^a-z0-9]+/)
    .map((token) => token.trim())
    .filter((token) => token.length > 0);
}

/**
 * Canonicalise a bone name to a VRM human bone name, or `null` when the joint
 * has no VRM counterpart (twist bones, IK targets, unmodelled fingers, ...).
 */
export function canonicalBoneName(raw: string): VRMHumanBoneName | null {
  const direct = raw.trim();
  if (VRM_BONE_SET.has(direct)) return direct as VRMHumanBoneName;

  const tokens = tokeniseBoneName(raw).filter((token) => !NOISE_TOKENS.has(token));
  if (tokens.length === 0) return null;
  if (tokens.some((token) => HELPER_TOKENS.has(token))) return null;

  let side: 'left' | 'right' | null = null;
  let finger: string | null = null;
  let segment: string | null = null;
  let part: string | null = null;

  for (const token of tokens) {
    if (SIDE_TOKENS[token]) {
      side = SIDE_TOKENS[token];
      continue;
    }
    if (FINGER_TOKENS[token]) {
      finger = FINGER_TOKENS[token];
      continue;
    }
    if (finger !== null && segment === null && SEGMENT_TOKENS[token]) {
      segment = SEGMENT_TOKENS[token];
      continue;
    }
    if (PART_TOKENS[token]) {
      part = PART_TOKENS[token];
      continue;
    }
    if (/^[0-9]+$/.test(token)) {
      // `spine1` / `spine 2` numbering: the spine is split into three VRM bones.
      if (part === 'spine') part = token === '1' ? 'chest' : 'upperChest';
      else if (part === 'chest') part = 'upperChest';
      continue;
    }
    // A word we do not know and no joint identified yet: not a humanoid bone.
    if (part === null && finger === null) return null;
  }

  if (finger) {
    if (!side || !segment) return null;
    const name = `${side}${finger}${segment}`;
    return VRM_BONE_SET.has(name) ? (name as VRMHumanBoneName) : null;
  }

  if (!part) return null;
  if (CENTRE_PARTS.has(part)) return part as VRMHumanBoneName;
  if (!side) return null;

  const name = `${side}${part.charAt(0).toUpperCase()}${part.slice(1)}`;
  return VRM_BONE_SET.has(name) ? (name as VRMHumanBoneName) : null;
}

/** Every human bone the model actually has, by canonical name. */
export function modelBoneNames(vrm: VRM): VRMHumanBoneName[] {
  const found: VRMHumanBoneName[] = [];
  for (const name of VRMHumanBoneList) {
    if (vrm.humanoid.getNormalizedBoneNode(name)) found.push(name);
  }
  return found;
}

export interface BoneIndex {
  /** Canonical VRM bone name -> normalized node (what the mixer writes). */
  byCanonical: Map<string, Object3D>;
  /** Lookup by the *target node's* own name (VRMA tracks are already aligned). */
  byNodeName: Map<string, Object3D>;
  /** `NodeName.quaternion` / `NodeName.position` prefixes we must not duplicate. */
  nodeNames: ReadonlySet<string>;
}

/** Index of a model's normalized rig, keyed the two ways the mixer needs. */
export function buildBoneIndex(vrm: VRM): BoneIndex {
  const byCanonical = new Map<string, Object3D>();
  const byNodeName = new Map<string, Object3D>();
  const nodeNames = new Set<string>();

  for (const name of VRMHumanBoneList) {
    const node = vrm.humanoid.getNormalizedBoneNode(name);
    if (!node) continue;
    byCanonical.set(name, node);
    byNodeName.set(node.name, node);
    nodeNames.add(node.name);
  }

  return { byCanonical, byNodeName, nodeNames };
}
