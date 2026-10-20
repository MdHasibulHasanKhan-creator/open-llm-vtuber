"""Live smoke test: does *your* configured provider actually answer?

Runs from anywhere:

    backend\\.venv\\Scripts\\python.exe scripts\\smoke_llm.py

This is the manual counterpart to ``pytest``: the test suite proves the code is
correct, this proves your provider, base_url and key are. It drives the real
settings (``backend/.env``, ``backend/.env.local``, ``backend/secrets.json``) and
asks a Bengali, an English, a Banglish and a contextual question.

The API key is never printed - only a fingerprint, which is enough to tell two
keys apart and useless to anyone else.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))
# `.env` and `.env.local` are resolved against the working directory.
os.chdir(BACKEND)
# The Windows console defaults to cp1252, which cannot encode Bengali.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from app.config import get_settings  # noqa: E402
from app.llm import ChatMessage, create_llm  # noqa: E402
from app.llm.discover import list_models  # noqa: E402
from app.security import mask_secret  # noqa: E402

PROBES: list[tuple[str, str]] = [
    ("English", "What is your name, and what are you?"),
    ("Bengali", "তুমি কেমন আছো? আজ আমার দিনটা খুব ভালো গেছে।"),
    ("Banglish", "ami ajke ekta notun project shuru korechi, tumi ki bhabo?"),
    ("Contextual", "Earlier you told me your name. Do you actually remember it?"),
]

SYSTEM = (
    "You are Ai, a warm, playful 3D VTuber companion. Reply in the same language "
    "the user used. One or two short spoken sentences. No markdown."
)


async def main() -> int:
    settings = get_settings()
    key = settings.llm.effective_api_key()

    print("provider     :", settings.llm.provider)
    print("base_url     :", settings.llm.base_url)
    print("model        :", settings.llm.model)
    print("timeout      :", settings.llm.timeout)
    print("auto_discover:", settings.llm.auto_discover)
    print("fallback_echo:", settings.llm.fallback_to_echo)
    print("key present  :", bool(key), "| fingerprint:", mask_secret(key) or "(none)")

    try:
        models = await list_models(settings.llm.base_url, api_key=key)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the probe
        print(f"\n!! could not list models: {type(exc).__name__}: {exc}")
        models = []
    print(f"\nmodels offered by the provider: {len(models)}")
    for name in models:
        if any(tag in name.lower() for tag in ("qwen", "gpt-oss", "llama")):
            print("   ", name)

    llm = create_llm(settings.llm)
    failures = 0
    try:
        for label, question in PROBES:
            messages = [ChatMessage("system", SYSTEM), ChatMessage("user", question)]
            started = time.perf_counter()
            out = ""
            try:
                async for delta in llm.stream_chat(messages, max_tokens=160):
                    out += delta
            except Exception as exc:  # noqa: BLE001 - report, do not crash the probe
                failures += 1
                print(f"\n[{label}] FAILED: {type(exc).__name__}: {exc}")
                continue
            elapsed = (time.perf_counter() - started) * 1000
            print(f"\n[{label}] {question}")
            print(f"  -> {out.strip()}")
            print(f"  ({elapsed:.0f} ms)")
    finally:
        await llm.aclose()

    if failures:
        print(f"\n{failures} probe(s) failed")
        return 1
    print("\nall probes answered")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
