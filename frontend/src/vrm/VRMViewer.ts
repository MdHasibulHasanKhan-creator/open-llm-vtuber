/**
 * The Three.js stage: renderer, camera, lights, model loading and the one
 * animation loop everything else hangs off.
 *
 * Order inside the loop matters and is the whole trick to a coherent avatar:
 *
 *   1. controllers write *expressions* and *normalized bones*,
 *   2. `vrm.update(dt)` copies the normalized rig onto the real bones, applies
 *      the blendshape weights and points the eyes,
 *   3. we render.
 *
 * Getting 2 before 1 means every pose is one frame late; getting 3 before 2
 * means nothing moves at all.
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import type { VRM } from '@pixiv/three-vrm';

import { CAMERA_FRAMING } from './constants';
import type { FrameController } from './types';
import type { CameraPreset } from '../types/config';

export interface ViewerStats {
  fps: number;
  drawCalls: number;
  triangles: number;
}

export interface VRMViewerOptions {
  pixelRatio?: number;
  shadows?: boolean;
  cameraPreset?: CameraPreset;
  /** 0..1 while downloading, `null` label when the size is unknown. */
  onProgress?: (fraction: number | null, label: string) => void;
  onStats?: (stats: ViewerStats) => void;
}

const NEAR = 0.05;
const FAR = 40;
/** Guard against a huge dt after a tab switch (which would jolt every pose). */
const MAX_DELTA = 0.1;

export class VRMViewer {
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  readonly renderer: THREE.WebGLRenderer;
  readonly clock = new THREE.Clock();
  /** Pointer in normalised device coordinates (`-1..1`), fed by the stage. */
  readonly pointer = new THREE.Vector2();
  pointerAt = -Infinity;

  vrm: VRM | null = null;

  private readonly container: HTMLElement;
  private readonly options: VRMViewerOptions;
  private readonly controllers: FrameController[] = [];
  private readonly listeners = new Set<(vrm: VRM | null) => void>();
  private readonly resizeObserver: ResizeObserver;
  private readonly keyLight: THREE.DirectionalLight;

  private elapsed = 0;
  private running = false;
  private cameraPreset: CameraPreset;
  private readonly cameraDesired = { position: new THREE.Vector3(), target: new THREE.Vector3(), fov: 30 };
  private readonly cameraTarget = new THREE.Vector3(0, 1.4, 0);
  private cameraReady = false;

  private frames = 0;
  private statsAt = 0;
  private lastStats: ViewerStats = { fps: 0, drawCalls: 0, triangles: 0 };

  constructor(container: HTMLElement, options: VRMViewerOptions = {}) {
    this.container = container;
    this.options = options;
    this.cameraPreset = options.cameraPreset ?? 'bust';

    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance',
      // The avatar is composited over a CSS gradient: no need for an opaque
      // buffer, which also avoids a black flash before the first frame.
      premultipliedAlpha: true,
    });
    this.renderer.setClearAlpha(0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.setPixelRatio(this.effectivePixelRatio());
    this.renderer.domElement.style.display = 'block';
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    this.renderer.domElement.style.touchAction = 'none';
    container.appendChild(this.renderer.domElement);

    this.camera = new THREE.PerspectiveCamera(this.cameraDesired.fov, 1, NEAR, FAR);
    this.camera.position.set(0, 1.4, 1.2);

    this.keyLight = new THREE.DirectionalLight(0xffffff, 1.15);
    this.keyLight.position.set(1.2, 2.4, 1.6);
    this.scene.add(this.keyLight);

    const fill = new THREE.DirectionalLight(0x9fb4ff, 0.35);
    fill.position.set(-1.6, 1.2, -1.4);
    this.scene.add(fill);

    const rim = new THREE.DirectionalLight(0xffd7c2, 0.5);
    rim.position.set(-0.6, 1.8, -2.2);
    this.scene.add(rim);

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8d9bb5, 0.85));

    this.setShadows(options.shadows ?? false);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();

    const canvas = this.renderer.domElement;
    canvas.addEventListener('webglcontextlost', this.onContextLost);
    canvas.addEventListener('webglcontextrestored', this.onContextRestored);
  }

  /* ------------------------------------------------------------------ */
  /*  Model                                                              */
  /* ------------------------------------------------------------------ */

  /** Download and prepare a VRM. Replaces whatever is currently on stage. */
  async load(url: string): Promise<VRM> {
    const vrm = await this.download(url);
    this.setModel(vrm);
    return vrm;
  }

  /** Swap in an already-loaded VRM (also used by tests). */
  setModel(vrm: VRM | null): void {
    if (this.vrm) {
      this.scene.remove(this.vrm.scene);
      VRMUtils.deepDispose(this.vrm.scene);
    }
    this.vrm = vrm;

    if (vrm) {
      vrm.scene.traverse((object) => {
        // Skinned meshes move far outside their bind box; culling them makes
        // limbs vanish when the camera is close.
        object.frustumCulled = false;
        if (this.options.shadows) {
          object.castShadow = true;
          object.receiveShadow = false;
        }
      });
      this.scene.add(vrm.scene);
      vrm.update(0);
      this.applyFraming(this.cameraPreset, /* immediate */ true);
    }

    for (const listener of this.listeners) listener(vrm);
  }

  /** Register a callback for model swaps. Returns an unsubscribe function. */
  onModelLoaded(listener: (vrm: VRM | null) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private download(url: string): Promise<VRM> {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));

    return new Promise<VRM>((resolve, reject) => {
      loader.load(
        url,
        (gltf) => {
          const vrm = gltf.userData.vrm as VRM | undefined;
          if (!vrm) {
            reject(new Error('That file is a glTF/GLB but not a VRM avatar (no VRM extension found).'));
            return;
          }

          // Optimisations from three-vrm's own examples. Each is optional: a
          // failure here must never stop the avatar from appearing.
          try {
            VRMUtils.removeUnnecessaryVertices(gltf.scene);
            VRMUtils.combineMorphs(vrm);
            VRMUtils.combineSkeletons(gltf.scene);
          } catch (error) {
            console.warn('[vrm] optimisation skipped:', error);
          }

          // VRM 0.x models face -Z; this rotates them to the VRM 1.0 convention.
          VRMUtils.rotateVRM0(vrm);

          vrm.scene.name = 'VRMRoot';
          resolve(vrm);
        },
        (event: ProgressEvent<EventTarget>) => {
          const total = event.lengthComputable && event.total > 0 ? event.total : 0;
          const label = `${formatBytes(event.loaded)}${total ? ` / ${formatBytes(total)}` : ''}`;
          this.options.onProgress?.(total ? event.loaded / total : null, label);
        },
        (error) => reject(new Error(`Could not load the avatar: ${String(error)}`)),
      );
    });
  }

  /* ------------------------------------------------------------------ */
  /*  Loop                                                               */
  /* ------------------------------------------------------------------ */

  addController(controller: FrameController): () => void {
    this.controllers.push(controller);
    return () => {
      const index = this.controllers.indexOf(controller);
      if (index >= 0) this.controllers.splice(index, 1);
    };
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.clock.start();
    this.renderer.setAnimationLoop(this.tick);
  }

  stop(): void {
    this.running = false;
    this.renderer.setAnimationLoop(null);
  }

  get stats(): ViewerStats {
    return this.lastStats;
  }

  private tick = (): void => {
    const delta = Math.min(this.clock.getDelta(), MAX_DELTA);
    this.elapsed += delta;

    for (const controller of this.controllers) controller.update(delta, this.elapsed);
    this.vrm?.update(delta);

    this.updateCamera(delta);
    this.renderer.render(this.scene, this.camera);

    this.frames += 1;
    const now = this.elapsed;
    if (now - this.statsAt >= 0.5) {
      const fps = this.frames / (now - this.statsAt);
      this.statsAt = now;
      this.frames = 0;
      const info = this.renderer.info.render;
      this.lastStats = { fps, drawCalls: info.calls, triangles: info.triangles };
      this.options.onStats?.(this.lastStats);
    }
  };

  /* ------------------------------------------------------------------ */
  /*  Camera                                                             */
  /* ------------------------------------------------------------------ */

  setCameraPreset(preset: CameraPreset, immediate = false): void {
    this.cameraPreset = preset;
    this.applyFraming(preset, immediate);
  }

  private applyFraming(preset: CameraPreset, immediate: boolean): void {
    const vrm = this.vrm;
    if (!vrm) return;

    const box = new THREE.Box3().setFromObject(vrm.scene);
    const size = new THREE.Vector3();
    const center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);
    const height = size.y > 0.01 ? size.y : 1.6;

    const framing = CAMERA_FRAMING[preset];
    let targetY = box.min.y + height * framing.height;

    if (preset === 'face') {
      // Frame the actual head bone: chibi models have very different proportions.
      const head = vrm.humanoid?.getNormalizedBoneNode('head');
      if (head) targetY = head.getWorldPosition(new THREE.Vector3()).y - height * 0.02;
    }

    this.cameraDesired.target.set(center.x, targetY, center.z);
    this.cameraDesired.position.set(
      center.x,
      targetY + (preset === 'full' ? height * 0.06 : 0),
      center.z + height * framing.distance,
    );
    this.cameraDesired.fov = framing.fov;

    if (immediate || !this.cameraReady) {
      this.camera.position.copy(this.cameraDesired.position);
      this.cameraTarget.copy(this.cameraDesired.target);
      this.camera.fov = framing.fov;
      this.camera.updateProjectionMatrix();
      this.camera.lookAt(this.cameraTarget);
      this.cameraReady = true;
    }
  }

  private updateCamera(delta: number): void {
    if (!this.cameraReady) return;
    const k = 1 - Math.exp(-delta / 0.18);
    this.camera.position.lerp(this.cameraDesired.position, k);
    this.cameraTarget.lerp(this.cameraDesired.target, k);
    if (Math.abs(this.camera.fov - this.cameraDesired.fov) > 0.01) {
      this.camera.fov += (this.cameraDesired.fov - this.camera.fov) * k;
      this.camera.updateProjectionMatrix();
    }
    this.camera.lookAt(this.cameraTarget);
  }

  /** Pointer in normalised device coordinates (`-1..1`). */
  setPointer(clientX: number, clientY: number): void {
    const rect = this.container.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    this.pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    this.pointer.y = ((clientY - rect.top) / rect.height) * 2 - 1;
    this.pointerAt = this.elapsed;
  }

  /* ------------------------------------------------------------------ */
  /*  Renderer options                                                   */
  /* ------------------------------------------------------------------ */

  setPixelRatio(cap: number): void {
    this.options.pixelRatio = cap;
    this.renderer.setPixelRatio(this.effectivePixelRatio());
    this.resize();
  }

  setShadows(enabled: boolean): void {
    this.options.shadows = enabled;
    this.renderer.shadowMap.enabled = enabled;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.keyLight.castShadow = enabled;
    if (enabled) {
      this.keyLight.shadow.mapSize.set(1024, 1024);
      this.keyLight.shadow.camera.near = 0.1;
      this.keyLight.shadow.camera.far = 12;
      this.keyLight.shadow.bias = -0.0006;
    }
    this.vrm?.scene.traverse((object) => {
      object.castShadow = enabled;
    });
  }

  private effectivePixelRatio(): number {
    const cap = this.options.pixelRatio ?? 2;
    return Math.min(window.devicePixelRatio || 1, Math.max(0.5, cap));
  }

  resize(): void {
    const width = this.container.clientWidth || 1;
    const height = this.container.clientHeight || 1;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  /* ------------------------------------------------------------------ */
  /*  Teardown                                                           */
  /* ------------------------------------------------------------------ */

  dispose(): void {
    this.stop();
    this.resizeObserver.disconnect();
    const canvas = this.renderer.domElement;
    canvas.removeEventListener('webglcontextlost', this.onContextLost);
    canvas.removeEventListener('webglcontextrestored', this.onContextRestored);

    for (const controller of this.controllers) controller.dispose?.();
    this.controllers.length = 0;
    this.listeners.clear();

    if (this.vrm) {
      this.scene.remove(this.vrm.scene);
      VRMUtils.deepDispose(this.vrm.scene);
      this.vrm = null;
    }

    this.renderer.dispose();
    canvas.remove();
  }

  private onContextLost = (event: Event): void => {
    event.preventDefault();
    console.warn('[vrm] WebGL context lost');
    this.stop();
  };

  private onContextRestored = (): void => {
    console.info('[vrm] WebGL context restored');
    this.start();
  };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
