# Security

This is a local desktop app: the backend runs on your machine, the avatar runs in
your browser, and the only network calls out are to whichever model provider you
configure. That shape drives every decision below.

## Where secrets live

| What | Where | Committed? |
|---|---|---|
| **LLM API key** | `backend/secrets.json` (written by Settings → Model) | **No** — git-ignored and docker-ignored |
| Provider / model / URL | `backend/.env.local` (machine-specific overrides) | No — git-ignored |
| Shared non-secret defaults | `backend/.env` | No — git-ignored |
| Documented placeholders | `backend/.env.example` | Yes — placeholders only (`gsk_xxxxxxxx`) |
| Long-term memory | `backend/memory/profile.json` | No — git-ignored |
| Logs | `backend/logs/` | No — git-ignored |

Precedence, highest first:

1. **Environment variables** (`VTUBER_LLM__API_KEY=...`) — always win, so a
   container secret or CI box can override what is on disk.
2. **`backend/secrets.json`** — what the settings UI writes.
3. **`backend/.env.local`** → **`backend/.env`** → built-in defaults.

A *placeholder* key (`ollama`, `none`, `not-needed`) means "this endpoint wants
no authentication". It is never treated as a real key, which is what stops the
app from sending `Authorization: Bearer ollama` to a paid provider and earning a
confusing 401.

On Windows the file's ACL is narrowed to the current user when it is written
(`icacls /inheritance:r /grant:r %USERNAME%:F`). If that fails the write still
succeeds and the reason is logged at debug level — a local desktop file is not
made meaningfully safer by an ACL, so this is a nicety, not the guarantee. The
guarantee is that it is git-ignored.

## What never happens

* The key is **never** sent to the browser. `/api/config` and the WebSocket
  `config_ack` return a fingerprint only — `gsk_1f2e****9c3a` — and
  `PUT /api/llm/key` is write-only: it accepts a key and returns the fingerprint
  of what it stored, never the value.
* The key is **never** logged. Every log sink is wrapped in a redacting writer
  that scrubs branded key patterns (`gsk_`, `sk-`, `AIza`, `hf_`, …),
  `Bearer <token>` headers, and `api_key: "..."` fields — applied to the
  *finished* line, so tracebacks are covered too.
* The key is **never** put in a prompt, an error message, or an exception.
* Request bodies and user messages are not logged at INFO. Full wire logging is
  opt-in via `VTUBER_LOG__LEVEL=TRACE`.

## Network exposure

* The server binds `127.0.0.1` by default (`server.host`). Use
  `--host 0.0.0.0` only if you understand that it exposes the app to your LAN.
* CORS is restricted to the four local origins (`localhost`/`127.0.0.1` on 5173
  and 8000). An empty list falls back to those defaults — **never** to `*`.
* WebSocket connections are origin-checked. Same-origin is always allowed, a
  literal `"*"` Origin header is rejected, and a missing `Origin` (a CLI client,
  not a browser) is allowed on purpose.
* Settings-writing endpoints reject requests whose `Origin` header is a
  different site, which blocks the cross-site-request case that CORS alone does
  not cover for body-less POSTs. This covers **every** state-changing route -
  `/api/llm/key`, `/api/config/validate`, `/api/llm/test`, `/api/llm/discover`
  (otherwise a port scanner) - and the profile routes `/api/memory` (read) and
  `DELETE /api/memory` (irreversible), so a stray page can neither read what the
  companion remembers nor make it forget. `GET`/`PUT`/`DELETE /api/character`
  are guarded too: the persona is your own text, and the `PUT` writes a file.
* A key configured for a *backup* provider is never inherited by the next one:
  a Groq credential can never be sent to a local server, even though a
  placeholder key such as `"ollama"` would otherwise resolve to it.

## Dependencies

Pinned in `backend/requirements.txt`. No telemetry, no analytics, no crash
reporting. Anything new is called out in the reply that adds it.

## If a key leaks

1. **Rotate it now.** A leaked key is not a "later" problem.
   * Groq → <https://console.groq.com/keys> — revoke, create a new one.
   * OpenAI → <https://platform.openai.com/api-keys>
   * Google AI Studio → <https://aistudio.google.com/apikey>
2. Paste the new key into **Settings → Model → API key → Save**. Never send it
   through a chat window, an issue tracker, or a screenshot.
3. Confirm nothing was committed: `python scripts/check_secrets.py`.
4. If it *was* committed, rotating is not optional — assume the value is public
   from the moment it was pushed, even in a private repo, and rewrite history
   only as a secondary measure.

## Checking before you commit

```powershell
cd "D:\everything\Coding and others\projects\open_LLm"
.\.venv\Scripts\python.exe scripts\check_secrets.py   # only if you made a venv at the root
python scripts\check_secrets.py
```

Exit code 0 = clean, 1 = something key-shaped was found in a file that git would
track. It skips git-ignored paths, so `secrets.json` and `.env` never trip it.

To make it automatic, add a pre-commit hook once the repository is initialised:

```powershell
git init
"python scripts/check_secrets.py || exit 1" | Set-Content .git\hooks\pre-commit -Encoding ascii
```
