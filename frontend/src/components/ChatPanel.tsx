/**
 * The transcript + the input box.
 *
 * While the model is writing, completed sentences appear as they are finished
 * (which is the same moment their audio starts, so the bubble grows in step with
 * the voice) and the trailing, still-unfinished sentence streams in underneath.
 */

import { useEffect, useRef, useState } from 'react';

import { getConversation } from '../core/conversation';
import { useAppStore } from '../state/store';
import { splitMarkers, stripMarkers } from '../util/text';
import type { ChatEntry } from '../state/store';

function Bubble({ entry }: { entry: ChatEntry }) {
  const isUser = entry.role === 'user';
  return (
    <div className={`bubble ${isUser ? 'bubble--user' : 'bubble--assistant'}`}>
      {isUser ? (
        <p>{entry.text}</p>
      ) : (
        <p>
          {splitMarkers(entry.text).map((segment, index) =>
            segment.kind === 'chip' ? (
              <span className="chip" key={index} title={segment.label} role="img" aria-label={segment.label}>
                {segment.value}
              </span>
            ) : (
              <span key={index}>{segment.value}</span>
            ),
          )}
        </p>
      )}
      <time>{new Date(entry.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
    </div>
  );
}

export function ChatPanel() {
  const messages = useAppStore((state) => state.messages);
  const sentences = useAppStore((state) => state.turnSentences);
  const streaming = useAppStore((state) => state.streamingText);
  const turnActive = useAppStore((state) => state.turnActive);
  const partial = useAppStore((state) => state.partialSpeech);

  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages.length, sentences.length, streaming, partial]);

  const send = (): void => {
    const text = draft.trim();
    if (!text) return;
    getConversation().submitText(text);
    setDraft('');
  };

  const pending = sentences.join(' ').trim();
  const tail = stripMarkers(streaming);
  const showPending = turnActive || pending.length > 0 || tail.length > 0;

  return (
    <section className="chat" aria-label="Conversation">
      <div className="chat__list" ref={listRef}>
        {messages.length === 0 ? (
          <div className="chat__empty">
            <h2>Say hello.</h2>
            <p>
              Type below, or switch the microphone on and just talk. The avatar answers out loud, in real time, with
              lip-sync driven by the audio itself.
            </p>
          </div>
        ) : (
          messages.map((entry) => <Bubble key={entry.id} entry={entry} />)
        )}

        {showPending ? (
          <div className="bubble bubble--assistant bubble--pending">
            <p>
              {pending}
              {tail ? <span className="stream">{tail}</span> : null}
              <span className="caret" aria-hidden />
            </p>
          </div>
        ) : null}
      </div>

      {partial ? <div className="chat__partial">🎙 {partial}</div> : null}

      <form
        className="chat__input"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              send();
            }
          }}
          rows={1}
          placeholder="Say something…  (Enter to send, Shift+Enter for a new line)"
          aria-label="Message"
        />
        <button type="submit" className="btn btn--primary" disabled={!draft.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}
