/**
 * The body animation layer.
 *
 * It owns two stacked sources of motion and decides, *per frame and per bone*,
 * how they share the skeleton:
 *
 *  1. **Authored clips** - Mixamo FBX/GLB, Blender exports or VRM-native
 *     `.vrma`, played through a `THREE.AnimationMixer` and retargeted onto the
 *     VRM's normalized rig (see `retarget.ts`). Idle clips loop and rotate;
 *     gesture clips play once and blend out.
 *  2. **Procedural motion** - `MotionController`: breathing, weight shift, head
 *     drift and hand-written gestures. It needs no assets, so it is always the
 *     floor the avatar falls back to.
 *
 * Why the procedural layer runs *after* the mixer
 * ----------------------------------------------
 * `AnimationMixer` writes (and, at partial weight, blends against a snapshot of)
 * the properties it animates. So the mixer has the first word and the procedural
 * layer gets the last: for a bone it does not own it writes its own pose
 * outright, for a bone a clip owns it writes nothing at all, and *in between* it
 * slerps its pose over the mixer's output by `1 - ownership`. That last case is
 * what turns a clip's fade-in into a smooth blend instead of the hard snap you
 * get from two writers fighting over one quaternion.
 *
 * A pleasant side effect: because a clip's first activation happens while the
 * procedural layer still owns the bone, the mixer's "original state" snapshot is
 * the *procedural idle pose*, not the T-pose - so even a slow fade never dips
 * through a mannequin pose.
 */

import * as THREE from 'three';
import type { VRM } from '@pixiv/three-vrm';

import type { GestureName } from '../../types/config';
import { MotionController } from '../MotionController';
import { clamp01, smoothTowards } from '../smooth';
import type { FrameController } from '../types';
import { clipsForModel, clearAnimationCache, loadAnimationFile } from './loaders';
import type { RetargetReport } from './retarget';
import {
  DEFAULT_ANIMATION,
  type AnimationClipEntry,
  type AnimationManifest,
  type AnimationOptions,
  type AnimationStatus,
  type ClipKind,
} from './types';

/** Weight the idle is held at while a gesture clip plays, so gestures read. */
const IDLE_DUCK = 0.35;
const DEFAULT_FADE_IN = 0.35;
const DEFAULT_FADE_OUT = 0.5;
/** Below this a slot is considered silent and is stopped. */
const WEIGHT_EPSILON = 0.008;
/** Status is republished at most this often (it feeds a React store). */
const STATUS_INTERVAL = 0.25;

interface Slot {
  key: string;
  entry: AnimationClipEntry;
  kind: ClipKind;
  action: THREE.AnimationAction;
  report: RetargetReport;
  /** Canonical VRM bone names this clip drives. */
  bones: ReadonlySet<string>;
  fadeIn: number;
  fadeOut: number;
  intensity: number;
  /** Smoothed 0..1 influence, pushed into the action every frame. */
  weight: number;
  /** Gestures: inside the play window. */
  wanted: boolean;
  /** Gestures: `elapsed` at which the clip has finished and may fade out. */
  endsAt: number;
}

export type AnimationStatusListener = (status: AnimationStatus) => void;

export class AnimationController implements FrameController {
  /** The procedural layer. Public because the settings UI tunes it directly. */
  readonly procedural = new MotionController();

  private vrm: VRM | null = null;
  private mixer: THREE.AnimationMixer | null = null;

  private options: AnimationOptions = { ...DEFAULT_ANIMATION };
  private manifest: AnimationManifest = { clips: [] };
  private entries: AnimationClipEntry[] = [];

  private readonly slots = new Map<string, Slot>();
  private idleKeys: string[] = [];
  private gestureKeys: string[] = [];
  private activeIdle: string | null = null;
  private activeGesture: string | null = null;

  private ownership: ReadonlyMap<string, number> = new Map();

  private loading = false;
  private problems: string[] = [];
  private manifestLoaded = false;
  /** Invalidates in-flight loads when the model or config changes. */
  private generation = 0;

  private elapsed = 0;
  private idleNextAt = 0;
  private idleSeconds = 0;
  private statusAt = -Infinity;
  private statusSignature = '';
  private listeners = new Set<AnimationStatusListener>();

  /* ------------------------------------------------------------------ */
  /*  Public API                                                        */
  /* ------------------------------------------------------------------ */

  onStatus(listener: AnimationStatusListener): () => void {
    this.listeners.add(listener);
    listener(this.status);
    return () => this.listeners.delete(listener);
  }

  get status(): AnimationStatus {
    return {
      mode: this.slots.size > 0 ? 'clips' : 'procedural',
      loading: this.loading,
      problems: this.problems,
      clips: this.slots.size,
      idleClips: this.idleKeys.length,
      gestureClips: this.gestureKeys.length,
      activeIdle: this.activeIdle ? (this.slots.get(this.activeIdle)?.entry.name ?? null) : null,
      activeGesture: this.activeGesture ? (this.slots.get(this.activeGesture)?.entry.name ?? null) : null,
      drivenBones: this.ownership.size,
    };
  }

  /** Clip names the manifest provides, for the settings UI. */
  get clipNames(): Array<{ name: string; kind: ClipKind }> {
    return [...this.slots.values()].map((slot) => ({ name: slot.entry.name, kind: slot.kind }));
  }

  configure(patch: Partial<AnimationOptions>): void {
    const previous = this.options;
    this.options = { ...this.options, ...patch };

    const manifestChanged = this.options.manifestUrl !== previous.manifestUrl;
    if (manifestChanged) {
      this.manifestLoaded = false;
      this.slots.clear();
      this.idleKeys = [];
      this.gestureKeys = [];
      this.activeIdle = null;
      this.activeGesture = null;
      this.mixer = null;
    }

    if (!this.options.enabled) {
      this.deactivateAll();
      this.publishStatus(true);
      return;
    }

    if (!this.manifestLoaded) {
      void this.loadManifest();
      return;
    }

    if (this.vrm && this.slots.size === 0 && this.entries.length > 0) {
      void this.buildSlots();
      return;
    }

    this.syncIdleTiming();
    this.publishStatus(true);
  }

  get config(): AnimationOptions {
    return { ...this.options };
  }

  /** Re-fetch the manifest and every clip (the settings drawer's "Reload"). */
  reload(): void {
    clearAnimationCache();
    this.manifestLoaded = false;
    this.slots.clear();
    this.idleKeys = [];
    this.gestureKeys = [];
    this.activeIdle = null;
    this.activeGesture = null;
    this.mixer = null;
    this.problems = [];
    if (this.options.enabled) void this.loadManifest();
    else this.publishStatus(true);
  }

  attach(vrm: VRM | null): void {
    this.generation += 1;
    this.mixer = null;
    this.slots.clear();
    this.idleKeys = [];
    this.gestureKeys = [];
    this.activeIdle = null;
    this.activeGesture = null;
    this.ownership = new Map();

    this.vrm = vrm;
    this.procedural.attach(vrm);

    if (!vrm) {
      this.publishStatus(true);
      return;
    }
    vrm.scene.updateWorldMatrix(true, true);
    if (!this.options.enabled) {
      this.publishStatus(true);
      return;
    }
    if (!this.manifestLoaded) void this.loadManifest();
    else void this.buildSlots();
  }

  /**
   * Play a clip by name. Falls back to the procedural gesture with the same
   * name when the manifest has no clip for it, so the backend can keep asking
   * for `nod`/`wave`/`think` whether or not the user shipped animations.
   */
  play(name: string, intensity = 1): void {
    const slot = this.slots.get(`gesture:${name}`) ?? this.slots.get(`idle:${name}`);
    if (!slot || !this.options.enabled) {
      this.procedural.play(name as GestureName, intensity);
      return;
    }

    if (slot.kind === 'idle') {
      this.setActiveIdle(slot.key);
      return;
    }

    // A new gesture interrupts the current one rather than queueing behind it:
    // a wave the avatar delivers five seconds after the joke is a bug.
    for (const key of this.gestureKeys) {
      if (key !== slot.key) this.slots.get(key)!.wanted = false;
    }
    slot.wanted = true;
    slot.endsAt = this.elapsed + slot.report.clip.duration;
    slot.intensity = clamp01(intensity) * (slot.entry.intensity ?? 1) * this.options.gestureIntensity;
    this.activeGesture = slot.key;
  }

  get activeClip(): string | null {
    return this.activeGesture ? (this.slots.get(this.activeGesture)?.entry.name ?? null) : null;
  }

  /* ------------------------------------------------------------------ */
  /*  Frame                                                             */
  /* ------------------------------------------------------------------ */

  update(delta: number, elapsed: number): void {
    this.elapsed = elapsed;
    if (!this.vrm) return;

    if (!this.options.enabled || this.slots.size === 0) {
      this.ownership = new Map();
      this.procedural.setOwnership(this.ownership);
      this.procedural.update(delta, elapsed);
      this.publishStatus();
      return;
    }

    this.advanceGestures(elapsed);
    this.advanceIdle(elapsed);

    const ducking = this.gestureKeys.some((key) => this.slots.get(key)!.wanted) ? IDLE_DUCK : 1;
    this.stepWeights(delta, ducking);

    // Stop finished slots *before* the mixer runs: stopping deactivates the
    // action and restores the mixer's saved state, which the procedural layer
    // then overwrites for every bone it tracks.
    for (const slot of this.slots.values()) {
      if (slot.weight <= WEIGHT_EPSILON && !this.isWanted(slot) && slot.action.isRunning()) {
        slot.action.stop();
        slot.action.setEffectiveWeight(0);
      }
    }

    const mixer = this.mixer;
    if (mixer) {
      // Activate late so the mixer's "original state" snapshot is the procedural
      // pose of this frame rather than the model's rest pose.
      for (const slot of this.slots.values()) {
        if (slot.weight > WEIGHT_EPSILON && !slot.action.isRunning()) {
          slot.action.play();
          slot.action.setEffectiveWeight(slot.weight);
        } else if (slot.action.isRunning()) {
          slot.action.setEffectiveWeight(slot.weight);
        }
      }
      mixer.update(delta);
    }

    this.ownership = this.computeOwnership();
    this.procedural.setOwnership(this.ownership);
    this.procedural.update(delta, elapsed);

    this.publishStatus();
  }

  dispose(): void {
    this.deactivateAll();
    this.procedural.dispose();
    this.vrm = null;
    this.mixer = null;
    this.listeners.clear();
  }

  /* ------------------------------------------------------------------ */
  /*  Internals                                                         */
  /* ------------------------------------------------------------------ */

  private isWanted(slot: Slot): boolean {
    return slot.kind === 'gesture' ? slot.wanted : slot.key === this.activeIdle;
  }

  /** Slot weights are smoothed here and pushed to the actions in `update`. */
  private stepWeights(delta: number, ducking: number): void {
    for (const slot of this.slots.values()) {
      let target = 0;
      if (slot.kind === 'idle') {
        target = slot.key === this.activeIdle ? ducking : 0;
      } else if (slot.wanted) {
        target = slot.intensity;
      }
      const fade = target > slot.weight ? slot.fadeIn : slot.fadeOut;
      // tau = fade / 2.5 lands ~92% of the way after `fade` seconds.
      slot.weight = smoothTowards(slot.weight, target, delta, fade / 2.5);
      if (Math.abs(slot.weight - target) < 1e-4) slot.weight = target;
    }
  }

  private computeOwnership(): ReadonlyMap<string, number> {
    const ownership = new Map<string, number>();
    for (const slot of this.slots.values()) {
      if (slot.weight <= WEIGHT_EPSILON) continue;
      for (const bone of slot.bones) {
        const previous = ownership.get(bone) ?? 0;
        if (slot.weight > previous) ownership.set(bone, slot.weight);
      }
    }
    return ownership;
  }

  private deactivateAll(): void {
    for (const slot of this.slots.values()) {
      slot.wanted = false;
      slot.weight = 0;
      if (slot.action.isRunning()) slot.action.stop();
    }
    this.ownership = new Map();
    this.procedural.setOwnership(this.ownership);
    this.activeGesture = null;
  }

  private advanceGestures(elapsed: number): void {
    let running = false;
    for (const key of this.gestureKeys) {
      const slot = this.slots.get(key)!;
      if (slot.wanted && elapsed >= slot.endsAt) slot.wanted = false;
      if (slot.wanted) running = true;
    }
    if (!running && this.activeGesture) {
      const slot = this.slots.get(this.activeGesture);
      if (!slot?.wanted) this.activeGesture = null;
    }
  }

  private advanceIdle(elapsed: number): void {
    if (this.idleKeys.length === 0) return;
    if (this.activeIdle === null) {
      this.setActiveIdle(this.idleKeys[Math.floor(Math.random() * this.idleKeys.length)]!);
      return;
    }
    if (this.idleSeconds > 0 && elapsed >= this.idleNextAt) {
      const pool = this.idleKeys.filter((key) => key !== this.activeIdle);
      if (pool.length > 0) this.setActiveIdle(pool[Math.floor(Math.random() * pool.length)]!);
      else this.idleNextAt = elapsed + this.idleSeconds;
    }
  }

  private setActiveIdle(key: string): void {
    this.activeIdle = key;
    const slot = this.slots.get(key);
    if (slot) {
      // Re-seed the idle clip at a random phase so two loops never line up.
      slot.action.time = Math.random() * Math.max(0.001, slot.report.clip.duration);
      slot.weight = Math.max(slot.weight, WEIGHT_EPSILON);
    }
    this.idleNextAt = this.elapsed + this.idleSeconds * (0.75 + Math.random() * 0.5);
  }

  private syncIdleTiming(): void {
    const configured = this.options.idleSeconds > 0 ? this.options.idleSeconds : (this.manifest.idleSeconds ?? 0);
    this.idleSeconds = configured;
    this.idleNextAt = this.elapsed + this.idleSeconds;
  }

  private async loadManifest(): Promise<void> {
    if (this.loading) return;
    const url = this.options.manifestUrl;
    this.loading = true;
    this.problems = [];
    this.publishStatus(true);

    try {
      const response = await fetch(url, { cache: 'no-cache' });
      if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
      const raw = (await response.json()) as unknown;
      const parsed = normalizeManifest(raw);
      this.manifest = parsed.manifest;
      this.entries = parsed.entries;
      this.problems = parsed.problems;
    } catch (error) {
      this.manifest = { clips: [] };
      this.entries = [];
      this.problems =
        this.options.manifestUrl.trim() === ''
          ? []
          : [
              `Could not load ${url} (${errorText(error)}). ` +
                'Falling back to procedural motion.',
            ];
    } finally {
      this.loading = false;
      this.manifestLoaded = true;
    }

    this.syncIdleTiming();
    await this.buildSlots();
  }

  private async buildSlots(): Promise<void> {
    const vrm = this.vrm;
    this.slots.clear();
    this.idleKeys = [];
    this.gestureKeys = [];
    this.activeIdle = null;
    this.activeGesture = null;
    this.ownership = new Map();
    this.procedural.setOwnership(this.ownership);
    this.mixer = null;

    if (!vrm || !this.options.enabled || this.entries.length === 0) {
      this.publishStatus(true);
      return;
    }

    const generation = ++this.generation;
    const mixer = new THREE.AnimationMixer(vrm.scene);
    this.mixer = mixer;

    const results = await Promise.allSettled(
      this.entries.map(async (entry) => {
        const file = await loadAnimationFile(entry.url);
        const reports = clipsForModel(file, vrm, {
          positionScale: entry.positionScale ?? 1,
          positionStrength: entry.positionStrength ?? 1,
          rootRotation: entry.rootRotation ?? null,
        });
        return { entry, reports };
      }),
    );

    // A newer attach/configure happened while we were loading: discard.
    if (generation !== this.generation) return;

    const problems: string[] = [];
    for (const result of results) {
      if (result.status === 'rejected') {
        problems.push(`${errorText(result.reason)}`);
        continue;
      }
      const { entry, reports } = result.value;
      const report = pickReport(reports, entry.name);
      if (!report) {
        problems.push(`"${entry.name}" (${entry.url}) contains no usable animation track.`);
        continue;
      }
      if (reports.length > 1 && !reports.some((item) => item.clip.name === entry.name)) {
        problems.push(
          `"${entry.name}" (${entry.url}) holds ${reports.length} clips; using the first ` +
            '(`' + report.clip.name + '`). Add one manifest entry per clip.',
        );
      }
      if (report.bones.length === 0) {
        problems.push(
          `"${entry.name}" drives no bone of this avatar - check the file's skeleton ` +
            `(unmapped: ${report.unmapped.slice(0, 4).join(', ') || 'none'}).`,
        );
        continue;
      }
      if (report.unmapped.length > 0) {
        problems.push(
          `"${entry.name}": ignored ${report.unmapped.length} unmapped track(s) ` +
            `(${report.unmapped.slice(0, 3).join(', ')}).`,
        );
      }

      const clip = report.clip;
      clip.name = `${entry.kind}:${entry.name}`;
      const action = mixer.clipAction(clip);
      const loop = entry.loop ?? entry.kind === 'idle';
      if (loop) {
        action.setLoop(THREE.LoopRepeat, Infinity);
      } else {
        action.setLoop(THREE.LoopOnce, 1);
        // Hold the final frame while the weight fades out, instead of snapping.
        action.clampWhenFinished = true;
      }
      action.enabled = true;
      action.setEffectiveWeight(0);

      const key = `${entry.kind}:${entry.name}`;
      const slot: Slot = {
        key,
        entry,
        kind: entry.kind,
        action,
        report,
        bones: new Set(report.bones),
        fadeIn: entry.fadeIn ?? DEFAULT_FADE_IN,
        fadeOut: entry.fadeOut ?? DEFAULT_FADE_OUT,
        intensity: clamp01(entry.intensity ?? 1) * this.options.gestureIntensity,
        weight: 0,
        wanted: false,
        endsAt: Infinity,
      };
      this.slots.set(key, slot);
      if (slot.kind === 'idle') this.idleKeys.push(key);
      else this.gestureKeys.push(key);
    }

    this.problems = problems;
    this.syncIdleTiming();
    // Start on an idle immediately: a manifest with only gesture clips keeps the
    // procedural idle, which is exactly what we want.
    if (this.idleKeys.length > 0) {
      this.setActiveIdle(this.idleKeys[Math.floor(Math.random() * this.idleKeys.length)]!);
    }
    this.publishStatus(true);
  }

  private publishStatus(force = false): void {
    const status = this.status;
    const signature = [
      status.mode,
      status.loading,
      status.clips,
      status.activeIdle,
      status.activeGesture,
      status.problems.length,
      status.drivenBones,
    ].join('|');
    if (!force) {
      if (signature === this.statusSignature) return;
      if (this.elapsed - this.statusAt < STATUS_INTERVAL) return;
    }
    if (signature === this.statusSignature && !force) return;
    this.statusSignature = signature;
    this.statusAt = this.elapsed;
    for (const listener of this.listeners) listener(status);
  }
}

/* -------------------------------------------------------------------------- */
/*  Manifest parsing                                                          */
/* -------------------------------------------------------------------------- */

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * Accept both a full manifest object and a bare array of clips, and drop entries
 * that could never work (no name, no url) while saying so.
 */
export function normalizeManifest(raw: unknown): {
  manifest: AnimationManifest;
  entries: AnimationClipEntry[];
  problems: string[];
} {
  const problems: string[] = [];
  let source: unknown = raw;
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    source = (raw as { clips?: unknown }).clips ?? [];
  }

  const clips: AnimationClipEntry[] = [];
  if (!Array.isArray(source)) {
    return { manifest: { clips: [] }, entries: [], problems: ['The manifest has no `clips` array.'] };
  }

  for (const item of source) {
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    const name = typeof record.name === 'string' ? record.name.trim() : '';
    const url = typeof record.url === 'string' ? record.url.trim() : '';
    if (!name || !url) {
      problems.push('Skipped a manifest entry without both `name` and `url`.');
      continue;
    }
    const kind: ClipKind = record.kind === 'idle' || record.kind === 'gesture' ? record.kind : 'gesture';
    const entry: AnimationClipEntry = { name, url, kind };
    if (typeof record.loop === 'boolean') entry.loop = record.loop;
    if (typeof record.fadeIn === 'number') entry.fadeIn = record.fadeIn;
    if (typeof record.fadeOut === 'number') entry.fadeOut = record.fadeOut;
    if (typeof record.intensity === 'number') entry.intensity = record.intensity;
    if (typeof record.positionScale === 'number') entry.positionScale = record.positionScale;
    if (typeof record.positionStrength === 'number') entry.positionStrength = record.positionStrength;
    if (Array.isArray(record.rootRotation) && record.rootRotation.length === 3) {
      const [x, y, z] = record.rootRotation;
      if (typeof x === 'number' && typeof y === 'number' && typeof z === 'number') {
        entry.rootRotation = [x, y, z];
      }
    }
    clips.push(entry);
  }

  return { manifest: { clips }, entries: clips, problems };
}

/** The clip whose own name matches the entry, else the first one available. */
function pickReport(reports: RetargetReport[], name: string): RetargetReport | undefined {
  return reports.find((report) => report.clip.name === name) ?? reports[0];
}
