/**
 * The single client-side store.
 *
 * Two kinds of state live here and they behave very differently:
 *
 *  * **session state** - connection, transcript, engine config. Replaced by the
 *    server, re-rendered rarely.
 *  * **preferences** - the only persisted slice (avatar URL, camera, motion
 *    tuning, volume). These survive a reload and are never sent to the server,
 *    except the parts the backend also knows about (voice, engine), which the
 *    settings drawer patches explicitly.
 *
 * Per-frame animation values are deliberately *not* here: 60 FPS updates belong
 * in the controllers, not in React's render path.
 */

import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import type { MicMode } from '../audio/MicrophoneCapture';
import type { BrowserASRState } from '../audio/BrowserASR';
import type { ConnectionStatus } from '../net/WebSocketClient';
import { DEFAULT_MOTION, type MotionOptions } from '../vrm/constants';
import { DEFAULT_APPEARANCE, type AppearanceOptions } from '../vrm/AppearanceController';
import {
  DEFAULT_ANIMATION,
  type AnimationOptions,
  type AnimationStatus,
  type ClipKind,
} from '../vrm/animation/types';
import type {
  Capabilities,
  CameraPreset,
  CharacterConfig,
  CharacterSummary,
  PublicConfig,
} from '../types/config';
import type { SessionState } from '../types/protocol';

export type ChatRole = 'user' | 'assistant' | 'system';

export interface ChatEntry {
  id: string;
  role: ChatRole;
  text: string;
  at: number;
  /** Emotion the backend reported for this reply, if any. */
  emotion?: string | null;
  /** Speaker tag shown for user messages (`you`, `mic`, ...). */
  source?: string;
}

export interface TurnMetrics {
  turn?: number;
  asr_ms?: number;
  ttft_ms?: number;
  total_ms?: number;
  sentences?: number;
  history?: number;
}

export interface Preferences {
  /** Empty -> use the backend's `avatar.vrm_url`, then the placeholder. */
  vrmUrl: string;
  /** `null` -> adopt the backend's `avatar.camera_preset`. */
  cameraPreset: CameraPreset | null;
  /** `null` -> adopt the backend's `avatar.pixel_ratio`. */
  pixelRatio: number | null;
  /** `null` -> adopt the backend's `avatar.show_stats`. */
  showStats: boolean | null;
  volume: number;
  micMode: MicMode;
  /** `null` -> on when the backend has no server-side ASR engine. */
  useBrowserAsr: boolean | null;
  motion: MotionOptions;
  /** Authored animation clips (Mixamo / `.vrma`) layered over the procedural idle. */
  animation: AnimationOptions;
  /**
   * Shirt colour and blush. Client-only: these are pure rendering tweaks, so
   * the backend has no reason to know about them.
   */
  appearance: AppearanceOptions;
  /**
   * Hide every piece of UI and render only the avatar, for capture.
   *
   * `null` -> decide from the URL (`?streamer=1`), which is how an OBS browser
   * source opens this page.
   */
  streamer: boolean | null;
}

export type AvatarPhase = 'idle' | 'loading' | 'ready' | 'error';

/**
 * The top colour v1 shipped as its default. Kept only so `merge` can recognise
 * a saved preference that nobody ever edited (see the note there).
 */
const LEGACY_SHIRT_DEFAULT = '#8f9398';

export const DEFAULT_PREFERENCES: Preferences = {
  vrmUrl: '',
  cameraPreset: null,
  pixelRatio: null,
  showStats: null,
  volume: 1,
  micMode: 'vad',
  useBrowserAsr: null,
  motion: { ...DEFAULT_MOTION },
  animation: { ...DEFAULT_ANIMATION },
  appearance: { ...DEFAULT_APPEARANCE },
  streamer: null,
};

interface AppState {
  /* connection */
  connection: ConnectionStatus;
  connectionDetail: string;
  sessionId: string;
  rttMs: number | null;
  serverTimeOffsetMs: number | null;
  /** True once the user has unlocked audio with a gesture. */
  started: boolean;

  /* session */
  serverState: SessionState;
  stateDetail: string;
  speaking: boolean;
  partialSpeech: string;
  emotion: string | null;
  error: string | null;
  errorFatal: boolean;
  /**
   * A persistent, non-fatal advisory (e.g. "replies are scripted because no
   * model is connected"). Distinct from `error`, which is transient and usually
   * the result of something the user just did.
   */
  notice: { text: string; tone: 'info' | 'warn' } | null;
  /** Browser-ASR engine state, so the UI can explain a silent microphone. */
  asrState: BrowserASRState;
  asrDetail: string;
  /** Set to open the settings drawer on a specific tab (e.g. from the notice). */
  settingsTabRequest: string | null;

  /* server data */
  config: PublicConfig | null;
  character: CharacterConfig | null;
  capabilities: Capabilities | null;
  metrics: TurnMetrics | null;

  /* transcript */
  messages: ChatEntry[];
  streamingText: string;
  turnSentences: string[];
  /** True from the first token until `llm.done`: the reply is still being written. */
  turnActive: boolean;

  /* avatar */
  avatarPhase: AvatarPhase;
  avatarProgress: number | null;
  avatarLabel: string;
  avatarError: string | null;
  armsAlreadyDown: boolean;
  /** What the animation layer is doing (clip count, problems, active clip). */
  animationStatus: AnimationStatus;
  /** Clip names the manifest provided, so the drawer can list them. */
  animationClips: Array<{ name: string; kind: ClipKind }>;

  /* preferences (persisted) */
  prefs: Preferences;
}

interface AppActions {
  setConnection: (status: ConnectionStatus, detail?: string) => void;
  setLatency: (rttMs: number, offsetMs: number) => void;
  setStarted: (started: boolean) => void;

  setServerState: (state: SessionState, detail?: string) => void;
  setSpeaking: (speaking: boolean) => void;
  setPartial: (text: string) => void;
  setEmotion: (emotion: string | null) => void;
  setError: (message: string | null, fatal?: boolean) => void;
  setNotice: (notice: { text: string; tone: 'info' | 'warn' } | null) => void;
  requestSettingsTab: (tab: string | null) => void;
  setAsrState: (state: BrowserASRState, detail: string) => void;

  setReady: (payload: {
    sessionId: string;
    config: PublicConfig;
    character: CharacterConfig;
    capabilities: Capabilities;
  }) => void;
  setConfig: (config: PublicConfig, character?: CharacterConfig) => void;
  setMetrics: (metrics: TurnMetrics) => void;
  setCharacters: (characters: CharacterSummary[]) => void;

  pushMessage: (entry: Omit<ChatEntry, 'id' | 'at'> & { id?: string; at?: number }) => void;
  beginTurn: () => void;
  setStreamingText: (text: string) => void;
  pushSentence: (displayText: string) => void;
  finishTurn: (text: string, emotion?: string | null) => void;
  clearChat: () => void;

  setAvatarPhase: (phase: AvatarPhase) => void;
  setAvatarProgress: (fraction: number | null, label: string) => void;
  setAvatarError: (message: string | null) => void;
  setArmsAlreadyDown: (value: boolean) => void;
  setAnimationStatus: (status: AnimationStatus, clips: Array<{ name: string; kind: ClipKind }>) => void;

  setPrefs: (patch: Partial<Preferences>) => void;
  setMotion: (patch: Partial<MotionOptions>) => void;
  setAnimation: (patch: Partial<AnimationOptions>) => void;
  setAppearance: (patch: Partial<AppearanceOptions>) => void;
}

export type Store = AppState & AppActions;

const INITIAL: AppState = {
  connection: 'idle',
  connectionDetail: '',
  sessionId: '',
  rttMs: null,
  serverTimeOffsetMs: null,
  started: false,

  serverState: 'idle',
  stateDetail: '',
  speaking: false,
  partialSpeech: '',
  emotion: null,
  error: null,
  errorFatal: false,
  notice: null,
  asrState: 'idle',
  asrDetail: '',
  settingsTabRequest: null,

  config: null,
  character: null,
  capabilities: null,
  metrics: null,

  messages: [],
  streamingText: '',
  turnSentences: [],
  turnActive: false,

  avatarPhase: 'idle',
  avatarProgress: null,
  avatarLabel: '',
  avatarError: null,
  armsAlreadyDown: false,
  animationStatus: {
    mode: 'procedural',
    loading: false,
    problems: [],
    clips: 0,
    idleClips: 0,
    gestureClips: 0,
    activeIdle: null,
    activeGesture: null,
    drivenBones: 0,
  },
  animationClips: [],

  prefs: { ...DEFAULT_PREFERENCES },
};

let messageSeq = 0;

export const useAppStore = create<Store>()(
  persist(
    (set) => ({
      ...INITIAL,

      setConnection: (connection, connectionDetail = '') => set({ connection, connectionDetail }),
      setLatency: (rttMs, serverTimeOffsetMs) => set({ rttMs, serverTimeOffsetMs }),
      setStarted: (started) => set({ started }),

      setServerState: (serverState, stateDetail = '') => set({ serverState, stateDetail }),
      setSpeaking: (speaking) => set({ speaking }),
      setPartial: (partialSpeech) => set({ partialSpeech }),
      setEmotion: (emotion) => set({ emotion }),
      setError: (error, fatal = false) => set({ error, errorFatal: fatal }),
      setNotice: (notice) => set({ notice }),
      requestSettingsTab: (settingsTabRequest) => set({ settingsTabRequest }),
      setAsrState: (asrState, asrDetail) => set({ asrState, asrDetail }),

      setReady: ({ sessionId, config, character, capabilities }) =>
        set({ sessionId, config, character, capabilities }),

      setConfig: (config, character) =>
        set((state) => ({
          config,
          character: character ?? state.character,
        })),

      setMetrics: (metrics) => set({ metrics }),
      setCharacters: (characters) =>
        set((state) => ({
          capabilities: state.capabilities ? { ...state.capabilities, characters } : null,
        })),

      pushMessage: (entry) =>
        set((state) => ({
          messages: [
            ...state.messages,
            { at: Date.now(), id: `m${++messageSeq}`, ...entry },
          ].slice(-400),
        })),

      setStreamingText: (streamingText) => set({ streamingText }),

      beginTurn: () => set({ turnActive: true, turnSentences: [], streamingText: '' }),

      pushSentence: (displayText) =>
        set((state) => ({
          streamingText: '',
          turnSentences: [...state.turnSentences, displayText],
        })),

      finishTurn: (text, emotion = null) =>
        set((state) => {
          const trimmed = text.trim();
          if (!trimmed) return { streamingText: '', turnSentences: [], turnActive: false };
          return {
            streamingText: '',
            turnSentences: [],
            turnActive: false,
            messages: [
              ...state.messages,
              { id: `m${++messageSeq}`, role: 'assistant' as const, text: trimmed, at: Date.now(), emotion },
            ].slice(-400),
          };
        }),

      clearChat: () => set({ messages: [], streamingText: '', turnSentences: [], turnActive: false }),

      setAvatarPhase: (avatarPhase) =>
        set(avatarPhase === 'loading' ? { avatarPhase, avatarError: null, avatarProgress: null } : { avatarPhase }),
      setAvatarProgress: (avatarProgress, avatarLabel) => set({ avatarProgress, avatarLabel }),
      setAvatarError: (avatarError) => set({ avatarError, avatarPhase: avatarError ? 'error' : 'idle' }),
      setArmsAlreadyDown: (armsAlreadyDown) => set({ armsAlreadyDown }),
      setAnimationStatus: (animationStatus, animationClips) => set({ animationStatus, animationClips }),

      setPrefs: (patch) => set((state) => ({ prefs: { ...state.prefs, ...patch } })),
      setMotion: (patch) =>
        set((state) => ({ prefs: { ...state.prefs, motion: { ...state.prefs.motion, ...patch } } })),
      setAnimation: (patch) =>
        set((state) => ({ prefs: { ...state.prefs, animation: { ...state.prefs.animation, ...patch } } })),
      setAppearance: (patch) =>
        set((state) => ({ prefs: { ...state.prefs, appearance: { ...state.prefs.appearance, ...patch } } })),
    }),
    {
      name: 'open-vrm-vtuber',
      version: 1,
      storage: createJSONStorage(() => localStorage),
      // Only preferences survive a reload; everything else comes from the server.
      partialize: (state) => ({ prefs: state.prefs }) as unknown as Store,
      merge: (persisted, current) => {
        const saved = (persisted as { prefs?: Partial<Preferences> } | undefined)?.prefs;
        if (!saved) return current;

        // v1 shipped a flat grey top. Somebody who never touched the swatch
        // still has that value saved, and it would mask the new default look,
        // so upgrade *exactly* the untouched old default and nothing else.
        const savedAppearance = { ...(saved.appearance ?? {}) };
        if (savedAppearance.shirtColor === LEGACY_SHIRT_DEFAULT) {
          delete savedAppearance.shirtColor;
        }

        return {
          ...current,
          prefs: {
            ...DEFAULT_PREFERENCES,
            ...saved,
            motion: { ...DEFAULT_MOTION, ...(saved.motion ?? {}) },
            animation: { ...DEFAULT_ANIMATION, ...(saved.animation ?? {}) },
            appearance: { ...DEFAULT_APPEARANCE, ...savedAppearance },
          },
        };
      },
    },
  ),
);

/* Handy selectors (kept out of components so they stay stable references). */
export const selectSpeaking = (state: Store): boolean => state.speaking;
export const selectPrefs = (state: Store): Preferences => state.prefs;

/** The state shown in the status pill: server facts + what we know locally. */
export function deriveStatus(state: Store): SessionState | 'offline' {
  if (state.connection !== 'open') return 'offline';
  if (state.error) return 'error';
  if (state.speaking) return 'speaking';
  if (state.serverState === 'thinking' || state.serverState === 'listening') return state.serverState;
  if (state.partialSpeech) return 'listening';
  return 'idle';
}
