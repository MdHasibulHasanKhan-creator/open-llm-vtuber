# Troubleshooting

The four problems that come up on a first run, with the exact log lines they
produce, what actually causes them, and how to verify the fix. Everything here
has been reproduced against a real `python main.py` on Windows.

---

## 1. `WebSocket / 403` / `connection rejected (403 Forbidden)`

**What you see**

```
INFO:     ('127.0.0.1', 51234) - "WebSocket /" 403
INFO:     connection rejected (403 Forbidden)
```

**What it actually means.** This line is uvicorn's, not ours, and it is emitted
whenever the ASGI app *closes a WebSocket handshake before accepting it*. The
`403` is not a CORS verdict — it is uvicorn's generic wording for "the handshake
was not accepted". Since the app closed the socket before accepting, the browser
recovers only "handshake failed"; it never sees a reason. Three different
mistakes produce exactly this line:

| Cause | How to tell | Fix |
| --- | --- | --- |
| **The client connected to a path we do not serve** (most common: the bare `/`, or a reverse proxy that rewrote `/ws`). | The path in the log is not `/ws`. | Use `ws(s)://<host>/ws`. `ws(s)://<host>/` is now also accepted as an alias. |
| **A page on a disallowed origin opened the socket.** | The path is right but the connecting page was served from somewhere else. | `VTUBER_SERVER__WS_ALLOWED_ORIGINS` — see below. |
| **The upgrade never reached the app** (nginx/Caddy/Traefik/Cloudflare dropping the `Upgrade`/`Connection` headers). | The request never appears in the application log at all. | Forward the upgrade headers (`proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`). |

**What this repo does about it now.** A rejected handshake is *accepted first,
then closed with code 1008 plus a JSON `error` frame*, so the client can say why:

```json
{ "type": "error", "code": "origin",
  "message": "Origin 'http://evil.example.com' is not allowed to open a WebSocket. Allowed: ...",
  "fatal": true }
```

and the terminal gets a runnable log line instead:

```
WARNING | refusing WebSocket from origin 'http://evil.example.com' (host='127.0.0.1:8000'):
          origin is not allowed and does not match the request Host.
          Add it to VTUBER_SERVER__WS_ALLOWED_ORIGINS, or use ["*"] to disable the check.
```

Every accepted connection reports its path and origin, which makes the
"wrong path" case visible immediately:

```
INFO | client connected: 0fb17a38212a (path=/ws, origin=http://localhost:5173, 1 open)
```

**The origin policy.** Two rules, both in `backend/app/server/origins.py`:

1. **Same-origin is always allowed.** The `Origin` header is compared with the
   request's `Host`, so `http://192.168.1.20:8000`, `https://vtuber.example.com`
   and anything behind a TLS proxy work with no configuration.
2. **Anything else must be listed.** `ws_allowed_origins` falls back to
   `cors_origins`, which defaults to the four local addresses:

   ```dotenv
   VTUBER_SERVER__CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173","http://localhost:8000","http://127.0.0.1:8000"]
   VTUBER_SERVER__WS_ALLOWED_ORIGINS=[]          # extra origins; ["*"] disables the check
   ```

   A request with **no** `Origin` header (curl, a CLI, a load-balancer probe) is
   allowed: only browsers send one, so the empty case carries no information.

The same four origins are applied to CORS for the REST API, so `npm run dev`
(`:5173`) and the built app served by FastAPI (`:8000`) both work, over
`localhost` **and** `127.0.0.1` — the mismatch between those two spellings is the
classic reason something "works in the browser" but not in a script.

**Verify it** from `backend/` with the server running (no browser needed):

```powershell
@'
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8000/ws", origin="http://localhost:5173") as ws:
        print("allowed origin ->", json.loads(await ws.recv())["type"])
    try:
        async with websockets.connect("ws://127.0.0.1:8000/ws", origin="http://evil.example.com") as ws:
            print("bad origin     ->", await ws.recv())
    except Exception as exc:
        print("bad origin     ->", type(exc).__name__, exc)

asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
```

Expected:

```
allowed origin -> ready
bad origin     -> {"type":"error","message":"Origin 'http://evil.example.com' is not allowed to open a WebSocket. Allowed: ...","fatal":true,"code":"origin"}
```

Note the second line: the connection is **accepted** and then explained, which
is the whole point — a browser could never have read that message from a
handshake that was refused. In the terminal you should see
`client connected ... (path=/ws, origin=http://localhost:5173, 1 open)` for the
first and a `refusing WebSocket from origin ...` warning for the second, and
**no** `403` line. The browser's DevTools → Network → WS should show
`Status 101 Switching Protocols`.

---

## 2. `LLM openai unreachable (ConnectError)`

**What you see**

```
WARNING | LLM  openai     unreachable (ConnectError). Is your local server (Ollama / LM Studio / vLLM) running?
```

**Cause.** The default configuration points at a local Ollama
(`http://localhost:11434/v1`) that is not running. Nothing is broken — the
preflight just tells you the brain is missing, then carries on.

> Before you reach for the terminal: the startup path already tries to find a
> model for you. It probes the usual local ports (`AUTO_DISCOVER=true`), and if
> one answers, this warning is replaced by
> `SUCCESS | LLM auto-detected: <server> -> <model>`. See
> [§6](#6-the-replies-look-like-a-fixed-script-that-ignores-my-question) for what
> happens when nothing answers.

**Fix A — run a local model (nothing else to change):**

```powershell
ollama serve                 # in its own terminal, or: it runs as a service
ollama pull llama3.1:8b      # once; alternatives: qwen2.5:7b, gemma3:4b, phi4-mini
```

**Fix B — offline demo mode, no model at all:**

```dotenv
VTUBER_LLM__PROVIDER=echo
```

The avatar, TTS, emotion analysis and lip-sync all work; the replies are canned.
This is the fastest way to confirm the *renderer* works before debugging a model.

**Fix C — the smooth-testing default that is shipped in `backend/.env`:**

```dotenv
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=http://localhost:11434/v1
VTUBER_LLM__MODEL=llama3.1:8b
VTUBER_LLM__FALLBACK_TO_ECHO=true
```

With the flag on, an unreachable provider is replaced by the offline `echo`
brain for this process (logged loudly, one line) instead of failing every turn:

```
WARNING | LLM unreachable -> falling back to the offline 'echo' brain. Set VTUBER_LLM__FALLBACK_TO_ECHO=false to turn this off.
INFO    | ASR  browser    the browser transcribes and sends text
SUCCESS | all startup checks passed
```

`GET /api/health` reports the same thing, including whether the fallback fired:

```json
"preflight": { "llm": { "ok": true, "fallback": true,
                        "detail": "configured provider unreachable -> using the offline 'echo' brain (...)" } }
```

### Provider templates (pick one)

```dotenv
# --- Ollama, native endpoint (keeps Ollama's own error messages) ------------
VTUBER_LLM__PROVIDER=ollama
VTUBER_LLM__BASE_URL=http://localhost:11434
VTUBER_LLM__MODEL=llama3.1:8b
VTUBER_LLM__API_KEY=ollama          # ignored, kept for symmetry
```

```dotenv
# --- OpenAI ----------------------------------------------------------------
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=https://api.openai.com/v1
VTUBER_LLM__MODEL=gpt-4o-mini
VTUBER_LLM__API_KEY=sk-...
```

```dotenv
# --- Groq (fast, generous free tier, OpenAI compatible) --------------------
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=https://api.groq.com/openai/v1
VTUBER_LLM__MODEL=llama-3.3-70b-versatile
VTUBER_LLM__API_KEY=gsk_...
```

```dotenv
# --- DeepSeek --------------------------------------------------------------
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=https://api.deepseek.com/v1
VTUBER_LLM__MODEL=deepseek-chat
VTUBER_LLM__API_KEY=sk-...
```

```dotenv
# --- LM Studio / llama.cpp server / vLLM on this machine -------------------
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=http://localhost:1234/v1     # llama.cpp: :8080, LM Studio: :1234
VTUBER_LLM__MODEL=local-model                     # LM Studio: the id from its UI
VTUBER_LLM__API_KEY=not-needed
```

> A `404` on `/chat/completions` means the URL and the provider disagree: a
> native Ollama server needs `PROVIDER=ollama` (no `/v1`), anything with `/v1`
> needs `PROVIDER=openai`. **Check the key is actually being read** with
> `GET /api/config` — the value comes back masked as `********`, so a missing or
> empty string tells you the `.env` is not where you think it is (it must sit
> next to `main.py`: `backend/.env`).

---

## 3. `ASR local faster-whisper is not installed`

**What you see**

```
WARNING | ASR  local      faster-whisper is not installed. Install it with
                           `pip install -r requirements-local.txt`, or set
                           VTUBER_ASR__ENGINE=browser to transcribe with the Web Speech API instead.
```

**Cause.** The default ASR engine is `local`, and `faster-whisper` ships in the
*optional* requirements file (it is roughly 250 MB of wheels). The shipped
`backend/.env` sets `VTUBER_ASR__ENGINE=browser` precisely so a fresh clone has a
working microphone with nothing extra to install.

**Fix A — browser speech recognition (zero install).**

```dotenv
VTUBER_ASR__ENGINE=browser
```

The browser transcribes and sends text (`chat`), so no audio crosses the socket
at all. Caveat: the Web Speech API exists in Chrome, Edge and Safari, but **not
in Firefox** — there, install Fix B.

**Fix B — install the local engine** (from `backend/`, with the venv active):

```powershell
# Windows PowerShell
cd backend
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-local.txt
```

```bash
# macOS / Linux
cd backend
source .venv/bin/activate
python -m pip install -r requirements-local.txt
```

```powershell
# or without activating anything:
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
```

That pulls in (verified via `pip install --dry-run` on Windows / Python 3.12):

```
faster-whisper==1.2.1  ctranslate2-4.8.3  onnxruntime-1.31.0
av-19.0.1  tokenizers  huggingface_hub  protobuf  tqdm ...
```

Then switch the engine and pick a model size:

```dotenv
VTUBER_ASR__ENGINE=local
VTUBER_ASR__MODEL=base.en        # tiny.en (fastest) · base.en · small.en · medium.en (best)
VTUBER_ASR__DEVICE=auto          # auto | cpu | cuda
VTUBER_ASR__COMPUTE_TYPE=int8    # int8 | int8_float16 | float16 | float32
VTUBER_ASR__LANGUAGE=en          # empty = auto-detect
```

The weights (~75 MB for `base.en`) download from Hugging Face into
`backend/models/` on first use, so the first transcription is slow and the ones
after it are not. A working preflight line looks like:

```
INFO | ASR  local      ready (model 'base.en' loads on first use)
```

**Fix C — a remote Whisper** (nothing to install locally either):

```dotenv
VTUBER_ASR__ENGINE=openai
VTUBER_ASR__BASE_URL=https://api.groq.com/openai/v1
VTUBER_ASR__MODEL=whisper-large-v3-turbo
VTUBER_ASR__API_KEY=gsk_...
```

**Note on a fourth option that looks tempting:** there is no "auto" ASR engine.
Which one is *right* depends on the browser you use and whether you can install
250 MB, and both of those are your decision — so the server never silently
switches the ASR engine for you.

---

## 4. `GET /health 404` and other unexpected 404s

**The path.** `/health` is not the API path — everything REST lives under
`/api`:

| Path | Status |
| --- | --- |
| `/api/health` | canonical (in the OpenAPI schema) |
| `/health` | alias, same payload (what uptime probes and people type) |
| `/api/config`, `/api/characters`, `/api/character`, `/api/voices`, `/api/vrm-models` | available |
| `/api/character` | same-origin only (`GET`/`PUT`/`DELETE`) — a cross-site call gets `403` by design |
| `/v1/models` | **not served** — that is an OpenAI client looking at the wrong port |

The `/v1/models` pattern is worth recognising: it means some tool is probing
`:8000` for an OpenAI-compatible server. Our backend is a *client* of those
APIs, never a server of them, so the answer is a 404 by design.

**The static files.** `frontend/dist` is served from the same port
(`VTUBER_SERVER__SERVE_FRONTEND=true`), which is why a typo in a path can look
like a routing bug. Two rules now apply:

* a **navigation** request (one whose `Accept` includes `text/html`, which is
  what every browser sends) for an unknown path renders the app, so deep links
  and refreshes work;
* a **resource** request (an `<img>`, a stylesheet, a `fetch()`, `curl` — they
  all advertise `Accept: */*` or a concrete type) for a missing file gets an
  honest `404`, so a broken asset is visible in the network tab instead of being
  hidden behind an HTML page with status 200;
* anything under `/api/…` always gets its own JSON `404`.

**If the frontend is missing entirely**, the log says so and the API keeps
working:

```
INFO | serving the built frontend from .../frontend/dist
# or, if you have not built it yet:
DEBUG | no built frontend at .../frontend/dist (run `npm run build`)
```

---

## 5. `Speech recognition error: network` — the mic never transcribes

**What you see.** In the settings drawer's status line:

```
Speech recognition stopped. — The browser could not reach its speech service.
```

and in the console / error bar: `Speech recognition error: network`.

**What it actually means.** This error does **not** come from our backend, and it
usually does not mean *your* network is down. The Web Speech API is not local: in
Chrome, `SpeechRecognition` streams your microphone to *Google's* speech service.
`network` means Chrome could not reach **its own** service: no internet, a
proxy/firewall/VPN that blocks it, a corporate policy, or an offline machine.
Nothing the app does can fix that.

**What this repo does about it.** `frontend/src/audio/BrowserASR.ts` classifies
every error code, so behaviour is now predictable:

| Code | Treated as | What happens |
| --- | --- | --- |
| `network` | transient | Retry with exponential backoff (1 s → 30 s). After 4 consecutive failures the engine stops, and — if the backend has a real ASR engine — the client **switches to server-side ASR automatically** and says so. |
| `not-allowed` | fatal | "Microphone permission was blocked." No retry. |
| `service-not-allowed` | fatal | Needs a secure context. No retry. |
| `audio-capture` | fatal | No input device. No retry. |
| `no-speech`, `aborted` | normal | Ignored — Chrome fires these on every pause in speech. |
| anything else | reported | One message, no loop. |

It also **pauses while the avatar speaks**: the Web Speech API does its own
transcription from the microphone, so without that it happily transcribes the
TTS coming out of your speakers.

**Fix it, in order of preference**

1. **Let it switch.** With a real backend ASR engine (`asr.engine = local`,
   `openai` or `whisper`), one mic press after the failures switches you to
   server-side recognition permanently — the banner says so. It needs no internet
   from the browser at all.
2. **Check the obvious:** are you online? Is a VPN/proxy intercepting
   `*.google.com`? Does `chrome://settings/content/speech` allow this site?
3. **Use `localhost`, not a LAN IP,** if you can: a few Chromium builds refuse
   speech recognition on non-secure origins.
4. **Don't fight it.** Type in the box — it is the same code path as a final ASR
   result.

Diagnose with the status line rather than guessing (`Settings → Input`): it shows
the live state (`listening`, `retrying`, `paused`, `stopped`) plus the last
reason.

---

## 6. "The replies look like a fixed script that ignores my question"

**Symptom.** You ask one thing and get a reply that has nothing to do with it,
and the next question gets another reply from what feels like the same short
list. Nothing you say changes the answers.

**This was a real bug and it is fixed.** It was never the model: it was that
**no model was ever reached**. The shipped `backend/.env` had

```env
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=http://localhost:11434/v1      # Ollama
VTUBER_LLM__FALLBACK_TO_ECHO=true
```

Ollama was not installed, so the startup preflight declared the provider
unreachable and `FALLBACK_TO_ECHO=true` silently swapped in the offline `echo`
brain. Every turn was answered from a canned pool. The old `echo` implementation
made it worse by quoting the user back at them, which is why it looked like a
broken script rather than a missing model.

### How it behaves now

Startup order is **configured provider → auto-discovered local model → (only if
you asked for it) the offline brain → an honest warning**. Nothing is disguised.

```env
# Look for a running model on the usual local ports and adopt it.
VTUBER_LLM__AUTO_DISCOVER=true
# Never silently answer from a script.
VTUBER_LLM__FALLBACK_TO_ECHO=false
```

* `GET /api/health` → `preflight.llm` says exactly what happened, including
  `auto_discovered`, `canned` and `action_required`.
* The UI shows a banner built from that: *"No language model is connected, so I
  cannot answer questions yet…"* with a **Connect a model** button, or *"Offline
  demo mode: my words are a script, not a model."*
* The **Detect models** button in *Settings → Model* probes the ports and adopts
  a server in one click; **Test connection** proves it before you rely on it.

### Fixing it

1. Open **Settings → Model** and press **Detect models on this machine**. If a
   local server is running (Ollama, LM Studio, llama.cpp, Jan, vLLM) it appears
   with a **Use** button.
2. Nothing running? Start one — `ollama serve` + `ollama pull llama3.1:8b` — or
   pick a free hosted entry from the ready-made list and paste an API key. Then
   press **Test connection**; it does a real round-trip and reports the latency
   or the exact error.
3. Confirm the banner is gone. If it still says *Offline demo mode*, your
   `PROVIDER` is still `echo`.

The log line to look for at startup is now either:

```
SUCCESS | LLM auto-detected: Ollama -> llama3.1:8b (http://localhost:11434/v1)
```

or

```
WARNING | LLM is not reachable and no local model was found - set up a model in
          Settings -> Model, or set VTUBER_LLM__PROVIDER=echo to use the demo brain.
```

### If you *want* the offline brain

That is a supported mode, not a failure — set `VTUBER_LLM__PROVIDER=echo`. It
now reacts to the topic of your message (so "how do I fix my deadlines?" is
answered *about deadlines*), folds in what the memory profile knows, and never
repeats a reply until its pool is exhausted. It still cannot answer questions,
and it always says so.

### 6a. A real model that parrots

Some small local models are trained on instruct data that invites
*"You said: …"*. Three defences, all on by default:

| Defence | Where | What it does |
| --- | --- | --- |
| **Persona + conversation rules** | `backend/characters/default.yaml`, `CONVERSATION_PROMPT` | Explicitly forbids opening with "You said", "So you're saying", "It sounds like you"; requires reacting to the *intent* and adding something new. |
| **Sampling penalties** | `llm.presence_penalty` / `llm.frequency_penalty` / `llm.top_p` | Pushes the model off words it has already used, in the prompt *and* in its own output. `top_p < 1` also reduces loop-prone tail sampling. |
| **The reply-quality guard** | `app/pipeline/quality.py` + `_run_turn` | Measures how much of the reply's first sentence is copied from the user (or from the previous reply) and, above `llm.echo_threshold` / `llm.repeat_threshold`, throws the attempt away and regenerates **once** with a corrective system note. The client is told with `llm.restart` so it drops the streamed preview. |

The guard is safe by construction: it only ever judges the **first sentence**,
before anything has been queued for TTS, so a rejected attempt is never spoken
and cannot talk over audio. `metrics.retried` reports when it fired.

**Tune it:** `Settings → Model`.

* Replies still echo → lower `echo_threshold` (0.75 → 0.6).
* Legit replies get regenerated → raise it (0.75 → 0.9), or untick
  **Reply guard**.
* Model is repetitive but not echoing → raise `presence_penalty` (0.35 → 0.7) and
  `frequency_penalty`.

### 6b. The avatar answers its *own* voice (speakers on)

This one never reaches the model, so the reply guard cannot see it: the
microphone picks up the TTS, the ASR transcribes it, and the transcript arrives
looking like a user turn. On a laptop with speakers and no headphones this is the
single most common cause of a looping conversation.

The **transcript gate** (`app/pipeline/transcript.py`, always on) drops four
narrow classes before a turn is created and reports why:

```
← asr.final {"text":"","duration":null,"reason":"echo"}
```

| `reason` | means | fix |
| --- | --- | --- |
| `empty`, `tag`, `short` | nothing intelligible was heard | nothing — it is the desired behaviour |
| `noise` | the ASR hallucinated on near-silence ("Thanks for watching.") | nothing; add your own with `VTUBER_ASR__TRANSCRIPT__EXTRA_NOISE_PHRASES` |
| `echo` | a near-copy of the avatar's last sentences | see below |
| `duplicate` | the same transcript twice | off by default (`…TRANSCRIPT__DEDUPE`) |

If real speech is being flagged as `echo`:

* Raise `VTUBER_ASR__TRANSCRIPT__ECHO_SIMILARITY` (0.72 → 0.85).
* Headphones are still the real fix; the gate is a safety net, not a substitute.
* Turn the whole thing off with `VTUBER_ASR__TRANSCRIPT__ENABLED=false` to prove
  it is the gate (and not the reply guard) by elimination. `/ws` logs
  `ignored ASR transcript '…' (echo)` at `INFO` when it fires.

`len(cleaned) < min_chars` is the only length-based rule, and it defaults to 2 —
so "no", "ok" and "হ্যাঁ" all still get a turn.

### 6c. She answers in the wrong language, or Bengali sounds like noise

Two different failures, two different fixes. **Wrong language:** the reply comes
back in English when you wrote Bengali or Banglish. **Wrong voice:** the words
are right but the audio is unlistenable, because a Bengali sentence is being read
by an English voice.

| symptom | cause | fix |
| --- | --- | --- |
| Bengali script in, English out | the directive never reached the model | check the model actually follows the system prompt (a 3B model may not); try a larger model |
| `ami bhalo achi` in, English out | Banglish not detected as Bengali | needs **two** distinct markers from `_BANGLISH_MARKERS` (`app/pipeline/language.py`); a one-word message like "ki" is deliberately read as English — say a little more |
| Bengali text spoken with English phonemes | `tts.auto_voice` off, or a non-Edge engine | `VTUBER_TTS__AUTO_VOICE=true` (default) and `VTUBER_TTS__ENGINE=edge`; `openai` voices are styles, `browser` voices come from `tts.start.lang` |
| a Bengali voice was *not* used although one is configured | that is correct, and by design | a voice whose locale already matches the reply's language is never overridden |
| the accent changes halfway through a reply | impossible since this release | the language is decided once per turn, from *your* message, before the first sentence is synthesised — if it flickers, the reply is mixing languages and the model is at fault, not the voice |

The decision is visible: `INFO` logs the detected language, and every
`tts.start` frame carries `lang` (`bn-BD`, `en-US`) next to the `voice` actually
used. `GET /api/voices` lists what Edge offers if you want to pick a different
voice per language — put the id in the persona (`tts_voice`) and it wins
whenever the language matches.

---

## 7. The avatar does not move, or a clip looks wrong

The procedural idle needs no assets, so "no motion at all" always has one of
three causes: the animation layer is switched off, no clip loaded, or the clip's
skeleton was not recognised. `Settings → Motion` answers all three — the line
above the clip buttons reads, for example:

```
No clips loaded — using procedural motion
```

with the reasons listed underneath (missing file, `404`, unmapped skeleton, bad
JSON). Full pipeline, tuning and syntax live in [ANIMATIONS.md](ANIMATIONS.md).

| Symptom | Cause | Fix |
| --- | --- | --- |
| Completely static | **Use clips** off, or all of *Idle motion / Breathing / Body sway* off | Tick them back on. |
| `Could not load /animations/manifest.json` | no manifest (404) | Create it (an empty `{"clips": []}` works) or clear the manifest URL to silence it. |
| `… drives no bone of this avatar` | the file's skeleton is not humanoid, or the export has no armature | Re-export: see ANIMATIONS.md §3. |
| Avatar lies down / faces the wall | odd up-axis export | `rootRotation` in the manifest: ANIMATIONS.md §6. |
| Snaps at the end of a gesture | clip's last frame is far from the idle | Raise `fadeOut`, or trim the clip. |
| Walks off screen | root motion | `"positionStrength": 0`. |

---

## 8. The socket reconnects in a loop, or closes with `1008` / `1013`

**`1008` (policy violation)** is the server saying *"this connection will never
be accepted"* — usually the origin or the path (see §1). The client now treats it
as terminal: it stops auto-reconnecting and shows the server's own message
instead of hammering the port 1000 times. Fix the origin/path, then reload.

**`1013` (try again later)** is capacity (`server.max_sessions`). The client keeps
retrying, but with a long delay, and reports *"server at capacity - waiting"*.

**A drop with no code** (network change, sleep/wake, Wi-Fi switch) reconnects with
exponential backoff up to 15 s. Two improvements worth knowing:

* **Offline is respected.** While `navigator.onLine` is false nothing is retried;
  the socket reconnects immediately on the browser's `online` event.
* **`hello` is re-sent on the new session.** A reconnect gives the server a
  fresh session with empty history, and the old client used to believe it had
  already said `hello`. It would connect, look fine, and never speak. The
  client now resets that flag on every `ready`.

**Silent-but-connected (no audio, no replies).** Check the status pill's tooltip:
a *zombie* socket (open, no `pong` for 2.5 heartbeat intervals, i.e. 37.5 s) is
detected and forced to reconnect. Then check §6a — the *most* likely reason for a
connected client that answers nothing useful is the `echo` fallback.

**Audio never plays although everything else works.** The `AudioContext` is
suspended: the page has not seen a trusted gesture (or the tab was backgrounded
for a long time). The client now tries to resume on the first `tts.start`, and
the start overlay refuses to dismiss itself if `unlockAudio()` did not actually
succeed — it shows *"The browser blocked audio playback"* and retries on your next
click instead of going quiet.

---

## 9. The mouth does not move (or flaps constantly)

Lip-sync has two modes and the default changed to the simpler one:

```env
VTUBER_LIPSYNC__MODE=mouth        # default
```

* **`mouth`** drives *one* blendshape (`aa`) from the audio's RMS envelope. No
  viseme timeline is produced server-side at all.
* **`visemes`** is the older full mouth-shape timeline (`aa ih ou ee oh nn`), best
  on a model with well authored mouth shapes.

`Settings → Face` tunes whichever is active, live.

| Symptom | Fix |
| --- | --- |
| Mouth never moves | The model has no `aa` preset (check `frontend/public/models/README.md`), or lip-sync is off, or **Threshold** is too high for your voice → lower it (0.12 → 0.04). |
| Mouth is always slightly open | Threshold too low → raise it. |
| Mouth flaps/chews between syllables | Raise **Min hold** (60 → 100 ms) and **Hysteresis**. |
| Mouth opens far too wide / looks like shouting | Lower **Openness** (default 0.72) and **Max open** (default 0.68). Both are ceilings on the `aa` blendshape; `1.0` is a shout on a VRoid face. |
| Mouth barely opens | Raise **Openness** (`mouth_scale`) or **Max open**. |
| Mouth is open during silence | The envelope's noise floor: raise Threshold, or leave `mouth` mode and reduce `amplitude_floor` in `visemes` mode. |
| Nothing moves while the *browser* speaks (`format: "browser"`) | That transport's audio never enters the Web Audio graph (the OS speech synthesiser plays it), so neither the server envelope nor the analyser tap can see it, and the client uses a synthetic speech-shaped level for the utterance's duration. If it is still dead, the utterance never started — check §8 (audio) instead. |
| The mouth *stops* matching loud syllables that are clearly audible | That sentence arrived without an envelope (an engine that does not report one, or `lipsync.enabled=false` on the server). The analyser tap should have caught it; if it did not, the audio is playing outside Web Audio — check `tts.start.format` in the browser console. |

---

## 10. OBS shows a black frame, a wrong size, or no sound

**Symptom.** You added `http://localhost:8000/?streamer=1` as a Browser source and
the source is empty, or the avatar is there but the stream is mute.

**Check, in order:**

1. **Does the page work in a normal browser window?** Open the URL yourself
   first. A blank source that is also blank in Chrome/Firefox is a page problem
   (usually a missing `frontend/dist` build), not an OBS problem.
2. **Autoplay.** Capture tools normally permit it, but if not, streamer mode
   shows a single "Start talking" button at the bottom. In OBS, right-click the
   source → **Interact**, and click it once. The setting is then remembered for
   that source.
3. **Size and position.** Set the Browser source's width/height to your canvas
   (e.g. 1920×1080) and leave scaling to *Fit to screen*. The page fills whatever
   the source is.
4. **Transparency.** The page is deliberately transparent in streamer mode, so
   "black frame" is usually *nothing rendered yet* rather than a black
   background. If your scene is dark, add a bright colour source behind it while
   you debug; if you would rather have an opaque background, put a colour source
   behind it permanently.
5. **Sound.** The avatar's voice is produced by the page, so route the **Browser
   source's** audio: right-click the source → **Advanced Audio Properties** →
   tick *Monitor and Output* (or *Output*) for that source. A muted source in OBS
   is the usual cause of "the avatar talks but the stream is silent".
6. **Reloading the model.** Tick **Shutdown source when not visible** so the
   avatar reloads when you switch scenes — otherwise a long stream slowly
   accumulates GPU memory.
7. **It is not a `403`.** OBS's embedded Chromium loads the page from
   `http://localhost:8000`, which is the same origin as the API and the socket,
   so no CORS configuration is involved. If you are pointing OBS at a LAN
   address instead (`http://192.168.x.y:8000`), that origin *does* have to be in
   `VTUBER_SERVER__CORS_ORIGINS` / `WS_ALLOWED_ORIGINS` — see §1.

For the whole pipeline — where the `.vrm` comes from, virtual camera versus
browser source, and what is *not* built in — see
[**docs/STREAMING.md**](STREAMING.md).

---

## Verification recipe

The short version, from a clean state:

```powershell
cd backend

# 1. Make sure you are testing the code and config you think you are.
Get-Content .env | Select-String 'LLM__PROVIDER|ASR__ENGINE|FALLBACK|CORS_ORIGINS'

# 2. Install/refresh the deps (idempotent).
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Start it in the foreground so you can read the log.
.\.venv\Scripts\python.exe main.py
```

Expected output on a machine with no Ollama running and the shipped `.env`:

```
INFO     | running startup checks ...
WARNING  | LLM  openai     unreachable (ConnectError). ... Is your local server (Ollama / LM Studio / vLLM) running?
WARNING  | LLM is not reachable and no local model was found - set up a model in Settings -> Model ...
INFO     | TTS  edge       ready (needs internet at synthesis time)
INFO     | ASR  browser    the browser transcribes and sends text
SUCCESS  | all startup checks passed
INFO     | serving the built frontend from ...\frontend\dist
INFO     | ready -> http://127.0.0.1:8000/  (WebSocket: ws://127.0.0.1:8000/ws)
```

That is the **honest** first-run output: no model, said out loud. (Before the
model-setup work, the second line was
`LLM unreachable -> falling back to the offline 'echo' brain` and every reply
came from a script.) If a local model server *is* running, the same startup
prints `SUCCESS | LLM auto-detected: <server> -> <model> (<base_url>)` instead of
the two warnings. Either way the verdict is in `GET /api/health` under
`preflight.llm`.

Then, in a second terminal:

```powershell
# HTTP routes: 200, 200, 404-as-JSON, 200
curl.exe -s -o NUL -w "health %{http_code}`n"     http://127.0.0.1:8000/health
curl.exe -s -o NUL -w "api/health %{http_code}`n" http://127.0.0.1:8000/api/health

# What the UI is reading for its banner, and what backs the model-setup tab
curl.exe -s http://127.0.0.1:8000/api/health | Select-String "llm"
curl.exe -s http://127.0.0.1:8000/api/llm/presets | Select-Object -First 1
curl.exe -s -X POST http://127.0.0.1:8000/api/llm/discover
curl.exe -s http://127.0.0.1:8000/api/memory
```

And a full WebSocket + origin check (no browser needed):

```powershell
curl.exe -i http://127.0.0.1:8000/health | Select-Object -First 1
```

For the socket itself, open the app and watch the terminal — a healthy
connection logs `client connected: <id> (path=/ws, origin=http://localhost:5173, 1 open)`
with **no 403**, and DevTools → Network → WS shows `101 Switching Protocols`.

### Restarting cleanly

`Ctrl-C` in the backend terminal is enough; uvicorn closes the sessions and the
lifespan shuts them down (`shutdown complete`). Two cases need more care:

* **`python main.py --reload`** spawns a child process. If the port stays busy
  after `Ctrl-C`, the child survived:

  ```powershell
  Get-NetTCPConnection -State Listen -LocalPort 8000 | Select-Object OwningProcess
  Stop-Process -Id <the pid from above> -Force
  ```

* **A stale `.env`.** Configuration is read at *process start* only. Editing
  `.env` while the server runs changes nothing — restart it. (The settings
  drawer patches a *session* at runtime, which is a different mechanism.)

---

## Still stuck?

1. `GET /api/health` — per-engine status, the preflight report, the live session
   count, and which engines are even available in this install.
2. `GET /api/config` — the effective configuration (secrets masked). If a value
   here is not what your `.env` says, the `.env` is in the wrong directory or a
   real environment variable is overriding it (those win over the file).
3. The logs. Since this codebase logs *why* a decision was made, the warning text
   usually contains its own fix; if a line is obscure, that is a bug worth
   reporting.
