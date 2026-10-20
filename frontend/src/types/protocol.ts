/**
 * The WebSocket wire protocol - the TypeScript twin of `backend/app/schemas.py`.
 *
 * Rules (see `docs/PROTOCOL.md`):
 *  * text frames are JSON and always carry a `type`,
 *  * binary frames are raw PCM belonging to the most recent `tts.start`,
 *  * the server orders everything, so the client never has to correlate ids for
 *    audio - only to know *which* sentence is playing.
 */

import type { Capabilities, CharacterConfig, ConfigPatch, LipsyncMode, PublicConfig } from './config';

/** `[start, end, viseme, weight]` - compact on purpose (hottest path). */
export type VisemeFrame = [number, number, string, number];

export interface ExpressionPayload {
  preset: string;
  /** The emotion that produced `preset`; `shy` and `thinking` both map to `relaxed`. */
  emotion: string;
  weight: number;
  hold_ms: number;
  override_blink: boolean;
}

export interface ActionPayload {
  name: string;
  intensity: number;
}

export type SessionState = 'idle' | 'listening' | 'thinking' | 'speaking' | 'error';

/* -------------------------------------------------------------------------- */
/*  Server -> client                                                          */
/* -------------------------------------------------------------------------- */

export interface ReadyMessage {
  type: 'ready';
  session_id: string;
  config: PublicConfig;
  character: CharacterConfig;
  capabilities: Capabilities;
}

export interface HelloAckMessage {
  type: 'hello.ack';
  session_id: string;
  server_time: number;
}

export interface StateMessage {
  type: 'state';
  value: SessionState;
  detail: string;
}

export interface AsrMessage {
  type: 'asr.final' | 'asr.partial';
  text: string;
  duration: number | null;
  /** Why an empty transcript was dropped (see `pipeline/transcript.py`). */
  reason?: string;
}

export interface UserTextMessage {
  type: 'user.text';
  text: string;
}

export interface LlmDeltaMessage {
  type: 'llm.delta';
  text: string;
}

/**
 * The server threw away the reply it was streaming and started again (the
 * quality guard rejected the first sentence). The streaming bubble must be
 * cleared so the user never sees text that is not spoken.
 */
export interface LlmRestartMessage {
  type: 'llm.restart';
}

export interface LlmDoneMessage {
  type: 'llm.done';
  text: string;
  usage: Record<string, unknown>;
}

export interface LlmSentenceMessage {
  type: 'llm.sentence';
  index: number;
  /** Text handed to TTS (emotion tags and gesture notes removed). */
  text: string;
  /** Text for the chat bubble (tags kept). */
  display_text: string;
}

export interface InterruptedMessage {
  type: 'interrupted';
}

export interface ConfigAckMessage {
  type: 'config.ack';
  config: PublicConfig & { character?: CharacterConfig };
  changed: string[];
}

export interface MetricsMessage {
  type: 'metrics';
  turn?: number;
  asr_ms?: number;
  ttft_ms?: number;
  total_ms?: number;
  sentences?: number;
  history?: number;
  [key: string]: unknown;
}

export interface HistoryMessage {
  type: 'history';
  messages: Array<{ role: string; content: string }>;
}

export interface ErrorMessage {
  type: 'error';
  message: string;
  fatal: boolean;
  code: string;
}

export interface PongMessage {
  type: 'pong';
  t: number;
  server_time: number;
}

export interface TtsStartMessage {
  type: 'tts.start';
  /** Monotonic per session; binary frames that follow belong to this id. */
  id: number;
  /** Sentence index inside the current turn. */
  index: number;
  text: string;
  /** `pcm_s16le` | `mp3` | `browser` */
  format: string;
  sample_rate: number;
  channels: number;
  duration: number;
  /** BCP-47 locale of `text` (`bn-BD`), for browser `speechSynthesis`. */
  lang?: string;
  /** Normalised RMS envelope, one value per `lipsync.slice_ms`. */
  volumes: number[];
  /**
   * Mouth-shape timeline. Empty in `mouth` lip-sync mode, where the client
   * drives one blendshape from `volumes` instead.
   */
  visemes: VisemeFrame[];
  /** `mouth` | `visemes` - how to read the two fields above. */
  lipsync_mode?: LipsyncMode;
  expression: ExpressionPayload | null;
  actions: string[];
  voice: string;
}

export interface TtsEndMessage {
  type: 'tts.end';
  id: number;
}

export interface ExpressionMessage extends ExpressionPayload {
  type: 'expression';
}

export interface ActionMessage extends ActionPayload {
  type: 'action';
}

/**
 * Anything we do not know about yet - ignored, never fatal.
 *
 * It is deliberately *not* part of `ServerMessage`: a catch-all `type: string`
 * member would stop `switch (message.type)` from narrowing, because a plain
 * `string` discriminant is never excluded by a literal `case`.
 */
export interface UnknownMessage {
  type: string;
  [key: string]: unknown;
}

/** Every message the client actually understands (narrows cleanly). */
export type ServerMessage =
  | ReadyMessage
  | HelloAckMessage
  | StateMessage
  | AsrMessage
  | UserTextMessage
  | LlmDeltaMessage
  | LlmRestartMessage
  | LlmDoneMessage
  | LlmSentenceMessage
  | InterruptedMessage
  | ConfigAckMessage
  | MetricsMessage
  | HistoryMessage
  | ErrorMessage
  | PongMessage
  | TtsStartMessage
  | TtsEndMessage
  | ExpressionMessage
  | ActionMessage;

/** What a text frame may contain: a known message or a future one. */
export type IncomingMessage = ServerMessage | UnknownMessage;

const KNOWN_MESSAGE_TYPES: ReadonlySet<string> = new Set<string>([
  'ready',
  'hello.ack',
  'state',
  'asr.partial',
  'asr.final',
  'user.text',
  'llm.delta',
  'llm.restart',
  'llm.done',
  'llm.sentence',
  'interrupted',
  'config.ack',
  'metrics',
  'history',
  'error',
  'pong',
  'tts.start',
  'tts.end',
  'expression',
  'action',
]);

export function isKnownMessage(message: IncomingMessage): message is ServerMessage {
  return KNOWN_MESSAGE_TYPES.has(message.type);
}

/** Parse a text frame. Returns `null` for anything that is not a message. */
export function parseServerMessage(raw: string): IncomingMessage | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null) return null;
    const type = (parsed as { type?: unknown }).type;
    if (typeof type !== 'string') return null;
    return parsed as IncomingMessage;
  } catch {
    return null;
  }
}

/* -------------------------------------------------------------------------- */
/*  Client -> server                                                          */
/* -------------------------------------------------------------------------- */

export interface HelloMessage {
  type: 'hello';
  client: string;
  version: string;
  sample_rate: number;
  client_side_asr: boolean;
  greet: boolean;
}

export interface ChatMessage {
  type: 'chat';
  text: string;
  /** `typed` skips the server's transcript gate; `asr` goes through it. */
  source?: 'typed' | 'asr';
}

export interface AudioStartMessage {
  type: 'audio.start';
  sample_rate: number;
}

export interface AudioEndMessage {
  type: 'audio.end';
}

export interface InterruptMessage {
  type: 'interrupt';
  heard: string;
}

export interface ConfigRequestMessage {
  type: 'config';
  payload: ConfigPatch;
}

export interface ResetMessage {
  type: 'reset';
}

/**
 * The persona file changed under the server (the settings UI saved an edit).
 * It answers with a `config.ack` carrying the new character.
 */
export interface CharacterReloadMessage {
  type: 'character.reload';
}

export interface PingMessage {
  type: 'ping';
  t: number;
}

export type ClientMessage =
  | HelloMessage
  | ChatMessage
  | AudioStartMessage
  | AudioEndMessage
  | InterruptMessage
  | ConfigRequestMessage
  | ResetMessage
  | CharacterReloadMessage
  | PingMessage;
