/// <reference types="vite/client" />

/**
 * Only `VITE_*` variables are inlined into the bundle. See `.env.example`.
 */
interface ImportMetaEnv {
  /** WebSocket endpoint; empty derives it from the page origin. */
  readonly VITE_WS_URL?: string;
  /** Avatar to load on startup; empty uses the built-in placeholder. */
  readonly VITE_VRM_URL?: string;
  /** `face` | `bust` | `full` */
  readonly VITE_CAMERA_PRESET?: string;
  /** Device pixel ratio cap, e.g. `2`. */
  readonly VITE_PIXEL_RATIO?: string;
  /** Backend the Vite dev server proxies to (used by `vite.config.ts`). */
  readonly VITE_BACKEND?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
