# Architecture

How the pieces fit, and why they are built this way. `docs/PROTOCOL.md` covers
the wire format; this document is the map of the code.

```
┌────────────────────────────── browser (frontend/) ───────────────────────────────┐
│                                                                                  │
│  React (components/)          zustand (state/store.ts)                           │
│        │  reads state, calls commands                                            │
│        ▼                                                                         │
│  ConversationController (core/conversation.ts)   ← the app runtime               │
│    ├── WebSocketClient      net/WebSocketClient.ts   one socket, text + binary    │
│    ├── AudioPlayer          audio/AudioPlayer.ts      multi-stream PCM scheduler  │
│    ├── SpeechSynthesisPlayer audio/SpeechSynthesisPlayer.ts  browser TTS + clock  │
│    ├── MicrophoneCapture    audio/MicrophoneCapture.ts  AudioWorklet → 16 kHz PCM │
│    ├── BrowserASR           audio/BrowserASR.ts       Web Speech API (optional)   │
│    └── VRMViewer            vrm/VRMViewer.ts          renderer + animation loop   │
│          ├── ExpressionController  emotions, holds, priority                      │
│          ├── LipSyncController     viseme timeline → mouth blendshapes            │
│          ├── BlinkController       natural, non-metronomic blinking               │
│          ├── LookAtController      pointer-driven eyes                            │
│          └── MotionController      idle breathing/sway + skeleton gestures        │
└───────────────────────────────────┬──────────────────────────────────────────────┘
                                    │  /ws : JSON control + raw PCM
┌───────────────────────────────────▼──────────────────────────────────────────────┐
│                                backend/ (FastAPI)                                │
│                                                                                  │
│  server/websocket.py  ──►  session.py  ──►  pipeline/orchestrator.py             │
│    frame demuxing            handshake        the turn: LLM → split → TTS → WS    │
│                              history          │  + pipeline/quality.py  guard     │
│                              config patches   ├── llm/       openai·ollama·echo    │
│                                               ├── tts/       edge·openai·browser   │
│                                               ├── asr/       local·openai·browser  │
│                                               ├── emotion.py tags + heuristics    │
│                                               ├── pipeline/sentence_splitter.py  │
│                                               ├── pipeline/speech.py  decode +    │
│                                               │      analyse → volumes(+visemes)  │
│                                               └── lipsync/  G2P + timeline        │
│  server/http_routes.py  /api/*   ·  server/preflight.py  startup checks           │
│  server/origins.py  handshake policy  ·  server/static.py  SPA + real 404s        │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## Backend

### Request flow

```
origin allowed?        (app/server/origins.py)  → error(origin) + close(1008)
path is /ws or /?      (aliases; any other path) → error(path)   + close(1008)
websocket.accept()
  ├─ over capacity? → error(busy) + close(1013)
  ├─ ClientSession(settings.model_copy(deep=True))     one copy per tab
  ├─ send_ready()                                      config + character + capabilities
  └─ loop: receive()
       ├─ text  → session.handle_text()                discriminated union, validated
       └─ bytes → session.handle_binary()              appended to the utterance buffer
```

The two handshake gates run **before** `accept()` in the sense that matters: a
rejection is still delivered on an accepted socket, then closed with 1008. That
is deliberate. Refusing the upgrade outright makes uvicorn log
`"WebSocket /" 403 / connection rejected (403 Forbidden)` and leaves the client
with nothing but "handshake failed" — the single most confusing symptom in this
codebase's history, and one where the browser is told nothing and the person
reading the terminal is told nothing useful either. Accepting first lets the
reason travel as a normal `error` frame.

`server/origins.py` keeps the policy pure and unit-testable: same-origin
(compared against the `Host` header) is always allowed so LAN addresses and
reverse-proxied domains need no configuration, a missing `Origin` header is
allowed because non-browser clients do not send one, and everything else must be
listed in `ws_allowed_origins` (falling back to `cors_origins`), with `["*"]` as
the escape hatch.

`websocket.receive()` is used rather than `receive_text()`/`receive_bytes()`
because the socket carries **both** kinds of frame: JSON control messages and raw
PCM. Mixing them on one socket is what removes HTTP round trips (and base64) from
the audio path. Ordering guarantees the association: a binary frame belongs to
the most recent `tts.start`.

### The turn

`pipeline/orchestrator.py` is the core of the product. One turn is:

```
user text ──► LLM.stream_chat()  ──token──► sentence_splitter ──sentence──►
                                                      │
                                        ┌─────────────┴──────────────┐
                                        │  expression/action events  │  (face first!)
                                        │  TTS worker (one per turn) │
                                        │  decode + analyse audio    │
                                        │  tts.start + PCM + tts.end │
                                        └────────────────────────────┘
```

Four deliberate decisions:

1. **Sentence-level streaming.** The first sentence is synthesised while the
   model is still writing the second. The intermediate queue has `maxsize=3`,
   which applies back-pressure so the server never synthesises far ahead of what
   the user is hearing (it would waste work the moment the user interrupts, and
   it would make barge-in feel laggy).
2. **One TTS worker per turn.** Audio leaves strictly in sentence order, and
   the client never has to reorder anything.
3. **The face leads the voice.** `expression` and `action` events are emitted as
   soon as a sentence is *known*, not when its audio is ready. A face that reacts
   one TTS round trip (~300 ms) after the words is the difference between "alive"
   and "broken".
4. **Failures degrade, they do not die.** A `TTSError` falls back to the
   browser's own speech engine for that sentence; an LLM error becomes an
   `error` frame and the session keeps its history.

#### The reply-quality guard

Before a sentence is handed to the TTS worker, `pipeline/quality.py` compares it
against the user's message and the previous assistant reply. But only the **first
sentence** of an attempt is judged, and this is what makes the guard safe:

```
attempt 1 ──► first sentence ──► guard ──► reject ──► (nothing queued, nothing spoken)
                                    │                   │
                                    │                   └─► llm.restart ─► regenerate once
                                    └─► accept ──► queue ──► tts.start ──► spoken
```

Because the decision happens before anything reaches the queue, a rejected
attempt can never talk over audio that is already playing, and the client only
has to drop a *preview* (`llm.restart` → clear the streaming bubble). The retry
gets one extra system note (`_RETRY_NUDGE`) telling the model its last answer was
rejected for parroting. A false positive therefore costs exactly one extra
generation — which is why the gate is deliberately simple word-set overlap rather
than anything cleverer.

That "simple word set" is also where the guard is easiest to get wrong
invisibly: `content_tokens()` tokenises by Unicode **category** (`L`/`M`/`N`),
not by Python's `\w`, which discards combining marks and would return an *empty
set* for Bengali — a guard that silently does nothing in exactly the language
this app is built for. Accent folding (`Café` == `cafe`) is applied to Latin
letters only, for the same reason: the obvious "strip every combining mark"
would leave Bengali consonant skeletons. The stop-word list is folded the same
way the text is, because `য়`/`ড়`/`ঢ়` decompose under NFKC-style normalisation.

#### The transcript gate (the other end of the same problem)

The quality guard looks at what the model *says*. `pipeline/transcript.py` looks
at what the microphone *heard*, before a turn is ever created:

```
microphone ──► ASR ──► transcript gate ──► accepted ──► user turn ──► LLM
                             │
                             └─► rejected ──► asr.final {reason} + state:idle  (no turn)
```

Without it, "the avatar talks to itself" had a second cause that the reply guard
cannot see: with speakers on, the microphone picks up the TTS, the ASR
transcribes it, and the model is asked to answer its own last sentence. The gate
rejects four narrow classes and lets everything else through, because dropping a
real utterance is worse than accepting noise:

| rule | dropped when |
| --- | --- |
| silence/punctuation/tags | nothing intelligible (`empty`, `tag`) |
| hard hallucinations | video sign-offs, subtitle credits, `字幕` (`noise`) |
| short hallucinations | "thank you"/"you" — only if the audio was < `short_utterance_seconds` |
| speaker echo | similarity to the avatar's last 4 sentences ≥ `echo_similarity` |
| duplicate | identical to the previous transcript (off by default) |

Similarity is `difflib.SequenceMatcher.ratio` over a punctuation-free, case-folded
key, so it needs no dependency and behaves identically for Bengali, Japanese and
English. The key keeps Unicode **marks** (`L`/`M`/`N` categories) rather than
ASCII `\w` — Python's `\w` throws away Bengali matras and Devanagari vowel signs,
which would silently gut every non-Latin comparison.

Every threshold is in `ASRConfig.transcript` (`VTUBER_ASR__TRANSCRIPT__*`) and the
whole gate can be switched off. Rejections are sent to the client as
`asr.final` with a `reason`, so the UI can say "ignored my own voice" instead of
going quiet for no visible reason.

### Language (`pipeline/language.py`)

The persona promises to mirror the user's language, and a promise like that is
worthless unless the model is *told* which language it just read — so the last
user message is classified and named in the system prompt ("The user just wrote
in Bengali (বাংলা). Reply in Bengali (বাংলা).").

Two kinds of input, and only one of them is a script question:

* **Script.** An ordered list of Unicode ranges (`_SCRIPTS`) names the language
  of anything written in Bengali, Devanagari, Arabic, Japanese, Hangul, Han,
  Cyrillic… — exact, free, no model.
* **Romanisation.** `ami bhalo achi` is Bengali typed on a Latin keyboard
  ("Banglish"), and a script-only detector reads it as English. `is_banglish()`
  needs **two distinct** Bengali function words from `_BANGLISH_MARKERS`, so a
  stray "ki" in an English sentence never flips the language while a real
  Banglish sentence always does. It answers the pseudo-code `bn-Latn`, and the
  directive then asks for Bengali **script** in the reply — because the voice
  that will read it is a Bengali voice, and a Bengali voice can only pronounce
  Bengali letters.

The same module owns `LOCALES`/`locale_for()`: script tags collapse onto the
language that is *spoken* (`bn-Latn` → `bn-BD`) and a real region is left alone
(`en-GB`). That single value decides three things — the TTS voice
(`app/tts/voices.py`), the `tts.start.lang` a browser-side voice is chosen from,
and the language the viseme timeline is built with.

It is deliberately a heuristic, not a classifier: it must cost nothing, run on
the token path, and be trivially unit-testable (`tests/test_language.py`). Wrong
guesses are cheap and self-correcting — the model reads the user's actual words
regardless, and one bad accent is one sentence.

### Errors and logging

Two rules keep failures legible:

* **One error shape.** The WebSocket sends an `error` frame (with a stable
  `code`) before closing; the REST surface answers with the same
  `{"error", "code"}` vocabulary via two exception handlers registered in
  `server/app.py` — one for `RequestValidationError` (422, `code: "protocol"`),
  one for unhandled `Exception` (500, `code: "internal"`, traceback logged
  server-side only). FastAPI's default `{"detail": [...]}` and Starlette's bare
  500 are both replaced, so the client needs exactly one error path.
* **Two log sinks.** `logging_setup.py` always writes the pretty rotating
  `logs/backend.log`; set `VTUBER_LOG__JSON_FILE` to add a second sink that emits
  one JSON object per line for log aggregators.

### Lip-sync production (`pipeline/speech.py` + `lipsync/`)

```
TTSResult(text, audio bytes, word boundaries)
        │
        ├─ decode        miniaudio (or soundfile) → float32 mono @ sample_rate
        ├─ trim          drop leading/trailing silence, keep the trim as an offset
        ├─ normalise     peak-normalise so quiet engines still animate
        ├─ resample      to tts.sample_rate (cheap linear/low-pass path)
        │
        ├─ volumes[]     RMS per lipsync.slice_ms, normalised to 0..1
        │
        └─ timeline      G2P(text)  →  phonemes  →  visemes
                         scale the phoneme estimate to the *real* audio duration
                         snap to WordBoundary timings when the engine gave them
                         merge frames shorter than 45 ms (they only read as flicker)
                         fill gaps/pauses with `nn`
                        │
                        ▼
        TtsStart(text, duration, volumes, visemes, expression, actions)
```

The phoneme step (`lipsync/visemes.py`) is a rule-based grapheme-to-phoneme
converter with a complete kana → romaji table, so Japanese text animates properly
instead of falling through to a vowel heuristic. Six visemes are produced: the
five VRM mouth presets plus `nn`, which means *closed* — consonants, pauses and
silence.

Two implementation notes that matter:

* The timeline is normalised to the **measured** audio duration, not to the G2P
  estimate. Speech rate and TTS engines vary far too much for the estimate alone
  to stay in sync by the end of a sentence.
* Word boundaries from Edge TTS are used as anchors, not as the timeline itself:
  the phoneme estimate is *stretched between* them. This keeps the smoothness of
  a phoneme model while pinning it to reality.

#### The mode gate

`volumes[]` is always computed — it is cheap and both lip-sync modes use it. The
whole G2P/viseme half of the diagram is only built when it is asked for:

```python
want_visemes = settings.lipsync.enabled and settings.lipsync.mode == "visemes"
```

`mode` defaults to `"mouth"`, where the avatar's mouth is one number derived from
`volumes[]` on the client. That removes the single largest source of jitter (a
phoneme estimate disagreeing with the audio) at the cost of consonants no longer
visibly closing the mouth — a trade that reads as *better* on almost every
stylised VRM. The chosen mode travels on each `tts.start` as `lipsync_mode`, so
patching the setting mid-reply cannot leave a sentence with no driver. See
`docs/PROTOCOL.md` for the payload fields and `frontend/src/vrm/LipSyncController.ts`
for the hysteresis/dwell logic.

### Configuration and personas

`config.py` holds one `Settings` tree (Pydantic settings, `VTUBER_` prefix,
`__` for nesting). Three access patterns:

| Pattern | Where |
| --- | --- |
| Process-wide defaults, cached | `get_settings()` |
| Per-session copy, patched at runtime | `ClientSession` + `apply_patch()` |
| Persona | `characters/*.yaml` → `load_character()` |
| Persona **edits** | `characters/*.user.json` → merged on top by `load_character()` |

A persona is a *file*, and the shipped one is meant to be read and hand-edited:
it is heavily commented, and dumping it back through a YAML writer would throw
those comments away. So an edit made in the settings UI is written **beside**
it, as `characters/<id>.user.json`, and applied to the already-validated
persona (`{**base.model_dump(), **overrides}` → re-validated). Consequences:
`Reset` is an `unlink`, a corrupt override can never break a working persona,
the `.yaml` stays the reference, and both files are ignored by git and Docker
(they are per-machine, like memory). `GET/PUT/DELETE /api/character` expose it;
`character.reload` tells a *running* session to re-read the file, because the
`avatar.character` id has not changed and `apply_patch()` would see nothing to
do.

`apply_patch()` is defensive by construction: only the `llm`, `tts`, `asr`,
`lipsync`, `avatar`, `memory` sections are patchable, unknown keys are ignored,
every value is re-validated through the same models used at boot, and the masked
secret `"********"` means "keep what you have". A malicious or buggy client
cannot corrupt a running session, and `POST /api/config/validate` runs the
identical path so the UI can pre-flight a patch.

Each session gets `settings.model_copy(deep=True)`, which is why two tabs can
speak different languages of the same character without interfering.

### Startup preflight

`server/preflight.py` probes LLM, TTS, ASR and the audio decoder at boot and logs
an actionable summary. Nothing is fatal — a typo in `BASE_URL` should not stop
the server — and the same report is exposed at `GET /api/health`, so the settings
drawer can show "this engine is not reachable" instead of letting the user
discover it three messages into a conversation.

Every warning carries the two things a person actually wants: how to install
what is missing, and how to switch to something that works without it.

The LLM check runs a four-step decision, in order, and the health report says
which step won:

1. **The configured provider.** One cheap probe (model list, or a one-token
   chat). If it answers, we are done.
2. **`llm.auto_discover`** (`app/llm/discover.py`). Probe the ports local model
   servers actually bind — Ollama `11434`, LM Studio `1234`, llama.cpp `8080`,
   Jan `1337`, text-generation-webui `5000`, KoboldCpp `5001`, vLLM
   `8000`/`8001` — and adopt the first one offering at least one chat model.
   Two rules make this safe rather than magic: **only loopback addresses are
   probed**, so a conversation is never silently routed to a third party, and
   the app's own port is excluded, so a vLLM on `8000` cannot be mistaken for
   this server. Success is logged at `SUCCESS` and flagged as
   `"auto_discovered": true`.
3. **`llm.fallback_to_echo`**, if the user opted in: swap the process to the
   offline `echo` brain. Flagged `"canned": true` and `"fallback": true`.
4. **Nothing.** Flagged `"action_required": true` and `"ok": false`, with a
   detail string naming the button to press. The configured provider is left
   *unchanged* — step 3 is the only way a script can end up answering, and it
   has to have been asked for.

That ordering is the whole fix for the "the replies are a fixed script"
report: before it, step 1 failed and step 3 fired silently, so every turn was
answered from a pool of canned lines while the UI showed nothing wrong. The
client now reads `preflight.llm` on every `ready` and raises a banner
(`ConversationController.probeModelStatus`), so the state is impossible to miss.

### Long-term memory (`memory/`)

A companion that reintroduces itself every reload is not a companion, so the
profile is a first-class object rather than a prompt-stuffing afterthought.
Three files carry it:

| File | Responsibility |
| --- | --- |
| `memory/facts.py` | **Deterministic extraction.** Regex/rule based, English plus best-effort Bengali (which is object-before-verb, so the patterns are positional rather than template-matched). `topical_words()` is the script-agnostic salience helper — it splits on whitespace and punctuation only, never on `\w`, because Python's `\w` discards combining marks (`Mn`/`Mc`) and would silently strip every Bengali matra. |
| `memory/profile.py` | A `Profile` model (name, facts, likes, dislikes, topics, summary, turns) with caps on every collection, plus `ProfileStore`: **atomic** writes (temp file + `os.replace`), a re-read when the file's mtime changes outside the process, and `describe()` which renders the short prompt block. A corrupt or unreadable file degrades to an empty profile, never an exception. |
| `pipeline/orchestrator.py` | Injects `describe()` into the system prompt, observes each *user* turn, and schedules the rolling summary. |

Design constraints that shaped it:

* **Explicit statements only.** "my name is…", "I work as…", "I love…", "I can't
  stand…". Nothing is inferred from vague text; a wrong memory is worse than no
  memory, and it is the user's own file to correct.
* **The greeting is never mined.** Internal turns are flagged so the character
  cannot end up remembering its own hello as something you said.
* **Summarising never blocks playback.** The summary runs as a separate task
  *after* the reply is queued. With the `echo` brain it is built from tracked
  topics instead of a model call, so it works with no model at all.
* **One file, one user.** A single global profile matches the single-user
  desktop reality; the path is configurable and `backend/memory/` is git-ignored.

### Static files and the 404 problem

`frontend/dist` is mounted at `/` (after `/api` and `/ws`, so those keep
priority) through `SpaStaticFiles`, which draws one line between two kinds of
missing path:

* a **navigation** (the request advertises `Accept: text/html`, which every
  browser does when you type a URL or hit refresh) renders `index.html`, so deep
  links work;
* a **resource** (an `<img>`, a stylesheet, a `fetch()`, `curl` — they send
  `*/*` or a concrete type) gets an honest `404`, so a broken asset is visible
  in the network tab instead of being masked by an HTML page with status 200;
* `/api/…` and `/ws…` are excluded from the fallback entirely, so a typo in an
  API path is still a JSON `404` and never an HTML document.

That last point is not hypothetical: `StaticFiles.get_path` passes the path
through `os.path.normpath`, which on Windows turns `api/nope` into `api\nope` —
so the prefix check normalises separators before matching. The test that caught
it is `test_unknown_api_path_stays_a_404`.

---

## Frontend

### React is not in the hot path

`ConversationController` (`core/conversation.ts`) owns the socket, the audio graph
and all five VRM controllers. It is created once (a module singleton, fetched with
`getConversation()`), and React only:

* reads state that arrives through the zustand store, and
* calls commands (`sendChat`, `beginSession`, `patchConfig`, …) on the singleton.

The per-frame work never touches React. Rebuilding a component tree 60 times a
second is the usual reason a Three.js React app stutters; here the render loop
talks to plain classes, and React re-renders only when a *message* arrives.

`main.tsx` deliberately does not wrap the app in `StrictMode`: its double-invoke
would create two WebGL contexts, two sockets and two microphones in development.

### The frame loop

```ts
tick() {
  const delta = Math.min(clock.getDelta(), MAX_DELTA);   // clamp after a tab switch
  elapsed += delta;
  touchInput();
  for (const controller of controllers) controller.update(delta, elapsed);
  vrm.update(delta);                                     // ← flushes blendshapes
  renderer.render(scene, camera);
}
renderer.setAnimationLoop(tick);
```

`MAX_DELTA` matters: after an alt-tab the clock can report seconds, and an
unclamped delta would teleport the smoothing filters.

The order is not arbitrary. Controllers **write** (expression weights, bone
offsets); `vrm.update(delta)` then copies the normalised humanoid rig onto the
real bones and applies the expression manager; the renderer draws. Forgetting
`vrm.update()` is the classic "my VRM does not move" bug, so it lives in exactly
one place.

Within the controllers, one ordering is load-bearing: `AnimationController`
runs the `AnimationMixer` **first**, and the procedural `MotionController` runs
**after** it. The mixer overwrites whatever it animates, so a partial procedural
write before it would simply be clobbered; running last lets the procedural layer
decide, per bone, whether to write, skip, or blend over the mixer's output (see
*Animation clips* below).

### Controllers

| Controller | Owns | Notes |
| --- | --- | --- |
| `ExpressionController` | `neutral happy angry sad relaxed surprised` | Holds an emotion for `hold_ms` then releases; a *changed* mood lands with a 240 ms overshoot so it reads as a reaction. A separate **ambient** layer shows `thinking` while the model works and a soft `relaxed` face while the user talks, and is suppressed the moment a sentence mood arrives. Facial `action`s (`smile`, `laugh`, `blush`) are transient expressions here, not skeleton clips. Exposes `blinkSuppressed` so the others can defer. |
| `LipSyncController` | `aa` (mouth mode) or `aa ih ou ee oh` + `nn` (visemes mode) | Queues utterances (the backend sends them faster than they play), reads the payload at each utterance's own playback clock, and applies attack/release smoothing. `mouth` mode adds a hysteresis band, a minimum dwell time and a `mouth_max` ceiling over the envelope; `visemes` mode zeroes all five presets on `nn` and obeys the same ceiling. Also exposes `mouthOpenness` for secondary motion. |
| `BlinkController` | `blink` / `blinkLeft` / `blinkRight` | Randomised intervals (never metronomic), occasional double-blinks, and it yields to an active expression hold. |
| `LookAtController` | `lookAt` target | Pointer position with damping and range limits; falls back to the camera when there is no pointer. |
| `AnimationController` | The `AnimationMixer` and every loaded clip | Owns the idle rotation and gesture playback, and exposes a per-bone `ownership` map to the procedural layer. Assets are optional: with none, everything falls through to `MotionController`. |
| `MotionController` | Humanoid bones | Idle breathing/sway/head-drift, procedural gesture clips, the arm rest pose, and a slow **mood posture** (head tilt for `thinking`/`shy`, a slump for `sad`) that follows `setMood`. Voice energy reaches the chest through a 0.4 s low-pass, never the head. Auto-detects an already-A-posed model and stops rotating the arms. Runs last, so it can defer to clips per bone. |

### Animation clips

`frontend/src/vrm/animation/` is a self-contained, optional layer:

```
manifest.json ──► loaders.ts ──► AnimationClip[]
                                     │
   .vrma ──► used as-is ─────────────┤
   .glb  ──► retarget.ts ────────────┤
   .fbx  ──► retarget.ts ────────────┤
                                     ▼
                        AnimationController (AnimationMixer)
                                     │
                     smooth slot weights, ducked idle while a gesture plays
                                     ▼
                        ownership: bone -> 0..1
                                     ▼
                        MotionController.setOwnership(...)  → write last
```

* **Retargeting is rotation-only and calibration-free.** A source clip's rotations
  are normalised against *its own* rest pose (`q ⊗ q_rest⁻¹`) and applied to the
  matching avatar bone, so no per-model tuning is needed. Hip translation is
  carried as a delta from the clip's first frame (scaled by `positionScale`,
  switchable off with `positionStrength: 0`); scale tracks are dropped. `.vrma`
  skips all of this because it is already normalised.
* **Bones are matched by meaning, not by spelling** (`vrm/animation/bones.ts`
  canonicalises `mixamorig:LeftForeArm`, `J_Bip_L_LowerArm`, `Bip01 L Forearm`
  and the VRoid finger conventions to the same keys, and rejects twist/IK/helper
  joints rather than guessing).
* **The two layers share the skeleton by ownership.** A clip writes; then the
  procedural layer reads `ownership` per bone and either writes outright (0),
  leaves it alone (1), or slerps its own pose over the mixer's output by `1 - w`
  in between. That threshold band is what turns a fade-in into a blend rather
  than a snap — and because a clip's first activation happens while the
  procedural layer still owns the bone, the mixer's "original state" snapshot is
  the live idle pose, not the T-pose.
* **Nothing is required.** No manifest, an empty manifest, a 404 or an unmappable
  skeleton all degrade to the procedural idle and are reported in the settings
  drawer instead of thrown.

Two shared rules keep them from fighting:

* **Nothing sets a weight directly.** Every target goes through
  `smoothTowards()` in `vrm/smooth.ts`, with separate attack and release
  constants (40 ms / 120 ms by default, live-tunable). That is what makes the
  mouth look like speech instead of a flip-book, and it is frame-rate
  independent:
  `w += (target - w) * (1 - exp(-dt / tau))`.
* **Priority is explicit.** An expression with `override_blink` freezes blinking;
  a blink is deferred while an emotion hold is active; `nn` zeroes the mouth
  presets. There is no "last writer wins".

### Sound

* `audio/engine.ts` owns the single `AudioContext`, created lazily on the first
  user gesture (browsers refuse to start audio before one) and exposed through
  `getMasterGain()` so volume lives in one node. The master chain is
  `gain → AnalyserNode → destination`: an `AnalyserNode` is a pass-through, so
  putting the lip-sync tap *in* the path (rather than hanging it off the side)
  costs nothing and guarantees the renderer pulls it.
* `audio/TtsLevelMeter.ts` reads that tap: RMS over a ~21 ms window
  (`fftSize = 1024`, `smoothingTimeConstant = 0` because the controller owns
  smoothing), noise-gated and normalised to 0..1. Its `available`/`null` result
  distinguishes "no meter" from "silence", which need different fallbacks.
* `AudioPlayer` is a multi-stream PCM scheduler. Each `tts.start` opens a stream;
  chunks are turned into `AudioBuffer`s and scheduled back to back with a small
  lead; `positionFor(id)` reports the playback position of a stream (`+Infinity`
  once it is retired) — this is the clock the lip-sync reads.
* `SpeechSynthesisPlayer` handles the `browser` transport: it queues native
  utterances and re-anchors its clock on `onstart` and `onboundary`, so the
  viseme timeline stays aligned even though the browser gives no audio buffers.
  Note what it *cannot* do: the OS synthesiser's output never enters the audio
  graph, so neither the server envelope nor the analyser can see it — that is
  the one input to `LipSyncController` that stays synthetic.
* `MicrophoneCapture` streams 100 ms Int16 blocks at 16 kHz from
  `public/pcm-worklet.js`, and does voice-activity detection on the main thread
  (noise threshold, barge-in threshold, attack/release hangovers, a preroll ring
  so the first syllable is not clipped). Two resilience details: the
  `getUserMedia` stream is **reused** across starts (re-requesting it costs
  100–400 ms and re-opens the browser's mic indicator, which made toggling voice
  input feel broken), and a track `onended` (device unplugged, taken by another
  tab) is reported instead of going silently deaf.
* `BrowserASR` wraps the Web Speech API and treats it as the unreliable remote
  service it is: every error code is classified (`network` and friends are
  transient, `not-allowed`/`audio-capture` are fatal), transient failures back off
  exponentially instead of looping, the recogniser is **paused while the avatar
  speaks** (it has no echo cancellation of its own), and after a few consecutive
  `network` failures it stops and offers the server-side engine — which
  `ConversationController.fallBackToServerAsr()` switches on automatically when
  the backend has one. Its state (`listening`/`retrying`/`paused`/…) is mirrored
  into the store so the settings drawer can explain a silent microphone.

### Reacting to a message

`ConversationController.handleMessage()` switches on the message type (the union
is a proper discriminated union, so each case is fully typed) and routes to the
store, the audio graph, or the controllers:

```ts
case 'tts.start':
  // open an audio stream, hand the timeline to the lip-sync controller
  this.lipsync.enqueue(message, this.player.positionFor(id));
  break;
case 'expression':
  this.expressions.apply(message.preset, message.weight, message.hold_ms, ...);
  break;
```

Unknown types never reach here: `WebSocketClient` drops them, which keeps the
switch exhaustive and the "the backend may grow" promise honest.

### The greeting gate

Three things must be true before the spoken greeting fires:
`avatarReady && isAudioUnlocked() && client.isOpen`. The socket connects and the
VRM loads immediately; `hello` (and therefore the greeting) waits for the user's
first click, because that click is what unlocks audio. Without this ordering, half
of all first-run experiences would be "the avatar is silent".

Two follow-ups make that gate trustworthy rather than merely present:

* **The unlock is verified, not assumed.** If `AudioContext.resume()` did not
  actually succeed (strict autoplay policy, an embedded frame), the overlay stays
  up, says *"The browser blocked audio playback"*, and retries on the next
  interaction instead of dismissing itself into silence. A context can also be
  suspended again later (backgrounded tab, long idle), so every `tts.start` is a
  chance to resume it.
* **`hello` is re-sent on every `ready`.** A reconnect gives the server a *new*
  session with empty history. The client used to believe it had already said
  `hello`, so it would sit there connected — looking perfectly healthy — and
  never speak. Resetting that flag in `onReady` is the whole fix.

### Connection lifecycle

`net/WebSocketClient.ts` distinguishes *transient* drops from *deliberate*
rejections, because they need opposite responses:

| Close | Meaning | Client behaviour |
| --- | --- | --- |
| `1008` (policy) | Origin or path rejected — retrying cannot help | Stop auto-reconnecting, surface the server's `error` message |
| `1013` (capacity) | `server.max_sessions` reached | Keep retrying, with a much longer backoff |
| anything else | Network change, sleep/wake, server restart | Backoff reconnect (up to 15 s), randomised so many tabs do not synchronise |
| `navigator.onLine === false` | The network is simply gone | Stop retrying and wait for the browser's `online` event |

### Persisted preferences

`state/store.ts` keeps server state (chat, session, metrics) separate from
client-only preferences persisted under the `open-vrm-vtuber` key: `vrmUrl`,
`cameraPreset`, `pixelRatio`, `showStats`, `volume`, `micMode`,
`useBrowserAsr`, `motion`, `animation` (manifest URL, on/off, idle duration,
gesture strength), `streamer`. A `null` means "adopt whatever the backend says",
which is what lets the settings drawer stay a thin layer over the server config.
Persisted objects are deep-merged against their defaults on load, so a new
setting added in a later version appears with its default instead of
`undefined`.

`streamer` follows the same rule with a twist: `null` means "decide from the
URL". `?streamer=1` (also `?mode=streamer`, `?obs`, `?clean`) renders the avatar
alone — no top bar, no chat panel, no drawer — on a transparent page, and
implies autostart. That is exactly what OBS wants from a Browser source, and a
Browser source is always a fresh browser profile, so the URL is the right place
for the decision and the persisted toggle is only for a normal window. The
`error`/`notice` surfaces are deliberately still rendered in streamer mode (a
broken microphone has to be visible to the person who can fix it), and if the
capture tool refuses autoplay a single "Start talking" button appears rather
than a black rectangle.

`App.tsx` also renders the app-wide advisory from `state.notice`, filled by
`ConversationController.probeModelStatus()` on every `ready` from
`GET /api/health`: a warning when no model is connected (`preflight.llm.ok ===
false`), an informational line when the replies are the offline script
(`canned`), and an informational line when a local model was auto-detected. It
is the client half of the "the replies are a fixed script" fix — the state is
now impossible to miss.

---

## Design decisions worth knowing

**Why one WebSocket instead of WebRTC or HTTP?**
Audio is already streaming in from the server; the round trip that matters is
TTS → sound, and that is inside the browser. A data channel would buy latency
that does not exist here, at the cost of a signalling server, STUN/TURN and a
whole second transport to debug. If you later add video from a remote GPU, that
is the moment to reach for WebRTC — the protocol is designed so audio could move
to one without touching the control plane.

**Why PCM instead of Opus/MP3?**
No encoder dependency on the server (only a decoder), no extra latency from a
frame buffer, and it removes base64 entirely. The cost is bandwidth — ~48 KB/s
at 24 kHz mono 16-bit — which is irrelevant for localhost and fine on a LAN. For
the public internet, flip `VTUBER_TTS__FORMAT=mp3`; the client handles both.

**Why server-side visemes at all, rather than analysing audio in the browser?**
The server already has the text and the engine's word boundaries, which is
strictly more information than an amplitude analysis of the waveform. It also
keeps the work off the main thread of a machine that is already busy rendering
3D, and it makes the lip-sync identical on every client, including a phone.

That is not an argument against the browser analyser — it is the reason the
analyser is the *second* choice. Both are measurements of real audio; the
server's is better informed and arrives aligned, so it wins when present, and
the analyser (`TtsLevelMeter`) covers the cases where it is not. What neither
can do is measure the `browser` TTS transport, whose audio the page never
touches.

**Why `nn` as a sixth "viseme"?**
Because lip-sync is mostly about *closing*. Phoneme timelines that only describe
vowels leave the mouth hanging open through every consonant cluster, which is the
single most common tell of a fake-talking avatar. `nn` explicitly targets zero on
all five mouth presets, and the smoothing release then closes the mouth
gracefully.

**Why the emotion analysis lives on the server?**
Because it needs the character's vocabulary (which emotion names the persona
understands and what each maps to in VRM presets). That mapping belongs to the
persona file, not the client. The client receives a resolved preset, weight, hold
time and a blink-override flag — nothing to interpret.

---

## Extending it

| Goal | Where |
| --- | --- |
| Add an LLM provider | Implement `LLMEngine` in `backend/app/llm/`, register it in `factory.py`. `stream_chat()` yields text deltas. |
| Add a TTS engine | Implement `TTSEngine` in `backend/app/tts/`; return a `TTSResult` with audio bytes and, ideally, word boundaries. |
| Add an ASR engine | Implement `ASREngine` in `backend/app/asr/`; `transcribe(DecodedAudio) -> str`. |
| Change the personality | Copy `backend/characters/default.yaml`; it is picked up by `GET /api/characters` and the settings drawer automatically. |
| Add a gesture | Add a duration to `CLIP_DURATION` and a `case` to `MotionController.evalClip()`, then reference it from a character's `actions` map. |
| Add an animation clip | Drop the file in `frontend/public/animations/` and add a manifest entry — see `docs/ANIMATIONS.md`. No code changes. |
| Change what counts as a bad reply | Tune `llm.echo_threshold` / `repeat_threshold`, or extend `backend/app/pipeline/quality.py`. The orchestrator only needs `guard(sentence) -> bool`. |
| Support facial expressions the model already has | Add the preset to `EMOTION_PRESETS` in `frontend/src/vrm/constants.ts` and map an emotion to it in the character YAML. |
| Add a protocol message | Add it to `backend/app/schemas.py` **and** `frontend/src/types/protocol.ts` (and the known-type set in `WebSocketClient`), then to `docs/PROTOCOL.md`. |
