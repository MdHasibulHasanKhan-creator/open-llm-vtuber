/** Shared shapes for the VRM controllers. */

import type { VRM } from '@pixiv/three-vrm';

/** Anything the viewer ticks every frame (see `VRMViewer.addController`). */
export interface FrameController {
  /** @param delta seconds since the previous frame @param elapsed total seconds */
  update(delta: number, elapsed: number): void;
  /** Called when the model is replaced or the viewer is disposed. */
  dispose?(): void;
}

/** A monotonic "seconds into this sentence" source, used to drive lip-sync. */
export type Clock = () => number;

export interface AvatarHandle {
  vrm: VRM;
  /** The VRM's root object (already added to the scene). */
  root: VRM['scene'];
}
