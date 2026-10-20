/**
 * Application shell: stage on the left, transcript and controls on the right,
 * settings in a drawer.
 *
 * The "start" veil exists because browsers refuse to play audio before a user
 * gesture. Rather than starting silently and surprising the user with half a
 * greeting later, we connect and load the avatar immediately (that costs nothing)
 * and only send `hello` - which triggers the spoken greeting - once audio is
 * unlocked *and* the model is on screen.
 */

import { useEffect, useState } from 'react';

import { AvatarStage } from './components/AvatarStage';
import { ChatPanel } from './components/ChatPanel';
import { ControlBar } from './components/ControlBar';
import { SettingsDrawer } from './components/SettingsDrawer';
import { StatusPill } from './components/StatusPill';
import { getConversation } from './core/conversation';
import { isAutostart, isStreamerMode } from './config';
import { useAppStore } from './state/store';

export default function App() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);

  const started = useAppStore((state) => state.started);
  const phase = useAppStore((state) => state.avatarPhase);
  const progress = useAppStore((state) => state.avatarProgress);
  const avatarLabel = useAppStore((state) => state.avatarLabel);
  const avatarError = useAppStore((state) => state.avatarError);
  const error = useAppStore((state) => state.error);
  const notice = useAppStore((state) => state.notice);
  const character = useAppStore((state) => state.character);
  const prefs = useAppStore((state) => state.prefs);

  // URL decides by default; the Settings toggle can override it for this browser.
  const streamer = prefs.streamer ?? isStreamerMode();

  useEffect(() => {
    const conversation = getConversation();
    conversation.start();
    return () => conversation.dispose();
  }, []);

  // `?autostart=1` (implied by streamer mode): skip the click. Browsers may still
  // refuse to start audio - then the normal overlay simply stays put.
  useEffect(() => {
    if (!isAutostart()) return;
    const timer = window.setTimeout(() => {
      if (!useAppStore.getState().started) void getConversation().beginSession();
    }, 400);
    return () => window.clearTimeout(timer);
  }, []);

  const start = (): void => {
    void getConversation().beginSession();
  };

  /** Open the drawer straight on the Model tab and dismiss the advisory. */
  const fixModel = (): void => {
    useAppStore.getState().requestSettingsTab('model');
    setSettingsOpen(true);
    useAppStore.getState().setNotice(null);
  };

  if (streamer) {
    return (
      <div className="app app--streamer">
        <div className="streamer__stage">
          <AvatarStage />
        </div>
        {/* Only shown if the capture tool refused to autoplay - a click fixes it. */}
        {!started ? (
          <button type="button" className="streamer__start" onClick={start}>
            Start talking
          </button>
        ) : null}
        {error ? <p className="streamer__error">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className={`app${settingsOpen ? ' app--drawer' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand__mark" aria-hidden>
            🗣️
          </span>
          <span className="brand__text">
            <strong>Open VRM VTuber</strong>
            {character ? <em>{character.name}</em> : null}
          </span>
        </div>
        <StatusPill />
      </header>

      <main className="main">
        {notice ? (
          <div className={`notice-bar notice-bar--${notice.tone}`} role="status">
            <span>{notice.text}</span>
            <span className="notice-bar__actions">
              <button type="button" className="btn btn--small" onClick={fixModel}>
                Connect a model
              </button>
              <button
                type="button"
                className="btn btn--icon"
                onClick={() => useAppStore.getState().setNotice(null)}
              >
                ✕<span className="sr-only">Dismiss</span>
              </button>
            </span>
          </div>
        ) : null}
        <div className="stage-wrap">
          <AvatarStage />
          <button
            type="button"
            className="chat-toggle"
            onClick={() => setChatOpen((open) => !open)}
            aria-expanded={chatOpen}
          >
            {chatOpen ? 'Hide chat' : 'Chat'}
          </button>
        </div>

        <div className={`side${chatOpen ? ' is-open' : ''}`}>
          <ChatPanel />
          <ControlBar onToggleSettings={() => setSettingsOpen((open) => !open)} />
        </div>
      </main>

      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      {!started ? (
        <div className="veil">
          <div className="veil__card">
            <h1>Open VRM VTuber</h1>
            <p>
              A 3D avatar that listens, thinks and answers out loud - lip-sync driven by the audio itself. Everything runs
              on your own machine; the language model can be local (Ollama) or a hosted API.
            </p>

            {phase === 'loading' ? (
              <>
                <p className="muted">Preparing the avatar{avatarLabel ? ` · ${avatarLabel}` : ''}…</p>
                {progress !== null ? (
                  <div className="progress">
                    <span style={{ transform: `scaleX(${progress.toFixed(3)})` }} />
                  </div>
                ) : null}
              </>
            ) : null}

            {avatarError ? <p className="error-text">{avatarError}</p> : null}

            <button type="button" className="btn btn--primary btn--large" onClick={start}>
              Start talking
            </button>
            <p className="muted small">
              Browsers only allow sound after a click. Connecting and loading the avatar are already done.
            </p>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="toast" role="status">
          <span>{error}</span>
          <button type="button" className="btn btn--icon" onClick={() => useAppStore.getState().setError(null)}>
            ✕<span className="sr-only">Dismiss</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
