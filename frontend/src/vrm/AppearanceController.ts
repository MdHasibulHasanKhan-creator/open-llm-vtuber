/**
 * The avatar's *look*: hair, eyes, skin and outfit, plus a runtime blush.
 *
 * All of it is texture work on the model the user already has, because a VRoid
 * avatar ships a single flat white base colour factor and puts every bit of its
 * colour in the texture. Two consequences shape this file:
 *
 *  * **A near-white texture takes a tint beautifully.** The T-shirt atlas
 *    averages `#f8f6f6`, so multiplying `material.color` into it recolours the
 *    fabric while keeping every fold, seam and shadow. Nothing is redrawn.
 *  * **A dark texture does not.** Multiplication can only darken, so hair
 *    (`#6c4a39`, brown) or an iris (`#5b3a1f`) can never become pink by
 *    multiplying - brown x pink is brown. Those parts are *recoloured* instead:
 *    the texture's own luminance keeps the shading, and the hue comes from the
 *    target colour. That is the same trick a paint program's "colorize" layer
 *    uses, and it works on any authored texture.
 *
 * Each role therefore declares how it wants to be tinted (`ROLE_TINT`), and
 * every number worth touching is in one of the three exported config objects.
 * `describe()` reports what the classifier concluded, so a differently-named
 * avatar can be diagnosed from the console instead of guessed at.
 */

import * as THREE from 'three';
import type { VRM } from '@pixiv/three-vrm';

import { clamp01, smoothTowards } from './smooth';
import type { FrameController } from './types';

/* -------------------------------------------------------------------------- */
/*  Tunables                                                                  */
/* -------------------------------------------------------------------------- */

/** Soft rose - the default top colour when nothing else is chosen. */
export const DEFAULT_SHIRT_COLOR = '#ffe3ee';

/** How much darker `shadeColor` is than the base tint (multiply roles only). */
const SHADE_DARKEN = 0.8;

/**
 * The blush. The two `u`/`v` pairs were measured off this model's own
 * `Face_00` texture: the face, eyeline, iris and mouth textures share one UV
 * layout, so the cheeks were placed relative to the eye geometry and verified
 * with a render rather than guessed. If a different avatar lands the blush on
 * the forehead, these are the two numbers to nudge.
 */
export const BLUSH = {
  color: '#ff8fa3',
  left: { u: 0.325, v: 0.635 },
  right: { u: 0.675, v: 0.635 },
  /** Radii as a fraction of the texture, so they scale with resolution. */
  radiusU: 0.072,
  radiusV: 0.05,
  /** Peak opacity at full intensity. Higher reads as feverish, lower as invisible. */
  maxAlpha: 0.62,
  /** Seconds to reach the target (63%) / to fade back out. */
  attackSeconds: 0.4,
  releaseSeconds: 0.6,
  /** Repaint only when the intensity moved this much. */
  repaintEpsilon: 0.012,
} as const;

/** How red the cheeks go per emotion. `shy` is the whole point of this feature. */
export const BLUSH_BY_EMOTION: Readonly<Record<string, number>> = {
  neutral: 0.15,
  relaxed: 0.35,
  happy: 0.45,
  surprised: 0.5,
  sad: 0.25,
  angry: 0.3,
  thinking: 0.2,
  shy: 0.9,
};

const FALLBACK_EMOTION_BLUSH = 0.3;

/** Which part of the body a material is, and how it must be tinted. */
export type AppearanceRole =
  | 'top'
  | 'bottom'
  | 'shoes'
  | 'skin'
  | 'face'
  | 'hair'
  | 'brow'
  | 'iris'
  | 'sclera'
  | 'highlight'
  | 'mouth'
  | 'eyeline'
  | 'other';

export interface RoleTint {
  /**
   * `multiply` scales the existing texture colours (right for near-white
   * fabric); `recolor` keeps the texture's shading and replaces its hue (right
   * for hair and irises); `none` is never tinted.
   */
  mode: 'multiply' | 'recolor' | 'none';
  /**
   * Recolor only: brightness applied to the texture's luminance before the
   * colour is mixed in. >1 lifts a dark texture (brown hair) up to a usable
   * value, so the result reads as the chosen colour rather than as mud.
   */
  gain?: number;
}

export const ROLE_TINT: Readonly<Record<AppearanceRole, RoleTint>> = {
  top: { mode: 'multiply' },
  bottom: { mode: 'multiply' },
  shoes: { mode: 'multiply' },
  skin: { mode: 'multiply' },
  face: { mode: 'multiply' },
  hair: { mode: 'recolor', gain: 1.8 },
  brow: { mode: 'recolor', gain: 1.7 },
  iris: { mode: 'recolor', gain: 2.0 },
  sclera: { mode: 'none' },
  highlight: { mode: 'none' },
  mouth: { mode: 'none' },
  eyeline: { mode: 'none' },
  other: { mode: 'none' },
};

/**
 * Ordered classification rules. Order matters: `eyeline` must win over `eye`,
 * and `bottom` must win over `top` (`bottoms` contains neither, but a
 * `TopBottom` naming mistake should not silently tint the skirt).
 */
const ROLE_RULES: readonly { role: AppearanceRole; any: readonly string[] }[] = [
  { role: 'eyeline', any: ['eyeline', 'eyelash', 'lash'] },
  { role: 'brow', any: ['brow', 'mayu'] },
  { role: 'highlight', any: ['highlight', 'hl_'] },
  { role: 'iris', any: ['iris', 'pupil', 'hitomi'] },
  { role: 'sclera', any: ['eyewhite', 'eye_white', 'sclera'] },
  { role: 'mouth', any: ['mouth', 'teeth', 'tongue', 'lip', 'kuchi'] },
  { role: 'bottom', any: ['bottom', 'skirt', 'pant', 'short', 'jean', 'slack', 'skirt'] },
  { role: 'shoes', any: ['shoe', 'boot', 'sock', 'footwear', 'leg'] },
  { role: 'hair', any: ['hair', 'kami'] },
  { role: 'top', any: ['tops', 'top_', '_top', 'shirt', 'tee', 'tunic', 'jacket', 'hoodie', 'sweater', 'blouse', 'dress', 'upper'] },
  { role: 'face', any: ['face'] },
  { role: 'skin', any: ['skin', 'body'] },
  { role: 'sclera', any: ['eye'] },
];

/* -------------------------------------------------------------------------- */
/*  Options                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * Every colour is `''` for "leave the model as authored", which is also what
 * `LOOK_PRESETS.original` sets. A colour that is present but has no effect (a
 * dark texture under `multiply`) is reported in `describe().warnings` rather
 * than silently ignored.
 */
export interface AppearanceOptions {
  /** The torso covering. Kept under its old name so saved prefs still load. */
  shirtColor: string;
  shirtEnabled: boolean;

  /** Skin tone, multiplied over both skin materials. Multiply can only darken. */
  skinTone: string;
  /** Hair colour, recoloured from the hair texture's luminance. */
  hairColor: string;
  /** Iris colour, recoloured from the iris texture's luminance. */
  eyeColor: string;
  /** Brow colour. `''` follows `hairColor`, which is what looks deliberate. */
  browColor: string;
  /** Trousers / skirt / shorts. */
  outfitBottom: string;
  /** Shoes. */
  shoesColor: string;

  blushEnabled: boolean;
  /** 0..1 multiplier applied on top of the per-emotion amount. */
  blushIntensity: number;
}

export const DEFAULT_APPEARANCE: AppearanceOptions = {
  shirtColor: DEFAULT_SHIRT_COLOR,
  shirtEnabled: true,
  skinTone: '#fff4ef',
  hairColor: '#ffa8c6',
  eyeColor: '#ff8fb0',
  browColor: '',
  outfitBottom: '',
  shoesColor: '',
  blushEnabled: true,
  blushIntensity: 1,
};

/**
 * One-click looks. `original` is the avatar exactly as its artist shipped it,
 * which is the honest default for somebody else's model.
 */
export const LOOK_PRESETS: Readonly<Record<string, { label: string; look: Partial<AppearanceOptions> }>> = {
  rose: {
    label: 'Rose (default)',
    look: {
      shirtColor: '#ffe3ee',
      shirtEnabled: true,
      hairColor: '#ffa8c6',
      eyeColor: '#ff8fb0',
      browColor: '',
      skinTone: '#fff4ef',
      outfitBottom: '',
      shoesColor: '',
    },
  },
  sakura: {
    label: 'Sakura',
    look: {
      shirtColor: '#fff1f5',
      shirtEnabled: true,
      hairColor: '#ffc9dd',
      eyeColor: '#ffb36b',
      browColor: '',
      skinTone: '#fff8f4',
      outfitBottom: '',
      shoesColor: '#ffe9f0',
    },
  },
  midnight: {
    label: 'Midnight',
    look: {
      shirtColor: '#d9dcf2',
      shirtEnabled: true,
      hairColor: '#9aa6ff',
      eyeColor: '#8fd8ff',
      browColor: '',
      skinTone: '#f6ecec',
      outfitBottom: '',
      shoesColor: '#e6e8f5',
    },
  },
  vanilla: {
    label: 'Vanilla',
    look: {
      shirtColor: '#fff6e6',
      shirtEnabled: true,
      hairColor: '#ffe6a8',
      eyeColor: '#7fe0a8',
      browColor: '',
      skinTone: '#fff6ee',
      outfitBottom: '',
      shoesColor: '',
    },
  },
  original: {
    label: 'As authored',
    look: {
      shirtColor: '',
      shirtEnabled: false,
      hairColor: '',
      eyeColor: '',
      browColor: '',
      skinTone: '',
      outfitBottom: '',
      shoesColor: '',
    },
  },
};

/* -------------------------------------------------------------------------- */
/*  Reports                                                                   */
/* -------------------------------------------------------------------------- */

export interface MaterialSummary {
  name: string;
  shader: string;
  /** `#rrggbb` as authored, before any tint. */
  color: string;
  shadeColor: string | null;
  hasMap: boolean;
  role: AppearanceRole;
  /** How this role is tinted, or `undefined` when it is never touched. */
  tint?: 'multiply' | 'recolor';
  tinted: boolean;
}

export interface AppearanceReport {
  materials: MaterialSummary[];
  /** Material name -> role, for everything that got a role at all. */
  roles: Record<string, AppearanceRole>;
  /** The material the blush is painted into, if any. */
  face: string | null;
  /** `'expression'` when the model has a blush blendshape, else `'texture'`. */
  blushMode: 'expression' | 'texture' | 'none';
  blushExpression: string | null;
  warnings: string[];
}

/* -------------------------------------------------------------------------- */
/*  Internals                                                                 */
/* -------------------------------------------------------------------------- */

interface MutableMaterial extends THREE.Material {
  color?: THREE.Color;
  map?: THREE.Texture | null;
  shadeColor?: THREE.Color;
  shadeColorFactor?: THREE.Color;
}

interface Snapshot {
  material: MutableMaterial;
  role: AppearanceRole;
  color: THREE.Color;
  shadeColor: THREE.Color | null;
  shadeKey: 'shadeColor' | 'shadeColorFactor' | null;
  map: THREE.Texture | null;
}

/** A recoloured texture: the original kept for reset, plus its working canvas. */
interface RecoloredTexture {
  original: THREE.Texture;
  canvas: HTMLCanvasElement;
  texture: THREE.CanvasTexture;
  /** The colour currently baked into `canvas`; `''` means "not yet drawn". */
  baked: string;
}

/** Blendshape-ish names that mean the model already owns a blush. */
const BLUSH_EXPRESSION_HINTS = ['blush', 'cheek', 'shy', 'fcl_all'] as const;

function hasAny(haystack: string, needles: readonly string[]): boolean {
  return needles.some((needle) => haystack.includes(needle));
}

export function classifyMaterial(name: string): AppearanceRole {
  const lower = name.toLowerCase();
  for (const rule of ROLE_RULES) {
    if (hasAny(lower, rule.any)) return rule.role;
  }
  return 'other';
}

function hexOf(color: THREE.Color | undefined | null): string {
  return color ? `#${color.getHexString()}` : '#ffffff';
}

/** A colour string that is actually usable, or `null`. */
function parseColor(value: string | undefined): THREE.Color | null {
  const raw = (value ?? '').trim();
  if (!raw) return null;
  try {
    return new THREE.Color(raw);
  } catch {
    return null;
  }
}

const LUMA = { r: 0.2126, g: 0.7152, b: 0.0722 };

/* -------------------------------------------------------------------------- */
/*  The controller                                                            */
/* -------------------------------------------------------------------------- */

export class AppearanceController implements FrameController {
  private vrm: VRM | null = null;
  private options: AppearanceOptions = { ...DEFAULT_APPEARANCE };
  private report: AppearanceReport = { materials: [], roles: {}, face: null, blushMode: 'none', blushExpression: null, warnings: [] };

  private snapshots: Snapshot[] = [];
  private byRole = new Map<AppearanceRole, MutableMaterial[]>();
  private recolor = new Map<MutableMaterial, RecoloredTexture>();
  private faceMaterial: MutableMaterial | null = null;

  private emotion = 'neutral';
  private intensity = 0;
  private lastPainted = -1;

  private blushCanvas: HTMLCanvasElement | null = null;
  private blushTexture: THREE.CanvasTexture | null = null;
  private blushSource: THREE.Texture | null = null;

  /* ---------------------------------------------------------------------- */
  /*  Attach / detach                                                       */
  /* ---------------------------------------------------------------------- */

  attach(vrm: VRM | null): void {
    this.restore();
    this.vrm = vrm;
    if (!vrm) return;

    const summaries: MaterialSummary[] = [];
    const warnings: string[] = [];
    const roles: Record<string, AppearanceRole> = {};
    const seen = new Set<string>();

    vrm.scene.traverse((object) => {
      const mesh = object as THREE.Mesh;
      if (!mesh.isMesh || !mesh.material) return;
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];

      for (const material of materials) {
        const mat = material as MutableMaterial;
        const name = mat.name || `${mesh.name || 'mesh'}::${mat.type}`;
        // One material can be shared by several meshes; classify it once.
        const key = `${name}|${mat.uuid}`;
        if (seen.has(key)) continue;
        seen.add(key);

        const role = classifyMaterial(name);
        const snapshot: Snapshot = {
          material: mat,
          role,
          color: (mat.color ?? new THREE.Color(0xffffff)).clone(),
          shadeColor: this.shadeValueOf(mat)?.clone() ?? null,
          shadeKey: this.shadeKeyOf(mat),
          map: mat.map ?? null,
        };
        this.snapshots.push(snapshot);

        const list = this.byRole.get(role) ?? [];
        list.push(mat);
        this.byRole.set(role, list);
        if (role === 'face' && !this.faceMaterial && mat.map) this.faceMaterial = mat;

        const tint = ROLE_TINT[role].mode;
        summaries.push({
          name,
          shader: mat.type,
          color: hexOf(mat.color),
          shadeColor: this.shadeValueOf(mat) ? hexOf(this.shadeValueOf(mat)) : null,
          hasMap: !!mat.map,
          role,
          tint: tint === 'none' ? undefined : tint,
          tinted: false,
        });
        if (role !== 'other') roles[name] = role;
      }
    });

    const tintable: AppearanceRole[] = ['top', 'bottom', 'shoes', 'skin', 'face', 'hair', 'brow', 'iris'];
    for (const role of tintable) {
      if (!this.byRole.get(role)?.length) warnings.push(`no "${role}" material matched on this model`);
    }
    // Multiply can only darken; say so where it is likely to bite.
    for (const role of ['bottom'] as const) {
      const materials = this.byRole.get(role) ?? [];
      const dark = materials.some((mat) => this.averageTextureLightness(mat) < 0.25);
      if (dark) {
        warnings.push(`the "${role}" texture is already very dark - tinting it can only darken it further`);
      }
    }

    const blush = this.detectBlushExpression(vrm);
    if (!blush && !this.faceMaterial) {
      warnings.push('no face-skin material with a texture; the runtime blush is unavailable');
    }

    this.report = {
      materials: summaries,
      roles,
      face: this.faceMaterial?.name ?? null,
      blushMode: blush ? 'expression' : this.faceMaterial ? 'texture' : 'none',
      blushExpression: blush,
      warnings,
    };

    if (blush === null && this.faceMaterial) this.buildBlushTexture();

    this.dumpToConsole();
    this.applyLook();
    this.lastPainted = -1;
    this.intensity = 0;
    this.repaint(true);
  }

  /** Put every material back exactly as it was and drop every generated texture. */
  restore(): void {
    for (const snapshot of this.snapshots) {
      const mat = snapshot.material;
      mat.color?.copy(snapshot.color);
      if (snapshot.shadeKey && snapshot.shadeColor) {
        (mat[snapshot.shadeKey] as THREE.Color | undefined)?.copy(snapshot.shadeColor);
      }
      if (mat.map !== snapshot.map) mat.map = snapshot.map;
    }
    for (const entry of this.recolor.values()) {
      entry.texture.dispose();
    }
    this.recolor.clear();
    this.snapshots = [];
    this.byRole = new Map();
    this.faceMaterial = null;
    this.blushTexture?.dispose();
    this.blushTexture = null;
    this.blushCanvas = null;
    this.blushSource = null;
  }

  dispose(): void {
    this.restore();
    this.vrm = null;
  }

  /** The model dump, for the settings drawer or a bug report. */
  describe(): AppearanceReport {
    return this.report;
  }

  configure(options: Partial<AppearanceOptions>): void {
    this.options = { ...this.options, ...options };
    this.applyLook();
  }

  setEmotion(emotion: string | null | undefined): void {
    this.emotion = (emotion ?? 'neutral').toLowerCase();
  }

  /* ---------------------------------------------------------------------- */
  /*  The look                                                              */
  /* ---------------------------------------------------------------------- */

  /** The colour requested for a role, or `null` for "leave it alone". */
  private colorFor(role: AppearanceRole): string {
    const opt = this.options;
    switch (role) {
      case 'top':
        return opt.shirtEnabled ? opt.shirtColor : '';
      case 'bottom':
        return opt.outfitBottom;
      case 'shoes':
        return opt.shoesColor;
      case 'skin':
      case 'face':
        return opt.skinTone;
      case 'hair':
        return opt.hairColor;
      case 'brow':
        // Brows follow the hair unless told otherwise: it is what makes a
        // recoloured head look intentional instead of half-finished.
        return opt.browColor || opt.hairColor;
      case 'iris':
        return opt.eyeColor;
      default:
        return '';
    }
  }

  /** Re-derive every material's colour / texture from the current options. */
  private applyLook(): void {
    for (const snapshot of this.snapshots) {
      const mat = snapshot.material;
      const tintConfig = ROLE_TINT[snapshot.role];
      const wanted = this.colorFor(snapshot.role);

      if (tintConfig.mode === 'none' || !wanted) {
        this.resetMaterial(snapshot);
        continue;
      }

      const tint = parseColor(wanted);
      if (!tint) {
        this.resetMaterial(snapshot);
        continue;
      }

      if (tintConfig.mode === 'multiply') {
        // Multiply by the *authored* colour rather than replacing it: if an
        // artist ships a non-white baseColorFactor, tinting still composes.
        mat.color?.copy(snapshot.color).multiply(tint);
        if (snapshot.shadeKey) {
          const target = mat[snapshot.shadeKey] as THREE.Color | undefined;
          if (target) {
            const base = snapshot.shadeColor ?? new THREE.Color(0xffffff);
            target.copy(base).multiply(tint).multiplyScalar(SHADE_DARKEN);
          }
        }
      } else {
        // Recolor writes the hue into the texture, so the material colour must
        // go back to what the artist had (usually white) or it double-tints.
        mat.color?.copy(snapshot.color);
        if (snapshot.shadeKey && snapshot.shadeColor) {
          (mat[snapshot.shadeKey] as THREE.Color | undefined)?.copy(snapshot.shadeColor);
        }
        this.recolorTexture(snapshot, wanted, tintConfig.gain ?? 1);
      }
    }

    for (const summary of this.report.materials) {
      const wanted = this.colorFor(summary.role);
      summary.tinted = ROLE_TINT[summary.role].mode !== 'none' && !!wanted;
    }
  }

  private resetMaterial(snapshot: Snapshot): void {
    const mat = snapshot.material;
    mat.color?.copy(snapshot.color);
    if (snapshot.shadeKey && snapshot.shadeColor) {
      (mat[snapshot.shadeKey] as THREE.Color | undefined)?.copy(snapshot.shadeColor);
    }
    // The blush owns the face material's map; the look must never restore it.
    if (mat === this.faceMaterial) return;
    if (mat.map !== snapshot.map) {
      const entry = this.recolor.get(mat);
      mat.map = snapshot.map;
      if (entry) {
        entry.texture.dispose();
        this.recolor.delete(mat);
      }
    }
  }

  /**
   * Bake `hex` into a copy of the material's texture, preserving the original's
   * shading.
   *
   * `out = clamp(luminance * gain) * colour` per pixel: the texture keeps its
   * detail (folds, strands, iris rings) and the hue comes entirely from the
   * mix. Cached per material and per colour, because it touches every pixel.
   */
  private recolorTexture(snapshot: Snapshot, hex: string, gain: number): void {
    const mat = snapshot.material;
    const source = snapshot.map;
    if (!mat || !source) return;

    const image = source.image as { width?: number; height?: number } | undefined;
    if (!image?.width || !image?.height) return;

    const key = normalizeHex(hex);
    let entry = this.recolor.get(mat);

    if (!entry) {
      const canvas = document.createElement('canvas');
      canvas.width = image.width;
      canvas.height = image.height;
      const texture = new THREE.CanvasTexture(canvas);
      cloneTextureSettings(source, texture, 'recolor');
      entry = { original: source, canvas, texture, baked: '' };
      this.recolor.set(mat, entry);
      mat.map = texture;
    }

    if (entry.baked === key) return;
    entry.baked = key;

    const ctx = entry.canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return;

    ctx.clearRect(0, 0, entry.canvas.width, entry.canvas.height);
    ctx.drawImage(source.image as CanvasImageSource, 0, 0, entry.canvas.width, entry.canvas.height);

    const colour = new THREE.Color(key).convertLinearToSRGB();
    const target = { r: colour.r, g: colour.g, b: colour.b };

    try {
      const frame = ctx.getImageData(0, 0, entry.canvas.width, entry.canvas.height);
      const px = frame.data;
      for (let i = 0; i < px.length; i += 4) {
        const luminance = Math.min(
          1,
          (px[i] * LUMA.r + px[i + 1] * LUMA.g + px[i + 2] * LUMA.b) / 255 * gain,
        );
        px[i] = clamp255(luminance * target.r * 255);
        px[i + 1] = clamp255(luminance * target.g * 255);
        px[i + 2] = clamp255(luminance * target.b * 255);
      }
      ctx.putImageData(frame, 0, 0);
      entry.texture.needsUpdate = true;
    } catch (error) {
      // A tainted canvas (texture loaded without CORS) throws here. Better to
      // keep the model's own colours than to break the avatar entirely.
      console.warn('[appearance] could not recolour a texture:', error);
    }
  }

  /* ---------------------------------------------------------------------- */
  /*  Blush                                                                 */
  /* ---------------------------------------------------------------------- */

  private detectBlushExpression(vrm: VRM): string | null {
    const manager = vrm.expressionManager as unknown as
      | { expressionMap?: Record<string, unknown> }
      | undefined;
    if (!manager?.expressionMap) return null;

    for (const name of Object.keys(manager.expressionMap)) {
      if (hasAny(name.toLowerCase(), BLUSH_EXPRESSION_HINTS)) return name;
    }
    return null;
  }

  private buildBlushTexture(): void {
    const material = this.faceMaterial;
    const source = material?.map ?? null;
    const image = source?.image as { width?: number; height?: number } | undefined;
    if (!material || !source || !image?.width || !image?.height) return;

    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;

    const texture = new THREE.CanvasTexture(canvas);
    // Match the original exactly, or the blush lands mirrored / in the wrong
    // colour space. This is the single easiest thing to get wrong here.
    cloneTextureSettings(source, texture, 'blush');

    this.blushSource = source;
    this.blushCanvas = canvas;
    this.blushTexture = texture;
    material.map = texture;
  }

  private paintBlush(amount: number): void {
    const canvas = this.blushCanvas;
    const source = this.blushSource;
    const texture = this.blushTexture;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !source || !texture || !ctx) return;

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(source.image as CanvasImageSource, 0, 0, canvas.width, canvas.height);

    const alpha = clamp01(amount) * BLUSH.maxAlpha;
    if (alpha > 0.002) {
      const radiusX = BLUSH.radiusU * canvas.width;
      const radiusY = BLUSH.radiusV * canvas.height;
      const flip = source.flipY;
      const srgb = new THREE.Color(BLUSH.color).convertLinearToSRGB();
      const rgba = (a: number) =>
        `rgba(${Math.round(srgb.r * 255)}, ${Math.round(srgb.g * 255)}, ${Math.round(srgb.b * 255)}, ${a})`;

      for (const cheek of [BLUSH.left, BLUSH.right]) {
        const cx = cheek.u * canvas.width;
        const cy = (flip ? 1 - cheek.v : cheek.v) * canvas.height;
        ctx.save();
        ctx.translate(cx, cy);
        // Squash a circular gradient into the ellipse of a cheek.
        ctx.scale(1, Math.max(0.05, radiusY / radiusX));
        const gradient = ctx.createRadialGradient(0, 0, 0, 0, 0, radiusX);
        gradient.addColorStop(0, rgba(alpha));
        gradient.addColorStop(0.55, rgba(alpha * 0.7));
        gradient.addColorStop(1, rgba(0));
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(0, 0, radiusX, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    texture.needsUpdate = true;
  }

  private repaint(force = false): void {
    if (this.report.blushMode !== 'texture') return;
    if (!force && Math.abs(this.intensity - this.lastPainted) < BLUSH.repaintEpsilon) return;
    this.lastPainted = this.intensity;
    this.paintBlush(this.intensity);
  }

  /* ---------------------------------------------------------------------- */
  /*  Frame loop                                                            */
  /* ---------------------------------------------------------------------- */

  update(delta: number): void {
    if (this.report.blushMode === 'none') return;

    const emotionLevel = BLUSH_BY_EMOTION[this.emotion] ?? FALLBACK_EMOTION_BLUSH;
    const enabled = this.options.blushEnabled;
    const target = enabled ? clamp01(emotionLevel * this.options.blushIntensity) : 0;
    const tau = target > this.intensity ? BLUSH.attackSeconds : BLUSH.releaseSeconds;

    const next = smoothTowards(this.intensity, target, delta, tau);
    if (Math.abs(next - this.intensity) < 1e-4 && Math.abs(next - target) < 1e-4) {
      this.intensity = target;
    } else {
      this.intensity = next;
    }

    if (this.report.blushMode === 'expression' && this.report.blushExpression) {
      const manager = this.vrm?.expressionManager as unknown as
        | { setValue?: (name: string, weight: number) => void }
        | undefined;
      manager?.setValue?.(this.report.blushExpression, this.intensity);
      return;
    }

    this.repaint();
  }

  /* ---------------------------------------------------------------------- */
  /*  Material helpers                                                      */
  /* ---------------------------------------------------------------------- */

  private shadeKeyOf(mat: MutableMaterial): Snapshot['shadeKey'] {
    if (mat.shadeColor instanceof THREE.Color) return 'shadeColor';
    if (mat.shadeColorFactor instanceof THREE.Color) return 'shadeColorFactor';
    return null;
  }

  private shadeValueOf(mat: MutableMaterial): THREE.Color | null {
    const key = this.shadeKeyOf(mat);
    return key ? (mat[key] as THREE.Color) : null;
  }

  /**
   * Mean lightness of a material's texture, sampled sparsely.
   *
   * Only used to warn that a `multiply` tint cannot brighten a dark texture, so
   * a 16x16 sample is plenty and costs nothing.
   */
  private averageTextureLightness(mat: MutableMaterial): number {
    const source = mat.map;
    const image = source?.image as { width?: number; height?: number } | undefined;
    if (!source || !image?.width || !image?.height) return 1;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 16;
      canvas.height = 16;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      if (!ctx) return 1;
      ctx.drawImage(source.image as CanvasImageSource, 0, 0, 16, 16);
      const px = ctx.getImageData(0, 0, 16, 16).data;
      let total = 0;
      let count = 0;
      for (let i = 0; i < px.length; i += 4) {
        total += (px[i] * LUMA.r + px[i + 1] * LUMA.g + px[i + 2] * LUMA.b) / 255;
        count += 1;
      }
      return count ? total / count : 1;
    } catch {
      return 1;
    }
  }

  /* ---------------------------------------------------------------------- */
  /*  Diagnostics                                                           */
  /* ---------------------------------------------------------------------- */

  private dumpToConsole(): void {
    if (typeof console === 'undefined') return;
    const rows = this.report.materials.map((entry) => ({
      name: entry.name,
      role: entry.role,
      tint: entry.tint ?? '',
      color: entry.color,
      shadeColor: entry.shadeColor ?? '',
      map: entry.hasMap ? 'yes' : 'no',
      shader: entry.shader,
    }));
    console.info(`[appearance] ${rows.length} materials on this model`);
    // A table is the fastest way for a human to spot a misclassified material.
    (console.table ?? console.info)(rows);
    console.info('[appearance] report', {
      faceMaterial: this.report.face,
      blushMode: this.report.blushMode,
      blushExpression: this.report.blushExpression,
      warnings: this.report.warnings,
    });
  }
}

/* -------------------------------------------------------------------------- */
/*  Texture plumbing                                                          */
/* -------------------------------------------------------------------------- */

/** Copy every sampling-relevant setting so a copy composites identically. */
function cloneTextureSettings(source: THREE.Texture, texture: THREE.CanvasTexture, suffix: string): void {
  texture.flipY = source.flipY;
  texture.colorSpace = source.colorSpace;
  texture.wrapS = source.wrapS;
  texture.wrapT = source.wrapT;
  texture.repeat.copy(source.repeat);
  texture.offset.copy(source.offset);
  texture.center.copy(source.center);
  texture.rotation = source.rotation;
  texture.minFilter = source.minFilter;
  texture.magFilter = source.magFilter;
  texture.generateMipmaps = source.generateMipmaps;
  texture.anisotropy = source.anisotropy;
  texture.name = `${source.name || 'texture'}#${suffix}`;
}

function normalizeHex(hex: string): string {
  const trimmed = hex.trim().toLowerCase();
  return trimmed.startsWith('#') ? trimmed : `#${trimmed}`;
}

function clamp255(value: number): number {
  return value < 0 ? 0 : value > 255 ? 255 : value;
}
