/**
 * The application runtime: one instance owns the socket, the audio pipeline and
 * every VRM controller, and translates the wire protocol into "the avatar should
 * do this now".
 *
 * Why a plain class instead of React context: the interesting work happens at
 * 60 FPS (controllers) and at message rate (socket), neither of which should
 * trigger a React render. Components read *state* from the store and call
 * *commands* here; everything else lives in this object.
 *
 * Latency policy (the part that makes it feel live):
 *  * `expression` events are applied the moment they arrive - before the audio
 *    for that sentence exists - so the face leads the voice instead of trailing
 *    it by a TTS round trip.
 *  * Speech starts as soon as `tts.start` arrives, and the viseme timeline is
 *    queued rather than replaced, so a multi-sentence reply keeps its rhythm.
 *  * A barge-in stops local audio *first*, then tells the server, so the avatar
 *    never keeps talking over the user.
 */

import { AudioPlayer } from '../audio/AudioPlayer';
import { BrowserASR } from '../audio/BrowserASR';
import { MicrophoneCapture, MIC_DEFAULTS, type MicMode } from '../audio/MicrophoneCapture';
import { SpeechSynthesisPlayer } from '../audio/SpeechSynthesisPlayer';
import { isAudioUnlocked, setOutputVolume, unlockAudio } from '../audio/engine';
import { PLACEHOLDER_VRM_URL, runtimeConfig } from '../config';
import { fetchHealth } from '../net/api';
import { WebSocketClient } from '../net/WebSocketClient';
import { useAppStore } from '../state/store';
import { BlinkController } from '../vrm/BlinkController';
import { ExpressionController } from '../vrm/ExpressionController';
import { AppearanceController } from '../vrm/AppearanceController';
import { LipSyncController } from '../vrm/LipSyncController';
import { LookAtController } from '../vrm/LookAtController';
import { AnimationController } from '../vrm/animation/AnimationController';
import { FACIAL_ACTIONS, MOTION_ACTIONS } from '../vrm/constants';
import type { VRMViewer } from '../vrm/VRMViewer';
import type {
  ActionMessage,
  ExpressionMessage,
  LlmSentenceMessage,
  MetricsMessage,
  ServerMessage,
  SessionState,
  TtsStartMessage,
} from '../types/protocol';
import type { CharacterConfig, ConfigPatch, PublicConfig } from '../types/config';
import type { VRM } from '@pixiv/three-vrm';

/** How long the "speaking" flag lingers between sentences of one reply. */
const SPEAKING_LINGER_MS = 600;
/** Streaming preview refresh interval (ms). */
const STREAM_FLUSH_MS = 70;

const MOTION_ACTION_SET = new Set<string>(MOTION_ACTIONS);

/**
 * Why the backend dropped a transcript it heard. Mirrors `REASON_LABELS` in
 * `backend/app/pipeline/transcript.py`; unknown codes fall back to the code.
 */
const ASR_IGNORED_LABELS: Record<string, string> = {
  empty: "didn't catch that",
  tag: "didn't catch that",
  short: "didn't catch that",
  noise: "ignored background noise",
  echo: "ignored my own voice",
  duplicate: "ignored a repeat",
};

export interface ConversationOptions {
  /** Overrides `VITE_WS_URL` (tests, embedded clients). */
  wsUrl?: string;
}

export class ConversationController {
  readonly client: WebSocketClient;
  readonly player: AudioPlayer;
  readonly speech = new SpeechSynthesisPlayer();

  readonly lipsync = new LipSyncController();
  readonly expressions = new ExpressionController();
  readonly blink = new BlinkController();
  readonly lookAt = new LookAtController();
  /** Shirt tint + the runtime blush. Pure model-side; the backend never sees it. */
  readonly appearance = new AppearanceController();
  /**
   * The body: authored clips (Mixamo / `.vrma`) blended over procedural idle
   * life. `motion` is the procedural layer inside it, kept as a named member
   * because the settings drawer tunes it directly.
   */
  readonly animation = new AnimationController();
  readonly motion = this.animation.procedural;

  /** Smoothed microphone level, 0..1 (polled by the mic button). */
  inputLevel = 0;

  private readonly mic: MicrophoneCapture;
  private readonly asr: BrowserASR;

  private viewer: VRMViewer | null = null;
  private detachViewer: Array<() => void> = [];

  private avatarReady = false;
  private helloSent = false;
  private unlocked = false;
  private audioRetryArmed = false;

  private language = 'en-US';
  private voice = '';
  private rate = '+0%';

  private sentences: LlmSentenceMessage[] = [];
  private streamBuffer = '';
  private lastStreamFlush = 0;
  private speakingTimer: ReturnType<typeof setTimeout> | null = null;
  private browserSpeechCount = 0;
  private hearing = false;

  constructor(options: ConversationOptions = {}) {
    this.blink.setSuppressor(() => this.expressions.blinkSuppressed);
    this.motion.setEnergySource(() => this.lipsync.mouthOpenness);

    this.animation.onStatus((status) => {
      useAppStore.getState().setAnimationStatus(status, this.animation.clipNames);
    });

    this.player = new AudioPlayer({
      onStreamEnd: () => this.blink.requestBlink(),
      onIdle: () => this.setSpeaking(false),
      onError: (message) => useAppStore.getState().setError(message),
    });

    this.mic = new MicrophoneCapture(
      {
        onChunk: (bytes) => {
          if (this.hearing) this.client.sendBinary(bytes);
        },
        onSpeechStart: () => this.onSpeechStart(),
        onSpeechEnd: () => this.onSpeechEnd(),
        onLevel: (level) => {
          this.inputLevel = level;
        },
        onError: (message) => useAppStore.getState().setError(message),
      },
      { ...MIC_DEFAULTS, mode: 'off' },
    );

    this.asr = new BrowserASR({
      onPartial: (text) => useAppStore.getState().setPartial(text),
      onFinal: (text) => {
        useAppStore.getState().setPartial('');
        this.interrupt();
        this.client.chat(text, 'asr');
      },
      onError: (message, fatal) => {
        useAppStore.getState().setError(message);
        // Chrome's `network` error is not something the user can fix from here,
        // so after a few failures we switch engines for them instead of leaving
        // a dead microphone on screen.
        if (fatal && this.wantsBrowserAsr()) this.fallBackToServerAsr(message);
      },
      onState: (state, detail) => useAppStore.getState().setAsrState(state, detail ?? ''),
      onEnd: () => useAppStore.getState().setPartial(''),
    });

    this.client = new WebSocketClient({
      url: options.wsUrl ?? runtimeConfig.wsUrl,
      onMessage: (message) => this.handleMessage(message),
      onBinary: (data) => this.player.pushChunk(data),
      onStatus: (status, detail) => this.onConnectionStatus(status, detail),
      onLatency: (rttMs, offsetMs) => useAppStore.getState().setLatency(rttMs, offsetMs),
    });
  }

  /* ------------------------------------------------------------------ */
  /*  Lifecycle                                                          */
  /* ------------------------------------------------------------------ */

  start(): void {
    this.client.connect();
    this.applyAnimationPrefs();
    this.applyPrefsToViewer();
  }

  dispose(): void {
    this.detachViewer.forEach((detach) => detach());
    this.detachViewer = [];
    this.client.close();
    this.asr.stop();
    this.mic.release();
    this.speech.cancelAll();
    this.player.stop();
    this.animation.dispose();
  }

  /** Hand over the Three.js stage. Controllers are ticked by its render loop. */
  attachViewer(viewer: VRMViewer): () => void {
    this.viewer = viewer;
    this.lookAt.setPointerSource(() => ({
      x: viewer.pointer.x,
      y: viewer.pointer.y,
      at: viewer.pointerAt,
    }));
    this.detachViewer = [
      viewer.addController(this.lipsync),
      viewer.addController(this.lookAt),
      viewer.addController(this.expressions),
      viewer.addController(this.blink),
      viewer.addController(this.animation),
      viewer.addController(this.appearance),
      viewer.onModelLoaded((vrm) => this.onModelLoaded(vrm)),
    ];
    return () => {
      this.detachViewer.forEach((detach) => detach());
      this.detachViewer = [];
      if (this.viewer === viewer) this.viewer = null;
    };
  }

  private onModelLoaded(vrm: VRM | null): void {
    this.lipsync.attach(vrm);
    this.expressions.attach(vrm);
    this.blink.attach(vrm);
    this.animation.attach(vrm);
    this.appearance.attach(vrm);
    if (this.viewer) this.lookAt.attach(vrm, this.viewer.camera, this.viewer.scene);

    this.avatarReady = vrm !== null;
    if (!vrm) return;

    const store = useAppStore.getState();
    store.setAvatarPhase('ready');
    store.setArmsAlreadyDown(this.motion.detectedArmsDown);
    this.applyMotionPrefs();
    this.applyAnimationPrefs();
    this.applyPrefsToViewer();
    this.maybeHello();
  }

  /** Which avatar to load: the user's choice, then the backend, then the sample. */
  resolveVrmUrl(): string {
    const { prefs, config } = useAppStore.getState();
    return prefs.vrmUrl || config?.avatar.vrm_url || runtimeConfig.vrmUrl || PLACEHOLDER_VRM_URL;
  }

  /**
   * Called from the "start" gesture: unlocks Web Audio (browsers require a
   * gesture), then sends `hello` so the backend greets a fully loaded stage.
   */
  async beginSession(): Promise<void> {
    await unlockAudio();

    if (!isAudioUnlocked()) {
      // The autoplay policy refused to start the context (strict settings, an
      // embedded frame, or a gesture the browser did not trust). Do not pretend
      // the session started: the overlay stays so the click can be repeated, and
      // the next gesture retries on its own.
      useAppStore
        .getState()
        .setError('The browser blocked audio playback. Click "Start talking" again, or allow sound for this site.');
      this.armAudioRetry();
      return;
    }

    useAppStore.getState().setError(null);
    this.unlocked = true;

    const store = useAppStore.getState();
    setOutputVolume(store.prefs.volume);
    store.setStarted(true);
    this.maybeHello();
    this.syncInput();
  }

  /** One-shot: retry the audio unlock on the user's next interaction. */
  private armAudioRetry(): void {
    if (this.audioRetryArmed) return;
    this.audioRetryArmed = true;
    const retry = (): void => {
      window.removeEventListener('pointerdown', retry, true);
      window.removeEventListener('keydown', retry, true);
      this.audioRetryArmed = false;
      if (!useAppStore.getState().started) void this.beginSession();
    };
    window.addEventListener('pointerdown', retry, true);
    window.addEventListener('keydown', retry, true);
  }

  /**
   * A running `AudioContext` can be suspended again at any time (tab in the
   * background, long idle, an output device change). A reply that arrives while
   * it is suspended plays *silently*, which is indistinguishable from a broken
   * pipeline - so every sentence is a chance to notice and recover.
   */
  private ensureAudio(): void {
    if (isAudioUnlocked()) return;
    void unlockAudio().catch(() => undefined);
  }

  private maybeHello(): void {
    if (this.helloSent || !this.avatarReady || !this.unlocked || !this.client.isOpen) return;
    this.helloSent = true;
    this.client.hello({ greet: true, clientSideAsr: this.usesBrowserAsr(), sampleRate: 16000 });
  }

  private onConnectionStatus(status: string, detail?: string): void {
    useAppStore.getState().setConnection(status as never, detail ?? '');
    if (status === 'open') {
      this.maybeHello();
      return;
    }
    if (status === 'closed' || status === 'error') {
      // The server is gone: stop pretending to talk.
      this.player.stop();
      this.speech.cancelAll();
      this.lipsync.clear();
      this.expressions.clear();
      this.motion.setMood(null);
      this.setSpeaking(false);
      useAppStore.getState().setServerState('idle');
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Commands                                                           */
  /* ------------------------------------------------------------------ */

  /** Typed message (also used by client-side ASR). */
  submitText(text: string): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (this.usesBrowserAsr()) {
      // The browser ASR is listening: hand the text over as a normal chat.
      this.interrupt();
    }
    this.client.chat(trimmed);
  }

  /** Barge-in: stop the avatar locally first, then tell the server. */
  interrupt(heard = ''): void {
    this.player.stop();
    this.speech.cancelAll();
    this.lipsync.clear();
    // A face left mid-expression after the voice has stopped reads as frozen.
    this.expressions.clear();
    this.motion.setMood(null);
    this.appearance.setEmotion('neutral');
    this.browserSpeechCount = 0;
    this.hearing = false;
    this.setSpeaking(false, true);
    useAppStore.getState().setServerState('idle');
    this.client.interrupt(heard);
  }

  reset(): void {
    this.player.stop();
    this.speech.cancelAll();
    this.lipsync.clear();
    this.expressions.clear();
    this.motion.setMood(null);
    this.appearance.setEmotion('neutral');
    this.sentences = [];
    this.streamBuffer = '';
    useAppStore.getState().clearChat();
    this.client.reset();
  }

  patchConfig(patch: ConfigPatch): void {
    this.client.patchConfig(patch);
  }

  /**
   * The persona file changed under the server (the settings UI just saved an
   * edit). The server answers with a `config.ack`, which is what updates the
   * language, the voice and the name the UI shows.
   */
  reloadPersona(): void {
    this.client.reloadCharacter();
  }

  setVolume(volume: number): void {
    setOutputVolume(volume);
    this.player.setVolume(volume);
  }

  /** Re-apply viewer-side preferences after the settings drawer changes them. */
  applyPrefsToViewer(): void {
    const { prefs, config } = useAppStore.getState();

    // Appearance is model-side, not viewer-side, so it is applied *before* the
    // viewer check: the controller keeps the options and paints them onto the
    // model when (or if) it loads.
    this.appearance.configure(prefs.appearance);

    const viewer = this.viewer;
    if (!viewer) return;
    viewer.setCameraPreset(prefs.cameraPreset ?? config?.avatar.camera_preset ?? runtimeConfig.cameraPreset, true);
    viewer.setPixelRatio(prefs.pixelRatio ?? config?.avatar.pixel_ratio ?? runtimeConfig.pixelRatio);
    viewer.setShadows(config?.avatar.enable_shadows ?? false);
    this.applyMotionPrefs();
  }

  private applyMotionPrefs(): void {
    this.motion.setOptions(useAppStore.getState().prefs.motion);
  }

  /**
   * The animation layer is client-only: the manifest lists *local* files, so
   * there is nothing for the backend to know about it.
   */
  private applyAnimationPrefs(): void {
    this.animation.configure(useAppStore.getState().prefs.animation);
  }

  /** Used by the settings drawer after it edits the preferences. */
  refreshAnimation(): void {
    this.applyAnimationPrefs();
    this.applyMotionPrefs();
  }

  /** Re-fetch the manifest + clips (settings drawer -> Motion -> Reload). */
  reloadAnimations(): void {
    this.animation.reload();
  }

  /** Mic mode / ASR engine changes both funnel through here. */
  syncInput(): void {
    const { prefs } = useAppStore.getState();

    if (this.usesBrowserAsr()) {
      this.mic.release();
      if (prefs.micMode === 'off') this.asr.stop();
      else this.asr.start(this.language);
      return;
    }

    this.asr.stop();
    this.mic.setOptions({ mode: prefs.micMode as MicMode });
    if (prefs.micMode === 'off' || !this.unlocked) {
      this.mic.release();
      return;
    }
    void this.mic.start();
  }

  beginPushToTalk(): void {
    if (useAppStore.getState().prefs.micMode !== 'ptt') return;
    if (this.usesBrowserAsr()) {
      this.asr.start(this.language);
      return;
    }
    if (!this.unlocked) void this.beginSession();
    this.mic.beginPushToTalk();
  }

  endPushToTalk(): void {
    if (useAppStore.getState().prefs.micMode !== 'ptt') return;
    if (this.usesBrowserAsr()) {
      this.asr.stop();
      return;
    }
    this.mic.endPushToTalk();
  }

  requestBlink(): void {
    this.blink.requestBlink();
  }

  private wantsBrowserAsr(): boolean {
    const { prefs, config } = useAppStore.getState();
    return prefs.useBrowserAsr ?? config?.asr.engine === 'browser';
  }

  private usesBrowserAsr(): boolean {
    return this.wantsBrowserAsr() && BrowserASR.available;
  }

  /**
   * The browser recogniser gave up (see `BrowserASR`): switch the *preference*
   * to the server engine so the user does not have to understand why Chrome
   * could not reach its speech service.
   */
  private fallBackToServerAsr(message: string): void {
    const store = useAppStore.getState();
    if (store.config?.asr.engine === 'browser') {
      // There is nothing to fall back to: the backend does not transcribe.
      store.setError(`${message} This backend has no server-side ASR engine either - try Chrome online, or type.`);
      return;
    }
    store.setPrefs({ useBrowserAsr: false });
    store.setError(`${message} Switched to the server-side engine - press the mic again.`);
    this.syncInput();
  }

  /* ------------------------------------------------------------------ */
  /*  Microphone                                                         */
  /* ------------------------------------------------------------------ */

  private onSpeechStart(): void {
    if (useAppStore.getState().speaking || this.player.isSpeaking()) {
      // Barge-in: the user started talking over the avatar.
      this.interrupt();
    }
    this.hearing = true;
    useAppStore.getState().setServerState('listening', 'recording');
    this.client.send({ type: 'audio.start', sample_rate: 16000 });
  }

  private onSpeechEnd(): void {
    if (!this.hearing) return;
    this.hearing = false;
    this.client.send({ type: 'audio.end' });
  }

  /* ------------------------------------------------------------------ */
  /*  Protocol                                                           */
  /* ------------------------------------------------------------------ */

  private handleMessage(message: ServerMessage): void {
    switch (message.type) {
      case 'ready':
        this.onReady(message);
        break;

      case 'state':
        useAppStore.getState().setServerState(message.value, message.detail);
        this.onServerState(message.value);
        break;

      case 'asr.partial':
        useAppStore.getState().setPartial(message.text);
        break;

      case 'asr.final': {
        const store = useAppStore.getState();
        store.setPartial('');
        // The backend silently drops noise and speaker echo. Say so, otherwise
        // a rejected utterance looks like the avatar ignoring the user.
        if (message.reason) {
          store.setAsrState('idle', ASR_IGNORED_LABELS[message.reason] ?? message.reason);
        }
        break;
      }

      case 'user.text': {
        const store = useAppStore.getState();
        store.beginTurn();
        store.pushMessage({ role: 'user', text: message.text, source: 'you' });
        this.sentences = [];
        this.streamBuffer = '';
        break;
      }

      case 'llm.delta':
        this.onDelta(message.text);
        break;

      case 'llm.restart':
        // The server rejected the attempt it was streaming (it only parroted the
        // user) and is generating again. Drop the preview, keep the bubble.
        this.streamBuffer = '';
        useAppStore.getState().setStreamingText('');
        break;

      case 'llm.sentence':
        this.sentences.push(message);
        useAppStore.getState().pushSentence(message.display_text);
        break;

      case 'llm.done': {
        const store = useAppStore.getState();
        const display = this.sentences.map((item) => item.display_text).join(' ').trim();
        store.finishTurn(display || message.text, store.emotion);
        this.sentences = [];
        this.streamBuffer = '';
        break;
      }

      case 'interrupted':
        this.player.stop();
        this.speech.cancelAll();
        this.lipsync.clear();
        this.browserSpeechCount = 0;
        this.setSpeaking(false, true);
        useAppStore.getState().setServerState('idle');
        break;

      case 'tts.start':
        this.onTtsStart(message);
        break;

      case 'tts.end':
        this.player.endStream(message.id);
        break;

      case 'expression':
        this.applyExpression(message);
        break;

      case 'action':
        this.applyAction(message);
        break;

      case 'config.ack': {
        const store = useAppStore.getState();
        store.setConfig(message.config, message.config.character);
        this.onConfigChanged(message.config);
        break;
      }

      case 'metrics':
        useAppStore.getState().setMetrics(toMetrics(message));
        break;

      case 'history':
        if (message.messages.length === 0) useAppStore.getState().clearChat();
        break;

      case 'error': {
        const store = useAppStore.getState();
        store.setError(message.message, message.fatal);
        if (message.fatal) {
          this.player.stop();
          this.speech.cancelAll();
          this.setSpeaking(false, true);
        }
        break;
      }

      case 'pong':
        this.client.notePong(message.t, message.server_time);
        break;

      default:
        // Unknown message types are ignored on purpose: the backend may grow.
        break;
    }
  }

  private onReady(message: Extract<ServerMessage, { type: 'ready' }>): void {
    const store = useAppStore.getState();
    store.setReady({
      sessionId: message.session_id,
      config: message.config,
      character: message.character,
      capabilities: message.capabilities,
    });
    // `ready` means the server built a *fresh* session for this connection -
    // after a reconnect it has no history and is waiting for a `hello` that we
    // already sent on the previous socket. Without this reset the client would
    // sit there connected but mute, which is what the state-sync bug looked like.
    this.helloSent = false;
    this.onConfigChanged(message.config, message.character);
    this.maybeHello();
    void this.probeModelStatus(message.config);
  }

  /**
   * Ask the backend whether replies will actually be *generated*.
   *
   * This is the honest-signal half of the "the replies are a fixed script"
   * fix. The server already knows the answer (its startup preflight probes the
   * LLM and only falls back to the offline brain when it has to); the client's
   * job is to say so out loud instead of letting someone talk to a script
   * believing it is a model. Runs once per `ready`, so it survives reconnects.
   */
  private async probeModelStatus(config: PublicConfig): Promise<void> {
    const store = useAppStore.getState();
    try {
      const report = await fetchHealth();
      const llm = report.preflight?.llm;
      if (llm && !llm.ok) {
        store.setNotice({
          tone: 'warn',
          text: `No language model is connected, so I cannot answer questions yet. ${llm.detail}`,
        });
        return;
      }
      if (llm?.canned || config.llm.provider === 'echo') {
        store.setNotice({
          tone: 'info',
          text:
            'Offline demo mode: my words are a script, not a model. Everything else - voice, face, lip-sync - is real.' +
            (llm?.detail ? ` (${llm.detail})` : ''),
        });
        return;
      }
      if (llm?.auto_discovered) {
        store.setNotice({ tone: 'info', text: `Connected automatically. ${llm.detail}` });
        return;
      }
      store.setNotice(null);
    } catch {
      // Health is a nicety here; a failure must never disturb the session.
    }
  }

  /** Everything that must follow a config change (initial load or a patch). */
  private onConfigChanged(config: PublicConfig, character?: CharacterConfig | null): void {
    const persona = character ?? useAppStore.getState().character;

    this.lipsync.configure(config.lipsync);
    this.player.setVolume(useAppStore.getState().prefs.volume);
    this.language = toBcp47(persona?.language ?? 'en');
    this.voice = persona?.tts_voice || config.tts.voice;
    this.rate = persona?.tts_rate || config.tts.rate;
    this.applyPrefsToViewer();
    this.syncInput();
  }

  private onDelta(text: string): void {
    const store = useAppStore.getState();
    if (!store.turnActive) store.beginTurn();
    this.streamBuffer += text;

    const now = performance.now();
    if (now - this.lastStreamFlush < STREAM_FLUSH_MS) return;
    this.lastStreamFlush = now;
    store.setStreamingText(this.streamBuffer);
  }

  private onTtsStart(header: TtsStartMessage): void {
    this.ensureAudio();
    // A sentence mood owns the face from the moment the audio is ready; the
    // ambient "thinking / listening" layer must step aside rather than add to it.
    this.expressions.setAmbient(null);
    if (header.expression) this.applyExpression(header.expression);
    // `header.actions` is not replayed here: the server sends each action as its
    // own frame *before* the audio is ready, so applying it again when the audio
    // arrives played every gesture twice (and restarted `(smiles)` transients).
    this.setSpeaking(true);

    if (header.format === 'browser') {
      this.browserSpeechCount += 1;
      const handle = this.speech.speak({
        id: header.id,
        text: header.text,
        duration: header.duration,
        // The sentence's own language, not the persona's: the server detects it
        // from what the user wrote, so a Bengali line gets a Bengali voice even
        // though the persona file says `en`.
        lang: header.lang || this.language,
        voice: header.voice || this.voice,
        rate: this.rate,
      });
      this.lipsync.enqueue(header, handle.clock);
      void handle.done.then(() => {
        this.browserSpeechCount = Math.max(0, this.browserSpeechCount - 1);
        if (this.browserSpeechCount === 0 && !this.player.isSpeaking()) this.setSpeaking(false);
      });
      return;
    }

    this.player.beginStream(header);
    const id = header.id;
    this.lipsync.enqueue(header, () => this.player.positionFor(id));
  }

  /**
   * The avatar's reaction to the *phase* of a turn.
   *
   * Expressions only fire when a sentence is classified, which left her staring
   * blankly while the model thought and while the user was talking. This layer
   * is deliberately separate from the sentence mood: it is suppressed the
   * moment a real expression arrives (`setAmbient(null)` on `tts.start`) and it
   * never outlives the turn. The posture is the body half of the same idea.
   */
  private onServerState(state: SessionState): void {
    switch (state) {
      case 'thinking':
        this.expressions.setAmbient('thinking', 0.6);
        this.motion.setMood('thinking');
        this.appearance.setEmotion('thinking');
        break;
      case 'listening':
        this.expressions.setAmbient('relaxed', 0.3);
        this.motion.setMood('listening');
        this.appearance.setEmotion('relaxed');
        this.blink.requestBlink();
        break;
      case 'speaking':
        this.expressions.setAmbient(null);
        break;
      case 'idle':
        // The *mood* keeps its own hold and should not be cut short: `idle`
        // arrives when generation ends, which can be before the audio drains.
        this.expressions.setAmbient(null);
        break;
      default:
        break;
    }
  }

  private applyExpression(payload: ExpressionMessage | NonNullable<TtsStartMessage['expression']>): void {
    this.expressions.setMood({
      // `preset` is post-mapping, and `shy` and `thinking` both map to `relaxed`
      // - the emotion is what keeps them apart for the face and the blush.
      emotion: payload.emotion ?? payload.preset,
      preset: payload.preset,
      weight: payload.weight,
      holdMs: payload.hold_ms,
      overrideBlink: payload.override_blink,
    });
    // Body language follows the face, so a happy line is not delivered by a
    // neutral spine with a smiling mouth pasted on.
    this.motion.setMood(payload.emotion ?? payload.preset ?? null);
    this.appearance.setEmotion(payload.emotion ?? payload.preset);
    useAppStore.getState().setEmotion(payload.preset);
  }

  private applyAction(payload: ActionMessage): void {
    const facial = FACIAL_ACTIONS[payload.name];
    if (facial) {
      this.expressions.playTransient(facial.emotion, facial.weight, facial.ms);
      return;
    }
    if (MOTION_ACTION_SET.has(payload.name)) {
      this.animation.play(payload.name, payload.intensity);
    }
  }

  /**
   * `speaking` must survive the gaps between sentences of one reply (the LLM
   * pauses to think, the TTS queue drains), so turning it *off* is debounced.
   */
  private setSpeaking(speaking: boolean, immediate = false): void {
    const store = useAppStore.getState();

    if (speaking) {
      if (this.speakingTimer !== null) {
        clearTimeout(this.speakingTimer);
        this.speakingTimer = null;
      }
      this.mic.setAssistantSpeaking(true);
      // The browser recogniser has no echo cancellation of its own: it happily
      // transcribes the avatar. Pause it for the duration of the reply.
      this.asr.pause();
      store.setSpeaking(true);
      return;
    }

    if (this.speakingTimer !== null) return;
    const finish = (): void => {
      this.speakingTimer = null;
      this.mic.setAssistantSpeaking(false);
      this.asr.resume();
      useAppStore.getState().setSpeaking(false);
      this.blink.requestBlink();
    };
    if (immediate) finish();
    else this.speakingTimer = setTimeout(finish, SPEAKING_LINGER_MS);
  }
}

function toMetrics(message: MetricsMessage) {
  return {
    turn: typeof message.turn === 'number' ? message.turn : undefined,
    asr_ms: typeof message.asr_ms === 'number' ? message.asr_ms : undefined,
    ttft_ms: typeof message.ttft_ms === 'number' ? message.ttft_ms : undefined,
    total_ms: typeof message.total_ms === 'number' ? message.total_ms : undefined,
    sentences: typeof message.sentences === 'number' ? message.sentences : undefined,
    history: typeof message.history === 'number' ? message.history : undefined,
  };
}

/** `ja` -> `ja-JP`, `en` -> `en-US`; anything longer is passed through.
 *
 * Mirrors `LOCALES` in `backend/app/pipeline/language.py`. A script tag
 * (`bn-Latn`, the Banglish code) is *writing*, not a region, so it collapses
 * onto the spoken locale - the voice is chosen by the language.
 */
const LOCALES: Record<string, string> = {
  en: 'en-US',
  bn: 'bn-BD',
  hi: 'hi-IN',
  ur: 'ur-PK',
  ar: 'ar-SA',
  fa: 'fa-IR',
  ta: 'ta-IN',
  te: 'te-IN',
  kn: 'kn-IN',
  ml: 'ml-IN',
  gu: 'gu-IN',
  mr: 'mr-IN',
  ne: 'ne-NP',
  si: 'si-LK',
  ja: 'ja-JP',
  zh: 'zh-CN',
  ko: 'ko-KR',
  ru: 'ru-RU',
  uk: 'uk-UA',
  pl: 'pl-PL',
  cs: 'cs-CZ',
  tr: 'tr-TR',
  vi: 'vi-VN',
  th: 'th-TH',
  id: 'id-ID',
  ms: 'ms-MY',
  fil: 'fil-PH',
  nl: 'nl-NL',
  de: 'de-DE',
  fr: 'fr-FR',
  es: 'es-ES',
  it: 'it-IT',
  pt: 'pt-BR',
  sv: 'sv-SE',
  da: 'da-DK',
  fi: 'fi-FI',
  nb: 'nb-NO',
  el: 'el-GR',
  he: 'he-IL',
  hu: 'hu-HU',
  ro: 'ro-RO',
};

const SCRIPT_SUBTAGS = new Set(['latn', 'cyrl', 'arab', 'hans', 'hant', 'beng', 'deva', 'jpan', 'kore']);

function toBcp47(language: string): string {
  const code = (language || 'en').trim().toLowerCase();
  if (!code) return 'en-US';
  const [head, ...rest] = code.split('-');
  const known = LOCALES[head];
  if (!known) return code;
  if (rest.length > 0 && !SCRIPT_SUBTAGS.has(rest[0])) return code;
  return known;
}

/** The single instance the React tree talks to. */
let instance: ConversationController | null = null;

export function getConversation(): ConversationController {
  if (!instance) instance = new ConversationController();
  return instance;
}
