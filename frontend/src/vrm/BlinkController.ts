/**
 * Blinking.
 *
 * A blink is far too fast to leave to a linear ramp: the lid closes quickly and
 * opens slightly slower, and the whole thing lasts ~140 ms. Intervals are
 * randomised (2.2-6 s) with the occasional double blink, which is what makes a
 * face feel alive rather than metronomic.
 *
 * The conversation controller may suppress blinking while an expression owns
 * the face (`override_blink: true`, e.g. `surprised`).
 */

import type { VRM } from '@pixiv/three-vrm';

import { smoothTowards, smoothstep } from './smooth';
import type { FrameController } from './types';

/** Total blink length; the close takes `CLOSE_FRACTION` of it. */
const BLINK_SECONDS = 0.14;
const CLOSE_FRACTION = 0.42;
const MIN_INTERVAL = 2.2;
const MAX_INTERVAL = 6.0;

export class BlinkController implements FrameController {
  private vrm: VRM | null = null;
  private elapsed = 0;
  private nextAt = 0;
  private timer = 0;
  private blinking = false;
  private queued = 0;
  private value = 0;

  /** True while an expression owns the face (e.g. `surprised`). */
  private isSuppressed: () => boolean = () => false;

  /** Wired to the expression controller by the conversation controller. */
  setSuppressor(predicate: () => boolean): void {
    this.isSuppressed = predicate;
  }

  attach(vrm: VRM | null): void {
    this.vrm = vrm;
    this.value = 0;
    this.blinking = false;
    this.schedule();
  }

  get weight(): number {
    return this.value;
  }

  /** Force a blink soon (used when the avatar starts listening again). */
  requestBlink(): void {
    if (!this.blinking) this.nextAt = this.elapsed + 0.05;
  }

  update(delta: number): void {
    const manager = this.vrm?.expressionManager;
    if (!manager) return;

    this.elapsed += delta;

    if (this.isSuppressed()) {
      this.value = smoothTowards(this.value, 0, delta, 0.05);
      manager.setValue('blink', this.value);
      return;
    }

    if (!this.blinking) {
      if (this.elapsed >= this.nextAt) this.begin();
    } else {
      this.timer += delta;
      const progress = this.timer / BLINK_SECONDS;
      if (progress >= 1) {
        this.blinking = false;
        this.value = 0;
        this.schedule();
        if (this.queued > 0) {
          this.queued -= 1;
          this.nextAt = this.elapsed + 0.07;
        }
      } else if (progress < CLOSE_FRACTION) {
        this.value = smoothstep(0, CLOSE_FRACTION, progress);
      } else {
        this.value = smoothstep(1, CLOSE_FRACTION, progress);
      }
    }

    manager.setValue('blink', this.value);
  }

  private begin(): void {
    this.blinking = true;
    this.timer = 0;
    if (Math.random() < 0.18) this.queued = 1;
  }

  private schedule(): void {
    this.nextAt = this.elapsed + MIN_INTERVAL + Math.random() * (MAX_INTERVAL - MIN_INTERVAL);
  }
}
