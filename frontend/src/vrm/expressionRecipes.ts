/**
 * Emotion recipes: what each emotion *is*, in morph targets.
 *
 * ## Why not just use the model's `happy` expression?
 *
 * A VRoid avatar's emotion presets bind a single "all face" morph target
 * (`Fcl_ALL_Joy` and friends): brows, lids and mouth in one shape. That is fine
 * for a still image and wrong for a talking head, for three measurable reasons.
 *
 * 1. `happy`, `sad` and `surprised` ship `overrideMouth: "blend"`. three-vrm
 *    implements that as `mouthMultiplier = 1 - overrideWeight`, so a happy
 *    sentence at weight 0.75 **scales every mouth viseme to 25%** - the mouth
 *    stops tracking the voice exactly when the face is most expressive. Both
 *    `happy` and `sad` also carry `overrideBlink: "blend"`, so the eyes stop
 *    closing on the same frame.
 * 2. The all-face morph includes a mouth shape, which then *adds* to whatever
 *    the lip-sync controller writes. Two writers on one face is how a talking
 *    avatar ends up looking like it is being puppeteered.
 * 3. It is one morph at one weight. Real expression is brow + lid + mouth
 *    corner, moving by different amounts.
 *
 * So instead of the preset we build our own expressions from the *parts*:
 * brow targets, eye targets and a mouth-corner target. They are registered on
 * the model's own {@link VRMExpressionManager} (so `override*`, binary
 * thresholding and `weight` all still behave), and:
 *
 *  * every recipe leaves the mouth *shape* to lip-sync - only the corners move,
 *    which is additive and therefore safe;
 *  * `overrideBlink` / `overrideMouth` / `overrideLookAt` are `none`, so the
 *    blink and lip-sync layers are never scaled down;
 *  * intensities are per-part, so the brow can carry "angry" while the mouth
 *    only tightens.
 *
 * A model without these morph targets (any non-VRoid rig) falls back to its own
 * preset per emotion - see `ExpressionController`, which also strips the
 * override flags off that preset so the fallback cannot break lip-sync either.
 */

/** VRM 1.0 expressions we ever borrow as a fallback base. */
export type EmotionName =
  | 'neutral'
  | 'happy'
  | 'sad'
  | 'angry'
  | 'relaxed'
  | 'surprised'
  | 'thinking'
  | 'shy';

/** Every emotion a recipe exists for, in a stable order. */
export const EMOTIONS: readonly EmotionName[] = [
  'neutral',
  'happy',
  'sad',
  'angry',
  'relaxed',
  'surprised',
  'thinking',
  'shy',
] as const;

/** One morph target to drive, with alternates for other rig conventions. */
interface MorphAccent {
  /** Tried in order; the first one the avatar actually has is used. */
  candidates: readonly string[];
  /** Weight relative to the recipe's own 0..1 scale. */
  weight: number;
}

export interface EmotionRecipe {
  /** VRM preset used when none of `morphs` exist on this model. */
  fallbackPreset: string;
  morphs: readonly MorphAccent[];
}

/**
 * The recipes.
 *
 * Weights are what the shape looks like at emotion strength 1.0; the backend's
 * per-emotion `weight` in `characters/*.yaml` scales the whole recipe, so these
 * are "fully felt" values. They are deliberately conservative: an expressive
 * face is a readable one, and over-driving a brow reads as a grimace.
 */
export const EMOTION_RECIPES: Readonly<Record<EmotionName, EmotionRecipe>> = {
  neutral: {
    fallbackPreset: 'neutral',
    morphs: [],
  },
  happy: {
    fallbackPreset: 'happy',
    morphs: [
      { candidates: ['Fcl_BRW_Joy', 'Fcl_BRW_Fun'], weight: 0.5 },
      // `Fun` is the *open*-eyed smile; `Joy` is the closed ^^ shape. Preferring
      // Joy made a happy reply a four-second "talking in her sleep" face, and
      // because it also covers the lids, blinking had nothing left to move.
      { candidates: ['Fcl_EYE_Fun', 'Fcl_EYE_Joy'], weight: 0.5 },
      { candidates: ['Fcl_MTH_Up'], weight: 0.6 },
    ],
  },
  sad: {
    fallbackPreset: 'sad',
    morphs: [
      { candidates: ['Fcl_BRW_Sorrow'], weight: 0.7 },
      { candidates: ['Fcl_EYE_Sorrow'], weight: 0.55 },
      { candidates: ['Fcl_MTH_Down'], weight: 0.45 },
    ],
  },
  angry: {
    fallbackPreset: 'angry',
    morphs: [
      { candidates: ['Fcl_BRW_Angry'], weight: 0.8 },
      { candidates: ['Fcl_EYE_Angry'], weight: 0.65 },
      { candidates: ['Fcl_MTH_Down'], weight: 0.35 },
    ],
  },
  relaxed: {
    fallbackPreset: 'relaxed',
    // "Content" is soft eyes and almost nothing else. Adding a smile here is
    // what makes a relaxed face look smug.
    morphs: [
      { candidates: ['Fcl_BRW_Fun'], weight: 0.2 },
      { candidates: ['Fcl_EYE_Fun'], weight: 0.45 },
    ],
  },
  surprised: {
    fallbackPreset: 'surprised',
    morphs: [
      { candidates: ['Fcl_BRW_Surprised'], weight: 0.75 },
      { candidates: ['Fcl_EYE_Surprised', 'Fcl_EYE_Spread'], weight: 0.9 },
      // No mouth morph. `Fcl_MTH_Large` is a second writer on the jaw and it
      // added a permanent open "O" on top of lip-sync, which is most of what
      // made surprised sentences look like shouting.
    ],
  },
  thinking: {
    fallbackPreset: 'relaxed',
    // Pensive: the inner brow lifts, the eyes narrow a little. The first
    // version was so subtle (an eye shape at 0.4 of 0.55) that it was invisible.
    morphs: [
      { candidates: ['Fcl_BRW_Sorrow'], weight: 0.3 },
      { candidates: ['Fcl_EYE_Natural'], weight: 0.45 },
      { candidates: ['Fcl_EYE_Spread'], weight: 0.1 },
    ],
  },
  shy: {
    fallbackPreset: 'relaxed',
    // A small, bashful smile - the blush does the rest of the work. Soft eyes,
    // not closed ones: see `happy`.
    morphs: [
      { candidates: ['Fcl_BRW_Sorrow'], weight: 0.3 },
      { candidates: ['Fcl_EYE_Fun', 'Fcl_EYE_Joy'], weight: 0.4 },
      { candidates: ['Fcl_MTH_Up'], weight: 0.4 },
    ],
  },
};

/** A morph-target mesh three.js can be driven by name. */
export interface MorphMesh {
  mesh: import('three').Mesh;
  /** `morphTargetDictionary` is `undefined` on a mesh with no morphs. */
  dictionary: Record<string, number>;
}

/** Find every mesh on the avatar that actually carries morph targets. */
export function collectMorphMeshes(root: import('three').Object3D): MorphMesh[] {
  const found: MorphMesh[] = [];
  root.traverse((object) => {
    const mesh = object as import('three').Mesh;
    if (!mesh.isMesh) return;
    const dictionary = mesh.morphTargetDictionary as Record<string, number> | undefined;
    if (!dictionary || Object.keys(dictionary).length === 0) return;
    found.push({ mesh, dictionary });
  });
  return found;
}

/**
 * The first mesh that owns `candidate`, or `null`.
 *
 * Exact names are tried first. Failing that, a *qualified* name is accepted: a
 * Blender or VRoid export writes the mesh into the shape key, so the target is
 * `Face_Blendshape.Fcl_BRW_Joy` for the shape we asked for as `Fcl_BRW_Joy`.
 * Only whole trailing segments count, so `Fcl_EYE_Joy` cannot accidentally
 * match `Fcl_EYE_Joy_L`.
 */
function findMorph(
  meshes: readonly MorphMesh[],
  candidates: readonly string[],
): { mesh: import('three').Mesh; index: number; name: string } | null {
  for (const candidate of candidates) {
    for (const { mesh, dictionary } of meshes) {
      const index = dictionary[candidate];
      if (typeof index === 'number') return { mesh, index, name: candidate };
    }
  }

  for (const candidate of candidates) {
    const wanted = candidate.toLowerCase();
    for (const { mesh, dictionary } of meshes) {
      for (const [key, index] of Object.entries(dictionary)) {
        const qualified = key.toLowerCase();
        if (qualified.endsWith(`.${wanted}`) || qualified.endsWith(`_${wanted}`)) {
          return { mesh, index, name: key };
        }
      }
    }
  }

  return null;
}

export interface AccentBinding {
  mesh: import('three').Mesh;
  index: number;
  weight: number;
  /** The morph target name that was actually used. */
  target: string;
}

/** The binds a recipe resolves to on this avatar; empty means "use the fallback". */
export function resolveRecipe(
  meshes: readonly MorphMesh[],
  recipe: EmotionRecipe,
): AccentBinding[] {
  const binds: AccentBinding[] = [];
  for (const accent of recipe.morphs) {
    const hit = findMorph(meshes, accent.candidates);
    if (!hit) continue;
    binds.push({ mesh: hit.mesh, index: hit.index, weight: accent.weight, target: hit.name });
  }
  return binds;
}
