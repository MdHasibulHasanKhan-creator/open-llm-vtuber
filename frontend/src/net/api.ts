/** Thin typed wrappers around the REST endpoints (`backend/app/server/http_routes.py`). */

import { apiUrl } from '../config';
import type {
  CharacterDetail,
  ConfigPatch,
  DiscoveredEndpoint,
  EditableCharacterField,
  HealthReport,
  LlmTestResult,
  MemorySnapshot,
  ModelPreset,
  PublicConfig,
  VrmModelEntry,
  Voice,
} from '../types/config';

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path));
  if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
  return (await response.json()) as T;
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<HealthReport> {
  return getJson<HealthReport>('/health');
}

export function fetchConfig(): Promise<{ config: PublicConfig }> {
  return getJson<{ config: PublicConfig }>('/config');
}

export async function fetchVoices(engine: string, locale?: string): Promise<Voice[]> {
  const query = new URLSearchParams({ engine });
  if (locale) query.set('locale', locale);
  const data = await getJson<{ voices: Voice[] }>(`/voices?${query.toString()}`);
  return data.voices;
}

export async function fetchVrmModels(): Promise<VrmModelEntry[]> {
  const data = await getJson<{ models: VrmModelEntry[] }>('/vrm-models');
  return data.models;
}

/* -------------------------------------------------------------------------- */
/*  Model setup - the "why are the replies a script?" tooling                  */
/* -------------------------------------------------------------------------- */

/** Ready-made provider settings. Local and free options come first. */
export async function fetchModelPresets(): Promise<ModelPreset[]> {
  const data = await getJson<{ presets: ModelPreset[] }>('/llm/presets');
  return data.presets;
}

/** Probe the well-known local ports for a running model server. */
export async function discoverModels(): Promise<DiscoveredEndpoint[]> {
  const data = await sendJson<{ endpoints: DiscoveredEndpoint[] }>('/llm/discover', 'POST');
  return data.endpoints;
}

/**
 * Round-trip a completion through a *proposed* LLM configuration.
 *
 * Never throws for a bad connection: the returned `ok`/`error` pair is the
 * result. It only throws if the server itself is unreachable.
 */
export function testLlm(llm: Partial<ConfigPatch['llm']>): Promise<LlmTestResult> {
  return sendJson<LlmTestResult>('/llm/test', 'POST', { llm });
}

/* -------------------------------------------------------------------------- */
/*  API key - write-only                                                      */
/* -------------------------------------------------------------------------- */

export interface LlmKeyResult {
  ok: boolean;
  /** `false` when a masked value was echoed back, i.e. "unchanged". */
  changed?: boolean;
  /** Fingerprint of what is stored, e.g. `gsk_1f2e****9c3a`. Never the key. */
  api_key: string;
  api_key_set: boolean;
}

/**
 * Store an API key for the LLM provider.
 *
 * **Write-only.** The server persists it next to nothing else and answers with a
 * fingerprint; the value is never readable back through any endpoint, so there is
 * deliberately no `fetchLlmKey`.
 */
export function saveLlmKey(apiKey: string): Promise<LlmKeyResult> {
  return sendJson<LlmKeyResult>('/llm/key', 'PUT', { api_key: apiKey });
}

/** Forget the stored key. */
export function clearLlmKey(): Promise<LlmKeyResult> {
  return sendJson<LlmKeyResult>('/llm/key', 'DELETE');
}

/* -------------------------------------------------------------------------- */
/*  Persona - "make her yours"                                                */
/* -------------------------------------------------------------------------- */

/** The persona as it reads now, next to the values it shipped with. */
export function fetchCharacter(id: string): Promise<CharacterDetail> {
  return getJson<CharacterDetail>(`/character?id=${encodeURIComponent(id)}`);
}

/**
 * Save an edit. An empty string *clears* a field back to the shipped value, and
 * the reply is the persona as it now reads - so the caller never has to guess.
 */
export function saveCharacter(
  id: string,
  fields: Partial<Record<EditableCharacterField, string>>,
): Promise<CharacterDetail> {
  return sendJson<CharacterDetail>('/character', 'PUT', { id, fields });
}

/** Throw away every saved edit for this persona. */
export function resetCharacter(id: string): Promise<CharacterDetail> {
  return sendJson<CharacterDetail>(`/character?id=${encodeURIComponent(id)}`, 'DELETE');
}

/* -------------------------------------------------------------------------- */
/*  Long-term memory                                                          */
/* -------------------------------------------------------------------------- */

export async function fetchMemory(): Promise<{ enabled: boolean; path: string; memory: MemorySnapshot }> {
  return getJson<{ enabled: boolean; path: string; memory: MemorySnapshot }>('/memory');
}

export async function forgetMemory(): Promise<void> {
  await sendJson<{ ok: boolean }>('/memory', 'DELETE');
}

/** Dry-run a patch against the server's models before sending it for real. */
export async function validatePatch(
  patch: ConfigPatch,
): Promise<{ ok: true; config: PublicConfig } | { ok: false; error: string }> {
  try {
    const response = await fetch(apiUrl('/config/validate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!response.ok) {
      const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
      return { ok: false, error: detail?.detail ?? `HTTP ${response.status}` };
    }
    return { ok: true, config: ((await response.json()) as { config: PublicConfig }).config };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}
