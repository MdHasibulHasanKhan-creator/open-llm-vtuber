/**
 * The single WebSocket connection to the Python backend.
 *
 * Responsibilities: connect (with reconnect + backoff), split text/binary
 * frames, keep the RTT measured, and expose a small typed API so the rest of
 * the app never builds a protocol object by hand.
 *
 * Text frames are JSON control messages; binary frames are raw PCM belonging to
 * the most recent `tts.start` (see `types/protocol.ts`).
 */

import { resolveWebSocketUrl } from '../config';
import type { ConfigPatch } from '../types/config';
import {
  isKnownMessage,
  parseServerMessage,
  type ClientMessage,
  type HelloMessage,
  type ServerMessage,
} from '../types/protocol';

export const CLIENT_NAME = 'web';
export const CLIENT_VERSION = '1.0.0';

export type ConnectionStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed'
  | 'error';

export interface WebSocketClientOptions {
  url?: string;
  onMessage: (message: ServerMessage) => void;
  /** One binary frame = one slice of the current `tts.start`. */
  onBinary: (data: Uint8Array) => void;
  onStatus: (status: ConnectionStatus, detail?: string) => void;
  onLatency?: (rttMs: number, serverTimeOffsetMs: number) => void;
  /** How often to ping (ms). 0 disables the heartbeat. */
  heartbeatMs?: number;
  maxBackoffMs?: number;
  /** Auto reconnect on drop. */
  autoReconnect?: boolean;
}

const DEFAULTS = {
  heartbeatMs: 15_000,
  maxBackoffMs: 15_000,
  autoReconnect: true,
};

/**
 * Close codes the server uses deliberately. They must not be treated like a
 * dropped connection:
 *
 *  * `1008` (policy violation) - the origin or path was rejected. Reconnecting
 *    with the same URL can only fail the same way, so we stop and say why.
 *  * `1013` (try again later) - the server is at capacity. Keeping the retry
 *    loop but stretching the backoff avoids hammering a full server.
 */
const CLOSE_POLICY = 1008;
const CLOSE_CAPACITY = 1013;

/** `WebSocketClientOptions` with the optional fields filled in. */
type ResolvedOptions = Omit<
  WebSocketClientOptions,
  'heartbeatMs' | 'maxBackoffMs' | 'autoReconnect'
> &
  Required<Pick<WebSocketClientOptions, 'heartbeatMs' | 'maxBackoffMs' | 'autoReconnect'>>;

export class WebSocketClient {
  readonly url: string;

  private readonly options: ResolvedOptions;
  private socket: WebSocket | null = null;
  private status: ConnectionStatus = 'idle';

  /** Messages typed while the socket was down (never audio). */
  private pending: ClientMessage[] = [];
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastPongAt = 0;
  private closedByUser = false;

  constructor(options: WebSocketClientOptions) {
    this.options = { ...DEFAULTS, ...options };
    this.url = options.url ?? resolveWebSocketUrl();
  }

  /* ------------------------------------------------------------------ */
  /*  Lifecycle                                                          */
  /* ------------------------------------------------------------------ */

  connect(): void {
    this.closedByUser = false;
    this.clearReconnect();

    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }

    this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');

    let socket: WebSocket;
    try {
      socket = new WebSocket(this.url);
    } catch (error) {
      this.setStatus('error', describe(error));
      this.scheduleReconnect();
      return;
    }

    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.lastPongAt = Date.now();
      this.setStatus('open');
      this.startHeartbeat();
      for (const message of this.pending.splice(0)) this.write(message);
    };

    socket.onmessage = (event: MessageEvent<string | ArrayBuffer>) => {
      if (typeof event.data === 'string') {
        const message = parseServerMessage(event.data);
        // Unknown types are dropped on purpose: the backend may grow.
        if (message && isKnownMessage(message)) this.options.onMessage(message);
        return;
      }
      this.options.onBinary(new Uint8Array(event.data));
    };

    socket.onerror = () => {
      // `onclose` always follows; keep the detail short here.
      this.setStatus('error', 'socket error');
    };

    socket.onclose = (event: CloseEvent) => {
      this.stopHeartbeat();
      this.socket = null;
      if (this.closedByUser) {
        this.setStatus('closed');
        return;
      }

      if (event.code === CLOSE_POLICY) {
        // Not a transient failure: retrying the same origin/path loops forever.
        this.setStatus(
          'error',
          event.reason ||
            'the server rejected this connection (origin or path). Check server.cors_origins.',
        );
        return;
      }

      if (event.code === CLOSE_CAPACITY) {
        this.setStatus('reconnecting', 'server at capacity - waiting');
        this.scheduleReconnect(4);
        return;
      }

      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        this.setStatus('closed', 'offline - waiting for the network');
        this.waitForOnline();
        return;
      }

      this.setStatus('closed', `connection lost (${event.code})`);
      this.scheduleReconnect();
    };

    if (typeof window !== 'undefined') {
      // Coming back online is the fastest possible reconnect: skip the backoff.
      window.addEventListener('online', this.onOnline);
      window.addEventListener('offline', this.onOffline);
    }
  }

  private readonly onOnline = (): void => {
    if (this.closedByUser || this.status === 'open') return;
    this.attempt = 0;
    this.clearReconnect();
    this.connect();
  };

  private readonly onOffline = (): void => {
    if (this.status !== 'open') return;
    this.setStatus('reconnecting', 'offline');
  };

  /** Wait for the `online` event instead of retrying into a dead network. */
  private waitForOnline(): void {
    this.clearReconnect();
    if (typeof navigator !== 'undefined' && navigator.onLine !== false) {
      this.scheduleReconnect();
    }
  }

  close(): void {
    this.closedByUser = true;
    this.clearReconnect();
    this.stopHeartbeat();
    this.pending = [];
    if (typeof window !== 'undefined') {
      window.removeEventListener('online', this.onOnline);
      window.removeEventListener('offline', this.onOffline);
    }
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      try {
        socket.close(1000, 'client shutdown');
      } catch {
        /* already gone */
      }
    }
    this.setStatus('closed');
  }

  get currentStatus(): ConnectionStatus {
    return this.status;
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  /* ------------------------------------------------------------------ */
  /*  Sending                                                            */
  /* ------------------------------------------------------------------ */

  send(message: ClientMessage): void {
    if (this.isOpen) {
      this.write(message);
      return;
    }
    // Keep the last few control messages: the user may have typed before the
    // socket finished connecting. Audio is never queued.
    if (message.type === 'audio.start' || message.type === 'audio.end' || message.type === 'interrupt') return;
    if (this.pending.length >= 12) this.pending.shift();
    this.pending.push(message);
  }

  /** Raw PCM. Only meaningful right after a `tts.start` was received. */
  sendBinary(data: Uint8Array): void {
    if (!this.isOpen || data.byteLength === 0) return;
    try {
      this.socket?.send(data);
    } catch (error) {
      this.setStatus('error', describe(error));
    }
  }

  hello(options: { greet?: boolean; clientSideAsr?: boolean; sampleRate?: number } = {}): void {
    const message: HelloMessage = {
      type: 'hello',
      client: CLIENT_NAME,
      version: CLIENT_VERSION,
      sample_rate: options.sampleRate ?? 16000,
      client_side_asr: options.clientSideAsr ?? false,
      greet: options.greet ?? true,
    };
    this.send(message);
  }

  /**
   * A user utterance. `source: 'asr'` marks text that came out of speech
   * recognition, so the backend filters it (echo/noise) before spending a turn
   * on it; typed text is taken at face value.
   */
  chat(text: string, source: 'typed' | 'asr' = 'typed'): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    this.send({ type: 'chat', text: trimmed, source });
  }

  interrupt(heard = ''): void {
    this.send({ type: 'interrupt', heard });
  }

  patchConfig(payload: ConfigPatch): void {
    this.send({ type: 'config', payload });
  }

  reset(): void {
    this.send({ type: 'reset' });
  }

  /** Re-read the persona file after the settings UI saved an edit. */
  reloadCharacter(): void {
    this.send({ type: 'character.reload' });
  }

  ping(): void {
    this.send({ type: 'ping', t: Date.now() / 1000 });
  }

  /* ------------------------------------------------------------------ */
  /*  Internals                                                          */
  /* ------------------------------------------------------------------ */

  private write(message: ClientMessage): void {
    try {
      this.socket?.send(JSON.stringify(message));
    } catch (error) {
      this.setStatus('error', describe(error));
    }
  }

  private setStatus(status: ConnectionStatus, detail?: string): void {
    this.status = status;
    this.options.onStatus(status, detail);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    if (!this.options.heartbeatMs) return;
    this.heartbeatTimer = setInterval(() => {
      if (!this.isOpen) return;
      // No answer for two intervals -> the socket is a zombie: force a retry.
      if (Date.now() - this.lastPongAt > this.options.heartbeatMs * 2.5) {
        this.setStatus('error', 'heartbeat timeout');
        try {
          this.socket?.close(4000, 'heartbeat timeout');
        } catch {
          /* ignore */
        }
        return;
      }
      this.ping();
    }, this.options.heartbeatMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /** Called by the controller when a `pong` arrives. */
  notePong(sentAt: number, serverTime: number): void {
    this.lastPongAt = Date.now();
    if (!this.options.onLatency) return;
    const nowSeconds = Date.now() / 1000;
    const rttMs = Math.max(0, (nowSeconds - sentAt) * 1000);
    const offsetMs = (serverTime - (sentAt + rttMs / 2000)) * 1000;
    this.options.onLatency(rttMs, offsetMs);
  }

  private scheduleReconnect(extraDelaySeconds = 0): void {
    if (!this.options.autoReconnect || this.closedByUser) return;
    this.clearReconnect();
    const backoff = Math.min(this.options.maxBackoffMs, 500 * 2 ** this.attempt);
    const delay = backoff * (0.7 + Math.random() * 0.6) + extraDelaySeconds * 1000;
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.closedByUser) this.connect();
    }, delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
