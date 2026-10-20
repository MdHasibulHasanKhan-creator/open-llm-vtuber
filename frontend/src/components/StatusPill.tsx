/** Connection + conversation state, RTT and the current emotion, in one line. */

import { deriveStatus, useAppStore } from '../state/store';
import type { SessionState } from '../types/protocol';

const LABELS: Record<SessionState | 'offline', string> = {
  offline: 'Offline',
  idle: 'Ready',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  error: 'Error',
};

export function StatusPill() {
  const connection = useAppStore((state) => state.connection);
  const status = useAppStore(deriveStatus);
  const detail = useAppStore((state) => state.stateDetail);
  const partial = useAppStore((state) => state.partialSpeech);
  const emotion = useAppStore((state) => state.emotion);
  const rtt = useAppStore((state) => state.rttMs);

  if (connection !== 'open') {
    const label =
      connection === 'connecting' ? 'Connecting' : connection === 'reconnecting' ? 'Reconnecting' : 'Offline';
    return (
      <span className="pill pill--offline" title="WebSocket to the Python backend">
        <span className="dot" aria-hidden />
        {label}
      </span>
    );
  }

  const title = [detail || LABELS[status], partial && `heard: "${partial}"`].filter(Boolean).join(' · ');

  return (
    <div className="pill-row">
      <span className={`pill pill--${status}`} title={title}>
        <span className="dot" aria-hidden />
        {LABELS[status]}
      </span>
      {emotion && status === 'speaking' ? <span className="pill pill--ghost">{emotion}</span> : null}
      {rtt !== null && rtt > 0 ? <span className="pill pill--ghost">{Math.round(rtt)} ms</span> : null}
    </div>
  );
}
