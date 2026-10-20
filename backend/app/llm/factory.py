"""Pick an LLM implementation from the configuration.

One provider is named in the settings; zero or more *backups* may be listed
behind it. When there are backups the built engine is wrapped in a
:class:`~app.llm.fallback.FallbackLLM`, which switches links only before the
first token of a reply. The offline "echo" brain is never a backup link.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..config import FallbackTarget, LLMConfig
from .base import LLMEngine, LLMError
from .echo import EchoLLM
from .fallback import FallbackLLM
from .ollama import OllamaLLM
from .openai_compatible import OpenAICompatibleLLM

_REGISTRY: dict[str, type[LLMEngine]] = {
    "openai": OpenAICompatibleLLM,
    "ollama": OllamaLLM,
    "echo": EchoLLM,
}

#: Hosts that mean "on this machine". Used to skip a backup that would only
#: re-probe the endpoint the session is already using.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def create_llm(config: LLMConfig) -> LLMEngine:
    """Build the configured engine, wrapped in its fallback chain when it has one."""
    engine = _build(config)
    backups = _backup_configs(config)
    if not backups:
        return engine
    return FallbackLLM(config, engine, [_build(candidate) for candidate in backups])


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def _build(config: LLMConfig) -> LLMEngine:
    try:
        engine_class = _REGISTRY[config.provider]
    except KeyError as exc:  # pragma: no cover - config validates the name
        raise LLMError(f"Unknown LLM provider '{config.provider}'") from exc
    return engine_class(config)


def _backup_configs(config: LLMConfig) -> list[LLMConfig]:
    """Turn the configured chain into standalone engine configs.

    Two links are dropped before they are built: the offline brain (a scripted
    answer is not a fallback) and an endpoint this session already uses (it would
    only spend a second connect timeout on the same server).
    """
    if config.provider == "echo" or not config.fallback_chain:
        return []
    primary = _endpoint_key(config.base_url)
    backups: list[LLMConfig] = []
    for target in config.fallback_chain:
        if target.provider == "echo":
            continue
        if _endpoint_key(target.base_url) == primary:
            continue
        backups.append(_target_config(config, target))
    return backups


def _target_config(base: LLMConfig, target: FallbackTarget) -> LLMConfig:
    """A self-contained config for one backup link.

    Everything provider-shaped is replaced. Sampling and prompt settings are
    inherited, because they describe the *character* rather than the endpoint -
    and a backup that answered in a different tone would be a second persona.
    """
    key = (target.api_key or "").strip()
    return base.model_copy(
        update={
            "provider": target.provider,
            "model": target.model or base.model,
            "base_url": target.base_url,
            # Never inherit `base.api_key`: a placeholder such as `"ollama"`
            # resolves to the key stored for the *primary* provider, and sending
            # a Groq credential to a local server is precisely the leak this app
            # must not have. `"local"` is not a placeholder, so it is passed
            # through verbatim (and no local server cares about its value).
            "api_key": key or "local",
            "fallback_chain": [],
        }
    )


def _endpoint_key(base_url: str) -> str:
    """``host:port`` for a base URL, with the ``/v1`` suffix folded away.

    Every loopback spelling collapses to ``localhost``: ``127.0.0.1:11434``,
    ``::1`` and ``localhost`` are the same machine, and treating them as three
    endpoints is how a chain ends up probing the same dead port twice.
    """
    parts = urlsplit((base_url or "").strip())
    host = (parts.hostname or "").lower()
    if not host:
        return (base_url or "").strip().rstrip("/").removesuffix("/v1").lower()
    if host in _LOOPBACK_HOSTS:
        host = "localhost"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{host}:{port}"


__all__ = ["available_providers", "create_llm"]
