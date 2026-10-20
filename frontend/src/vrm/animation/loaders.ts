/**
 * Loading animation files.
 *
 * Four formats, three loaders:
 *
 *  * `.vrma` - the VRM Animation format. Model independent by design: the file
 *    stores normalized-rig rotations, so `createVRMAnimationClip` can bind it to
 *    *any* VRM. This is the best quality option (see docs/ANIMATIONS.md).
 *  * `.glb` / `.gltf` - what Blender exports, and what Mixamo's FBX becomes
 *    after one Blender round trip. Retargeted by bone name.
 *  * `.fbx` - straight from Mixamo. Loaded with three's `FBXLoader` (which needs
 *    `fflate`, already a three dependency) and retargeted by bone name.
 *
 * Files are fetched once and cached; retargeting (which depends on the model) is
 * done per attach and is cheap.
 */

import { AnimationClip } from 'three';
import type { Object3D } from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { FBXLoader } from 'three/examples/jsm/loaders/FBXLoader.js';
import type { VRM } from '@pixiv/three-vrm';
import {
  createVRMAnimationClip,
  VRMAnimation,
  VRMAnimationLoaderPlugin,
} from '@pixiv/three-vrm-animation';

import { adoptVrmClip, retargetClip, type RetargetOptions, type RetargetReport } from './retarget';

export type AnimationFileKind = 'skeleton' | 'vrma';

export interface RawAnimationFile {
  kind: AnimationFileKind;
  /** For `.skeleton` files: the clips exactly as the loader produced them. */
  clips: AnimationClip[];
  /** For `.skeleton` files: the rest-pose hierarchy the clips were authored for. */
  root: Object3D | null;
  /** For `.vrma` files. */
  animations: VRMAnimation[];
}

export class AnimationLoadError extends Error {}

function extensionOf(url: string): string {
  const clean = url.split(/[?#]/)[0] ?? url;
  const dot = clean.lastIndexOf('.');
  return dot >= 0 ? clean.slice(dot + 1).toLowerCase() : '';
}

async function loadGltf(url: string, vrma: boolean): Promise<RawAnimationFile> {
  const loader = new GLTFLoader();
  if (vrma) loader.register((parser) => new VRMAnimationLoaderPlugin(parser));

  const gltf = await loader.loadAsync(url);
  if (vrma) {
    const animations = (gltf.userData.vrmAnimations as VRMAnimation[] | undefined) ?? [];
    if (animations.length === 0) {
      throw new AnimationLoadError(
        `${url} has no VRMC_vrm_animation extension - it is a glTF, not a .vrma. ` +
          'Use a .glb/.gltf/.fbx file instead.',
      );
    }
    return { kind: 'vrma', clips: [], root: null, animations };
  }

  const clips = gltf.animations ?? [];
  if (clips.length === 0) throw new AnimationLoadError(`${url} contains no animation.`);
  return { kind: 'skeleton', clips, root: gltf.scene, animations: [] };
}

async function loadFbx(url: string): Promise<RawAnimationFile> {
  const loader = new FBXLoader();
  const group = await loader.loadAsync(url);
  const clips = (group as Object3D & { animations?: AnimationClip[] }).animations ?? [];
  if (clips.length === 0) {
    throw new AnimationLoadError(
      `${url} contains no animation track. Re-export it from Mixamo with ` +
        'the "Animation" bake option enabled.',
    );
  }
  return { kind: 'skeleton', clips, root: group, animations: [] };
}

/* -------------------------------------------------------------------------- */
/*  File cache                                                                */
/* -------------------------------------------------------------------------- */

const fileCache = new Map<string, Promise<RawAnimationFile>>();

/** Fetch + parse an animation file once per URL. */
export function loadAnimationFile(url: string): Promise<RawAnimationFile> {
  const cached = fileCache.get(url);
  if (cached) return cached;

  const extension = extensionOf(url);
  let task: Promise<RawAnimationFile>;
  if (extension === 'vrma') task = loadGltf(url, true);
  else if (extension === 'fbx') task = loadFbx(url);
  else if (extension === 'glb' || extension === 'gltf') task = loadGltf(url, false);
  else {
    task = Promise.reject(
      new AnimationLoadError(`Unsupported animation format ".${extension}" (use .vrma, .glb, .gltf or .fbx).`),
    );
  }

  const guarded = task.catch((error: unknown) => {
    // Never cache a failure: a fixed URL should be retryable without a reload.
    fileCache.delete(url);
    throw error;
  });
  fileCache.set(url, guarded);
  return guarded;
}

/** Forget everything (used by "reload animations" in the settings UI). */
export function clearAnimationCache(): void {
  fileCache.clear();
}

/**
 * Bind every clip in a file to a specific model.
 *
 * Returns one report per clip; `clip.name` is the file's own clip name, which
 * the caller overrides with the manifest entry's `name`.
 */
export function clipsForModel(
  file: RawAnimationFile,
  vrm: VRM,
  options: RetargetOptions,
): RetargetReport[] {
  if (file.kind === 'vrma') {
    return file.animations.map((animation) => adoptVrmClip(createVRMAnimationClip(animation, vrm), vrm));
  }
  const root = file.root;
  if (!root) return [];
  return file.clips.map((clip) => retargetClip(clip, vrm, root, options));
}
