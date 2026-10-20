# Open VRM VTuber

A real-time AI VTuber you can run on your own machine: **microphone → speech
recognition → LLM → neural speech synthesis → a 3D VRM avatar that lip-syncs
and emotes at 60 FPS.**

The avatar is rendered with [Three.js](https://threejs.org/) +
[`@pixiv/three-vrm`](https://github.com/pixiv/three-vrm), the brain and voice
pipeline is Python (FastAPI + one WebSocket), and the whole thing speaks a
single, documented wire protocol so you can swap any part for your own.

```
                       ┌──────────────────────────── your browser ───────────────────────────┐
   you speak ──mic──►  │  React UI · Three.js stage · @pixiv/three-vrm                       │
                       │  mouth/viseme driver · Mixamo/VRMA animation clips on top of the    │
                       │  procedural idle (breathing, weight shift, gestures)                │
                       └───────▲──────────────────────────────┬──────────────────────────────┘
                               │  JSON control frames         │  JSON + raw PCM frames
                       ┌───────┴──────────────────────────────▼──────────────────────────────┐
                       │                     FastAPI · /ws  (one socket, two frame kinds)   │
                       │  ASR (faster-whisper / OpenAI / browser)                            │
                       │  LLM (OpenAI-compatible / Ollama / echo)  + reply-quality guard     │
                       │  emotion + gesture analysis  → VRM preset + skeleton hints           │
                       │  TTS (Edge neural voices / OpenAI / browser)                        │
                       │  lip-sync payload builder  → volumes[] (+ visemes[] if requested)   │
                       └─────────────────────────────────────────────────────────────────────┘
```

---

## Table of contents

1. [What is in the box](#what-is-in-the-box)
2. [Analysis of the two reference projects](#analysis-of-the-two-reference-projects)
3. [Quick start](#quick-start)
4. [Step-by-step setup](#step-by-step-setup)
5. [Configuring the brain and the voice](#configuring-the-brain-and-the-voice)
6. [Memory: remembering you](#memory-remembering-you)
7. [Making her yours](#making-her-yours)
8. [Adding an avatar](#adding-an-avatar)
9. [How the lip-sync works](#how-the-lip-sync-works)
10. [Animations](#animations)
11. [Streaming to OBS / Twitch](#streaming-to-obs--twitch)
12. [Running both servers at once](#running-both-servers-at-once)
13. [Production / single-port deployment](#production--single-port-deployment)
14. [Project layout](#project-layout)
15. [Testing and linting](#testing-and-linting)
16. [Troubleshooting](#troubleshooting)
17. [Changelog](#changelog)
18. [License](#license)

---

## What is in the box

| Piece | What it does |
| --- | --- |
| **`frontend/`** | React 19 + Vite + TypeScript UI. Owns the WebSocket, the Web Audio graph, the microphone capture (AudioWorklet), and the Three.js/VRM scene. |
| **`backend/`** | FastAPI app: `/ws` conversation socket, `/api/*` REST helpers, ASR → LLM → TTS pipeline, emotion/gesture analysis, viseme timeline builder, startup preflight. |
| **`docs/`** | `ARCHITECTURE.md` (how the pieces fit and why), `PROTOCOL.md` (the exact wire format) and `ANIMATIONS.md` (the Mixamo → avatar clip pipeline). |
| **`scripts/`** | One-command dev launcher for PowerShell and for bash. |

Highlights:

* **Real 3D, not Live2D.** Any `.vrm` model — VRM 0.x and VRM 1.0 both work
  (three-vrm converts 0.x on load).
* **Never stiff.** A procedural idle (breathing, weight shift, head drift, arm
  rest, hand-written gestures) runs with **no assets at all**, and authored
  Mixamo/VRMA clips blend on top of it per bone. See
  [Animations](#animations).
* **Audio-driven lip-sync, in two flavours.** The default `mouth` mode opens one
  blendshape from the *actual generated audio's* RMS envelope — no phoneme
  guessing, nothing to jitter. Switch to `visemes` for a full `aa ih ou ee oh`
  timeline snapped to the TTS word boundaries.
* **The face leads the voice.** Emotion and gesture events are sent *before* a
  sentence's audio so the avatar is already smiling when the words arrive.
* **Shortest possible latency path.** Sentence-level streaming: the LLM reply is
  split as it generates, and each sentence is synthesised, shipped and played
  while the next one is still being generated. Raw PCM over the same socket —
  no base64, no HTTP round trips.
* **It answers you, it does not repeat you.** The persona plus a reply-quality
  guard (which only ever judges the *first* sentence, before anything is spoken)
  reject parroting replies and regenerate once. `metrics.retried` tells you when.
* **It finds your model instead of faking one.** At startup the backend probes
  the ports local servers actually use (Ollama, LM Studio, llama.cpp, Jan, vLLM)
  and adopts the first real model it finds. If there is genuinely none, it says
  so — loudly, in the UI — rather than quietly answering from a script. See
  [Choosing a brain](#the-llm-vtuber_llm__).
* **One-click model setup.** *Settings → Model → Detect models on this machine*
  finds a running local server; the ready-made list fills in the four values for
  free hosted providers (Groq, Gemini, OpenRouter) so you never hand-edit
  `BASE_URL`. A **Test connection** button round-trips a real completion and
  reports the latency or the exact error.
* **It remembers you.** A single human-editable JSON profile
  (`backend/memory/profile.json`) holds your name, what you say you like and
  what you have been talking about, plus a rolling summary every few turns. No
  embeddings, no server, no model required — and *Forget everything* is one
  click. See [Memory](#memory-remembering-you).
* **Barge-in.** Speak while the avatar is talking and it stops mid-sentence.
* **A microphone that recovers.** The browser recogniser classifies every Web
  Speech error, backs off instead of looping on `network`, pauses while the
  avatar speaks, and — after repeated failures — switches to the server-side
  engine by itself.
* **Offline capable.** Ollama for the brain, faster-whisper for the ears, and
  optionally the browser's own Web Speech API for voice in/out. The `echo`
  provider is a *rehearsal* brain: it reacts to your wording and the topic you
  raised, streams and animates exactly like a model, and is always labelled as a
  script in the UI.
* **Streams to OBS.** `http://localhost:8000/?streamer=1` renders the avatar
  alone, chrome-free and transparent, with the session started — a drop-in OBS
  browser source. See [docs/STREAMING.md](docs/STREAMING.md).
* **Everything is swappable and validated.** LLM, TTS and ASR each have a
  factory with several engines; the settings drawer can patch a live session and
  the server validates every patch before applying it.

---

## Analysis of the two reference projects

### 1. [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)

**What it is.** A cross-platform desktop (Electron/Tauri style) + web voice
assistant front end for LLMs, with a Live2D avatar, built so that every stage of
the pipeline is a pluggable module. It is the closest thing to a de-facto
standard for "talk to an LLM through an anime avatar" projects.

**Architecture, as we read it.** The Python side is organised into
`asr/`, `tts/`, `llm/`, `agent/`, `mcp/`, `live2d_model/` and a WebSocket
message handler; the web front end is a Vite/React app that renders Live2D with
PixiJS. Configuration is YAML, with one file per "character" plus a global
config; models are addressed by name and loaded on demand.

**The ideas we deliberately adopted.**

| Idea in Open-LLM-VTuber | What we do with it |
| --- | --- |
| `volumes` — a normalised RMS envelope of the synthesised audio, one value per time slice. | Same concept. The backend computes it while decoding the audio and sends it as `tts.start.volumes`. |
| A text/phoneme timeline that maps time → mouth shape. | We build a proper **viseme timeline** (`[start, end, viseme, weight]`) and, when the TTS engine reports word boundaries, snap the phoneme boundaries to the real audio timing instead of trusting the G2P estimate. |
| Pluggable `asr` / `tts` / `llm` factories with a common base class. | Same shape: `app/{asr,tts,llm}/base.py` + `factory.py` + one module per engine, each with a `health_check()` that the startup preflight calls. |
| Character YAML describing personality, language, voice and expression mapping. | `backend/characters/*.yaml` — persona, system prompt, per-emotion VRM preset/weight/hold time, and emotion → gesture mapping. |
| Emotion tags such as `[happy]` and gesture notes such as `(laughs)` stripped from the spoken text but kept for display. | `app/emotion.py` does exactly this, and falls back to keyword/punctuation heuristics when the model ignores the instruction. |
| Interrupt / "the model was cut off" flow. | `interrupt { heard }` stops the audio, flushes the viseme queue, and stores what the user actually heard in history. |

**Where we diverge — and why.**

| Open-LLM-VTuber | Open VRM VTuber |
| --- | --- |
| Live2D avatar (2D, PixiJS). | **Three.js + `@pixiv/three-vrm` 3D avatar** — real humanoid bones, so idle motion, breathing, head drift and skeleton gestures come for free, and any VRM is a drop-in avatar. |
| Audio usually shipped as base64 inside JSON. | **Raw binary WebSocket frames** for PCM. Roughly a third less bandwidth, no decode-then-play step, and the frame ordering gives us the sentence association for free. |
| No explicit viseme vocabulary in the protocol. | An explicit one: `aa ih ou ee oh` (VRM mouth presets) plus `nn` for "closed" (consonants and pauses), mirrored by `capabilities.visemes` in the handshake. |
| The desktop app is a heavy bundle. | A plain web app: `npm run dev` + `python main.py`. No Electron required (add one later if you want). |

### 2. [three-vrm expressions example](https://pixiv.github.io/three-vrm/packages/three-vrm/examples/expressions.html)

**What it is.** The canonical minimal demo of three-vrm's expression system: it
loads one VRM, then drives `VRMExpressionManager` directly with sliders.

**What it teaches, and how we use it.**

| Mechanism in the example | Our use |
| --- | --- |
| `GLTFLoader` + `VRMLoaderPlugin` to obtain a `VRM` instance, and `VRMLoaderPlugin`'s `VRMExpressionLoaderPlugin` to make expressions available. | `src/vrm/VRMViewer.ts` registers `VRMLoaderPlugin` and calls `VRMUtils.rotateVRM0(vrm)` so VRM 0.x models face the camera; VRM 0.x expression names are normalised by the plugin (`joy → happy`, `a → aa`, `blink_l → blinkLeft`, …) so one code path drives both formats. |
| `vrm.expressionManager.setValue(presetName, weight)` with the official preset names — `happy`, `angry`, `sad`, `relaxed`, `surprised`, `aa`, `ih`, `ou`, `ee`, `oh`, `blink`, `blinkLeft`, `blinkRight`, `lookUp`, … — typed as `VRMExpressionPresetName`. | `src/vrm/constants.ts` pins the same vocabulary, and `src/types/config.ts` keeps `VisemeName = 'aa' \| 'ih' \| 'ou' \| 'ee' \| 'oh' \| 'nn'` so the compiler catches a typo in the protocol. |
| The critical `vrm.update(delta)` call each frame to flush blendshape weights into the model. | The render loop always ends with `controllers → vrm.update(dt) → renderer.render()`. Skipping `update()` is the single most common reason "nothing moves", so it is centralised in one place. |
| `vrm.lookAt.target = object` for eye tracking. | `LookAtController` drives the same API from the pointer position, with damping so the eyes do not jitter. |
| The example sets weights directly from sliders. | We never set a weight directly. Every blendshape goes through a smoothing stage: `w += (target - w) * (1 - exp(-dt / tau))`, which is frame-rate independent and is what stops a 60 FPS mouth from looking like a flip-book. |

The net effect: the expressions demo gives you the *API surface*, and this
project turns it into a *controller loop* — viseme timeline, blink scheduling,
emotion holds and look-at all competing for the same blendshape channels, with
priority rules and smoothing.

---

## Quick start

Prerequisites: **Python 3.11+**, **Node.js 20.19+**, and a modern Chromium/Firefox
browser. Nothing else — no ffmpeg, no GPU, no Docker.

```powershell
# 1. backend --------------------------------------------------------------
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env          # Ollama on localhost + Edge TTS + browser ASR
python main.py                       # http://127.0.0.1:8000

# 2. frontend (new terminal) ---------------------------------------------
cd frontend
npm install
npm run dev                          # http://localhost:5173
```

Open <http://localhost:5173>, click **Start talking** (browsers refuse to start
audio before a user gesture), and say hello.

Both launchers are wrapped up in one command:

```powershell
# Windows
.\scripts\dev.ps1
```

```bash
# macOS / Linux
./scripts/dev.sh
```

> **No local LLM yet?** Open the app and use **Settings → Model → Detect models
> on this machine** — it finds a running Ollama / LM Studio / llama.cpp / Jan /
> vLLM and adopts it in one click. If nothing is running, either
> `ollama pull llama3.1:8b` first, or pick one of the ready-made entries (a free
> hosted key takes a minute), then press **Test connection**. Until then the app
> says so out loud: a banner at the top of the screen states that no model is
> connected, because the shipped `.env` deliberately sets
> `VTUBER_LLM__FALLBACK_TO_ECHO=false`. That setting is the fix for the original
> "the replies are a fixed script" bug — a clear *"no model connected"* beats
> quietly faking a conversation. Set `VTUBER_LLM__PROVIDER=echo` if you *want* a
> scripted offline demo (it labels itself as one and still exercises TTS,
> lip-sync and the avatar).

### Connecting a hosted model (Groq), in four clicks

1. Get a free key at <https://console.groq.com/keys>. **Never paste it into a chat
   window, an issue or a screenshot** — only into this field.
2. **Settings → Model → Use** on the *Groq* entry (sets the base URL and model).
3. Paste the key into **API key** and press **Save**. It is written to
   `backend/secrets.json` (git-ignored, docker-ignored) and the row then shows a
   fingerprint — `gsk_1f2e****9c3a`. The real value never travels back to the
   browser, so if you ever see the full key on screen, that is a bug: please
   report it.
4. Press **Test connection**. A green result names the provider, the model and
   the round-trip time.

Saving applies to a **running session immediately** — no server restart. To keep
this machine's provider/model without touching the shared defaults, put them in
`backend/.env.local` (also git-ignored); it is read after `.env` and wins.

Verify the whole chain, including that no key leaked anywhere:

```powershell
backend\.venv\Scripts\python.exe scripts\smoke_llm.py    # live provider probe
backend\.venv\Scripts\python.exe scripts\check_secrets.py # nothing key-shaped in tracked files
```

If a key ever does leak, the procedure is in [`SECURITY.md`](SECURITY.md):
rotate first, then replace it in the UI.

---

## Step-by-step setup

### 1. Clone and get the two halves installed

```powershell
git clone <your-fork-or-this-folder> open-vrm-vtuber
cd open-vrm-vtuber
```

#### Backend

```powershell
cd backend
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Windows cmd
# .venv\Scripts\activate.bat
# macOS / Linux
# source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

Optional extras:

```powershell
# Offline speech recognition (faster-whisper + CTranslate2). First run
# downloads the Whisper weights from Hugging Face.
pip install -r requirements-local.txt

# Tests + linter
pip install -r requirements-dev.txt
```

#### Frontend

```powershell
cd ../frontend
npm install
```

### 2. Create your config

```powershell
cd ../backend
Copy-Item .env.example .env      # macOS/Linux: cp .env.example .env
```

Every setting has a working default; the interesting ones are in the next
section. `frontend/.env.example` is optional — see
[Frontend variables](#frontend-variables).

### 3. Start it

```powershell
# terminal 1 - API + WebSocket on :8000
cd backend
.\.venv\Scripts\python.exe main.py

# terminal 2 - UI on :5173 (proxies /api and /ws to :8000)
cd frontend
npm run dev
```

The backend prints a preflight summary of which engines answered
(abridged — loguru also prints timestamps and module names):

```
INFO     | running startup checks ...
INFO     | LLM  openai     reachable (200)
INFO     | TTS  edge       ready (needs internet at synthesis time)
INFO     | ASR  local      ready (model 'base.en' loads on first use)
SUCCESS  | all startup checks passed
INFO     | ready -> http://127.0.0.1:8000/  (WebSocket: ws://127.0.0.1:8000/ws)
```

Warnings are not fatal — the server always starts, and the same report is
available at `GET /api/health` (and shown in the settings drawer).

### 4. Talk to it

1. Open <http://localhost:5173>.
2. The avatar loads and the socket connects immediately; the greeting is
   withheld until the model is rendered and audio is unlocked.
3. Click **Start talking** → this is the user gesture that unlocks the
   `AudioContext`.
4. Type in the chat box, or click the microphone button and speak. Microphone
   input needs `localhost` or HTTPS (a browser security rule).

---

## Configuring the brain and the voice

All backend configuration lives in `backend/.env`. The prefix is `VTUBER_`,
nested keys use a double underscore: `VTUBER_<SECTION>__<FIELD>`.

### The LLM (`VTUBER_LLM__*`)

> **Start here.** You do not have to hand-edit anything. Start the app, open
> **Settings → Model**, and press **Detect models on this machine**. If a local
> server is running it is adopted in one click; otherwise pick one of the
> ready-made entries (a free hosted provider needs only an API key) and press
> **Test connection** to prove it works before you rely on it.

| Provider | When to use | Key settings |
| --- | --- | --- |
| `openai` *(default)* | Anything that speaks `POST /v1/chat/completions`: OpenAI, DeepSeek, Groq, Gemini, OpenRouter, LM Studio, vLLM, llama.cpp server, and Ollama through its `/v1` compatibility layer. | `BASE_URL`, `MODEL`, `API_KEY` |
| `ollama` | Native Ollama `/api/chat` (keeps Ollama-specific error messages, and `keep_alive` so a paused model does not reload). | `BASE_URL=http://localhost:11434`, `MODEL` |
| `echo` | No model at all: a *rehearsal* brain. It reacts to the topic you raised, streams and animates exactly like a model — and is always labelled as a script in the UI. Use it to test the avatar, TTS and lip-sync offline. | — |

```dotenv
# --- local, fully offline (recommended starting point) -------------------
VTUBER_LLM__PROVIDER=openai
VTUBER_LLM__BASE_URL=http://localhost:11434/v1
VTUBER_LLM__MODEL=llama3.1:8b
VTUBER_LLM__API_KEY=ollama
```

```dotenv
# --- free hosted, no GPU needed -------------------------------------------
# Groq:
VTUBER_LLM__BASE_URL=https://api.groq.com/openai/v1
VTUBER_LLM__MODEL=llama-3.3-70b-versatile
# Google AI Studio / Gemini:
VTUBER_LLM__BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
VTUBER_LLM__MODEL=gemini-2.0-flash
# OpenRouter (free tier):
VTUBER_LLM__BASE_URL=https://openrouter.ai/api/v1
VTUBER_LLM__MODEL=meta-llama/llama-3.3-70b-instruct:free
```

```dotenv
# --- OpenAI / DeepSeek ----------------------------------------------------
VTUBER_LLM__BASE_URL=https://api.openai.com/v1     # or https://api.deepseek.com/v1
VTUBER_LLM__MODEL=gpt-4o-mini                      # or deepseek-chat
VTUBER_LLM__API_KEY=sk-...
```

#### If the cloud is down: the fallback chain

A hosted model can be unreachable for an afternoon (no wifi, a rate limit, a key
you rotated). Rather than fail every turn, the app can hand over to a **backup
provider** — and tell you it did, in one dismissible line, so the quieter local
model is never a mystery.

```dotenv
# The default: a single local Ollama. Empty list = no backup, report an error.
VTUBER_LLM__FALLBACK_CHAIN=[{"provider":"ollama","model":"llama3.2:3b","base_url":"http://127.0.0.1:11434/v1"}]
VTUBER_LLM__FALLBACK_COOLDOWN=60
```

The rules that keep it honest:

* **Only before the first token.** A reply that has already started streaming is
  never finished by a different model — the change of voice and of facts is worse
  than an error.
* **The offline `echo` brain is never a link.** Canned text is not a fallback;
  `VTUBER_LLM__FALLBACK_TO_ECHO` stays the explicit opt-in.
* **A dead link is skipped for `FALLBACK_COOLDOWN` seconds**, so a broken endpoint
  costs one timeout a minute instead of one per turn — and the primary is retried
  first as soon as the cooldown lapses, so a recovered cloud model is used again
  without a restart.
* **A backup never receives another provider's key.** A link is either given its
  own `api_key` or uses a harmless literal, so a Groq credential can never be sent
  to a local server.

**Test connection** deliberately ignores the chain: it answers "does *this*
setting work", which a silent rescue by the backup would hide.

Other knobs: `TEMPERATURE`, `TOP_P`, `MAX_TOKENS`, `TIMEOUT`,
`MAX_CONTEXT_MESSAGES` (rolling history window), `EXTRA_BODY` (provider
specific JSON, e.g. `{"top_p": 0.9}`), and `SYSTEM_PROMPT_SUFFIX`.

#### Finding a model instead of faking one

The reason early builds looked like "a fixed script that ignores my question" was
this: the shipped `.env` pointed at `localhost:11434`, Ollama was not installed,
`FALLBACK_TO_ECHO` was on, and every turn was answered out of a canned pool. The
conversation was never reaching a model. Two settings make that impossible now:

```dotenv
# Look for a running model on the usual local ports and adopt it.
# Only local ports are probed — nothing is sent anywhere else.
VTUBER_LLM__AUTO_DISCOVER=true

# Do NOT silently swap in a script when the configured provider is down.
# A clear "no model connected" banner beats a fake conversation.
VTUBER_LLM__FALLBACK_TO_ECHO=false
```

Startup order is: **configured provider → auto-discovered local model → (only if
you asked for it) the offline brain → an honest warning.** `GET /api/health`
reports which one won under `preflight.llm`, including `auto_discovered`,
`canned` and `action_required`, and the UI shows a banner from it.

Ports probed: Ollama `11434`, LM Studio `1234`, llama.cpp `8080`, Jan `1337`,
text-generation-webui `5000`, KoboldCpp `5001`, vLLM `8000`/`8001`. Your own
server's port is always excluded, so a vLLM on `8000` can never be confused with
the app itself.

### Making the conversation good

Small local models have two habits that make a companion avatar feel broken:
they answer by *repeating the question*, and they loop. Three layers fix that,
all on by default:

| Layer | Where | What it does |
| --- | --- | --- |
| **Persona + conversation rules** | `backend/characters/default.yaml` + `CONVERSATION_PROMPT` in `app/pipeline/orchestrator.py` | Gives the character opinions, memory of the thread, an explicit ban on opening with *"You said…"* / *"It sounds like you…"*, and a language rule so it answers in Bengali (or English, or Banglish) the way you wrote. |
| **Sampling penalties** | `VTUBER_LLM__TOP_P`, `PRESENCE_PENALTY`, `FREQUENCY_PENALTY` | `top_p` below 1 trims the loop-prone tail of the distribution; the penalties push the model off words it has already used, in the prompt *and* in its own output. |
| **Reply-quality guard** | `VTUBER_LLM__QUALITY_GUARD`, `app/pipeline/quality.py` | Compares the reply's **first sentence** against the user's message (`echo_threshold`) and the previous reply (`repeat_threshold`). Over the line: the attempt is discarded *before it is spoken*, the client is told with `llm.restart`, and one corrective regeneration runs. |

The guard is safe by construction — it only ever judges the first sentence, so a
rejected attempt can never talk over audio. `metrics.retried` reports when it
fired; the sliders and the on/off switch live in **Settings → Model**.

```dotenv
VTUBER_LLM__TOP_P=0.9
VTUBER_LLM__PRESENCE_PENALTY=0.35     # raise to ~0.7 if it keeps circling one topic
VTUBER_LLM__FREQUENCY_PENALTY=0.35    # raise if it reuses its own phrases
VTUBER_LLM__QUALITY_GUARD=true
VTUBER_LLM__ECHO_THRESHOLD=0.75       # lower = stricter about parroting
VTUBER_LLM__REPEAT_THRESHOLD=0.8
```

> **Local model recipes.** Any of these work with `PROVIDER=openai`:
> `ollama pull llama3.1:8b` / `ollama pull qwen2.5:7b`, LM Studio on
> `http://localhost:1234/v1`, `llama.cpp`'s `server` on `http://localhost:8080/v1`,
> or vLLM. If you get a 404 on `/chat/completions`, you are probably pointing at
> a native (non-`/v1`) endpoint — use `PROVIDER=ollama` instead.

---

## Memory: remembering you

A companion that asks your name twice is not a companion. Memory is on by
default and needs no model, no embeddings and no extra service.

* **One file.** `backend/memory/profile.json`, UTF-8, human-editable. Open it in
  an editor and fix anything the extractor got wrong — the running app notices
  the file changed and reloads it.
* **Deterministic extraction.** `app/memory/facts.py` looks for *explicit*
  statements only ("my name is…", "I work as…", "I love…", "I can't stand…",
  plus Bengali equivalents, which are object-before-verb). Nothing is inferred
  from vague text: a memory that is wrong is worse than no memory.
* **A rendered prompt block.** `describe()` turns the profile into a short block
  ("Their name is Rahim. Their likes: coffee.") that is appended to the system
  prompt, with an instruction to *use* it the way a friend does rather than
  recite it.
* **A rolling summary.** Every `SUMMARISE_EVERY` turns (default 8) the recent
  conversation is compressed into two sentences. It runs as a separate task
  *after* the reply is queued, so it can never delay what you are listening to.
  With the offline brain it is built from tracked topics instead of a model call.
* **Bounded.** Every list has a cap (facts 40, likes 16, dislikes 12, topics 14,
  summary 400 chars, prompt block 900 chars), because a memory that grows forever
  eventually crowds out the actual conversation.

```dotenv
VTUBER_MEMORY__ENABLED=true
VTUBER_MEMORY__PATH=memory/profile.json     # relative to backend/, or absolute
VTUBER_MEMORY__SUMMARISE_EVERY=8            # 0 disables summarising
VTUBER_MEMORY__INJECT=true                  # put the profile into the prompt
```

**Settings → Memory** shows exactly what it knows (name, facts, likes, dislikes,
recent topics, summary, turn count) with a **Forget everything** button, and
`GET`/`DELETE /api/memory` do the same over HTTP. `backend/memory/` is
git-ignored, and the test suite redirects every write to a temporary file, so a
`pytest` run can never touch yours.

### Language

The persona answers in the language it is addressed in, and the voice follows
that language out loud. Both halves are enforced rather than hoped for.

* **Naming the language.** `app/pipeline/language.py` identifies the *script* of
  your last message (Bengali, Hindi, Japanese, Arabic, Korean, Tamil, Telugu,
  …) and the orchestrator states it explicitly in the prompt ("The user just
  wrote in Bengali (বাংলা). Reply in Bengali (বাংলা).").
* **Banglish counts as Bengali.** `ami bhalo achi` or `ki koro tumi` is Bengali
  typed with a Latin keyboard, which is how a very large number of speakers
  actually type. A conservative marker-word heuristic detects it (two *distinct*
  Bengali function words, so a stray "ki" in an English sentence never flips
  it), the prompt asks for an answer in Bengali script with naturally-English
  words kept English, and the reply is spoken by the **Bengali** voice — a
  Bengali neural voice reading romanised Bengali is still Bengali, while an
  English voice reading Bengali script is noise.
* **One language per reply, one voice per reply.** The language is decided from
  *your* message before the first sentence is synthesised, so a reply cannot
  change accent halfway through. Edge gets a matching female voice per language
  (`app/tts/voices.py`; `bn` → `bn-BD-NabanitaNeural`) and the `tts.start` frame
  carries a BCP-47 `lang`, which is how browser-side `speechSynthesis` picks one
  too. A voice you chose *for that language* is never overridden; the switch can
  be turned off with `VTUBER_TTS__AUTO_VOICE=false`.
* **The guard works in all three.** The reply-quality heuristics (parroting,
  repetition) tokenise by Unicode category, not by Python's `\w` class — which
  silently discards combining marks and would have made the guard a no-op in
  Bengali, the one language this app is built for.

### The voice, TTS (`VTUBER_TTS__*`)

| Engine | Latency | Needs internet | Word timings | Notes |
| --- | --- | --- | --- | --- |
| `edge` *(default)* | ~300 ms to first audio | yes (Microsoft) | **yes** | Free, ~1000 high-quality neural voices. Best lip-sync because the real word boundaries let us align visemes precisely. |
| `openai` | depends on server | no, if local | no | Any `/v1/audio/speech` endpoint, including local ones (Piper gateways, Kokoro-FastAPI, …). |
| `browser` | instant | no | no | **No server audio at all.** The backend still streams a viseme timeline and the client speaks it with the Web Speech API. 100% offline fallback. |

```dotenv
VTUBER_TTS__ENGINE=edge
VTUBER_TTS__VOICE=en-US-AriaNeural     # ja-JP-NanamiNeural, zh-CN-XiaoxiaoNeural, ...
VTUBER_TTS__RATE=+0%
VTUBER_TTS__PITCH=+0Hz
VTUBER_TTS__VOLUME=+0%
VTUBER_TTS__FORMAT=pcm_s16le           # pcm_s16le (lowest latency) | mp3
VTUBER_TTS__SAMPLE_RATE=24000
VTUBER_TTS__CHUNK_MS=200
```

Browse the full voice list from the settings drawer or directly:

```
http://127.0.0.1:8000/api/voices?engine=edge&locale=ja
```

### The ears, ASR (`VTUBER_ASR__*`)

| Engine | Notes |
| --- | --- |
| `local` | `faster-whisper` in-process. Offline, CPU or CUDA. Needs `requirements-local.txt` (~250 MB of wheels). This is the *code* default. |
| `openai` | Any `/v1/audio/transcriptions` endpoint (OpenAI, Groq's `whisper-large-v3-turbo`, self-hosted). |
| `browser` | The browser transcribes with the Web Speech API and sends `chat` text; the backend never receives audio. No install at all — and the value shipped in `.env.example`, so a fresh clone has a working microphone out of the box. Chrome/Edge/Safari only. |

```dotenv
VTUBER_ASR__ENGINE=local
VTUBER_ASR__MODEL=base.en        # tiny.en is faster, small.en / medium.en more accurate
VTUBER_ASR__DEVICE=auto          # auto | cpu | cuda
VTUBER_ASR__COMPUTE_TYPE=int8    # int8 | int8_float16 | float16 | float32
VTUBER_ASR__LANGUAGE=en          # leave empty to auto-detect
VTUBER_ASR__MIN_SPEECH_SECONDS=0.35

# The transcript gate: what happens to a transcript once we have one. Stops the
# avatar answering its own voice when you are on speakers. A bare "no" still
# works; only silence, known ASR hallucinations, speaker echo and (opt-in)
# double submits are dropped. Rejections arrive as asr.final with a `reason`.
VTUBER_ASR__TRANSCRIPT__ENABLED=true
VTUBER_ASR__TRANSCRIPT__ECHO_GUARD=true
VTUBER_ASR__TRANSCRIPT__ECHO_SIMILARITY=0.72
```

> With speakers on, the microphone hears the TTS and the ASR transcribes it —
> which looks like a user turn and makes the avatar answer itself. The gate
> catches that (`reason: "echo"`) *and* Whisper's silence hallucinations such as
> "Thanks for watching." (`reason: "noise"`) before a turn is ever created. See
> [§6c](docs/TROUBLESHOOTING.md#6c-the-avatar-answers-its-own-voice-speakers-on).

The same three choices — and every voice/model/character — can be changed at
runtime from the **settings drawer** (gear icon). The drawer sends a `config`
message; the server validates the patch with the same Pydantic models it uses at
boot, applies it to that session only (each tab gets its own copy), and answers
with `config.ack`.

### The server (`VTUBER_SERVER__*`)

```dotenv
VTUBER_SERVER__HOST=127.0.0.1
VTUBER_SERVER__PORT=8000
VTUBER_SERVER__SERVE_FRONTEND=true          # serve frontend/dist from this port
VTUBER_SERVER__MAX_SESSIONS=8
```

```dotenv
# Console level and the rotating file log. Add the second line to also get one
# JSON object per line (for a log aggregator, or `jq`).
VTUBER_LOG__LEVEL=INFO
# VTUBER_LOG__JSON_FILE=logs/backend.jsonl
```

Browsers enforce two different gates, and both are covered by the same list:

```dotenv
# CORS for the REST API, and the default for the WebSocket check below.
VTUBER_SERVER__CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173","http://localhost:8000","http://127.0.0.1:8000"]
# Extra origins allowed to open the WebSocket; ["*"] disables the check.
VTUBER_SERVER__WS_ALLOWED_ORIGINS=[]
```

`localhost` and `127.0.0.1` are two different origins as far as a browser is
concerned, so both spellings are listed for both ports — that mismatch is the
usual reason something works in the address bar and fails in a script.
**Same-origin connections are always allowed**, so a LAN address
(`http://192.168.1.20:8000`) or a real domain needs no configuration at all;
`WS_ALLOWED_ORIGINS` is only for a frontend hosted somewhere else.

If a handshake is rejected you get a readable reason rather than a bare failure:

```json
{ "type": "error", "code": "origin", "fatal": true,
  "message": "Origin 'http://evil.example.com' is not allowed to open a WebSocket. Allowed: ..." }
```

Relative to the canonical `/ws` path: `ws://host/` is accepted as an alias, and
any other path is answered with `code: "path"` naming the right URL. See
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#1-websocket--403--connection-rejected-403-forbidden)
for the full picture (including why uvicorn's `403 Forbidden` line is not a CORS
verdict).

### Frontend variables

`frontend/.env.local` (copy `frontend/.env.example`), all optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VITE_WS_URL` | *(empty → page origin)* | Override the WebSocket URL, e.g. when the backend is on another host. |
| `VITE_VRM_URL` | *(empty → backend config → placeholder)* | Avatar to load on startup. |
| `VITE_CAMERA_PRESET` | `bust` | `face` \| `bust` \| `full`. |
| `VITE_PIXEL_RATIO` | `2` | Device-pixel-ratio cap; lower it on weak GPUs. |
| `VITE_BACKEND` | `http://127.0.0.1:8000` | Dev-server proxy target (read by `vite.config.ts`). |

Leaving `VITE_WS_URL` empty is the recommended setup: the client derives
`ws(s)://<page-host>/ws`, which the Vite dev server proxies in development and
FastAPI serves in production. One contract, no CORS, no URL juggling.

---

## Making her yours

**Settings → Persona** edits who she is: name, tagline, default language,
greeting, system prompt, voice and rate. Behind it:

* **Your edit is not written into the shipped file.** It is saved beside it as
  `backend/characters/<id>.user.json` and merged on top when the persona loads.
  The `.yaml` keeps its comments and stays the reference, which is what makes
  **Reset** a one-click undo; the `.user.json` file is git-ignored (and
  docker-ignored), so a persona you tune never ends up in a commit.
* **It cannot break her.** Values are whitespace-normalised, stripped of control
  characters and length-capped (the system prompt is sent on *every* turn, so a
  paste accident is an expensive one), then the merged persona is re-validated.
  A hand-mangled `.user.json` is ignored, not fatal. An unknown field name is a
  `422`, not a silent no-op.
* **It applies live.** Saving sends `character.reload`, and the running session
  re-reads the file and answers with a `config.ack` — the name, language and
  voice change without restarting the server, and *without* clearing the
  conversation (switching to a different persona still does).
* **Empty means empty.** Clearing the greeting box silences the greeting on
  connect; clearing any other box drops the edit and the shipped value returns.
* **One exception to "live":** the greeting is spoken on *connect*, so a new one
  is heard from the next page load (the name, language and voice change
  immediately).

The same thing over HTTP, same-origin only:

```http
GET    /api/character?id=default      # as edited, next to what it shipped as
PUT    /api/character                 # {"id": "default", "fields": {"name": "Tuki"}}
DELETE /api/character?id=default      # forget every edit
```

```dotenv
VTUBER_AVATAR__CHARACTER=default      # which characters/<id>.yaml to load
```

---

## Adding an avatar

1. Drop a `.vrm` file into `frontend/public/models/`.
2. Open the settings drawer → **Avatar → Model**; your file appears as
   `/models/<name>.vrm`. You can also set `VTUBER_AVATAR__VRM_URL` in
   `backend/.env`, or `VITE_VRM_URL` in `frontend/.env.local`.

With no model at all, the app loads three-vrm's sample VRM from a CDN so a fresh
clone has something on screen.

### Tinting the shirt and painting a blush

Most VRoid-style avatars have no `blush` blendshape at all, and a T-shirt is a
single material shared across the whole torso — so both are done at runtime in
`frontend/src/vrm/AppearanceController.ts`:

* **Shirt** — the tint is *multiplied* into the base colour texture, so the
  folds, seams and fabric shading the artist drew survive. It finds the shirt by
  material name (`tops`, `shirt`, `top_`, `jacket`, … while excluding
  `bottom`, `skirt`, `shoe`, `hair`, `skin`, …).
* **Blush** — painted into a *copy* of the face texture at two cheek UVs and
  swapped in as `material.map`, keeping `flipY` and `colorSpace` identical. If a
  model *does* have a blush blendshape, that is used instead.
* Intensity follows the emotion: `shy` 0.9, `happy` 0.45, `surprised` 0.5,
  `angry` 0.3, `relaxed` 0.35, `neutral` 0.15.

Controls are in **Settings → Avatar → Shirt & blush** and persist across reloads.
On load the console prints a table of every material with its shader, base colour,
shade colour and whether it has a texture, plus which one was classified as the
shirt — so a differently named avatar can be diagnosed instead of guessed at. If
the blush lands in the wrong place on your model, the two numbers to nudge are
`BLUSH.left` / `BLUSH.right` (UV space) at the top of that file.

What the app wants from a model:

| Feature | Used for | Required? |
| --- | --- | --- |
| `aa ih ou ee oh` mouth presets | lip-sync | **yes** — without them the mouth cannot move |
| `blink` (or `blinkLeft`/`blinkRight`) | blinking | recommended |
| `happy angry sad relaxed surprised` | emotion | recommended |
| Humanoid bones (`head`, `chest`, `hips`, arms) | idle motion, gestures + [animation clips](docs/ANIMATIONS.md) | recommended |
| Look-at (VRM 1.0 `lookAt` / VRM 0.x blendshapes) | eye tracking | optional |

Where to get one: [VRoid Studio](https://vroid.com/en/studio) (make your own),
the [three-vrm sample models](https://github.com/pixiv/three-vrm/tree/dev/packages/three-vrm/examples/models),
or the [VRM Consortium](https://vrm.dev/en/) model list. Check the licence of
any model before redistributing it. To give it authored motion, see
[docs/ANIMATIONS.md](docs/ANIMATIONS.md).

You can sanity-check a file without a 3D engine:

```powershell
curl "http://127.0.0.1:8000/api/vrm-models/myavatar/info"
# {"name":"myavatar","meta_version":"1","preset_expressions":["aa","blink","happy",...], ...}
```

If `preset_expressions` lacks `aa`/`oh`, that model will not lip-sync.

---

## How the lip-sync works

This is the part that makes or breaks the illusion, so it is worth being precise.
There are two modes, and the default is the simple one:

```dotenv
VTUBER_LIPSYNC__MODE=mouth        # mouth | visemes
```

| Mode | What drives the mouth | When to use |
| --- | --- | --- |
| **`mouth`** *(default)* | One blendshape (`aa`), opened by the part of the audio's RMS envelope above `mouth_threshold`. | Almost always. It cannot jitter, it needs no phoneme data, and it reads as natural on stylised avatars. |
| **`visemes`** | A full `aa ih ou ee oh nn` mouth-shape timeline from a rule-based grapheme-to-viseme step. | When your model has well authored mouth shapes and you want consonants to visibly close the mouth. |

**1. The backend always ships the envelope, and the timeline only if asked.**
For every sentence it produces:

* `volumes`: a normalised RMS envelope of the real audio, one value per
  `lipsync.slice_ms` (default 20 ms) — **always sent**, it drives both modes,
* `visemes`: a compact list of `[start, end, viseme, weight]` frames in seconds,
  covering the whole utterance (no gaps) — **only built and sent in `visemes`
  mode** (it is an empty list in `mouth` mode, so the payload is small),
* the audio itself, as raw 16-bit PCM.

`tts.start` carries `lipsync_mode` **per sentence**, so patching the mode
mid-reply cannot strand a sentence with the wrong driver.

**2. In `visemes` mode, phonemes come from the text and timing from the audio.**
A rule-based grapheme-to-phoneme step (complete kana → romaji table, so Japanese
works too) turns the sentence into phonemes, which map onto the six visemes:
`aa ih ou ee oh` for vowels and `nn` for consonants, pauses and silence. That
estimate is then **scaled to the real audio duration and snapped to Edge TTS's
`WordBoundary` events** when the engine provides them. Frames shorter than 45 ms
are merged, because a 30 ms mouth shape just reads as flicker.

**3. The client plays the audio and the payload together.**
`AudioPlayer` schedules decoded buffers into a Web Audio graph and exposes the
playback position; `LipSyncController` samples the envelope (or the viseme
timeline) at that position and writes weights. Each utterance is a queue entry,
so a backend that sends sentences faster than they play (which it does,
deliberately) cannot desynchronise the mouth.

**3a. Three sources of loudness, in order of trust.** The mouth is never driven
by a guess when a measurement exists:

| # | Source | When it is used |
| --- | --- | --- |
| 1 | The sentence's `volumes` envelope | Whenever the backend sent one. It is measured from the real waveform and aligned to that sentence's clock. |
| 2 | A live `AnalyserNode` tap (`audio/TtsLevelMeter.ts`) | Whenever the sentence is being played through Web Audio and there is no envelope. RMS over a ~21 ms window, noise-gated and normalised. This is the "drive the mouth from the sound that is coming out" path. |
| 3 | A synthetic speech-shaped level | Only for `TTS=browser`, where the OS speech synthesiser plays the audio and the page never sees it. |

The envelope is also **linearly interpolated** between its 20 ms slices rather
than looked up with nearest-neighbour, because a 50 Hz staircase is slower than
the frame rate and shows up as stepping.

**4. Nothing snaps.** Every weight is smoothed with an exponential filter whose
time constant is in milliseconds rather than frames:

```ts
weight += (target - weight) * (1 - Math.exp(-dt / tau));   // tau = attack/release
```

Defaults are 40 ms attack and 120 ms release (`VTUBER_LIPSYNC__ATTACK_MS` /
`RELEASE_MS`, tunable live from the settings drawer). The mouth opens quickly
and closes softly, which is what human speech does. In `mouth` mode that filter
sits *after* a hysteresis band and a minimum dwell time
(`mouth_hysteresis`, `mouth_min_dwell_ms`), so a level hovering on the threshold
cannot make the mouth flutter. **A barge-in is the one exception:** the audio
stops dead, so the close is cut to a 60 ms constant — instant enough to stay
attached to the silence, still a fade rather than a snap.

**5. The envelope keeps the mouth honest.** In `mouth` mode it *is* the signal:
the part of the envelope above the threshold is shaped by a mild curve, scaled by
`mouth_scale` (0.72) and capped at `mouth_max` (0.68), so a loud vowel opens
wider and a soft one barely moves — without turning a normal syllable into a
fully open jaw. `mouth_max` is a ceiling because the envelope is normalised to
its own peak: on a VRoid face, `aa` at 1.0 is a shout. In `visemes` mode the same
ceiling applies to the viseme weights, floored by `amplitude_floor` (0.45) so
consonants never fully disappear.

**6. Blink, emotion and look-at share the same channels** through explicit
priority rules: a `surprised` expression with `override_blink: true` freezes
blinking, a blink is suppressed while an emotion hold is active, and the
`nn` viseme explicitly zeroes all five mouth presets so a closed mouth really is
closed. In `mouth` mode the mouth is cleared on a real pause instead.

> Two edge cases worth knowing. With `TTS=openai` there are no word boundaries,
> so `visemes` mode fits its timing to the audio duration instead of snapping.
> With `TTS=browser` there is no server audio at all, so **no envelope is
> available either** — and because the OS speech synthesiser's output never
> enters the Web Audio graph, the analyser cannot see it either. That is the one
> case where source 3 (the synthetic level) is used, and it is the honest
> best available: there is nothing to measure.

---

## Animations

The avatar is never static: a procedural idle (breathing, weight shift, head
drift, relaxed arms, hand-written `nod`/`shake`/`tilt`/`wave`/`think`/`sigh`/
`laugh`/`lean_in` gestures) runs with **no downloaded assets at all**. Authored
clips from Mixamo sit on top of it, one manifest entry each:

```json
// frontend/public/animations/manifest.json
{
  "version": 1,
  "idleSeconds": 14,
  "clips": [
    { "name": "idle", "url": "/animations/idle.glb", "kind": "idle" },
    { "name": "wave", "url": "/animations/wave.glb", "kind": "gesture" }
  ]
}
```

The two layers share one skeleton: an `AnimationMixer` writes the bones a clip
animates and the procedural layer runs *after* it, claiming any bone the clip
does not (and blending across the fade-in instead of fighting it, so a clip
starts from the live idle pose rather than dipping through a T-pose). Formats
accepted per clip: **`.vrma`** (highest quality, model independent),
**`.glb`/`.gltf`**, and raw Mixamo **`.fbx`** — the latter two are retargeted by
matching bone *names* canonically, so `mixamorig:LeftForeArm`,
`J_Bip_L_LowerArm` and `Bip01 L Forearm` all resolve to the same bone.

Everything is client-side and per-browser, no backend involvement; **Settings →
Motion** shows exactly what loaded (or why nothing did) and can reload clips
without a refresh.

Full pipeline — Mixamo → Blender → `.glb`, the `.vrma` route, the bone-name
rules, every manifest field, and the fixes for *"my avatar is lying down"* /
*"it walks off screen"* / *"it snaps"* — is in
[**docs/ANIMATIONS.md**](docs/ANIMATIONS.md).

---

## Streaming to OBS / Twitch

This app is the brain and the body; OBS is the camera. Point an OBS **Browser**
source at:

```
http://localhost:8000/?streamer=1
```

`?streamer=1` hides every control, drops the decorative backdrop so the page is
**transparent** (the WebGL renderer is already `alpha: true` with a clear alpha
of 0), and starts the session on its own — so it behaves like a native VTuber
capture surface rather than a web page. `?mode=streamer`, `?clean` and `?obs`
work too, and `?autostart=1` asks for the auto-start alone.

The same switch is in **Settings → Avatar → Streaming (OBS)** for a normal
browser window, which is what you use with OBS's *Start Virtual Camera* when a
browser source will not do (Vupechat, a second machine, another app).

Where your `.vrm` comes from — VRoid Studio, Booth, or the Mannequin generator —
and how that maps onto the free VTuber methods, is covered in
[**docs/STREAMING.md**](docs/STREAMING.md), along with audio routing, autoplay,
and an honest note about what is *not* built in (reading your chat).

---

## Running both servers at once

### One command

```powershell
# Windows (opens the backend in a job and the Vite server in the foreground)
.\scripts\dev.ps1

# macOS / Linux
./scripts/dev.sh
```

Both scripts also accept flags to skip a half:

```powershell
.\scripts\dev.ps1 -BackendOnly
.\scripts\dev.ps1 -FrontendOnly
```

### Two terminals, by hand

| Terminal | Command | Serving |
| --- | --- | --- |
| 1 | `cd backend && .\.venv\Scripts\python.exe main.py` | `http://127.0.0.1:8000` (`/ws`, `/api/*`) |
| 2 | `cd frontend && npm run dev` | `http://localhost:5173` (proxies `/api` + `/ws` to 8000) |

Useful backend flags:

```powershell
python main.py --port 8765        # different port (remember VITE_BACKEND)
python main.py --host 0.0.0.0     # expose on the LAN — front it with HTTPS
python main.py --reload           # auto-reload while hacking on Python
python main.py --no-frontend      # never serve frontend/dist
python main.py --log-level DEBUG
```

---

## Production / single-port deployment

The backend can serve the built frontend itself, so the whole app lives on one
port with the same origin contract — no CORS, no proxy, no second process.

```powershell
# 1. build the UI
cd frontend
npm ci
npm run build                     # -> frontend/dist

# 2. serve everything from FastAPI
cd ../backend
$env:VTUBER_SERVER__SERVE_FRONTEND = "true"     # default is already true
.\.venv\Scripts\python.exe main.py               # http://127.0.0.1:8000
```

`serve_frontend` only mounts if `frontend/dist/index.html` exists, so a stale or
missing build degrades to "API only" with a log line rather than a 500.

Notes for a real deployment:

* Put a TLS reverse proxy (Caddy/nginx) in front; microphone access and
  `getUserMedia` require a secure context on anything but `localhost`.
* Set `VTUBER_SERVER__CORS_ORIGINS` to your real origins and
  `VTUBER_SERVER__MAX_SESSIONS` to something sane if you expose it publicly.
* API keys live in `backend/secrets.json` (written by **Settings → Model**) or in
  an environment variable, both git-ignored and docker-ignored. They are never
  sent to the browser: `/api/config` and `/api/llm/status` return a fingerprint
  (`gsk_1f2e****9c3a`) and never the value, `PUT /api/llm/key` is write-only, and
  a patch that arrives containing `****` keeps the stored secret. Every log sink
  is wrapped in a redacting writer, so a key cannot reach the terminal, the log
  file or a traceback. See [`SECURITY.md`](SECURITY.md).
* `Dockerfile` + `docker-compose.yml` are included as an optional convenience:
  a multi-stage image (frontend build → FastAPI runtime) and a compose file with
  an optional bundled Ollama service. `docker compose up --build`, then
  `docker compose exec ollama ollama pull llama3.1:8b`. See the comments at the
  top of the compose file — or skip Docker entirely and use `scripts/dev.ps1`.

---

## Project layout

```
open-vrm-vtuber/
├── backend/
│   ├── main.py                     # entry point: python main.py
│   ├── app/
│   │   ├── config.py               # Pydantic settings + character files + patches
│   │   ├── schemas.py              # the wire protocol (mirrored by types/protocol.ts)
│   │   ├── session.py              # per-tab session: handshake, routing, history
│   │   ├── emotion.py              # tags/heuristics -> VRM preset + gestures
│   │   ├── logging_setup.py
│   │   ├── llm/                    # base · factory · openai_compatible · ollama · echo · discover
│   │   ├── tts/                    # base · factory · edge · openai · browser
│   │   ├── asr/                    # base · factory · local · openai
│   │   ├── lipsync/                # visemes (G2P, kana) · timeline (build + snap)
│   │   ├── memory/                 # profile store (atomic JSON) · deterministic fact extractor
│   │   ├── pipeline/               # orchestrator · sentence_splitter · speech · quality
│   │   │                           # transcript gate · language detection
│   │   ├── server/                 # app factory · http_routes · websocket · origins · static · preflight
│   │   └── utils/                  # audio (decode/trim/normalise) · text
│   ├── characters/default.yaml     # persona + language + emotion/gesture mapping
│   ├── memory/profile.json         # your long-term memory (created at runtime, git-ignored)
│   ├── tests/                      # pytest: protocol, pipeline, lip-sync, quality, transcript, origins,
│   │                               #        brain, memory, model discovery
│   └── requirements*.txt, pyproject.toml, .env.example
├── frontend/
│   ├── src/
│   │   ├── core/conversation.ts    # the app runtime (socket + audio + controllers)
│   │   ├── net/                    # WebSocketClient · REST api
│   │   ├── audio/                  # engine · AudioPlayer · SpeechSynthesisPlayer
│   │   │                           # MicrophoneCapture · BrowserASR · TtsLevelMeter
│   │   ├── vrm/                    # VRMViewer + Expression/LipSync/Blink/LookAt/Motion
│   │   │   └── animation/          # clip layer: AnimationController · retarget · loaders
│   │   ├── state/store.ts          # zustand: chat, session, metrics, persisted prefs
│   │   └── components/             # AvatarStage · ChatPanel · ControlBar · SettingsDrawer
│   ├── public/pcm-worklet.js       # 16 kHz Int16 microphone blocks
│   ├── public/animations/          # manifest.json + your .vrma/.glb/.fbx clips
│   └── public/models/              # drop your .vrm files here
├── docs/ARCHITECTURE.md, docs/PROTOCOL.md, docs/ANIMATIONS.md,
│        docs/STREAMING.md, docs/TROUBLESHOOTING.md
├── scripts/dev.ps1, scripts/dev.sh
├── Dockerfile, docker-compose.yml, .dockerignore
└── README.md, LICENSE, .gitignore
```

---

## Testing and linting

```powershell
# backend
cd backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app main.py tests

# frontend
cd frontend
npm run typecheck                             # tsc --noEmit
npm run build                                 # tsc -b && vite build
```

The backend suite covers the lip-sync payload maths (both modes, including kana
and word-boundary snapping), the sentence splitter, the speech/pipeline helpers,
the reply-quality heuristics (parroting, repetition, and the one-shot
regeneration path driven through a scripted LLM), the input-side transcript gate
(silence, sound tags, Whisper hallucinations, speaker echo, Bengali/Japanese
normalisation, and both ASR paths through the orchestrator) and — most usefully —
an end-to-end WebSocket test that walks the real protocol:
`ready → hello → hello.ack → greeting → chat → tts.start/tts.end`, plus config
patching, interrupts, ping/pong and rejection of unknown messages. It also pins
the boot-time behaviour that is easiest to regress: which origins may open a
socket, the `/ws` + `/` aliases, `/health`, the SPA fallback versus a real
`404`, the one JSON error shape for the HTTP surface, and the LLM
fallback-to-echo path. The newer suites cover the pieces that make the
conversation *good* rather than merely working: the context-aware offline brain,
the memory extractor and profile store (every write redirected to a temporary
file by an autouse fixture, so a test run can never touch your real
`backend/memory/profile.json`), the local-model discovery helpers including the
"our own port is not a model server" rule, and the startup decision order
(configured provider → auto-discovered local model → opt-in offline brain →
honest warning).

---

## Troubleshooting

The problems that actually happen on a first run — the WebSocket `403`, the
unreachable-LLM warning, the missing `faster-whisper`, stray `404`s, the
`Speech recognition error: network` dead microphone, echo-like replies, clips
that do not load and a mouth that will not move — are written up with their real
log lines, causes and verification steps in
[**docs/TROUBLESHOOTING.md**](docs/TROUBLESHOOTING.md). Quick index:

| Symptom | Cause / fix |
| --- | --- |
| `"WebSocket /" 403` / `connection rejected (403 Forbidden)` | uvicorn's wording for "the handshake was not accepted", not a CORS verdict. Wrong path (`/ws` is canonical, `/` is an alias), a disallowed origin, or a proxy swallowing the `Upgrade` header. See [§1](docs/TROUBLESHOOTING.md#1-websocket--403--connection-rejected-403-forbidden). |
| `refusing WebSocket from origin ...` | The page was served from an origin outside `CORS_ORIGINS`/`WS_ALLOWED_ORIGINS`. Same-origin is always allowed; add the origin or use `["*"]`. |
| **The replies look like a fixed script that ignores my question** | **No model is being reached** — that is what the banner at the top of the screen is telling you. *Settings → Model → **Detect models on this machine***, or pick a ready-made preset and press **Test connection**. This is the bug the whole model-setup UI exists for: see [§6](docs/TROUBLESHOOTING.md#6-the-replies-look-like-a-fixed-script-that-ignores-my-question). |
| Where do I put my `.vrm` / how do I stream it to OBS? | `.vrm` files go in `frontend/public/models/` (VRoid Studio exports one for free); OBS opens `http://localhost:8000/?streamer=1` as a **Browser** source. Full walkthrough in [docs/STREAMING.md](docs/STREAMING.md). |
| `Unknown WebSocket path '/client-ws'` | The client is not pointed at `/ws`. The message names the right URL. |
| Socket reconnects in a loop / closes `1008` / `1013` | `1008` is permanent (origin/path — fix it, the client stops retrying); `1013` is `MAX_SESSIONS`. A silent-but-connected socket also re-sends `hello` on the server's fresh session. See [§8](docs/TROUBLESHOOTING.md#8-the-socket-reconnects-in-a-loop-or-closes-with-1008--1013). |
| `GET /health 404` | `/api/health` is canonical; `/health` is served as an alias. `/v1/models` 404 means some tool is probing this port for an OpenAI server — we are a *client* of those APIs, never a server. |
| `LLM openai ... unreachable (ConnectError)` at startup | Nothing is listening on `VTUBER_LLM__BASE_URL`. The preflight already tried the usual local ports (`AUTO_DISCOVER=true`); open **Settings → Model → Detect models** to pick a server, set `VTUBER_LLM__PROVIDER=echo` for the labelled offline demo brain, or `VTUBER_LLM__FALLBACK_TO_ECHO=true` to degrade automatically. See [§6](docs/TROUBLESHOOTING.md#6-the-replies-look-like-a-fixed-script-that-ignores-my-question). |
| **The avatar repeats what I say / replies are shallow** | Either you are on the offline `echo` brain (the drawer shows a banner; the log says *falling back to the offline 'echo' brain*), or a real model is parroting — the persona, sampling penalties and reply-quality guard fix that. See [§6](docs/TROUBLESHOOTING.md#6-the-avatar-repeats-what-i-say-or-replies-feel-shallow). |
| **The avatar answers its own voice (speakers on)** | The mic hears the TTS. The transcript gate drops it (`asr.final` with `reason: "echo"`) before a turn is created; the log says *ignored ASR transcript ... (echo)*. Raise `ECHO_SIMILARITY`, or use headphones. See [§6c](docs/TROUBLESHOOTING.md#6c-the-avatar-answers-its-own-voice-speakers-on). |
| `Speech recognition error: network` and the mic never transcribes | Chromium could not reach *its own* speech service (offline/proxy/VPN). The client backs off, then auto-switches to server-side ASR. See [§5](docs/TROUBLESHOOTING.md#5-speech-recognition-error-network--the-mic-never-transcribes). |
| `ASR local faster-whisper is not installed` | `python -m pip install -r requirements-local.txt`, or `VTUBER_ASR__ENGINE=browser` (already the shipped default, no install). |
| Nothing renders; console says "Failed to load VRM" | The placeholder comes from a CDN — check your connection, or drop your own `.vrm` into `frontend/public/models/`. |
| No sound, but the chat works | Browsers block audio before a user gesture. Click **Start talking** — and if the browser refuses that gesture too, the overlay says so and retries on your next click. |
| The avatar never moves | **Settings → Motion** states the reason (clips off, manifest 404, unmapped skeleton). The procedural idle needs no assets, so pure stillness means a toggle, not a missing file. See [§7](docs/TROUBLESHOOTING.md#7-the-avatar-does-not-move-or-a-clip-looks-wrong). |
| The avatar's mouth never moves | In `mouth` mode the model needs the `aa` preset; check `GET /api/vrm-models/<name>/info` → `preset_expressions`, and the Face sliders. See [§9](docs/TROUBLESHOOTING.md#9-the-mouth-does-not-move-or-flaps-constantly). |
| The mouth moves but looks jittery | Raise `VTUBER_LIPSYNC__RELEASE_MS` (try 150), raise **Min hold**, or lower **Openness**. The sliders in the settings drawer do the same live. |
| LLM returns 404 | You are pointing an OpenAI-style client at a native endpoint (or vice versa). `/v1` → `openai`; bare Ollama → `ollama`. |
| `no mp3 decoder found` | Install `miniaudio` (it is in `requirements.txt`) or keep `VTUBER_TTS__FORMAT=pcm_s16le`. |
| Microphone button does nothing | `getUserMedia` needs `localhost` or HTTPS. On a LAN IP, front the app with TLS. |
| ASR transcribes silence into phantom words | Raise `VTUBER_ASR__MIN_SPEECH_SECONDS`, or use push-to-talk. |
| `server at capacity` | More than `VTUBER_SERVER__MAX_SESSIONS` tabs are open (default 8). |
| Japanese/Chinese text gets no mouth shapes | Set the character's `language` (`ja`/`zh`) and pick a matching TTS voice. The G2P has a full kana table; other scripts fall back to vowel-group rules. |
| 30 FPS on a laptop | Lower `VITE_PIXEL_RATIO` (1.0), turn off `VTUBER_AVATAR__ENABLE_SHADOWS`, and switch the camera preset to `bust`. |

`GET /api/health` is the one-stop check: per-engine status, the startup
preflight report (including `preflight.llm` with its `ok` / `auto_discovered` /
`canned` / `action_required` verdict), the live session count, and which engines
even exist in this install. `GET /api/memory` shows what the companion
remembers; `DELETE /api/memory` forgets it. `GET /api/llm/presets` and
`POST /api/llm/discover` back the model-setup tab.

---

## Changelog

Recent work — Bengali and Banglish speech, an editable persona, and the
face/motion polish pass — is summarised in [`CHANGELOG.md`](CHANGELOG.md).

---

## License

MIT — see [`LICENSE`](LICENSE). Sample avatars and voices carry their own
licences; check before redistributing.
