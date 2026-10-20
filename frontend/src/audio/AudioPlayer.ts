/**
 * Streams TTS audio coming from the backend.
 *
 * The backend interleaves control frames and binary PCM on one socket:
 *
 *     tts.start {id:1}  ->  <pcm> <pcm> ...  tts.end {id:1}
 *     tts.start {id:2}  ->  <pcm> ...        tts.end {id:2}
 *
 * Sentences are produced as fast as the TTS engine allows - often sentence 2
 * arrives while sentence 1 is still playing - so this class keeps a *queue* of
 * streams and schedules each one right after the previous one ends. That also
 * gives per-sentence start times, which is exactly what the lip-sync controller
 * needs to align a viseme timeline with the audio.
 *
 * Two formats are supported:
 *   * `pcm_s16le` - decoded straight into an AudioBuffer (no codec, no delay);
 *   * `mp3`       - accumulated until `tts.end`, then decoded once.
 */

import { getAudioContext, getMasterGain } from './engine';
import type { TtsStartMessage } from '../types/protocol';

/** Small gap so the first chunk is never scheduled in the past. */
const LEAD_IN = 0.06;
/** Safety valve: stop queueing if a runaway model produces minutes of audio. */
const MAX_QUEUE_SECONDS = 120;

export interface AudioPlayerEvents {
  /** Audio for `id` actually started (ctx clock seconds). */
  onStreamStart?: (id: number, startAt: number) => void;
  /** Playback of `id` finished. */
  onStreamEnd?: (id: number) => void;
  /** Every queued stream finished. */
  onIdle?: () => void;
  onError?: (message: string) => void;
}

interface ActiveStream {
  id: number;
  format: string;
  sampleRate: number;
  declaredDuration: number;
  /** ctx time of the first sample of this stream. */
  startAt: number;
  /** ctx time the next chunk should be scheduled at. */
  nextTime: number;
  scheduledSeconds: number;
  sources: AudioBufferSourceNode[];
  /** Odd trailing byte waiting for its partner. */
  leftover: Uint8Array | null;
  mp3Chunks: Uint8Array[];
  mp3Bytes: number;
  ended: boolean;
  finished: boolean;
  started: boolean;
  endTimer: ReturnType<typeof setTimeout> | null;
}

export class AudioPlayer {
  private readonly ctx: AudioContext;
  private readonly gain: GainNode;
  private readonly events: AudioPlayerEvents;
  private queue: ActiveStream[] = [];
  private current: ActiveStream | null = null;
  /** ctx time the tail of the queue will be playing at. */
  private playhead = 0;
  private volume = 1;
  private muted = false;

  constructor(events: AudioPlayerEvents = {}) {
    this.ctx = getAudioContext();
    this.gain = getMasterGain();
    this.events = events;
  }

  /* ------------------------------------------------------------------ */
  /*  Volume                                                             */
  /* ------------------------------------------------------------------ */

  setVolume(volume: number): void {
    this.volume = Math.min(1, Math.max(0, volume));
    this.applyGain();
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.applyGain();
  }

  private applyGain(): void {
    // The gain node is shared with the rest of the app: scale, do not replace.
    this.gain.gain.value = this.muted ? 0 : this.volume;
  }

  /* ------------------------------------------------------------------ */
  /*  Stream lifecycle                                                   */
  /* ------------------------------------------------------------------ */

  /** Called on `tts.start`. Returns the id the following bytes belong to. */
  beginStream(header: TtsStartMessage): number {
    this.pruneFinished();

    const startAt = Math.max(this.playhead, this.ctx.currentTime + LEAD_IN);
    const stream: ActiveStream = {
      id: header.id,
      format: header.format,
      sampleRate: header.sample_rate || 24000,
      declaredDuration: header.duration,
      startAt,
      nextTime: startAt,
      scheduledSeconds: 0,
      sources: [],
      leftover: null,
      mp3Chunks: [],
      mp3Bytes: 0,
      ended: false,
      finished: false,
      started: false,
      endTimer: null,
    };

    this.playhead = startAt;
    this.queue.push(stream);
    this.current = stream;
    return stream.id;
  }

  /** Called for every binary frame: PCM for the most recent `tts.start`. */
  pushChunk(data: Uint8Array): void {
    const stream = this.current;
    if (!stream || stream.ended) return;

    if (stream.format === 'mp3') {
      stream.mp3Chunks.push(data);
      stream.mp3Bytes += data.byteLength;
      return;
    }

    let bytes = data;
    if (stream.leftover) {
      const merged = new Uint8Array(stream.leftover.byteLength + data.byteLength);
      merged.set(stream.leftover, 0);
      merged.set(data, stream.leftover.byteLength);
      stream.leftover = null;
      bytes = merged;
    }

    if (bytes.byteLength % 2 === 1) {
      stream.leftover = bytes.slice(bytes.byteLength - 1);
      bytes = bytes.subarray(0, bytes.byteLength - 1);
    }

    if (bytes.byteLength === 0) return;
    this.scheduleSamples(stream, bytes);
  }

  /** Called on `tts.end`. */
  endStream(id: number): void {
    const stream = this.queue.find((item) => item.id === id);
    if (!stream) return;
    stream.ended = true;

    if (stream.leftover) stream.leftover = null;

    if (stream.format === 'mp3') {
      void this.decodeAndScheduleMp3(stream);
      return;
    }

    if (stream.scheduledSeconds === 0) {
      // Nothing playable (silence, or a dropped stream): retire it now.
      this.retire(stream);
      return;
    }

    const endsAt = stream.startAt + stream.scheduledSeconds;
    this.scheduleEnd(stream, endsAt);
  }

  /** Barge-in: kill everything immediately. */
  stop(): void {
    for (const stream of this.queue) {
      if (stream.endTimer !== null) clearTimeout(stream.endTimer);
      stream.endTimer = null;
      for (const source of stream.sources) {
        try {
          source.stop();
        } catch {
          /* already stopped */
        }
        try {
          source.disconnect();
        } catch {
          /* already detached */
        }
      }
      stream.sources = [];
      stream.finished = true;
    }
    this.queue = [];
    this.current = null;
    this.playhead = this.ctx.currentTime;
  }

  /* ------------------------------------------------------------------ */
  /*  Clocks                                                             */
  /* ------------------------------------------------------------------ */

  /**
   * Seconds into stream `id`. Negative before it starts (the lip-sync
   * controller holds the mouth closed) and `Infinity` once the stream has been
   * retired, which reads as "played out long ago".
   */
  positionFor(id: number): number {
    const stream = this.queue.find((item) => item.id === id);
    if (!stream) return Number.POSITIVE_INFINITY;
    return this.ctx.currentTime - stream.startAt;
  }

  /** True while at least one stream still has audio to play. */
  isSpeaking(): boolean {
    return this.queue.some((stream) => !stream.finished);
  }

  get queuedSeconds(): number {
    return Math.max(0, this.playhead - this.ctx.currentTime);
  }

  /* ------------------------------------------------------------------ */
  /*  Internals                                                          */
  /* ------------------------------------------------------------------ */

  private scheduleSamples(stream: ActiveStream, bytes: Uint8Array): void {
    if (stream.scheduledSeconds > MAX_QUEUE_SECONDS) {
      this.events.onError?.('Audio queue overflow - dropping the rest of this sentence.');
      stream.ended = true;
      return;
    }

    const count = bytes.byteLength >> 1;
    if (count === 0) return;
    const buffer = this.ctx.createBuffer(1, count, stream.sampleRate);
    const channel = buffer.getChannelData(0);

    // Explicit little-endian decode: independent of ArrayBuffer alignment.
    for (let i = 0; i < count; i++) {
      const lo = bytes[i * 2];
      const hi = bytes[i * 2 + 1];
      let value = (hi << 8) | lo;
      if (value & 0x8000) value -= 0x10000;
      channel[i] = value / 32768;
    }

    this.schedule(stream, buffer);
  }

  private async decodeAndScheduleMp3(stream: ActiveStream): Promise<void> {
    if (stream.mp3Bytes === 0) {
      this.retire(stream);
      return;
    }
    const merged = new Uint8Array(stream.mp3Bytes);
    let offset = 0;
    for (const chunk of stream.mp3Chunks) {
      merged.set(chunk, offset);
      offset += chunk.byteLength;
    }
    stream.mp3Chunks = [];

    try {
      const buffer = await this.ctx.decodeAudioData(merged.buffer);
      if (stream.finished) return;
      this.schedule(stream, buffer);
    } catch (error) {
      this.events.onError?.(
        `Could not decode the MP3 stream: ${error instanceof Error ? error.message : String(error)}`,
      );
      this.retire(stream);
    }
  }

  /**
   * Put one buffer on the timeline. Chunks of one sentence are scheduled
   * back-to-back, and sentences queue behind each other, which is what makes
   * `positionFor()` a usable lip-sync clock.
   */
  private schedule(stream: ActiveStream, buffer: AudioBuffer): void {
    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    // A buffer whose rate differs from the context is resampled by the graph.
    source.connect(this.gain);

    const when = Math.max(stream.nextTime, this.ctx.currentTime);
    if (!stream.started) stream.startAt = when;

    source.start(when);
    stream.nextTime = when + buffer.duration;
    stream.scheduledSeconds += buffer.duration;
    stream.sources.push(source);

    if (!stream.started) {
      stream.started = true;
      this.playhead = Math.max(this.playhead, stream.nextTime);
      this.events.onStreamStart?.(stream.id, stream.startAt);
    } else {
      this.playhead = Math.max(this.playhead, stream.nextTime);
    }

    // Free the node as soon as it is done (long sessions hold many of them).
    source.onended = () => {
      const index = stream.sources.indexOf(source);
      if (index >= 0) stream.sources.splice(index, 1);
    };
  }

  private scheduleEnd(stream: ActiveStream, endsAt: number): void {
    const delayMs = Math.max(0, (endsAt - this.ctx.currentTime) * 1000) + 20;
    stream.endTimer = setTimeout(() => {
      stream.endTimer = null;
      this.retire(stream);
    }, delayMs);
  }

  /** Mark a stream played out and let the controller know. */
  private retire(stream: ActiveStream): void {
    if (stream.finished) return;
    stream.finished = true;
    this.events.onStreamEnd?.(stream.id);
    if (this.current === stream) this.current = null;
    this.pruneFinished();
    if (!this.isSpeaking()) this.events.onIdle?.();
  }

  private pruneFinished(): void {
    while (this.queue.length > 0 && this.queue[0].finished) {
      this.queue.shift();
    }
  }
}
