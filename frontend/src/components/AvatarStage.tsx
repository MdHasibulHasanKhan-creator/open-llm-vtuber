/**
 * The 3D stage.
 *
 * The viewer is created exactly once (WebGL contexts are expensive) and the
 * model is (re)loaded whenever the resolved `.vrm` URL changes - so switching
 * avatars in the settings is a swap, not a page reload.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { PLACEHOLDER_VRM_URL, runtimeConfig } from '../config';
import { getConversation } from '../core/conversation';
import { useAppStore } from '../state/store';
import { VRMViewer, type ViewerStats } from '../vrm/VRMViewer';

export function AvatarStage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<VRMViewer | null>(null);
  const [stats, setStats] = useState<ViewerStats | null>(null);

  const prefs = useAppStore((state) => state.prefs);
  const config = useAppStore((state) => state.config);
  const phase = useAppStore((state) => state.avatarPhase);
  const progress = useAppStore((state) => state.avatarProgress);
  const label = useAppStore((state) => state.avatarLabel);
  const error = useAppStore((state) => state.avatarError);

  const showStats = prefs.showStats ?? config?.avatar.show_stats ?? false;

  const vrmUrl = useMemo(
    () => prefs.vrmUrl || config?.avatar.vrm_url || runtimeConfig.vrmUrl || PLACEHOLDER_VRM_URL,
    [prefs.vrmUrl, config?.avatar.vrm_url],
  );

  /* Create the viewer once. */
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const conversation = getConversation();
    const store = useAppStore.getState();
    const viewer = new VRMViewer(container, {
      pixelRatio: store.prefs.pixelRatio ?? store.config?.avatar.pixel_ratio ?? runtimeConfig.pixelRatio,
      cameraPreset: store.prefs.cameraPreset ?? store.config?.avatar.camera_preset ?? runtimeConfig.cameraPreset,
      shadows: store.config?.avatar.enable_shadows ?? false,
      onProgress: (fraction, text) => useAppStore.getState().setAvatarProgress(fraction, text),
      onStats: (next) => setStats(next),
    });

    viewerRef.current = viewer;
    viewer.start();
    const detach = conversation.attachViewer(viewer);

    return () => {
      detach();
      viewer.dispose();
      viewerRef.current = null;
    };
  }, []);

  /* Load (or swap) the model. */
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;

    let cancelled = false;
    useAppStore.getState().setAvatarPhase('loading');
    useAppStore.getState().setAvatarError(null);

    viewer.load(vrmUrl).catch((cause: unknown) => {
      if (cancelled) return;
      useAppStore
        .getState()
        .setAvatarError(cause instanceof Error ? cause.message : `Could not load ${vrmUrl}`);
    });

    return () => {
      cancelled = true;
    };
  }, [vrmUrl]);

  const retry = (): void => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    useAppStore.getState().setAvatarPhase('loading');
    useAppStore.getState().setAvatarError(null);
    viewer.load(vrmUrl).catch((cause: unknown) => {
      useAppStore.getState().setAvatarError(cause instanceof Error ? cause.message : 'Could not load the avatar');
    });
  };

  return (
    <div
      className="stage"
      onPointerMove={(event) => viewerRef.current?.setPointer(event.clientX, event.clientY)}
      onPointerDown={(event) => viewerRef.current?.setPointer(event.clientX, event.clientY)}
    >
      <div className="stage__canvas" ref={containerRef} />

      {phase === 'loading' ? (
        <div className="stage__status">
          <div className="spinner" aria-hidden />
          <p>Loading avatar{label ? ` · ${label}` : ''}</p>
          {progress !== null ? (
            <div className="progress">
              <span style={{ transform: `scaleX(${progress.toFixed(3)})` }} />
            </div>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div className="stage__error" role="alert">
          <strong>The avatar could not be loaded.</strong>
          <p>{error}</p>
          <div className="stage__actions">
            <button type="button" className="btn btn--primary" onClick={retry}>
              Try again
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                useAppStore.getState().setPrefs({ vrmUrl: PLACEHOLDER_VRM_URL });
              }}
            >
              Use the sample avatar
            </button>
          </div>
        </div>
      ) : null}

      {showStats && stats ? (
        <div className="stats" aria-hidden>
          {Math.round(stats.fps)} FPS · {stats.drawCalls} calls · {(stats.triangles / 1000).toFixed(1)}k tris
        </div>
      ) : null}
    </div>
  );
}
