/**
 * Runtime configuration that ships *with the bundle*.
 *
 * Anything the user can change at runtime lives in the settings store / the
 * backend config instead - this file is only for build-time defaults, so it
 * stays trivially readable.
 */

import type { CameraPreset } from './types/config';

/**
 * Used when neither `VITE_VRM_URL` nor the backend config point at a model.
 * It is three-vrm's own sample avatar, served from jsDelivr, so a fresh clone
 * has something on screen before the user drops in their own `.vrm`.
 */
export const PLACEHOLDER_VRM_URL =
  'https://cdn.jsdelivr.net/gh/pixiv/three-vrm@dev/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm';

const CAMERA_PRESETS: readonly CameraPreset[] = ['face', 'bust', 'full'];

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function readNumber(raw: string | undefined, fallback: number, min: number, max: number): number {
  const parsed = Number.parseFloat(raw ?? '');
  return Number.isFinite(parsed) ? clamp(parsed, min, max) : fallback;
}

function readCameraPreset(raw: string | undefined): CameraPreset {
  const value = (raw ?? '').trim().toLowerCase() as CameraPreset;
  return CAMERA_PRESETS.includes(value) ? value : 'bust';
}

export const runtimeConfig = {
  /** Empty -> derive from the page origin (works in dev *and* prod). */
  wsUrl: (import.meta.env.VITE_WS_URL ?? '').trim(),
  /** Empty -> backend config, then the placeholder. */
  vrmUrl: (import.meta.env.VITE_VRM_URL ?? '').trim(),
  cameraPreset: readCameraPreset(import.meta.env.VITE_CAMERA_PRESET),
  pixelRatio: readNumber(import.meta.env.VITE_PIXEL_RATIO, 2, 0.5, 3),
} as const;

/**
 * The socket URL. `VITE_WS_URL` wins; otherwise we use the page's own origin,
 * which is proxied to the Python backend by Vite in development and served by
 * FastAPI in production - one contract, no CORS.
 */
export function resolveWebSocketUrl(): string {
  if (runtimeConfig.wsUrl) return runtimeConfig.wsUrl;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

/** Absolute HTTP URL for a `/api/...` path. */
export function apiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `/api${suffix}`;
}

/**
 * Was this page opened as a *capture surface* rather than as the app?
 *
 * OBS (and anything else that embeds a browser) is pointed at
 * `http://localhost:8000/?streamer=1`, which hides every control and leaves the
 * avatar alone on a transparent background. `?mode=streamer`, `?obs` and
 * `?clean` are accepted too, because that is what people type.
 */
export function isStreamerMode(): boolean {
  const params = new URLSearchParams(window.location.search);
  const flag = (params.get('streamer') ?? params.get('mode') ?? '').trim().toLowerCase();
  return flag === '1' || flag === 'true' || flag === 'streamer' || params.has('obs') || params.has('clean');
}

/**
 * Start without waiting for a click (`?autostart=1`).
 *
 * Capture tools usually permit autoplay, so streamer mode implies it. If the
 * browser still refuses, nothing breaks: the normal "Start talking" overlay
 * stays and the first click retries.
 */
export function isAutostart(): boolean {
  const params = new URLSearchParams(window.location.search);
  const flag = (params.get('autostart') ?? params.get('auto') ?? '').trim().toLowerCase();
  return flag === '1' || flag === 'true' || isStreamerMode();
}
