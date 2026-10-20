/**
 * Facial expressions.
 *
 * Three layers are blended with `max` per emotion, then smoothed:
 *
 *  * **mood** - what the backend detected for the current sentence
 *    (`expression` events: emotion + weight + hold time + blink override),
 *  * **transients** - short lived pokes from gesture notes such as `(laughs)`,
 *  * **ambient** - the *phase* of the conversation: a small pensive face while
 *    she is thinking, a soft attentive one while the user talks. It only shows
 *    when no sentence mood is active, so it can never fight a real reaction,
 *  * **decay** - the smooth return to neutral when all three have expired.
 *
 * A mood change also gets a short overshoot (`REACTION_SECONDS`): the new
 * expression arrives with a little more weight than it will settle at. That
 * single frame of emphasis is the difference between "the face changed" and
 * "she reacted".
 *
 * Each emotion resolves to *one* expression channel. Where the model has the
 * individual brow / lid / mouth-corner morph targets we register our own
 * expression for it (`AiHappy`, ...) so the mouth shape stays owned by
 * lip-sync and the lids stay owned by the blink controller - see
 * `expressionRecipes.ts` for why that matters. Where it does not, we fall back
 * to the model's VRM preset for that emotion and strip the preset's
 * `overrideMouth` / `overrideBlink` flags first, so the fallback cannot throttle
 * the mouth either.
 *
 * Nothing here ever writes a mouth *shape* (`aa`, `ih`, `ou`, `ee`, `oh`) or
 * `blink`: those have exactly one owner each, and two writers on one blendshape
 * is how a face starts to flutter.
 */

import type { VRM } from '@pixiv/three-vrm';
import { VRMExpression, VRMExpressionMorphTargetBind } from '@pixiv/three-vrm';

import { EMOTIONS, EMOTION_RECIPES, collectMorphMeshes, resolveRecipe, type EmotionName } from './expressionRecipes';
import { clamp01, smoothTowards } from './smooth';
import type { FrameController } from './types';

/** A face that reacts instantly reads as fake; a slow one reads as dead. */
const ATTACK_SECONDS = 0.16;
const RELEASE_SECONDS = 0.45;
/** Transients are pokes: they should arrive fast and leave without a trace. */
const TRANSIENT_ATTACK = 0.09;
/** The ambient "thinking / listening" face fades in more gently than a mood. */
const AMBIENT_ATTACK = 0.3;
/** Length of the overshoot a *changed* mood lands with. */
const REACTION_SECONDS = 0.24;
/** How much louder that first beat is than the mood it settles into. */
const REACTION_BOOST = 1.18;

/** Prefix for the expressions we register, so they can never shadow a preset. */
const CUSTOM_PREFIX = 'Ai';

interface Mood {
  emotion: EmotionName;
  weight: number;
  /** `performance.now()`-style seconds, or null to hold forever. */
  expiresAt: number | null;
  overrideBlink: boolean;
}

interface Transient {
  emotion: EmotionName;
  weight: number;
  expiresAt: number;
}

/** One emotion's public request, as it arrives over the wire or from the UI. */
export interface EmotionRequest {
  /** Friendly emotion name (a recipe key, or an alias such as `joy`). */
  emotion: string;
  /** The VRM preset the backend mapped it to; used only when `emotion` is unknown. */
  preset?: string;
  weight?: number;
  holdMs?: number;
  overrideBlink?: boolean;
}

/** What `attach()` worked out about the model's face. */
export interface ExpressionReport {
  /** `custom` when the avatar had the part morphs, `preset` when it did not. */
  mode: 'none' | 'custom' | 'preset' | 'mixed';
  /** Emotion -> the expression channel actually driven. */
  channels: Partial<Record<EmotionName, string>>;
  /** Morph targets used, per emotion, for the console dump. */
  targets: Partial<Record<EmotionName, string[]>>;
  /** Non-fatal problems worth showing a human. */
  notes: string[];
}

export class ExpressionController implements FrameController {
  private vrm: VRM | null = null;
  private mood: Mood | null = null;
  private transients: Transient[] = [];
  private ambientEmotion: EmotionName | null = null;
  private ambientWeight = 0;
  private readonly current = new Map<EmotionName, number>();
  private readonly channels = new Map<EmotionName, string>();
  private now = 0;

  private report: ExpressionReport = { mode: 'none', channels: {}, targets: {}, notes: [] };

  attach(vrm: VRM | null): void {
    this.vrm = vrm;
    this.current.clear();
    this.mood = null;
    this.transients = [];
    this.ambientEmotion = null;
    this.ambientWeight = 0;
    this.channels.clear();
    this.report = { mode: 'none', channels: {}, targets: {}, notes: [] };
    if (!vrm) return;

    const notes: string[] = [];
    const meshes = collectMorphMeshes(vrm.scene);
    const manager = vrm.expressionManager;

    let custom = 0;
    let preset = 0;

    const channels: ExpressionReport['channels'] = {};
    const targets: ExpressionReport['targets'] = {};

    for (const emotion of EMOTIONS) {
      const recipe = EMOTION_RECIPES[emotion];
      const binds = resolveRecipe(meshes, recipe);

      // A recipe with no morphs (neutral) needs no channel at all.
      if (recipe.morphs.length === 0) continue;

      const name = manager ? registerPartExpression(manager, emotion, binds) : null;
      if (name) {
        this.channels.set(emotion, name);
        channels[emotion] = name;
        targets[emotion] = binds.map((bind) => bind.target);
        custom += 1;
        continue;
      }

      // Fallback: drive the model's own preset, but neutralise the flags that
      // would otherwise scale down the mouth and the blink.
      const fallback = recipe.fallbackPreset;
      const expression = manager?.getExpression(fallback);
      if (expression) {
        expression.overrideMouth = 'none';
        expression.overrideBlink = 'none';
        expression.overrideLookAt = 'none';
        this.channels.set(emotion, fallback);
        channels[emotion] = fallback;
        preset += 1;
        continue;
      }

      if (!manager) {
        notes.push('this avatar has no expression manager; the face cannot animate');
        break;
      }
      notes.push(`"${emotion}" has no morph targets and no "${fallback}" preset - it will not show`);
    }

    // A model *can* have presets without part morphs (VRM 0.x, non-VRoid rigs).
    // Heal the flags on every preset we might fall back to, not just the ones
    // that were needed right now: a later sentence may reach a different one.
    if (manager) {
      for (const recipe of Object.values(EMOTION_RECIPES)) {
        const expression = manager.getExpression(recipe.fallbackPreset);
        if (expression) {
          expression.overrideMouth = 'none';
          expression.overrideBlink = 'none';
        }
      }
    }

    const mode: ExpressionReport['mode'] =
      custom > 0 && preset > 0 ? 'mixed' : custom > 0 ? 'custom' : preset > 0 ? 'preset' : 'none';

    this.report = { mode, channels, targets, notes };
    if (typeof console !== 'undefined') {
      console.info(`[expressions] ${mode} face: ${Object.entries(channels).map(([k, v]) => `${k}->${v}`).join(', ') || 'nothing'}`);
      for (const note of notes) console.warn(`[expressions] ${note}`);
    }
  }

  /** True while the current mood freezes blinking (e.g. `surprised`). */
  get blinkSuppressed(): boolean {
    const mood = this.mood;
    if (!mood || !mood.overrideBlink) return false;
    return mood.expiresAt === null || mood.expiresAt > this.now;
  }

  /** The emotion currently driving the face (for the UI). */
  get activeEmotion(): EmotionName | null {
    const mood = this.mood;
    if (!mood || mood.weight <= 0) return null;
    if (mood.expiresAt !== null && mood.expiresAt <= this.now) return null;
    return mood.emotion;
  }

  /** The model dump, for the settings drawer or a bug report. */
  describe(): ExpressionReport {
    return this.report;
  }

  /**
   * React to a sentence. `holdMs <= 0` keeps the face there until the next
   * emotion arrives (used for a lingering mood).
   */
  setMood(request: EmotionRequest | EmotionName): void {
    const options: EmotionRequest = typeof request === 'string' ? { emotion: request } : request;
    const resolved = normaliseEmotion(options.emotion, options.preset ?? '');
    if (!resolved) return;

    const weight = clamp01(options.weight ?? 0.75);
    const holdMs = options.holdMs ?? 2200;
    const changed = this.mood === null || this.mood.emotion !== resolved;

    this.mood = {
      emotion: resolved,
      weight,
      expiresAt: holdMs > 0 ? this.now + holdMs / 1000 : null,
      overrideBlink: options.overrideBlink ?? false,
    };

    // A *new* mood lands a little harder for a quarter of a second, then
    // settles. Refreshing the same mood (the backend re-sends it per sentence)
    // must not poke the face again, or a long reply would twitch every line.
    if (changed && resolved !== 'neutral' && weight > 0.05) {
      this.transients.push({
        emotion: resolved,
        weight: clamp01(weight * REACTION_BOOST + 0.04),
        expiresAt: this.now + REACTION_SECONDS,
      });
    }
  }

  /**
   * The face for a *conversation phase* rather than a sentence: `thinking`
   * while she waits for the model, `relaxed` while she listens. `null` clears
   * it. The ambient layer is suppressed whenever a sentence mood is alive.
   */
  setAmbient(emotion: EmotionName | null, weight = 0.5): void {
    this.ambientEmotion = emotion;
    this.ambientWeight = emotion ? clamp01(weight) : 0;
  }

  /** The ambient emotion currently requested, if any (for the UI / tests). */
  get ambient(): EmotionName | null {
    return this.ambientEmotion;
  }

  /** Short lived expression poke, e.g. `(laughs)`. */
  playTransient(emotion: string, weight = 0.6, durationMs = 1200): void {
    const resolved = normaliseEmotion(emotion, emotion);
    if (!resolved) return;
    this.transients.push({
      emotion: resolved,
      weight: clamp01(weight),
      expiresAt: this.now + Math.max(0.1, durationMs / 1000),
    });
  }

  /** Relax the face towards neutral. */
  clear(): void {
    this.mood = null;
    this.transients = [];
    this.ambientEmotion = null;
    this.ambientWeight = 0;
  }

  /** The smoothed weight currently on an emotion channel (debug overlay). */
  getWeight(emotion: string): number {
    const resolved = normaliseEmotion(emotion, emotion);
    return resolved ? (this.current.get(resolved) ?? 0) : 0;
  }

  /** Snapshot for the debug overlay. */
  snapshot(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const [emotion, weight] of this.current) {
      if (weight > 0.01) out[emotion] = Math.round(weight * 100) / 100;
    }
    return out;
  }

  update(delta: number): void {
    const manager = this.vrm?.expressionManager;
    if (!manager) return;

    this.now += delta;
    if (this.mood && this.mood.expiresAt !== null && this.mood.expiresAt <= this.now) {
      this.mood = null;
    }
    if (this.transients.length > 0) {
      this.transients = this.transients.filter((item) => item.expiresAt > this.now);
    }

    const moodEmotion = this.mood?.emotion ?? null;
    const moodWeight = this.mood?.weight ?? 0;
    // The ambient face is a *fallback*: it only shows when no sentence mood or
    // gesture poke is alive, so a real reaction always wins.
    const moodActive = this.mood !== null || this.transients.length > 0;

    // Always settle every channel we own, so an emotion that has no live
    // target is driven back to zero instead of being left at its last value.
    for (const emotion of EMOTIONS) {
      const channel = this.channels.get(emotion);
      if (!channel) continue;

      let transientWeight = 0;
      for (const transient of this.transients) {
        if (transient.emotion === emotion) transientWeight = Math.max(transientWeight, transient.weight);
      }
      const moodForThis = moodEmotion === emotion ? moodWeight : 0;
      const ambientForThis =
        !moodActive && this.ambientEmotion === emotion ? this.ambientWeight : 0;
      const target = Math.max(moodForThis, transientWeight, ambientForThis);

      const previous = this.current.get(emotion) ?? 0;
      const tau =
        target <= previous
          ? RELEASE_SECONDS
          : transientWeight > Math.max(moodForThis, ambientForThis)
            ? TRANSIENT_ATTACK
            : ambientForThis > moodForThis
              ? AMBIENT_ATTACK
              : ATTACK_SECONDS;

      const next = smoothTowards(previous, target, delta, tau);
      this.current.set(emotion, next);
      manager.setValue(channel, next);
    }
  }
}

/* -------------------------------------------------------------------------- */
/*  Registration                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Register a custom expression for one emotion from its resolved morph binds.
 *
 * Returns the expression name, or `null` when the avatar lacks the targets
 * (which is the caller's cue to fall back to a preset).
 */
function registerPartExpression(
  manager: NonNullable<VRM['expressionManager']>,
  emotion: EmotionName,
  binds: ReturnType<typeof resolveRecipe>,
): string | null {
  if (binds.length === 0) return null;

  const name = `${CUSTOM_PREFIX}${emotion[0].toUpperCase()}${emotion.slice(1)}`;
  // A second attach on the same manager (hot reload) must not stack duplicates.
  const existing = manager.getExpression(name);
  if (existing) return name;

  const expression = new VRMExpression(name);
  expression.isBinary = false;
  // The whole point of owning the parts: never throttle another layer.
  expression.overrideBlink = 'none';
  expression.overrideMouth = 'none';
  expression.overrideLookAt = 'none';

  for (const bind of binds) {
    expression.addBind(
      new VRMExpressionMorphTargetBind({
        primitives: [bind.mesh],
        index: bind.index,
        weight: bind.weight,
      }),
    );
  }

  manager.registerExpression(expression);
  return name;
}

/* -------------------------------------------------------------------------- */
/*  Emotion names                                                             */
/* -------------------------------------------------------------------------- */

/** `joy` -> `happy`: tolerate a VRM0 name, an alias, or a preset name. */
const ALIASES: Record<string, EmotionName> = {
  joy: 'happy',
  joyful: 'happy',
  excited: 'happy',
  cheerful: 'happy',
  grin: 'happy',
  sorrow: 'sad',
  cry: 'sad',
  upset: 'sad',
  fun: 'relaxed',
  calm: 'relaxed',
  content: 'relaxed',
  sleepy: 'relaxed',
  anger: 'angry',
  mad: 'angry',
  annoyed: 'angry',
  surprise: 'surprised',
  hmm: 'thinking',
  curious: 'thinking',
  bashful: 'shy',
  embarrassed: 'shy',
  blush: 'shy',
  neutral: 'neutral',
};

/**
 * Resolve a pair of hints to a known emotion.
 *
 * The backend's friendly `emotion` is authoritative when we recognise it;
 * `preset` is the safety net for a custom persona whose vocabulary we have
 * never seen - it at least maps onto something this face can do.
 */
export function normaliseEmotion(emotion: string, preset = ''): EmotionName | null {
  for (const raw of [emotion, preset]) {
    const key = raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
    if (!key) continue;
    if ((EMOTIONS as readonly string[]).includes(key)) return key as EmotionName;
    const alias = ALIASES[key];
    if (alias) return alias;
  }
  return null;
}
