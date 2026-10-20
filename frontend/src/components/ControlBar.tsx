/**
 * Microphone controls, barge-in and the drawer toggle.
 *
 * The level meter reads `controller.inputLevel` inside a `requestAnimationFrame`
 * loop and writes straight to the DOM: animating it through React state would
 * re-render the whole bar ten times a second for no reason.
 */

import { useEffect, useRef } from 'react';

import { getConversation } from '../core/conversation';
import { useAppStore } from '../state/store';
import type { MicMode } from '../audio/MicrophoneCapture';

const MODES: Array<{ id: MicMode; label: string; hint: string }> = [
  { id: 'off', label: 'Off', hint: 'Microphone disabled' },
  { id: 'vad', label: 'Auto', hint: 'Listen continuously and detect speech' },
  { id: 'ptt', label: 'Hold', hint: 'Push to talk: hold the button while you speak' },
];

function LevelMeter() {
  const barRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    let frame = 0;
    let smoothed = 0;
    const step = (): void => {
      const conversation = getConversation();
      smoothed = smoothed * 0.8 + conversation.inputLevel * 0.2;
      if (barRef.current) barRef.current.style.transform = `scaleX(${Math.min(1, smoothed).toFixed(3)})`;
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, []);

  return (
    <span className="meter" aria-hidden>
      <span className="meter__bar" ref={barRef} />
    </span>
  );
}

export function ControlBar({ onToggleSettings }: { onToggleSettings: () => void }) {
  const prefs = useAppStore((state) => state.prefs);
  const setPrefs = useAppStore((state) => state.setPrefs);
  const speaking = useAppStore((state) => state.speaking);
  const conversation = getConversation();

  const setMode = (mode: MicMode): void => {
    setPrefs({ micMode: mode });
    conversation.syncInput();
  };

  const pushToTalk = prefs.micMode === 'ptt';

  return (
    <div className="controls">
      <div className="controls__group segmented" role="group" aria-label="Microphone mode">
        {MODES.map((mode) => (
          <button
            key={mode.id}
            type="button"
            title={mode.hint}
            className={`btn btn--segment ${prefs.micMode === mode.id ? 'is-active' : ''}`}
            onClick={() => setMode(mode.id)}
          >
            {mode.label}
          </button>
        ))}
      </div>

      <LevelMeter />

      {pushToTalk ? (
        <button
          type="button"
          className="btn btn--talk"
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            conversation.beginPushToTalk();
          }}
          onPointerUp={() => conversation.endPushToTalk()}
          onPointerCancel={() => conversation.endPushToTalk()}
          onPointerLeave={() => conversation.endPushToTalk()}
        >
          Hold to talk
        </button>
      ) : null}

      <button
        type="button"
        className="btn"
        onClick={() => conversation.interrupt()}
        disabled={!speaking}
        title="Stop the avatar mid-sentence"
      >
        Stop
      </button>

      <button
        type="button"
        className="btn"
        onClick={() => conversation.reset()}
        title="Forget the conversation and start over"
      >
        Reset
      </button>

      <button type="button" className="btn btn--icon" onClick={onToggleSettings} title="Settings">
        ⚙<span className="sr-only">Settings</span>
      </button>
    </div>
  );
}
