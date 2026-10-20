/**
 * Eye tracking.
 *
 * three-vrm's `VRMLookAt` aims the eyebones (or the VRM0 look-at blend shapes)
 * at an `Object3D` on every `vrm.update()`. We keep that target glued to the
 * pointer, and - once the pointer has been still for a while - let it drift in
 * small saccades, because a perfectly motionless gaze looks uncanny.
 *
 * The pointer comes from the viewer (screen space -> normalised device
 * coordinates), which keeps this controller free of DOM concerns.
 */

import * as THREE from 'three';
import type { VRM } from '@pixiv/three-vrm';

import type { FrameController } from './types';

/** How far in front of the camera the virtual gaze target floats (metres). */
const GAZE_DISTANCE = 1.2;
/** Pointer travel is amplified: the eyes should lead the head slightly. */
const POINTER_GAIN = 1.35;
/** Pointer idle time before the gaze starts wandering (seconds). */
const IDLE_AFTER = 2.5;
/** Damping time constant for the gaze target. */
const FOLLOW_TAU = 0.09;

export interface PointerSample {
  /** Normalised device coordinates, `-1..1`. */
  x: number;
  y: number;
  /** Viewer clock time of the last movement (seconds). */
  at: number;
}

export class LookAtController implements FrameController {
  readonly target = new THREE.Object3D();

  private vrm: VRM | null = null;
  private camera: THREE.Camera | null = null;
  private attached = false;
  private enabled = true;

  private pointerSource: (() => PointerSample) | null = null;
  private readonly desired = new THREE.Vector3();
  private readonly saccade = new THREE.Vector2();
  private nextSaccadeAt = 0;

  setPointerSource(source: () => PointerSample): void {
    this.pointerSource = source;
  }

  attach(vrm: VRM | null, camera: THREE.Camera, scene: THREE.Object3D): void {
    if (this.attached) {
      scene.remove(this.target);
      this.attached = false;
    }
    this.vrm = vrm;
    this.camera = camera;
    if (!vrm) return;

    this.target.name = 'GazeTarget';
    scene.add(this.target);
    this.attached = true;

    if (vrm.lookAt) {
      vrm.lookAt.target = this.enabled ? this.target : null;
      vrm.lookAt.autoUpdate = true;
    }
    this.cameraPoint(this.desired);
    this.target.position.copy(this.desired);
  }

  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
    if (this.vrm?.lookAt) this.vrm.lookAt.target = enabled ? this.target : null;
  }

  update(delta: number, elapsed: number): void {
    if (!this.vrm || !this.camera || !this.attached) return;

    const sample = this.pointerSource?.() ?? { x: 0, y: 0, at: -Infinity };
    const idle = elapsed - sample.at > IDLE_AFTER;

    if (idle) {
      if (elapsed >= this.nextSaccadeAt) {
        this.saccade.set((Math.random() - 0.5) * 0.5, (Math.random() - 0.5) * 0.3);
        this.nextSaccadeAt = elapsed + 0.8 + Math.random() * 2.4;
      }
    } else {
      this.saccade.multiplyScalar(0.8);
    }

    this.cameraPoint(this.desired);

    if (!this.enabled) {
      this.target.position.copy(this.desired);
      return;
    }

    const perspective = this.camera as THREE.PerspectiveCamera;
    const halfHeight = Math.tan(((perspective.fov ?? 30) * THREE.MathUtils.DEG2RAD) / 2) * GAZE_DISTANCE;
    const halfWidth = halfHeight * (perspective.aspect ?? 1);

    const offsetX = clamp(sample.x + this.saccade.x, -2, 2) * POINTER_GAIN * halfWidth;
    const offsetY = clamp(-sample.y + this.saccade.y, -2, 2) * POINTER_GAIN * halfHeight;

    const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0);
    const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1);
    const ideal = this.desired.clone().addScaledVector(right, offsetX).addScaledVector(up, offsetY);

    const k = 1 - Math.exp(-delta / FOLLOW_TAU);
    this.target.position.lerp(ideal, Math.min(1, k));
  }

  /** A point straight ahead of the camera. */
  private cameraPoint(out: THREE.Vector3): THREE.Vector3 {
    const camera = this.camera;
    if (!camera) return out.set(0, 1.4, 0);
    return out.setFromMatrixColumn(camera.matrixWorld, 2).multiplyScalar(-GAZE_DISTANCE).add(camera.position);
  }

  dispose(): void {
    if (this.vrm?.lookAt) this.vrm.lookAt.target = null;
  }
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
