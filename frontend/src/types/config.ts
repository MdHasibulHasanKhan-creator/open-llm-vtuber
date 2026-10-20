/**
 * Shape of everything the backend reports about itself.
 *
 * These mirror `backend/app/config.py` and the `capabilities` block built in
 * `backend/app/session.py`. They are the *public* (secrets masked) views: the
 * settings drawer patches them back through the `config` WebSocket message.
 */

export type LLMProvider = 'openai' | 'ollama' | 'echo';
export type TTSEngine = 'edge' | 'openai' | 'browser';
export type ASREngine = 'local' | 'openai' | 'browser';
export type AudioFormat = 'pcm_s16le' | 'mp3';
export type CameraPreset = 'face' | 'bust' | 'full';
export type LogLevel = 'TRACE' | 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';

/** Skeleton gestures the backend can ask for (see `backend/app/emotion.py`). */
export type GestureName =
  | 'nod'
  | 'shake'
  | 'tilt'
  | 'wave'
  | 'think'
  | 'sigh'
  | 'smile'
  | 'laugh'
  | 'blush'
  | 'lean_in';

/** Mouth shapes the backend can emit (`nn` = closed / consonant). */
export type VisemeName = 'aa' | 'ih' | 'ou' | 'ee' | 'oh' | 'nn';

export interface ServerSettings {
  host: string;
  port: number;
  cors_origins: string[];
  serve_frontend: boolean;
  frontend_dist: string;
  max_sessions: number;
}

export interface LogSettings {
  level: LogLevel;
  file: string;
  colorize: boolean;
}

export interface FallbackTarget {
  provider: LLMProvider;
  model: string;
  base_url: string;
  /**
   * A fingerprint of a key belonging to *this* link, or `""` when it needs none.
   * Never inherited from the provider above it: a local server must never
   * receive the credential of a cloud service.
   */
  api_key?: string;
}

export interface LLMSettings {
  provider: LLMProvider;
  model: string;
  base_url: string;
  /**
   * A fingerprint such as `gsk_1f2e****9c3a` once a key has been stored, or
   * `""` when the endpoint needs no authentication. Echoing it back means
   * "keep the key" — this page never receives the real value.
   */
  api_key: string;
  /** Whether a key is stored at all. The secret store is the only reader. */
  api_key_set?: boolean;
  temperature: number;
  /** Nucleus sampling; below 1 makes looping replies less likely. */
  top_p: number;
  max_tokens: number;
  /** Push the model off topics it has already covered. */
  presence_penalty: number;
  /** Damp tokens the model has already used in this reply. */
  frequency_penalty: number;
  timeout: number;
  max_context_messages: number;
  extra_body: Record<string, unknown>;
  system_prompt_suffix: string;
  /** Reject a reply whose first sentence parrots the user / repeats the last one. */
  quality_guard: boolean;
  echo_threshold: number;
  repeat_threshold: number;
  /**
   * Look for a running model server on this machine before falling back to the
   * offline brain. Only local ports are probed.
   */
  auto_discover: boolean;
  /**
   * Backups tried when this provider fails *before the first token* of a reply,
   * in order. Empty means a failure is reported as an error instead. The offline
   * "echo" brain is never one of them.
   */
  fallback_chain?: FallbackTarget[];
  /** Seconds a failed link is skipped before it is tried again. */
  fallback_cooldown?: number;
}

export interface MemorySettings {
  enabled: boolean;
  /** Relative to `backend/`, or absolute. */
  path: string;
  /** Turns between rolling summaries; 0 disables them. */
  summarise_every: number;
  inject: boolean;
}

/** What the companion currently remembers (`GET /api/memory`). */
export interface MemorySnapshot {
  version: number;
  name: string;
  facts: Record<string, string>;
  likes: string[];
  dislikes: string[];
  topics: string[];
  summary: string;
  turns: number;
  updated: number;
}

/**
 * A ready-made provider configuration (`GET /api/llm/presets`).
 *
 * `needs_key` false means it works as-is once the server is running; true means
 * the user has to paste a key from `key_url` first.
 */
export interface ModelPreset {
  id: string;
  label: string;
  provider: LLMProvider;
  base_url: string;
  model: string;
  needs_key: boolean;
  key_url?: string;
  note: string;
}

/** One probed local port (`POST /api/llm/discover`). */
export interface DiscoveredEndpoint {
  id: string;
  label: string;
  base_url: string;
  reachable: boolean;
  models: string[];
  picked_model: string;
  detail: string;
}

/** Result of a real round-trip (`POST /api/llm/test`). */
export interface LlmTestResult {
  ok: boolean;
  error: string;
  reply: string;
  latency_ms: number;
  provider?: string;
  base_url?: string;
  model?: string;
  models: string[];
}

export interface TTSSettings {
  engine: TTSEngine;
  voice: string;
  /**
   * Speak each reply in its own language by switching to a voice that speaks it
   * (Edge only). A voice that already matches the language always wins.
   */
  auto_voice: boolean;
  rate: string;
  pitch: string;
  volume: string;
  format: AudioFormat;
  sample_rate: number;
  chunk_ms: number;
  model: string;
  base_url: string;
  api_key: string;
}

export interface ASRSettings {
  engine: ASREngine;
  model: string;
  device: string;
  compute_type: string;
  language: string | null;
  beam_size: number;
  base_url: string;
  api_key: string;
  prompt: string;
  min_speech_seconds: number;
}

/** `mouth` drives one blendshape from the envelope; `visemes` plays shapes. */
export type LipsyncMode = 'mouth' | 'visemes';

export interface LipsyncSettings {
  /** `mouth` (one blendshape, amplitude driven) or `visemes` (shape timeline). */
  mode: LipsyncMode;
  enabled: boolean;
  /** Exponential-smoothing time constants in milliseconds (attack = rise). */
  attack_ms: number;
  release_ms: number;
  viseme_gain: number;
  amplitude_modulation: boolean;
  amplitude_floor: number;
  /** RMS window used for the `volumes` envelope (server side). */
  slice_ms: number;

  /* -- `mouth` mode ---------------------------------------------------- */
  /** Envelope level below which the mouth stays shut (0..1). */
  mouth_threshold: number;
  /** Extra drop below the threshold needed to close again (anti-flutter). */
  mouth_hysteresis: number;
  /** Gain applied to the envelope above the threshold. */
  mouth_scale: number;
  /** Ceiling for the openness, 0..1. */
  mouth_max: number;
  /** Minimum time (ms) a decided open/closed state is held. */
  mouth_min_dwell_ms: number;
}

export interface AvatarSettings {
  character: string;
  /** Empty -> the client uses its own placeholder. */
  vrm_url: string;
  camera_preset: CameraPreset;
  pixel_ratio: number;
  enable_shadows: boolean;
  show_stats: boolean;
}

export interface PublicConfig {
  server: ServerSettings;
  log: LogSettings;
  llm: LLMSettings;
  tts: TTSSettings;
  asr: ASRSettings;
  lipsync: LipsyncSettings;
  avatar: AvatarSettings;
  memory: MemorySettings;
}

/** Sections the settings UI is allowed to patch on a live session. */
export interface ConfigPatch {
  llm?: Partial<LLMSettings>;
  tts?: Partial<TTSSettings>;
  asr?: Partial<ASRSettings>;
  lipsync?: Partial<LipsyncSettings>;
  avatar?: Partial<AvatarSettings>;
  memory?: Partial<MemorySettings>;
}

export interface ExpressionMapping {
  preset: string;
  weight: number;
  hold_ms: number;
  override_blink: boolean;
}

export interface CharacterConfig {
  name: string;
  tagline: string;
  language: string;
  greeting: string;
  system_prompt: string;
  tts_voice: string;
  tts_rate: string;
  emotions: Record<string, ExpressionMapping>;
  actions: Record<string, string>;
}

/** Entry of `capabilities.characters` (the persona picker). */
export interface CharacterSummary {
  id: string;
  name: string;
  tagline: string;
  language: string;
}

/** Persona fields the settings UI may write (see `EDITABLE_CHARACTER_FIELDS`). */
export type EditableCharacterField =
  | 'name'
  | 'tagline'
  | 'language'
  | 'greeting'
  | 'system_prompt'
  | 'tts_voice'
  | 'tts_rate';

/**
 * `GET/PUT/DELETE /api/character` - the persona editor's payload.
 *
 * `shipped` is what `characters/<id>.yaml` says, i.e. exactly what "Reset"
 * restores; `overridden` names the fields an edit is currently winning on.
 */
export interface CharacterDetail {
  id: string;
  editable: EditableCharacterField[];
  /** Fields currently edited away from the shipped file. Empty = untouched. */
  overridden: EditableCharacterField[];
  /** What the shipped `.yaml` says - the values a Reset goes back to. */
  shipped: Record<EditableCharacterField, string>;
  character: CharacterConfig;
}

export interface AudioCapability {
  transport: string;
  sample_rate: number;
  chunk_ms: number;
  capture_sample_rate: number;
}

export interface Capabilities {
  llm: string[];
  tts: string[];
  asr: string[];
  characters: CharacterSummary[];
  audio: AudioCapability;
  visemes: VisemeName[] | string[];
  expressions: string[];
}

export interface Voice {
  name: string;
  locale: string;
  gender?: string;
}

export interface VrmModelEntry {
  name: string;
  url: string;
  size_mb: number;
  source: string;
}

export interface HealthReport {
  status: string;
  version: string;
  uptime_s: number;
  engines: { llm: string; tts: string; asr: string; model: string };
  available: { llm: string[]; tts: string[]; asr: string[] };
  sessions: number;
  preflight: {
    llm?: {
      ok: boolean;
      detail: string;
      /** True when a real model was found on this machine at startup. */
      auto_discovered?: boolean;
      /** True when the offline script answered because it fell back. */
      fallback?: boolean;
      /** True when replies come from a script rather than a model. */
      canned?: boolean;
      /** True when nothing is connected and no fallback was used. */
      action_required?: boolean;
      provider?: string;
      model?: string;
      base_url?: string;
    };
    tts?: { ok: boolean; detail: string };
    asr?: { ok: boolean; detail: string };
    audio?: { ok: boolean; detail: string };
  } & Record<string, unknown>;
}
