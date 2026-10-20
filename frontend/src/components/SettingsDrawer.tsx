/**
 * The settings drawer.
 *
 * Two very different things live behind this panel, and they are kept apart:
 *
 *  * **server settings** (LLM provider, voice, lip-sync tuning, ...) - patched
 *    live over the socket. Every change is applied optimistically and then
 *    confirmed by the server's `config.ack`, so the UI never feels laggy.
 *  * **client preferences** (avatar file, camera, motion, volume) - local only,
 *    persisted in the browser.
 *
 * The "Face" tab doubles as a QA tool: it exercises the avatar directly, with no
 * language model involved, which is the fastest way to tune lip-sync or check
 * that a model's blendshapes are named the way the VRM spec says they are.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { getConversation } from '../core/conversation';
import { isStreamerMode } from '../config';
import { discoverModels, fetchCharacter, fetchMemory, fetchModelPresets, fetchVrmModels, fetchVoices, forgetMemory, resetCharacter, saveCharacter, saveLlmKey, clearLlmKey, testLlm } from '../net/api';
import { useAppStore } from '../state/store';
import { EMOTION_PRESETS, MOTION_ACTIONS, type MotionOptions } from '../vrm/constants';
import { DEFAULT_APPEARANCE, DEFAULT_SHIRT_COLOR, LOOK_PRESETS } from '../vrm/AppearanceController';
import type { AnimationOptions } from '../vrm/animation/types';
import type { BrowserASRState } from '../audio/BrowserASR';
import type {
  ASRSettings,
  CameraPreset,
  CharacterDetail,
  ConfigPatch,
  DiscoveredEndpoint,
  EditableCharacterField,
  LlmTestResult,
  LLMSettings,
  LipsyncMode,
  MemorySnapshot,
  ModelPreset,
  PublicConfig,
  TTSSettings,
  Voice,
  VrmModelEntry,
} from '../types/config';

type Tab = 'avatar' | 'persona' | 'voice' | 'model' | 'memory' | 'input' | 'face' | 'motion' | 'about';

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'avatar', label: 'Avatar' },
  { id: 'persona', label: 'Persona' },
  { id: 'voice', label: 'Voice' },
  { id: 'model', label: 'Model' },
  { id: 'memory', label: 'Memory' },
  { id: 'input', label: 'Input' },
  { id: 'face', label: 'Face' },
  { id: 'motion', label: 'Motion' },
  { id: 'about', label: 'About' },
];

/** Voice lists are expensive to fetch; keep them for the session. */
const voiceCache = new Map<string, Voice[]>();

/**
 * Plain-language descriptions of the browser recogniser's states. The point of
 * the whole `BrowserASR` rewrite is that the user can see *why* nothing is being
 * transcribed instead of watching a mic button that silently does nothing.
 */
const ASR_STATE_LABELS: Record<BrowserASRState, string> = {
  idle: 'Microphone idle.',
  starting: 'Starting speech recognition…',
  listening: 'Listening — speak normally and pause when you are done.',
  retrying: 'Speech recognition dropped; retrying…',
  paused: 'Paused while the avatar speaks.',
  stopped: 'Microphone off.',
  unavailable: 'This browser has no built-in speech recognition.',
  error: 'Speech recognition stopped.',
};

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="row">
      <span className="row__label">
        {label}
        {hint ? <em>{hint}</em> : null}
      </span>
      <span className="row__control">{children}</span>
    </label>
  );
}

function Slider({
  value,
  min,
  max,
  step = 1,
  suffix = '',
  onChange,
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  return (
    <span className="slider">
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <output>
        {Number.isInteger(value) ? value : value.toFixed(2)}
        {suffix}
      </output>
    </span>
  );
}

/**
 * A colour swatch. `value` may be `''` ("leave the model as authored"), in
 * which case the swatch previews the fallback so the row never looks broken.
 */
function ColorInput({
  label,
  value,
  fallback = '#ffffff',
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  fallback?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <input
      type="color"
      aria-label={label}
      title={label}
      style={{ width: 52, height: 28, padding: 0 }}
      disabled={disabled}
      value={value || fallback}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

/** A titled group of rows, so a long tab stays scannable. */function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <h3 className="section__title">
        {title}
        {hint ? <em>{hint}</em> : null}
      </h3>
      {children}
    </section>
  );
}

export function SettingsDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>('avatar');
  const [voices, setVoices] = useState<Voice[]>([]);
  const [models, setModels] = useState<VrmModelEntry[]>([]);
  const [localVrm, setLocalVrm] = useState('');
  const [manifestDraft, setManifestDraft] = useState('');

  /* model setup */
  const [presets, setPresets] = useState<ModelPreset[]>([]);
  const [endpoints, setEndpoints] = useState<DiscoveredEndpoint[]>([]);
  const [scanning, setScanning] = useState(false);
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [apiKeyDraft, setApiKeyDraft] = useState('');
  /** Result of the last "Save key" — a fingerprint, never the key itself. */
  const [keyStatus, setKeyStatus] = useState('');
  const [modelDraft, setModelDraft] = useState('');
  const [baseUrlDraft, setBaseUrlDraft] = useState('');

  /* memory */
  const [memory, setMemory] = useState<MemorySnapshot | null>(null);
  const [memoryPath, setMemoryPath] = useState('');
  const [memoryBusy, setMemoryBusy] = useState(false);

  /* persona */
  const [persona, setPersona] = useState<CharacterDetail | null>(null);
  const [personaDraft, setPersonaDraft] = useState<Partial<Record<EditableCharacterField, string>>>({});
  const [personaStatus, setPersonaStatus] = useState('');
  const [personaBusy, setPersonaBusy] = useState(false);

  const config = useAppStore((state) => state.config);
  const prefs = useAppStore((state) => state.prefs);
  const capabilities = useAppStore((state) => state.capabilities);
  const metrics = useAppStore((state) => state.metrics);
  const armsAlreadyDown = useAppStore((state) => state.armsAlreadyDown);
  const setPrefs = useAppStore((state) => state.setPrefs);
  const setMotion = useAppStore((state) => state.setMotion);
  const setAnimation = useAppStore((state) => state.setAnimation);
  const setAppearance = useAppStore((state) => state.setAppearance);
  const animationStatus = useAppStore((state) => state.animationStatus);
  const animationClips = useAppStore((state) => state.animationClips);
  const asrState = useAppStore((state) => state.asrState);
  const asrDetail = useAppStore((state) => state.asrDetail);
  const setConfig = useAppStore((state) => state.setConfig);
  const settingsTabRequest = useAppStore((state) => state.settingsTabRequest);
  const requestSettingsTab = useAppStore((state) => state.requestSettingsTab);

  const conversation = getConversation();
  const engine = config?.tts.engine ?? 'edge';
  const streamer = prefs.streamer ?? isStreamerMode();
  // Backups the backend will try if the provider above does not answer. Shown
  // read-only: the chain is a `.env` setting, and "what happens if Groq is down"
  // is the one thing a user cannot guess from the provider fields.
  const chain = config?.llm.fallback_chain ?? [];

  // Honour "open settings on this tab" (e.g. the no-model notice's button).
  useEffect(() => {
    if (!open || !settingsTabRequest) return;
    if (TABS.some((entry) => entry.id === settingsTabRequest)) {
      setTab(settingsTabRequest as Tab);
    }
    requestSettingsTab(null);
  }, [open, settingsTabRequest, requestSettingsTab]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void fetchVrmModels()
      .then((list) => {
        if (!cancelled) setModels(list);
      })
      .catch(() => setModels([]));
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const cached = voiceCache.get(engine);
    if (cached) {
      setVoices(cached);
      return;
    }
    let cancelled = false;
    void fetchVoices(engine)
      .then((list) => {
        voiceCache.set(engine, list);
        if (!cancelled) setVoices(list);
      })
      .catch(() => {
        if (!cancelled) setVoices([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open, engine]);

  useEffect(() => {
    if (open) setLocalVrm(useAppStore.getState().prefs.vrmUrl);
  }, [open]);

  useEffect(() => {
    if (open) setManifestDraft(useAppStore.getState().prefs.animation.manifestUrl);
  }, [open]);

  // The preset list is small and static - one fetch per open is plenty.
  useEffect(() => {
    if (!open) return;
    void fetchModelPresets()
      .then(setPresets)
      .catch(() => setPresets([]));
  }, [open]);

  // Seed the editable fields from whatever the server is using right now.
  useEffect(() => {
    if (!open) return;
    const current = useAppStore.getState().config;
    setModelDraft(current?.llm.model ?? '');
    setBaseUrlDraft(current?.llm.base_url ?? '');
    setApiKeyDraft('');
    setKeyStatus('');
    setTestResult(null);
    setEndpoints([]);
  }, [open]);

  const loadMemory = useCallback((): void => {
    void fetchMemory()
      .then((payload) => {
        setMemory(payload.memory);
        setMemoryPath(payload.path);
      })
      .catch(() => setMemory(null));
  }, []);

  useEffect(() => {
    if (!open || tab !== 'memory') return;
    loadMemory();
  }, [open, tab, loadMemory]);

  /* ------------------------------------------------------------------ */
  /*  Persona                                                            */
  /* ------------------------------------------------------------------ */
  const personaId = config?.avatar.character ?? 'default';

  const loadPersona = useCallback((): void => {
    setPersonaStatus('');
    void fetchCharacter(personaId)
      .then((detail) => {
        setPersona(detail);
        setPersonaDraft({});
      })
      .catch((error: unknown) => {
        setPersona(null);
        setPersonaStatus(error instanceof Error ? error.message : 'could not load the persona');
      });
  }, [personaId]);

  useEffect(() => {
    if (open && tab === 'persona') loadPersona();
  }, [open, tab, loadPersona]);

  /** Dirty fields only: sending everything would overwrite untouched values. */
  const personaFields = (): Partial<Record<EditableCharacterField, string>> =>
    Object.fromEntries(
      Object.entries(personaDraft).filter((entry): entry is [string, string] => entry[1] !== undefined),
    ) as Partial<Record<EditableCharacterField, string>>;

  const savePersona = (): void => {
    const fields = personaFields();
    if (Object.keys(fields).length === 0) {
      setPersonaStatus('nothing to save');
      return;
    }
    setPersonaBusy(true);
    setPersonaStatus('saving…');
    void saveCharacter(personaId, fields)
      .then((detail) => {
        setPersona(detail);
        setPersonaDraft({});
        setPersonaStatus('saved — the running session reloaded it');
        conversation.reloadPersona();
      })
      .catch((error: unknown) =>
        setPersonaStatus(error instanceof Error ? `save failed: ${error.message}` : 'save failed'),
      )
      .finally(() => setPersonaBusy(false));
  };

  const resetPersona = (): void => {
    setPersonaBusy(true);
    setPersonaStatus('resetting…');
    void resetCharacter(personaId)
      .then((detail) => {
        setPersona(detail);
        setPersonaDraft({});
        setPersonaStatus('back to the file on disk');
        conversation.reloadPersona();
      })
      .catch((error: unknown) =>
        setPersonaStatus(error instanceof Error ? `reset failed: ${error.message}` : 'reset failed'),
      )
      .finally(() => setPersonaBusy(false));
  };

  /** One editable field: a one-line input, or a text area for the prompt. */
  const personaField = (
    key: EditableCharacterField,
    label: string,
    hint?: string,
    rows = 0,
  ): ReactNode => {
    const saved = persona?.character[key] ?? '';
    const value = personaDraft[key] ?? saved;
    const edited = persona?.overridden.includes(key) ?? false;
    // Dirty is measured against what the server has *now*, not against the
    // shipped file: emptying the greeting and then typing it back are two
    // different edits, and the second one has to be sendable.
    const change = (next: string): void =>
      setPersonaDraft((current) => ({
        ...current,
        [key]: next === saved ? undefined : next,
      }));
    return (
      <Row key={key} label={label} hint={edited ? `${hint ? `${hint} · ` : ''}edited` : hint}>
        {rows > 0 ? (
          <textarea
            rows={rows}
            value={value}
            spellCheck={false}
            onChange={(event) => change(event.target.value)}
          />
        ) : (
          <input
            type="text"
            value={value}
            spellCheck={false}
            onChange={(event) => change(event.target.value)}
          />
        )}
      </Row>
    );
  };

  /** Optimistic server-side update: write locally, then tell the backend. */
  const update = <S extends keyof ConfigPatch>(section: S, values: NonNullable<ConfigPatch[S]>): void => {
    if (!config) return;
    const merged = { ...config, [section]: { ...config[section], ...values } } as unknown as PublicConfig;
    setConfig(merged);
    conversation.patchConfig({ [section]: values } as ConfigPatch);
  };

  /** Write a value into the local store *without* telling the server. */
  const patchLocalLlm = (values: Partial<LLMSettings>): void => {
    const current = useAppStore.getState().config;
    if (!current) return;
    setConfig({ ...current, llm: { ...current.llm, ...values } });
  };

  /**
   * Persist the API key.
   *
   * This is the one setting that does **not** travel as a WebSocket config patch:
   * a patch is a per-session overlay and is forgotten when the server restarts,
   * and a credential belongs in the secret store instead. The reply carries a
   * fingerprint (`gsk_1f2e****9c3a`) and never the value, which is also what the
   * row displays afterwards.
   */
  const saveApiKey = (): void => {
    const value = apiKeyDraft.trim();
    if (!value) return;
    setApiKeyDraft('');
    setKeyStatus('saving…');
    void saveLlmKey(value)
      .then((result) => {
        patchLocalLlm({ api_key: result.api_key, api_key_set: result.api_key_set });
        setKeyStatus(
          result.api_key ? `saved — using ${result.api_key}. Test the connection below.` : 'saved',
        );
      })
      .catch((error: unknown) => {
        setKeyStatus(error instanceof Error ? `save failed: ${error.message}` : 'save failed');
      });
  };

  const applyMotion = (values: Partial<MotionOptions>): void => {
    setMotion(values);
    conversation.applyPrefsToViewer();
  };

  const applyAnimation = (values: Partial<AnimationOptions>): void => {
    setAnimation(values);
    conversation.refreshAnimation();
  };

  /* ------------------------------------------------------------------ */
  /*  Model setup helpers                                               */
  /* ------------------------------------------------------------------ */

  /** Apply a complete provider setup (preset / detected endpoint) at once. */
  const useLlm = (values: Partial<LLMSettings>): void => {
    update('llm', values);
    setTestResult(null);
  };

  const scanForModels = (): void => {
    setScanning(true);
    setTestResult(null);
    void discoverModels()
      .then(setEndpoints)
      .catch(() => setEndpoints([]))
      .finally(() => setScanning(false));
  };

  /**
   * Round-trip a real completion through the *proposed* settings.
   *
   * An empty API-key draft is omitted so the server reuses the stored one
   * (`apply_patch` keeps a secret unless it is explicitly replaced).
   */
  const runTest = (override?: Partial<LLMSettings>): void => {
    const proposed: Partial<LLMSettings> = override ?? {
      model: modelDraft.trim() || config?.llm.model,
      base_url: baseUrlDraft.trim() || config?.llm.base_url,
    };
    if (!override && apiKeyDraft.trim()) proposed.api_key = apiKeyDraft.trim();

    setTesting(true);
    setTestResult(null);
    void testLlm(proposed)
      .then(setTestResult)
      .catch((error: unknown) =>
        setTestResult({
          ok: false,
          error: error instanceof Error ? error.message : String(error),
          reply: '',
          latency_ms: 0,
          models: [],
        }),
      )
      .finally(() => setTesting(false));
  };

  const groupedVoices = useMemo(() => {
    const buckets = new Map<string, Voice[]>();
    for (const voice of voices) {
      const locale = voice.locale || 'other';
      const bucket = buckets.get(locale);
      if (bucket) bucket.push(voice);
      else buckets.set(locale, [voice]);
    }
    return [...buckets.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [voices]);

  if (!open) return null;

  const tabBody = (): ReactNode => {
    if (!config) return <p className="muted">Waiting for the backend…</p>;

    switch (tab) {
      case 'avatar':
        return (
          <>
            <Row label="Model file" hint="a .vrm in frontend/public/models/">
              <select
                value={models.some((model) => model.url === prefs.vrmUrl) ? prefs.vrmUrl : ''}
                onChange={(event) => {
                  setPrefs({ vrmUrl: event.target.value });
                  setLocalVrm(event.target.value);
                  conversation.applyPrefsToViewer();
                }}
              >
                <option value="">Default (placeholder / server)</option>
                {models.map((model) => (
                  <option key={model.url} value={model.url}>
                    {model.name} · {model.size_mb} MB
                  </option>
                ))}
              </select>
            </Row>
            <Row label="Model URL" hint="any .vrm, local or remote">
              <input
                type="url"
                value={localVrm}
                placeholder="https://…/avatar.vrm"
                onChange={(event) => setLocalVrm(event.target.value)}
                onBlur={() => {
                  const url = localVrm.trim();
                  if (url === useAppStore.getState().prefs.vrmUrl) return;
                  setPrefs({ vrmUrl: url });
                  conversation.applyPrefsToViewer();
                }}
              />
            </Row>
            <Row label="Camera" hint="framing of the shot">
              <select
                value={prefs.cameraPreset ?? config.avatar.camera_preset}
                onChange={(event) => {
                  setPrefs({ cameraPreset: event.target.value as CameraPreset });
                  conversation.applyPrefsToViewer();
                }}
              >
                <option value="face">Face</option>
                <option value="bust">Bust</option>
                <option value="full">Full body</option>
              </select>
            </Row>
            <Row label="Pixel ratio" hint="drop to 1 if the fan spins up">
              <Slider
                min={1}
                max={3}
                step={0.5}
                value={prefs.pixelRatio ?? config.avatar.pixel_ratio}
                onChange={(value) => {
                  setPrefs({ pixelRatio: value });
                  conversation.applyPrefsToViewer();
                }}
              />
            </Row>
            <Row label="Shadows" hint="costly on integrated GPUs">
              <input
                type="checkbox"
                checked={config.avatar.enable_shadows}
                onChange={(event) => update('avatar', { enable_shadows: event.target.checked })}
              />
            </Row>
            <Row label="Performance overlay">
              <input
                type="checkbox"
                checked={prefs.showStats ?? config.avatar.show_stats}
                onChange={(event) => setPrefs({ showStats: event.target.checked })}
              />
            </Row>
            <Row label="Output volume">
              <Slider
                min={0}
                max={1}
                step={0.05}
                value={prefs.volume}
                onChange={(value) => {
                  setPrefs({ volume: value });
                  conversation.setVolume(value);
                }}
              />
            </Row>

            <Section
              title="Look — hair, eyes, skin & outfit"
              hint="model-side only — stored in this browser, never sent to the server"
            >
              <Row label="Ready-made looks" hint="one click, then tweak any swatch below">
                <div className="chip-row">
                  {Object.entries(LOOK_PRESETS).map(([id, preset]) => (
                    <button
                      key={id}
                      type="button"
                      className="btn btn--chip"
                      onClick={() => {
                        setAppearance(preset.look);
                        conversation.applyPrefsToViewer();
                      }}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
              </Row>

              <Row label="Top / shirt" hint="multiplied into the fabric texture, so folds and seams survive">
                <ColorInput
                  label="Top colour"
                  value={prefs.appearance.shirtColor}
                  fallback={DEFAULT_SHIRT_COLOR}
                  disabled={!prefs.appearance.shirtEnabled}
                  onChange={(value) => {
                    setAppearance({ shirtColor: value, shirtEnabled: true });
                    conversation.applyPrefsToViewer();
                  }}
                />
                <label className="muted">
                  <input
                    type="checkbox"
                    aria-label="Tint the top"
                    checked={prefs.appearance.shirtEnabled}
                    onChange={(event) => {
                      setAppearance({ shirtEnabled: event.target.checked });
                      conversation.applyPrefsToViewer();
                    }}
                  />{' '}
                  tint
                </label>
              </Row>

              <Row label="Hair" hint="recoloured from the texture's own shading, so a brown model can go pink">
                <ColorInput
                  label="Hair colour"
                  value={prefs.appearance.hairColor}
                  fallback={'#ffa8c6'}
                  onChange={(value) => {
                    setAppearance({ hairColor: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>

              <Row label="Eyes" hint="iris colour; the highlight and the white are left alone">
                <ColorInput
                  label="Eye colour"
                  value={prefs.appearance.eyeColor}
                  fallback={'#ff8fb0'}
                  onChange={(value) => {
                    setAppearance({ eyeColor: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>

              <Row label="Brows" hint="empty follows the hair colour, which is what looks deliberate">
                <ColorInput
                  label="Brow colour"
                  value={prefs.appearance.browColor}
                  fallback={prefs.appearance.hairColor || '#ffa8c6'}
                  onChange={(value) => {
                    setAppearance({ browColor: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
                <button
                  type="button"
                  className="btn btn--chip"
                  onClick={() => {
                    setAppearance({ browColor: '' });
                    conversation.applyPrefsToViewer();
                  }}
                >
                  follow hair
                </button>
              </Row>

              <Row label="Skin tone" hint="multiply can only darken — this warms or tans, it cannot lighten">
                <ColorInput
                  label="Skin tone"
                  value={prefs.appearance.skinTone}
                  fallback={'#fff4ef'}
                  onChange={(value) => {
                    setAppearance({ skinTone: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>

              <Row label="Bottoms" hint="only useful if your model's skirt/trousers texture is not already dark">
                <ColorInput
                  label="Bottoms colour"
                  value={prefs.appearance.outfitBottom}
                  fallback={'#ffffff'}
                  onChange={(value) => {
                    setAppearance({ outfitBottom: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>

              <Row label="Shoes">
                <ColorInput
                  label="Shoe colour"
                  value={prefs.appearance.shoesColor}
                  fallback={'#ffffff'}
                  onChange={(value) => {
                    setAppearance({ shoesColor: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>

              <Row label="Cheek blush" hint="this model ships no blush blendshape, so it is painted into the face texture">
                <input
                  type="checkbox"
                  aria-label="Cheek blush"
                  checked={prefs.appearance.blushEnabled}
                  onChange={(event) => {
                    setAppearance({ blushEnabled: event.target.checked });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>
              <Row label="Blush strength" hint="scales the per-emotion amount (shy peaks at 0.9)">
                <Slider
                  min={0}
                  max={1}
                  step={0.05}
                  value={prefs.appearance.blushIntensity}
                  onChange={(value) => {
                    setAppearance({ blushIntensity: value });
                    conversation.applyPrefsToViewer();
                  }}
                />
              </Row>

              <Row label="Reset" hint="every swatch back to the pastel default, or use 'As authored' above">
                <button
                  type="button"
                  className="btn btn--chip"
                  onClick={() => {
                    setAppearance({ ...DEFAULT_APPEARANCE });
                    conversation.applyPrefsToViewer();
                  }}
                >
                  Reset look
                </button>
              </Row>

              <ul className="notice">
                <li>
                  Nothing looks different? Open DevTools — the console prints a table of every material
                  on the model with its role and how it is tinted, so a differently named avatar can be
                  diagnosed instead of guessed at.
                </li>
              </ul>
            </Section>

            <Section title="Streaming (OBS)" hint="hide every control, keep only the avatar">
              <Row label="Streamer mode" hint="transparent, chrome-free capture surface">
                <input
                  type="checkbox"
                  checked={streamer}
                  onChange={(event) => setPrefs({ streamer: event.target.checked })}
                />
              </Row>
              <ul className="notice">
                <li>
                  Add a <strong>Browser</strong> source in OBS pointed at{' '}
                  <code>{`${window.location.origin}/?streamer=1`}</code> — the page opens
                  chrome-free and starts on its own. Turn on <em>Shutdown source when not
                  visible</em> so the model reloads when the scene changes.
                </li>
                <li>
                  For an animated overlay, keep transparency on; for a full-screen background,
                  add a colour source behind this one.
                </li>
              </ul>
            </Section>
          </>
        );

      case 'voice':
        return (
          <>
            <Row label="Engine">
              <select
                value={config.tts.engine}
                onChange={(event) => update('tts', { engine: event.target.value as TTSSettings['engine'] })}
              >
                {(capabilities?.tts ?? ['edge']).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </Row>
            <Row label="Voice" hint={`${voices.length} available`}>
              {config.tts.engine === 'browser' ? (
                <select value={config.tts.voice} onChange={(event) => update('tts', { voice: event.target.value })}>
                  <option value="">System default</option>
                  {conversation.speech.getVoices().map((voice) => (
                    <option key={voice.name} value={voice.name}>
                      {voice.name} ({voice.locale})
                    </option>
                  ))}
                </select>
              ) : groupedVoices.length > 0 ? (
                <select value={config.tts.voice} onChange={(event) => update('tts', { voice: event.target.value })}>
                  {groupedVoices.map(([locale, list]) => (
                    <optgroup key={locale} label={locale}>
                      {list.map((voice) => (
                        <option key={voice.name} value={voice.name}>
                          {voice.name}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={config.tts.voice}
                  onChange={(event) => update('tts', { voice: event.target.value })}
                />
              )}
            </Row>
            <Row label="Rate" hint="+10% / -5%">
              <input
                type="text"
                value={config.tts.rate}
                onChange={(event) => update('tts', { rate: event.target.value })}
              />
            </Row>
            <Row
              label="Speak my language"
              hint="follow the reply's language with a matching voice (Edge)"
            >
              <input
                type="checkbox"
                checked={config.tts.auto_voice}
                disabled={config.tts.engine !== 'edge'}
                onChange={(event) => update('tts', { auto_voice: event.target.checked })}
              />
            </Row>
            <Row label="Pitch" hint="+20Hz">
              <input
                type="text"
                value={config.tts.pitch}
                onChange={(event) => update('tts', { pitch: event.target.value })}
              />
            </Row>
            <Row label="Transport" hint="how audio reaches the browser">
              <select
                value={config.tts.format}
                onChange={(event) => update('tts', { format: event.target.value as TTSSettings['format'] })}
              >
                <option value="pcm_s16le">pcm_s16le (lowest latency)</option>
                <option value="mp3">mp3</option>
              </select>
            </Row>
            {config.tts.engine === 'openai' ? (
              <>
                <Row label="TTS base URL">
                  <input
                    type="text"
                    value={config.tts.base_url}
                    onChange={(event) => update('tts', { base_url: event.target.value })}
                  />
                </Row>
                <Row label="TTS API key">
                  <input
                    type="password"
                    value={config.tts.api_key}
                    placeholder="sk-…"
                    onChange={(event) => update('tts', { api_key: event.target.value })}
                  />
                </Row>
              </>
            ) : null}
          </>
        );

      case 'model':
        return (
          <>
            <Section title="Connection" hint="this is where the answers come from">
              {config.llm.provider === 'echo' ? (
                <ul className="notice notice--warn">
                  <li>
                    <strong>Offline demo brain.</strong> Replies come from a script, not a model —
                    they react to your wording but they cannot answer questions. Connect a real
                    model below to fix that.
                  </li>
                </ul>
              ) : null}

              <p className="muted small">
                Now using <strong>{config.llm.provider}</strong> · {config.llm.model || '(no model)'}
                <br />
                <code>{config.llm.base_url || '(no url)'}</code>
              </p>

              <div className="row row--stack">
                <button type="button" className="btn" onClick={scanForModels} disabled={scanning}>
                  {scanning ? 'Scanning…' : 'Detect models on this machine'}
                </button>
                <button type="button" className="btn" onClick={() => runTest()} disabled={testing}>
                  {testing ? 'Testing…' : 'Test connection'}
                </button>
              </div>

              {endpoints.length ? (
                <ul className="list">
                  {endpoints.map((endpoint) => (
                    <li key={endpoint.id} className="list__item">
                      <span>
                        <strong>{endpoint.label}</strong>
                        <em className={endpoint.reachable ? 'ok' : 'muted'}>
                          {endpoint.reachable
                            ? `${endpoint.models.length} model(s) · ${endpoint.picked_model}`
                            : endpoint.detail || 'not running'}
                        </em>
                      </span>
                      <button
                        type="button"
                        className="btn btn--small"
                        disabled={!endpoint.reachable || !endpoint.picked_model}
                        onClick={() =>
                          useLlm({
                            provider: 'openai',
                            base_url: endpoint.base_url,
                            model: endpoint.picked_model,
                            api_key: 'not-needed',
                          })
                        }
                      >
                        Use
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}

              {testResult ? (
                <p className={testResult.ok ? 'notice notice--ok' : 'notice notice--warn'}>
                  {testResult.ok
                    ? `Works — replied “${testResult.reply}” in ${Math.round(testResult.latency_ms)} ms.`
                    : `Failed: ${testResult.error}`}
                  {testResult.models.length ? (
                    <>
                      <br />
                      Models offered: {testResult.models.slice(0, 12).join(', ')}
                    </>
                  ) : null}
                </p>
              ) : null}
            </Section>

            <Section title="Ready-made setups" hint="local first, then free hosted">
              <ul className="list">
                {presets.map((preset) => (
                  <li key={preset.id} className="list__item">
                    <span>
                      <strong>{preset.label}</strong>
                      <em className={preset.needs_key ? 'muted' : 'ok'}>
                        {preset.needs_key ? 'needs an API key' : 'no key, works offline'}
                      </em>
                      <br />
                      <span className="muted small">{preset.note}</span>
                    </span>
                    <span className="list__actions">
                      {preset.key_url ? (
                        <a className="btn btn--small" href={preset.key_url} target="_blank" rel="noreferrer">
                          Get a key
                        </a>
                      ) : null}
                      <button
                        type="button"
                        className="btn btn--small"
                        onClick={() =>
                          useLlm({
                            provider: preset.provider,
                            base_url: preset.base_url,
                            model: preset.model,
                            api_key: preset.needs_key ? '' : 'not-needed',
                          })
                        }
                      >
                        Use
                      </button>
                    </span>
                  </li>
                ))}
              </ul>
            </Section>

            <Section title="Provider settings">
              <Row label="Provider">
                <select
                  value={config.llm.provider}
                  onChange={(event) => update('llm', { provider: event.target.value as LLMSettings['provider'] })}
                >
                  {(capabilities?.llm ?? ['openai']).map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </Row>
              <Row label="Model" hint="llama3.1:8b · gpt-4o-mini · deepseek-chat">
                <input
                  type="text"
                  value={modelDraft}
                  onChange={(event) => setModelDraft(event.target.value)}
                  onBlur={() => {
                    const value = modelDraft.trim();
                    if (value && value !== config.llm.model) update('llm', { model: value });
                  }}
                />
              </Row>
              <Row label="Base URL" hint="Ollama: http://localhost:11434/v1">
                <input
                  type="text"
                  value={baseUrlDraft}
                  onChange={(event) => setBaseUrlDraft(event.target.value)}
                  onBlur={() => {
                    const value = baseUrlDraft.trim();
                    if (value && value !== config.llm.base_url) update('llm', { base_url: value });
                  }}
                />
              </Row>
              <Row
                label="If unreachable"
                hint={
                  chain.length > 0
                    ? 'answers anyway, and tells you it did — edit VTUBER_LLM__FALLBACK_CHAIN in backend/.env'
                    : 'no backup — a failure is reported as an error instead'
                }
              >
                {chain.length > 0 ? (
                  <span className="muted">
                    {[`${config.llm.provider}/${config.llm.model}`, ...chain.map((link) => `${link.provider}/${link.model}`)].join(
                      ' → ',
                    )}
                  </span>
                ) : (
                  <span className="muted">nothing configured</span>
                )}
              </Row>
              <Row
                label="API key"
                hint={
                  config.llm.api_key_set
                    ? `stored: ${config.llm.api_key}`
                    : 'written to backend/secrets.json — never sent back to this page'
                }
              >
                <input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={apiKeyDraft}
                  placeholder={config.llm.api_key_set ? '••••••• (unchanged)' : 'gsk_…'}
                  onChange={(event) => setApiKeyDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') saveApiKey();
                  }}
                />
                <button type="button" className="btn" disabled={!apiKeyDraft.trim()} onClick={saveApiKey}>
                  Save
                </button>
                {config.llm.api_key_set ? (
                  <button
                    type="button"
                    className="btn"
                    onClick={() => {
                      setKeyStatus('clearing…');
                      void clearLlmKey()
                        .then(() => {
                          patchLocalLlm({ api_key: '', api_key_set: false });
                          setKeyStatus('cleared');
                        })
                        .catch((error: unknown) => {
                          setKeyStatus(error instanceof Error ? error.message : 'failed');
                        });
                    }}
                  >
                    Clear
                  </button>
                ) : null}
              </Row>
              {keyStatus ? (
                <p style={{ fontSize: 12, margin: '2px 0 8px', color: 'var(--text-dim)' }}>{keyStatus}</p>
              ) : null}
              <Row
                label="Auto-detect a local model"
                hint="scan the usual local ports at startup"
              >
                <input
                  type="checkbox"
                  checked={config.llm.auto_discover}
                  onChange={(event) => update('llm', { auto_discover: event.target.checked })}
                />
              </Row>
            </Section>

            <Section title="Generation" hint="how the model writes">
              <Row label="Temperature">
                <Slider
                  min={0}
                  max={2}
                  step={0.05}
                  value={config.llm.temperature}
                  onChange={(value) => update('llm', { temperature: value })}
                />
              </Row>
              <Row label="Top-p" hint="lower = more focused, less rambling">
                <Slider
                  min={0.1}
                  max={1}
                  step={0.05}
                  value={config.llm.top_p}
                  onChange={(value) => update('llm', { top_p: value })}
                />
              </Row>
              <Row label="Presence penalty" hint="pushes the model onto new topics">
                <Slider
                  min={0}
                  max={1.5}
                  step={0.05}
                  value={config.llm.presence_penalty}
                  onChange={(value) => update('llm', { presence_penalty: value })}
                />
              </Row>
              <Row label="Frequency penalty" hint="stops it reusing its own words">
                <Slider
                  min={0}
                  max={1.5}
                  step={0.05}
                  value={config.llm.frequency_penalty}
                  onChange={(value) => update('llm', { frequency_penalty: value })}
                />
              </Row>
              <Row label="Max tokens">
                <Slider
                  min={64}
                  max={2048}
                  step={64}
                  value={config.llm.max_tokens}
                  onChange={(value) => update('llm', { max_tokens: value })}
                />
              </Row>
              <Row label="Context window" hint="messages kept in history">
                <Slider
                  min={4}
                  max={64}
                  value={config.llm.max_context_messages}
                  onChange={(value) => update('llm', { max_context_messages: value })}
                />
              </Row>
              <Row
                label="Reply guard"
                hint="regenerate once when the reply only parrots you"
              >
                <input
                  type="checkbox"
                  checked={config.llm.quality_guard}
                  onChange={(event) => update('llm', { quality_guard: event.target.checked })}
                />
              </Row>
              <Row label="Style suffix" hint="appended to the persona prompt">
                <textarea
                  rows={3}
                  value={config.llm.system_prompt_suffix}
                  onChange={(event) => update('llm', { system_prompt_suffix: event.target.value })}
                />
              </Row>
            </Section>
          </>
        );

      case 'persona':
        return (
          <>
            <ul className="notice">
              <li>
                This is who she <em>is</em>: her name, how she greets you and the prompt that shapes every reply.
                Edits are saved next to the persona file as <code>{personaId}.user.json</code> — the shipped{' '}
                <code>.yaml</code> is never touched, and <strong>Reset</strong> throws the edits away.
              </li>
              <li>
                <strong>Language</strong> is the one she <em>defaults</em> to. She mirrors whatever you write
                anyway — English, Bengali (বাংলা) or Banglish — and the voice follows the language, so a Bengali
                reply is spoken by a Bengali voice.
              </li>
            </ul>

            {personaStatus ? <p className="muted">{personaStatus}</p> : null}
            {!persona ? (
              <p className="muted">Loading the persona…</p>
            ) : (
              <>
                <Section title="Identity" hint={`${persona.id} · ${persona.overridden.length} edited field(s)`}>
                  {personaField('name', 'Name')}
                  {personaField('tagline', 'Tagline', 'shown under the name')}
                  {personaField('language', 'Language', 'en · bn · ja — she mirrors you regardless')}
                  {personaField('greeting', 'Greeting', 'spoken on connect; empty stays silent', 2)}
                </Section>

                <Section title="Personality" hint="sent as her system prompt, before everything else">
                  {personaField('system_prompt', 'System prompt', 'who she is, how she talks', 10)}
                </Section>

                <Section title="Voice" hint="edge / openai / browser — see the Voice tab">
                  {personaField('tts_voice', 'Voice', 'empty = the engine default')}
                  {personaField('tts_rate', 'Rate', 'e.g. +4%')}
                </Section>

                <div className="row">
                  <span className="row__label" />
                  <span className="row__control">
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={personaBusy || Object.keys(personaDraft).length === 0}
                      onClick={savePersona}
                    >
                      Save persona
                    </button>
                    <button type="button" className="btn" disabled={personaBusy} onClick={resetPersona}>
                      Reset
                    </button>
                  </span>
                </div>
              </>
            )}
          </>
        );

      case 'memory':
        return (
          <>
            <ul className="notice">
              <li>
                The companion remembers your name and the things you mention, so it can pick a
                thread back up later. It is stored as plain JSON you can read and edit yourself.
              </li>
            </ul>

            <Row label="Remember things" hint="off = no profile is read or written">
              <input
                type="checkbox"
                checked={config.memory.enabled}
                onChange={(event) => update('memory', { enabled: event.target.checked })}
              />
            </Row>
            <Row label="Use it in replies" hint="inject what it knows into the prompt">
              <input
                type="checkbox"
                checked={config.memory.inject}
                onChange={(event) => update('memory', { inject: event.target.checked })}
              />
            </Row>
            <Row label="Summarise every" hint="turns between rolling summaries (0 = never)">
              <Slider
                min={0}
                max={30}
                value={config.memory.summarise_every}
                onChange={(value) => update('memory', { summarise_every: value })}
              />
            </Row>

            {memory ? (
              <>
                <h3 className="section__title">
                  What it knows
                  <em>
                    {memory.turns} turn(s) seen · {memoryPath}
                  </em>
                </h3>
                {memory.name ? <p>Name: {memory.name}</p> : null}
                {Object.keys(memory.facts).length ? (
                  <p>
                    Facts:{' '}
                    {Object.entries(memory.facts)
                      .map(([key, value]) => `${key.replace(/_/g, ' ')} = ${value}`)
                      .join(', ')}
                  </p>
                ) : null}
                {memory.likes.length ? <p>Likes: {memory.likes.join(', ')}</p> : null}
                {memory.dislikes.length ? <p>Dislikes: {memory.dislikes.join(', ')}</p> : null}
                {memory.topics.length ? <p>Recent topics: {memory.topics.join(', ')}</p> : null}
                {memory.summary ? <p>Summary: {memory.summary}</p> : null}
                {!memory.name &&
                !memory.likes.length &&
                !memory.dislikes.length &&
                !memory.topics.length &&
                !memory.summary ? (
                  <p className="muted">Nothing yet — have a conversation or two.</p>
                ) : null}
              </>
            ) : (
              <p className="muted">Waiting for the backend…</p>
            )}

            <div className="row row--stack">
              <button type="button" className="btn" onClick={loadMemory}>
                Refresh
              </button>
              <button
                type="button"
                className="btn btn--danger"
                disabled={memoryBusy}
                onClick={() => {
                  setMemoryBusy(true);
                  void forgetMemory()
                    .then(loadMemory)
                    .finally(() => setMemoryBusy(false));
                }}
              >
                {memoryBusy ? 'Forgetting…' : 'Forget everything'}
              </button>
            </div>
          </>
        );

      case 'input':
        return (
          <>
            <p className={asrState === 'listening' ? 'notice notice--ok' : 'notice'}>
              {ASR_STATE_LABELS[asrState]}
              {asrDetail ? ` — ${asrDetail}` : ''}
            </p>
            <Row label="Recognise in the browser" hint="no server model needed">
              <input
                type="checkbox"
                checked={prefs.useBrowserAsr ?? config.asr.engine === 'browser'}
                onChange={(event) => {
                  setPrefs({ useBrowserAsr: event.target.checked });
                  conversation.syncInput();
                }}
              />
            </Row>
            <Row label="Server engine" hint="used when the above is off">
              <select
                value={config.asr.engine}
                onChange={(event) => update('asr', { engine: event.target.value as ASRSettings['engine'] })}
              >
                {(capabilities?.asr ?? ['local']).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </Row>
            <Row label="Whisper model" hint="base.en / small / large-v3">
              <input
                type="text"
                value={config.asr.model}
                onChange={(event) => update('asr', { model: event.target.value })}
              />
            </Row>
            <Row label="Language" hint="blank = auto detect">
              <input
                type="text"
                value={config.asr.language ?? ''}
                onChange={(event) => update('asr', { language: event.target.value || null })}
              />
            </Row>
            <Row label="Device">
              <select value={config.asr.device} onChange={(event) => update('asr', { device: event.target.value })}>
                <option value="auto">auto</option>
                <option value="cpu">cpu</option>
                <option value="cuda">cuda</option>
              </select>
            </Row>
            <Row label="Mic mode" hint="also on the control bar">
              <select
                value={prefs.micMode}
                onChange={(event) => {
                  setPrefs({ micMode: event.target.value as typeof prefs.micMode });
                  conversation.syncInput();
                }}
              >
                <option value="off">off</option>
                <option value="vad">auto (voice activity)</option>
                <option value="ptt">hold to talk</option>
              </select>
            </Row>
            <p className="muted">
              Speak a sentence and then pause: the pause is what ends your turn. While the avatar is talking the
              threshold is raised, so its own voice is ignored even without headphones.
            </p>
          </>
        );

      case 'face':
        return (
          <>
            <p className="muted">Try the avatar directly - these buttons do not involve the language model.</p>
            <div className="chip-row">
              {EMOTION_PRESETS.filter((preset) => preset !== 'neutral').map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className="btn btn--chip"
                  onClick={() =>
                    conversation.expressions.setMood({
                      emotion: preset,
                      weight: 0.85,
                      holdMs: 3000,
                      overrideBlink: preset === 'surprised',
                    })
                  }
                >
                  {preset}
                </button>
              ))}
              <button type="button" className="btn btn--chip" onClick={() => conversation.expressions.clear()}>
                neutral
              </button>
              <button type="button" className="btn btn--chip" onClick={() => conversation.requestBlink()}>
                blink
              </button>
            </div>
            <div className="chip-row">
              {MOTION_ACTIONS.map((action) => (
                <button
                  key={action}
                  type="button"
                  className="btn btn--chip"
                  onClick={() => conversation.animation.play(action, 1)}
                >
                  {action}
                </button>
              ))}
            </div>
            <Row label="Lip-sync">
              <input
                type="checkbox"
                checked={config.lipsync.enabled}
                onChange={(event) => update('lipsync', { enabled: event.target.checked })}
              />
            </Row>
            <Row
              label="Mode"
              hint="mouth = one blendshape driven by loudness · visemes = shape timeline"
            >
              <select
                value={config.lipsync.mode}
                onChange={(event) => update('lipsync', { mode: event.target.value as LipsyncMode })}
              >
                <option value="mouth">mouth (recommended)</option>
                <option value="visemes">visemes</option>
              </select>
            </Row>

            {config.lipsync.mode === 'mouth' ? (
              <>
                <Row label="Threshold" hint="envelope level that opens the mouth">
                  <Slider
                    min={0}
                    max={0.5}
                    step={0.005}
                    value={config.lipsync.mouth_threshold}
                    onChange={(value) => update('lipsync', { mouth_threshold: value })}
                  />
                </Row>
                <Row label="Hysteresis" hint="extra drop needed to close again">
                  <Slider
                    min={0}
                    max={0.3}
                    step={0.005}
                    value={config.lipsync.mouth_hysteresis}
                    onChange={(value) => update('lipsync', { mouth_hysteresis: value })}
                  />
                </Row>
                <Row label="Openness" hint="how wide loud syllables open (0.7 ≈ natural speech)">
                  <Slider
                    min={0.2}
                    max={1.6}
                    step={0.02}
                    value={config.lipsync.mouth_scale}
                    onChange={(value) => update('lipsync', { mouth_scale: value })}
                  />
                </Row>
                <Row label="Max open" hint="ceiling for the mouth shape (1.0 = a shout)">
                  <Slider
                    min={0.1}
                    max={1}
                    step={0.02}
                    value={config.lipsync.mouth_max}
                    onChange={(value) => update('lipsync', { mouth_max: value })}
                  />
                </Row>
                <Row label="Min hold" hint="ms a state is held, stops flutter">
                  <Slider
                    min={0}
                    max={200}
                    value={config.lipsync.mouth_min_dwell_ms}
                    suffix="ms"
                    onChange={(value) => update('lipsync', { mouth_min_dwell_ms: value })}
                  />
                </Row>
              </>
            ) : null}

            <Row label="Attack" hint="how fast the mouth opens (ms)">
              <Slider
                min={5}
                max={200}
                value={config.lipsync.attack_ms}
                onChange={(value) => update('lipsync', { attack_ms: value })}
              />
            </Row>
            <Row label="Release" hint="how fast it closes (ms)">
              <Slider
                min={10}
                max={300}
                value={config.lipsync.release_ms}
                onChange={(value) => update('lipsync', { release_ms: value })}
              />
            </Row>
            {config.lipsync.mode === 'visemes' ? (
              <>
                <Row label="Viseme gain">
                  <Slider
                    min={0.2}
                    max={2}
                    step={0.05}
                    value={config.lipsync.viseme_gain}
                    onChange={(value) => update('lipsync', { viseme_gain: value })}
                  />
                </Row>
                <Row label="Amplitude modulation" hint="loud syllables open wider">
                  <input
                    type="checkbox"
                    checked={config.lipsync.amplitude_modulation}
                    onChange={(event) => update('lipsync', { amplitude_modulation: event.target.checked })}
                  />
                </Row>
                <Row label="Amplitude floor">
                  <Slider
                    min={0}
                    max={1}
                    step={0.05}
                    value={config.lipsync.amplitude_floor}
                    onChange={(value) => update('lipsync', { amplitude_floor: value })}
                  />
                </Row>
              </>
            ) : null}
            {metrics ? (
              <p className="muted">
                Last turn: ttft {metrics.ttft_ms ?? 0} ms · total {metrics.total_ms ?? 0} ms · {metrics.sentences ?? 0}{' '}
                sentence(s)
              </p>
            ) : null}
          </>
        );

      case 'motion':
        return (
          <>
            <Section
              title="Animation clips"
              hint="Mixamo / .vrma files, listed in public/animations/manifest.json"
            >
              <p className="muted">
                {animationStatus.loading
                  ? 'Loading clips…'
                  : animationStatus.mode === 'clips'
                    ? `${animationStatus.clips} clip${animationStatus.clips === 1 ? '' : 's'} · ` +
                      `${animationStatus.idleClips} idle / ${animationStatus.gestureClips} gesture · ` +
                      `${animationStatus.drivenBones} bones driven`
                    : 'No clips loaded — using procedural motion'}
                {animationStatus.activeIdle ? ` · idle: ${animationStatus.activeIdle}` : ''}
                {animationStatus.activeGesture ? ` · gesture: ${animationStatus.activeGesture}` : ''}
              </p>
              {animationStatus.problems.length > 0 ? (
                <ul className="notice notice--warn">
                  {animationStatus.problems.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              ) : null}

              <Row label="Use clips" hint="off = pure procedural motion">
                <input
                  type="checkbox"
                  checked={prefs.animation.enabled}
                  onChange={(event) => applyAnimation({ enabled: event.target.checked })}
                />
              </Row>
              <Row label="Manifest" hint="URL, relative to this site">
                <input
                  type="text"
                  value={manifestDraft}
                  placeholder="/animations/manifest.json"
                  onChange={(event) => setManifestDraft(event.target.value)}
                  onBlur={() => {
                    if (manifestDraft !== prefs.animation.manifestUrl) {
                      applyAnimation({ manifestUrl: manifestDraft.trim() });
                    }
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') event.currentTarget.blur();
                  }}
                />
              </Row>
              <Row label="Idle variety" hint="seconds between random idle clips · 0 = one idle">
                <Slider
                  min={0}
                  max={60}
                  value={prefs.animation.idleSeconds}
                  suffix="s"
                  onChange={(value) => applyAnimation({ idleSeconds: value })}
                />
              </Row>
              <Row label="Gesture strength" hint="global multiplier on clip gestures">
                <Slider
                  min={0}
                  max={1.5}
                  step={0.05}
                  value={prefs.animation.gestureIntensity}
                  onChange={(value) => applyAnimation({ gestureIntensity: value })}
                />
              </Row>
              <div className="chip-row">
                {animationStatus.clips > 0 ? (
                  animationClips.map((clip) => (
                    <button
                      key={`${clip.kind}:${clip.name}`}
                      type="button"
                      className="btn btn--chip"
                      title={clip.kind === 'idle' ? 'Switch to this idle' : 'Play this gesture'}
                      onClick={() => conversation.animation.play(clip.name, 1)}
                    >
                      {clip.kind === 'idle' ? '🔁' : '▶'} {clip.name}
                    </button>
                  ))
                ) : (
                  <span className="muted">Drop clips into frontend/public/animations/ — see docs/ANIMATIONS.md</span>
                )}
              </div>
              <Row label="Reload clips" hint="re-fetch the manifest and every file">
                <button type="button" className="btn" onClick={() => conversation.reloadAnimations()}>
                  Reload
                </button>
              </Row>
            </Section>

            <Section title="Procedural idle" hint="always active below the clips">
              <Row label="Idle motion">
                <input
                  type="checkbox"
                  checked={prefs.motion.idle}
                  onChange={(event) => applyMotion({ idle: event.target.checked })}
                />
              </Row>
              <Row label="Breathing">
                <input
                  type="checkbox"
                  checked={prefs.motion.breathing}
                  onChange={(event) => applyMotion({ breathing: event.target.checked })}
                />
              </Row>
              <Row label="Body sway">
                <input
                  type="checkbox"
                  checked={prefs.motion.sway}
                  onChange={(event) => applyMotion({ sway: event.target.checked })}
                />
              </Row>
              <Row label="Relaxed arms" hint={armsAlreadyDown ? 'this model already rests its arms down' : 'rotate the T-pose arms down'}>
                <input
                  type="checkbox"
                  disabled={armsAlreadyDown}
                  checked={prefs.motion.armRest}
                  onChange={(event) => applyMotion({ armRest: event.target.checked })}
                />
              </Row>
              <Row label="Arm angle" hint="tune per model">
                <Slider
                  min={0}
                  max={90}
                  value={prefs.motion.armAngle}
                  suffix="°"
                  onChange={(value) => applyMotion({ armAngle: value })}
                />
              </Row>
              <Row label="Intensity" hint="global multiplier">
                <Slider
                  min={0}
                  max={2}
                  step={0.05}
                  value={prefs.motion.intensity}
                  onChange={(value) => applyMotion({ intensity: value })}
                />
              </Row>
            </Section>
          </>
        );

      case 'about':
      default:
        return (
          <>
            <p className="muted">
              Expressions: {capabilities?.expressions.join(', ') ?? '-'}
              <br />
              Visemes: {capabilities?.visemes.join(' ') ?? '-'}
            </p>
            <pre className="code">{JSON.stringify(config, null, 2)}</pre>
          </>
        );
    }
  };

  return (
    <aside className="drawer" aria-label="Settings">
      <header className="drawer__head">
        <h2>Settings</h2>
        <button type="button" className="btn btn--icon" onClick={onClose} title="Close">
          ✕<span className="sr-only">Close settings</span>
        </button>
      </header>

      <nav className="tabs" role="tablist">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            className={`btn btn--segment ${tab === entry.id ? 'is-active' : ''}`}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <div className="drawer__body">{tabBody()}</div>
    </aside>
  );
}
