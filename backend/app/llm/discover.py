"""Find a language model that actually works, or say exactly how to get one.

The worst first-run experience this app can give is: the configured provider is
unreachable, the app silently substitutes a canned script, and the user concludes
that the *conversation* is broken. That is precisely what happened here - the
shipped ``.env`` pointed at ``localhost:11434`` (Ollama), Ollama was not
installed, ``fallback_to_echo`` was on, and every reply came out of a fixed pool
that had nothing to do with the question.

So there are two halves to the fix, and this module is the first one: rather than
falling back to a script, **look for a real model** on the handful of ports local
servers actually use, and adopt one. The startup path does that automatically
(``run_preflight``), and the settings UI uses it for a one click "Detect models"
button.

The second half is honesty: if nothing is reachable, the app now says so loudly
and tells you the three ways to fix it (``free_presets``) instead of quietly
shipping scripted replies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

__all__ = [
    "LOCAL_ENDPOINTS",
    "LocalEndpoint",
    "adopt_if_available",
    "discover",
    "free_presets",
    "list_models",
    "pick_model",
    "probe_endpoint",
]


@dataclass(frozen=True, slots=True)
class LocalEndpoint:
    """A local model server we know how to talk to."""

    id: str
    label: str
    #: OpenAI compatible base (what ``llm.base_url`` wants).
    base_url: str
    api_key: str = "none"
    #: What the user has to run for this to answer.
    note: str = ""
    #: Ollama exposes a richer, faster model list here; everything else uses
    #: the standard ``/v1/models``.
    tags_path: str = ""


#: Ordered by how likely a first-time user is to have them.
LOCAL_ENDPOINTS: tuple[LocalEndpoint, ...] = (
    LocalEndpoint(
        "ollama", "Ollama", "http://localhost:11434/v1", "ollama",
        "walkthrough: ollama serve  +  ollama pull llama3.1:8b", tags_path="/api/tags",
    ),
    LocalEndpoint(
        "lmstudio", "LM Studio", "http://localhost:1234/v1", "lm-studio",
        "Developer tab -> Start Server",
    ),
    LocalEndpoint(
        "llamacpp", "llama.cpp", "http://localhost:8080/v1", "none",
        "llama-server -m your-model.gguf",
    ),
    LocalEndpoint(
        "jan", "Jan", "http://localhost:1337/v1", "none",
        "Settings -> Local API Server -> Start",
    ),
    LocalEndpoint(
        "textgen", "text-generation-webui", "http://localhost:5000/v1", "none",
        "start it with the --api flag",
    ),
    LocalEndpoint(
        "koboldcpp", "KoboldCpp", "http://localhost:5001/v1", "none",
        "launch with --port 5001",
    ),
    LocalEndpoint(
        "vllm", "vLLM", "http://localhost:8000/v1", "none",
        "vllm serve <model>",
    ),
)

#: Model names that are never useful for a *chat* app. Filtered out of the
#: suggestions so the "Use this model" list is not full of noise.
_NOT_CHAT = ("embed", "embedding", "rerank", "whisper", "tts", "stable-diffusion", "clip")


def _ollama_models(data: Any) -> list[str]:
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    out: list[str] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def _openai_models(data: Any) -> list[str]:
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    out: list[str] = []
    for entry in models:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out.append(entry["id"])
    return out


def pick_model(models: Iterable[str]) -> str:
    """Best guess at a chat model from a server's list.

    Preference order: a name that looks like an instruct/chat model, then the
    shortest name (usually the newest small default), and never an embedding or
    speech model.
    """
    candidates = [
        name for name in models
        if name and not any(bad in name.lower() for bad in _NOT_CHAT)
    ]
    if not candidates:
        return ""
    for hint in ("instruct", "chat", "llama", "qwen", "mistral", "gemma", "phi"):
        for name in candidates:
            if hint in name.lower():
                return name
    return sorted(candidates, key=len)[0]


async def probe_endpoint(
    endpoint: LocalEndpoint,
    *,
    client: httpx.AsyncClient,
    timeout_s: float = 2.5,
) -> dict[str, Any]:
    """Ask one endpoint for its model list. Never raises."""
    result: dict[str, Any] = {
        "id": endpoint.id,
        "label": endpoint.label,
        "base_url": endpoint.base_url,
        "api_key": endpoint.api_key,
        "note": endpoint.note,
        "reachable": False,
        "models": [],
        "detail": "",
    }
    origin = endpoint.base_url.rsplit("/v1", 1)[0]
    attempts: list[tuple[str, Any]] = []
    if endpoint.tags_path:
        attempts.append((origin + endpoint.tags_path, _ollama_models))
        # Older Ollama builds have no /api/tags but do serve /v1/models.
        attempts.append((endpoint.base_url + "/models", _openai_models))
    else:
        attempts.append((endpoint.base_url + "/models", _openai_models))

    for url, extract in attempts:
        try:
            response = await client.get(url, timeout=timeout_s)
        except httpx.HTTPError as exc:
            result["detail"] = f"not listening ({exc.__class__.__name__})"
            continue
        if response.status_code >= 400:
            result["detail"] = f"HTTP {response.status_code}"
            continue
        try:
            models = extract(response.json())
        except Exception:
            result["detail"] = "unreadable model list"
            continue
        result["models"] = models
        result["reachable"] = True
        result["detail"] = f"{len(models)} model(s)" if models else "reachable, no models pulled"
        return result

    return result


async def discover(
    *,
    timeout_s: float = 2.5,
    exclude: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Probe every known local endpoint concurrently.

    ``exclude`` holds base URLs to skip - in practice the app's own port, which
    would otherwise answer ``/v1/models`` with a 404 and look like a broken
    server (vLLM's default port collides with ours).
    """
    skip = {url.rstrip("/") for url in exclude if url}
    targets = [e for e in LOCAL_ENDPOINTS if e.base_url.rstrip("/") not in skip]
    if not targets:
        return []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            *(probe_endpoint(endpoint, client=client, timeout_s=timeout_s) for endpoint in targets)
        )
    # Reachable-with-models first, then reachable, then the rest.
    return sorted(
        results,
        key=lambda item: (not item["reachable"], not item["models"], item["label"]),
    )


def free_presets() -> list[dict[str, Any]]:
    """Ready-made provider settings, cheapest and most private first.

    ``needs_key`` entries are the only realistic way to get a *good* model
    without a GPU; every one of them has a free tier that is enough for a
    desktop companion, and ``key_url`` is where the key comes from.
    """
    return [
        {
            "id": "ollama",
            "label": "Ollama - local, private, offline",
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model": "llama3.1:8b",
            "needs_key": False,
            "free": True,
            "best": True,
            "note": (
                "Install from ollama.com, then run `ollama pull llama3.1:8b`. "
                "Needs ~5 GB of disk and about 8 GB of RAM. Nothing leaves your PC."
            ),
        },
        {
            "id": "lmstudio",
            "label": "LM Studio - local, GUI",
            "provider": "openai",
            "base_url": "http://localhost:1234/v1",
            "model": "local-model",
            "needs_key": False,
            "free": True,
            "note": "Easier than Ollama if you prefer a window: pick a model, press Start Server.",
        },
        {
            "id": "groq",
            "label": "Groq - free tier, extremely fast",
            "provider": "openai",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "needs_key": True,
            "free": True,
            "key_url": "https://console.groq.com/keys",
            "note": "Free tier, no card required. Best quality-per-second if you have internet.",
        },
        {
            "id": "gemini",
            "label": "Google AI Studio - free tier",
            "provider": "openai",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-2.0-flash",
            "needs_key": True,
            "free": True,
            "key_url": "https://aistudio.google.com/apikey",
            "note": "Generous free tier. Note: free-tier prompts may be used to improve their models.",
        },
        {
            "id": "openrouter",
            "label": "OpenRouter - some models free",
            "provider": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "needs_key": True,
            "free": True,
            "key_url": "https://openrouter.ai/keys",
            "note": "One key, many models. The ':free' suffixed ones cost nothing but are rate limited.",
        },
        {
            "id": "deepseek",
            "label": "DeepSeek - very cheap, not free",
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "needs_key": True,
            "free": False,
            "key_url": "https://platform.deepseek.com/api_keys",
            "note": "Pennies per conversation, and unusually good at staying in character.",
        },
        {
            "id": "openai",
            "label": "OpenAI",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "needs_key": True,
            "free": False,
            "key_url": "https://platform.openai.com/api-keys",
            "note": "Paid. gpt-4o-mini is the cheap one.",
        },
    ]


async def list_models(
    base_url: str,
    *,
    api_key: str = "",
    timeout_s: float = 8.0,
) -> list[str]:
    """Model list for an arbitrary OpenAI-compatible endpoint (settings UI)."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return []
    headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key.lower() != "none" else {}
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
            response = await client.get(f"{url}/models", timeout=timeout_s)
            if response.status_code >= 400:
                return []
            return _openai_models(response.json())
    except (httpx.HTTPError, ValueError):
        return []


async def adopt_if_available(settings: Any) -> dict[str, Any] | None:
    """Try to point ``settings.llm`` at a working local model.

    Returns the winning discovery entry, or ``None`` if nothing is listening.
    Only local endpoints are considered: silently sending someone's conversation
    to a cloud provider is not a decision this code gets to make.
    """
    exclude = {settings.llm.base_url}
    if settings.llm.provider == "ollama":
        exclude.add("http://localhost:11434/v1")
    candidates = await discover(exclude=exclude)
    for entry in candidates:
        if not entry["models"]:
            continue
        model = pick_model(entry["models"])
        if not model:
            continue
        logger.info(
            "found a local model server: {} at {} ({} model(s))",
            entry["label"], entry["base_url"], len(entry["models"]),
        )
        return {**entry, "picked_model": model}
    return None
