"""Fail the build if a credential is about to be committed.

Run it by hand (``python scripts/check_secrets.py``) or wire it into a pre-commit
hook / CI step. Exit code 0 = clean, 1 = something key-shaped was found.

It deliberately does **not** look at ``.env``, ``.env.*`` or ``secrets.json``:
those are the files a secret is *supposed* to live in, they are git-ignored, and
flagging them would train everyone to ignore this check. Everything else is
scanned, including ``.env.example`` (a placeholder there is fine - only
key-shaped strings are reported, and the placeholders are not key shaped).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories that never contain first-party source, or contain so much text
#: that scanning them is pointless.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "dist-ssr",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".vite",
    "models",
    "logs",
}

#: Files a secret is allowed to live in.
ALLOWED_FILES = {
    "secrets.json",
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
}

ALLOWED_SUFFIXES = {".env"}

#: Only files we can read as text and that are small enough to be source.
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".json5",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt", ".html", ".css",
    ".sh", ".ps1", ".psm1", ".bat", ".cmd", ".dockerfile", ".env", ".example",
    ".gitignore", ".dockerignore",
}
MAX_BYTES = 2 * 1024 * 1024

#: How a deliberate fake opts out. Two granularities, both of which show up in
#: code review - which is the point. A scanner that cannot be told "this one is a
#: fixture" is a scanner everybody learns to ignore, and then it catches nothing.
#:   ``check-secrets: allow-line``  skip this line
#:   ``check-secrets: allow-file``  skip this file (for the redaction tests)
LINE_ALLOW = "check-secrets: allow-line"
FILE_ALLOW = "check-secrets: allow-file"
HEADER_BYTES = 4000

#: Anything that looks like a real credential. The branded prefixes are the ones
#: the providers in this app actually issue.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Groq key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{24,}")),
    ("OpenRouter key", re.compile(r"\bsk-or-[A-Za-z0-9_\-]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")),
    ("HuggingFace token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("xAI key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)


def git_ignored(path: Path) -> bool:
    """True when git already ignores this path (so it cannot be committed).

    Returns False when git is unavailable or the repo is not initialised, which
    is the safe direction: an unusual layout gets scanned rather than skipped.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def iter_candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.name in ALLOWED_FILES or path.suffix in ALLOWED_SUFFIXES:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            "Dockerfile",
            ".gitignore",
            ".dockerignore",
            "LICENSE",
        }:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def main() -> int:
    findings: list[tuple[Path, int, str, str]] = []
    scanned = 0
    skipped = 0

    for path in iter_candidate_files():
        if git_ignored(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if FILE_ALLOW in text[:HEADER_BYTES]:
            skipped += 1
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            if LINE_ALLOW in line:
                continue
            for label, pattern in PATTERNS:
                match = pattern.search(line)
                if match:
                    redacted = match.group(0)
                    redacted = f"{redacted[:6]}...{redacted[-4:]}" if len(redacted) > 14 else "***"
                    findings.append((path.relative_to(REPO_ROOT), lineno, label, redacted))

    if not findings:
        note = f", {skipped} allow-listed" if skipped else ""
        print(f"check_secrets: clean ({scanned} files scanned{note})")
        return 0

    print("check_secrets: possible credentials found in tracked files:\n")
    for path, lineno, label, redacted in findings:
        print(f"  {path}:{lineno}  {label}  {redacted}")
    print(
        "\nIf one of these is real: rotate it with the provider immediately, then "
        "move it to backend/secrets.json (or a .env file) and confirm it is "
        "git-ignored. See SECURITY.md."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
