/**
 * Frame-rate independent exponential smoothing.
 *
 * `w += (target - w) * (1 - e^(-dt/tau))` reaches ~63% of the gap after `tau`
 * seconds no matter the frame rate, which is what keeps the mouth and the face
 * from jittering on a 144 Hz display and from lagging on a 30 FPS one.
 */
export function smoothTowards(current: number, target: number, delta: number, tau: number): number {
  if (tau <= 0) return target;
  const k = 1 - Math.exp(-delta / tau);
  return current + (target - current) * k;
}

export function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

/** Smoothstep, for animation envelopes. */
export function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = clamp01((x - edge0) / (edge1 - edge0));
  return t * t * (3 - 2 * t);
}
