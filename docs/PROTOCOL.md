# Wire protocol

One WebSocket carries the whole conversation. This document is the contract
between `backend/app/schemas.py` and `frontend/src/types/protocol.ts`; both sides
mirror it, and `backend/tests/test_server.py` walks it end to end.

```
browser  ──ws://host/ws──►  FastAPI
```

## Three rules

1. **Text frames are JSON** and always carry a `type` field.
2. **Binary frames are raw audio.** A binary frame always belongs to the most
   recent `tts.start`, which the server guarantees it has already written
   (WebSocket preserves ordering). No base64, no correlation ids in the audio
   path.
3. **Unknown message types are ignored**, never fatal — on both sides. The
   client drops unknown types in `WebSocketClient`; the server answers one
   `error` with `code: "protocol"` for a malformed *client* frame.

There is no HTTP in the conversation path at all. `/api/*` exists only to let
the UI be built and configured.

---

## Connection lifecycle

The handshake is gated before anything else happens:

```
client                                server
  │  ────── TCP + upgrade ──────────────►   POST /ws
  │                                          1. origin allowed? (else 1008 + error)
  │                                          2. room for another session? (else 1013)
  │  ◄────────────── ready ──────────────   config, character, capabilities
  │
  │  ─────────── hello ──────────────────►   sent only once the VRM is rendered
  │                                           AND audio is unlocked (user gesture)
  │  ◄─────────── hello.ack ──────────────
  │  ◄─────────── state: thinking ────────   greeting turn starts
  │  ◄─────────── llm.delta ... ──────────
  │  ◄─────────── llm.restart ────────────   (optional) discard the preview and
  │  ◄─────────── llm.delta ... ──────────   regenerate once, see below
  │  ◄─────────── llm.sentence ───────────
  │  ◄─────────── expression / action ────   the face leads the voice
  │  ◄─────────── tts.start ──────────────
  │  ◄─────────── [binary PCM]×N ─────────
  │  ◄─────────── tts.end ────────────────
  │  ◄─────────── llm.done ───────────────
  │  ◄─────────── metrics ────────────────
  │  ◄─────────── state: idle ────────────
```

**The URL.** `ws://host/ws` is canonical. `ws://host/` is accepted as an alias,
because pointing a client at the bare origin is an easy mistake and answering it
with a connection beats answering it with an unrelated failure. Any *other* path
is answered by a catch-all that accepts, sends
`error(fatal=true, code="path")` naming the right URL, and closes with `1008` —
again, so the mistake is legible instead of silent.

**Origin.** Browsers attach an `Origin` header that a page on another site
cannot forge, so it is checked before a session is created. Same-origin
(origin == `Host`) is always allowed — that covers every LAN address, tunnel and
reverse-proxied domain with no configuration. Otherwise the origin must appear
in `server.ws_allowed_origins` (which falls back to `server.cors_origins`,
defaulting to the four local addresses); `["*"]` disables the check. A *missing*
`Origin` header is allowed, because non-browser clients do not send one.
Rejections are reported as `error(fatal=true, code="origin")` on an accepted
socket, then closed with `1008`.

**Why accept-then-close.** Refusing the handshake outright makes uvicorn log
`"WebSocket /" 403 / connection rejected (403 Forbidden)` and leaves the client
with nothing but "handshake failed". Accepting first lets the reason survive the
trip, in a frame both `WebSocketClient` and a person reading DevTools can see.

**Why the greeting waits for `hello`.** Loading a VRM can take a second or two
and the first audio needs a user gesture anyway. The server sends `ready`
immediately but stays silent until the client says it is ready to be seen and
heard. `hello.greet: false` connects without a greeting.

**Close codes.** `1008` policy violation (bad origin, unknown path), `1013` try
again later (at capacity). A clean client shutdown is `1000`/`1001`. The server
never closes a socket without first sending an `error` frame explaining why.

---

## Server → client

### `ready`

Sent once, on connect, before anything else.

```json
{
  "type": "ready",
  "session_id": "8f2c19a4",
  "config": { "server": {...}, "llm": {...}, "tts": {...}, "asr": {...},
              "lipsync": {...}, "avatar": {...}, "log": {...} },
  "character": { "name": "Ai", "tagline": "...", "language": "en",
                 "greeting": "...", "system_prompt": "...",
                 "tts_voice": "en-US-AriaNeural", "tts_rate": "+4%",
                 "emotions": { "happy": {"preset":"happy","weight":0.75,
                                         "hold_ms":2600,"override_blink":false}, ... },
                 "actions":  { "happy": "nod", ... } },
  "capabilities": {
    "llm": ["openai", "ollama", "echo"],
    "tts": ["edge", "openai", "browser"],
    "asr": ["local", "openai", "browser"],
    "characters": [{ "id": "default", "name": "Ai", "tagline": "...", "language": "en" }],
    "audio": { "transport": "pcm_s16le", "sample_rate": 24000,
               "chunk_ms": 200, "capture_sample_rate": 16000 },
    "visemes": ["aa", "ih", "ou", "ee", "oh", "nn"],
    "expressions": ["neutral", "happy", "angry", "sad", "relaxed", "surprised",
                    "blink", "blinkLeft", "blinkRight",
                    "lookUp", "lookDown", "lookLeft", "lookRight"]
  }
}
```

Every `api_key` in `config` is masked as `"********"`.

### `hello.ack`

```json
{ "type": "hello.ack", "session_id": "8f2c19a4", "server_time": 1770000000.12 }
```

`server_time` is Unix seconds and is used by the client to measure clock offset
from a `ping`/`pong` round trip.

### `state`

```json
{ "type": "state", "value": "listening", "detail": "transcribing" }
```

`value` ∈ `idle | listening | thinking | speaking | error`. `detail` is a short
free-form hint and is safe to display.

### `asr.partial` / `asr.final`

```json
{ "type": "asr.partial", "text": "what is the wea", "duration": null }
{ "type": "asr.final",   "text": "what is the weather", "duration": 1.84 }
{ "type": "asr.final",   "text": "", "duration": null, "reason": "echo" }
```

`duration` is the length of the analysed audio in seconds, or `null` when the
engine does not report it.

`reason` is empty when the transcript was accepted. Otherwise the input gate
(`app/pipeline/transcript.py`) dropped it and said why — the client shows this
instead of a silent stage:

| `reason` | meaning |
| --- | --- |
| `empty`, `tag`, `short` | nothing intelligible was heard |
| `noise` | a known ASR hallucination ("Thanks for watching.") |
| `echo` | a near-copy of the avatar's own recent speech (speakers on) |
| `duplicate` | the same transcript as the previous one (opt-in) |

### `user.text`

The utterance the turn is about to answer — echoed so every tab (and the chat
log) agrees on what was heard.

```json
{ "type": "user.text", "text": "what is the weather" }
```

### `llm.delta`

One streamed token/chunk of the reply, for the live "typing" preview.

```json
{ "type": "llm.delta", "text": "It looks" }
```

### `llm.sentence`

A completed sentence, sent *before* its audio.

```json
{
  "type": "llm.sentence",
  "index": 1,
  "text": "It looks lovely today.",
  "display_text": "[happy] It looks lovely today."
}
```

* `text` is what was handed to TTS (emotion tags and gesture notes removed) —
  this is the string the visemes were derived from.
* `display_text` is for the chat bubble (tags kept).

### `llm.restart`

```json
{ "type": "llm.restart" }
```

The server threw away the reply it was streaming and started generating again.
This is the reply-quality guard: when the *first* sentence of an attempt only
parrots the user's message, or repeats the previous assistant reply, the whole
attempt is dropped and regenerated once with a corrective system note (see
`app/pipeline/quality.py`).

The client must clear its streaming preview (`llm.delta` text so far) and keep
its bubble, because the discarded attempt is **never spoken**: it was rejected
before anything was queued for TTS. `llm.restart` is always followed by more
`llm.delta` frames for the new attempt. The `metrics` message at the end of the
turn carries `"retried": true` when it happened.

### `expression`

Sent as soon as a sentence's emotion is known — deliberately *before* its audio,
so the face reacts even though TTS is still working.

```json
{ "type": "expression", "preset": "happy", "weight": 0.75,
  "hold_ms": 2600, "override_blink": false }
```

* `preset` is a VRM 1.0 expression preset name (the character YAML controls the
  mapping from the model's own emotion vocabulary).
* `weight` is the target blendshape weight, `0..1`.
* `hold_ms` is how long to keep it before relaxing back.
* `override_blink: true` freezes blinking while the expression holds (used for
  `surprised`).

### `action`

A gesture hint for the skeleton or a transient facial expression.

```json
{ "type": "action", "name": "nod", "intensity": 1.0 }
```

Skeleton gestures (`nod shake tilt wave think sigh laugh lean_in`) are played by
`MotionController`; `smile laugh blush` are transient *facial* expressions
handled by `ExpressionController` (see `frontend/src/vrm/constants.ts`). Unknown
names are ignored.

### `tts.start`

The header for one spoken sentence. Binary frames follow until `tts.end`.

```json
{
  "type": "tts.start",
  "id": 7,
  "index": 1,
  "text": "It looks lovely today.",
  "format": "pcm_s16le",
  "sample_rate": 24000,
  "channels": 1,
  "duration": 1.86,
  "volumes": [0.0, 0.31, 0.62, 0.88, "..."],
  "visemes": [[0.0, 0.09, "nn", 0.4], [0.09, 0.21, "ih", 0.7], "..."],
  "lipsync_mode": "visemes",
  "expression": { "preset": "happy", "weight": 0.75, "hold_ms": 2600,
                  "override_blink": false },
  "actions": ["nod"],
  "voice": "bn-BD-NabanitaNeural",
  "lang": "bn-BD"
}
```

| Field | Meaning |
| --- | --- |
| `id` | Monotonic per session. Identifies the audio that follows. |
| `index` | Sentence number within the current turn (1-based). |
| `format` | `pcm_s16le` \| `mp3` \| `browser`. `browser` means **no binary frames follow** — the client speaks `text` itself with the Web Speech API, using the same timeline. |
| `sample_rate` / `channels` | Format of the binary frames (PCM is always mono 16-bit LE for now). |
| `duration` | Length of the utterance in seconds. The viseme timeline is normalised to exactly this. |
| `volumes` | Normalised RMS envelope of the real audio, one value per `lipsync.slice_ms` (default 20 ms), peak-normalised to `1.0`. Used to modulate how far the mouth opens. |
| `visemes` | Compact `[start, end, viseme, weight]` frames in **seconds**, covering the whole utterance with no gaps. This is the hottest path of the protocol, hence the flat arrays. **Empty when `lipsync_mode` is `mouth`.** |
| `lipsync_mode` | `mouth` (default) or `visemes` — how to interpret the two fields above. Sent per sentence so a mid-reply config patch can never strand a sentence without lip-sync. |
| `expression` / `actions` | The same data as the preceding `expression`/`action` events, repeated for clients that joined mid-sentence or ignore those events. |
| `voice` | Informative: the voice actually used. With `tts.auto_voice` (default) this is not the configured voice when the sentence's language differs — see `lang`. |
| `lang` | BCP-47 locale of `text` (`bn-BD`, `en-US`, …), decided from the **user's** message once per reply. Browser-side `speechSynthesis` uses it to pick a voice; a server-side engine has already used it to pick one. |

`lipsync_mode`:

| Mode | What the client does |
| --- | --- |
| `mouth` (default) | Ignores `visemes` (it is empty) and drives the single `aa` blendshape from `volumes`: below `lipsync.mouth_threshold` the mouth is shut, above it the openness follows the envelope, scaled by `mouth_scale` and capped at `mouth_max`. A hysteresis band and a minimum dwell time stop it fluttering. When there is no envelope at all (`format: "browser"`), the client falls back to a synthetic speech-shaped level for the utterance's duration. |
| `visemes` | Plays the shape timeline, optionally modulated by `volumes` (see `lipsync.amplitude_modulation`). |

`visemes` vocabulary:

| Viseme | Meaning |
| --- | --- |
| `aa` | open — "father", "kana" |
| `ih` | narrow — "see", "bit" |
| `ou` | rounded — "you", "book" |
| `ee` | spread — "bed", "say" |
| `oh` | mid-round — "go", "thought" |
| `nn` | closed / consonant / pause — zeroes every mouth preset |

### binary frames

Raw audio for the most recent `tts.start`: `format` gives the encoding,
`sample_rate`/`channels` the shape. Frames are chunked by the server at
`tts.chunk_ms` (default 200 ms, i.e. ~9600 bytes of 24 kHz mono PCM), which is
small enough for low latency and large enough to avoid per-frame overhead.

### `tts.end`

```json
{ "type": "tts.end", "id": 7 }
```

The previous `tts.start` is complete. The client may now retire that stream from
its audio graph once it has finished playing.

### `interrupted`

The current turn was abandoned (user barge-in, or a new utterance arrived).

```json
{ "type": "interrupted" }
```

The client stops playback immediately, flushes every queued utterance and zeroes
the mouth, then switches to `idle`.

### `config.ack`

A runtime config patch was accepted and applied to *this session*.

```json
{ "type": "config.ack", "config": { "...": "...", "character": { "...": "..." } },
  "changed": ["tts"] }
```

`changed` lists the top-level sections whose values actually differ. Rejected
patches come back as `error` with `code: "config"` instead.

### `metrics`

Per-turn timings, for the debug pane.

```json
{ "type": "metrics", "turn": 3, "asr_ms": 412.0, "ttft_ms": 318.4,
  "total_ms": 2410.7, "sentences": 2, "history": 9, "retried": false }
```

`ttft_ms` = time to first LLM token; `total_ms` = whole turn, including audio
being handed to the socket; `retried` = the reply-quality guard rejected the
first attempt and regenerated it (see `llm.restart`). Extra keys may be added
later — treat this message as open.

### `history`

```json
{ "type": "history", "messages": [{ "role": "user", "content": "hi" }] }
```

Sent after `reset`. An empty list means "clear the chat log".

### `error`

```json
{ "type": "error", "message": "TTS failed: ...", "fatal": false, "code": "tts" }
```

`fatal: true` means the client should stop and show the error prominently.
Codes in use: `protocol`, `config`, `audio`, `asr`, `asr_disabled`, `llm`, `tts`,
`busy`, `origin`, `path`, `internal`.

`origin` and `path` are handshake-time rejections (see *Connection lifecycle*):
both arrive on a socket that *was* accepted, immediately followed by a `1008`
close, so a bad URL or a stray page produces a readable message instead of an
opaque failed handshake.

### `pong`

```json
{ "type": "pong", "t": 1770000000.5, "server_time": 1770000000.52 }
```

`t` echoes the `ping`. The client computes RTT and a clock offset; a missing pong
for 2.5 heartbeat intervals (default: 37.5 s) marks the socket a zombie and
forces a reconnect.

---

## Client → server

### `hello`

Sent once the avatar is rendered and audio is unlocked. Triggers the greeting.

```json
{ "type": "hello", "client": "web", "version": "1.0.0",
  "sample_rate": 16000, "client_side_asr": false, "greet": true }
```

* `sample_rate` — the rate of the microphone PCM the client will send.
* `client_side_asr: true` — the client transcribes locally (Web Speech API) and
  will use `chat` instead of `audio.start`/`audio.end`.
* `greet: false` — connect silently.

### `chat`

A typed utterance, or one produced by client-side ASR.

```json
{ "type": "chat", "text": "tell me a joke" }
{ "type": "chat", "text": "tell me a joke", "source": "asr" }
```

`source` (optional, default `typed`) says where the text came from. `asr` marks
output of the browser's speech recognition, and is filtered by the transcript
gate exactly like audio the server transcribes itself; `typed` is taken at face
value. A rejected `asr` message is answered with an `asr.final` carrying a
`reason` and nothing else (see above) — no `user.text`, no turn.

A `chat` while the avatar is speaking is treated as barge-in: the current turn is
cancelled first.

### `audio.start` / binary / `audio.end`

```json
{ "type": "audio.start", "sample_rate": 16000 }
{ "type": "audio.end" }
```

Binary frames between the two are the utterance: mono **16-bit little-endian PCM**
(no header, no container). The server buffers them, and on `audio.end` runs ASR,
echoes `asr.final` + `user.text`, and starts the turn. The buffer is capped
(`MAX_AUDIO_BYTES`); overflowing it drops the utterance and sends
`error(code="audio")`. Streaming binary without an `audio.start` is tolerated
(capture starts lazily).

### `interrupt`

```json
{ "type": "interrupt", "heard": "It looks lovely" }
```

Cancels the running turn, stops all audio on the client, and — if `heard` is
non-empty — records what the user actually heard as an assistant message, so the
model continues the thought instead of restating it.

### `config`

```json
{ "type": "config", "payload": { "tts": { "voice": "ja-JP-NanamiNeural" },
                                 "lipsync": { "release_ms": 110 } } }
```

Only the sections `llm`, `tts`, `asr`, `lipsync`, `avatar` may be patched, only
known keys inside them, and values are validated with the same Pydantic models
used at boot. Sending a secret back as `"********"` keeps the stored one.

### `reset`

```json
{ "type": "reset" }
```

Cancels any running turn, clears the conversation history, and replies with
`history([])`.

### `character.reload`

```json
{ "type": "character.reload" }
```

The persona file changed under the server (the settings UI just saved an edit to
`backend/characters/<id>.user.json`). The session re-reads the persona and
replies with a `config.ack` whose `changed` list is `["character"]` and whose
`config.character` is the new name/greeting/language/voice. Deliberately *not* a
`config` patch: re-sending `avatar.character` would reload nothing, because the
id has not changed. An edit does **not** clear the conversation; a real persona
*switch* still does.

### `ping`

```json
{ "type": "ping", "t": 1770000000.5 }
```

Sent every 15 s by default. Answer with `pong`.

---

## Reference sequences

### A typed turn

```
→ chat {"text":"hello"}
← user.text {"text":"hello"}
← state {"value":"thinking"}
← llm.delta ... (many)
← llm.sentence {"index":1,"text":"Hi there!","display_text":"[happy] Hi there!"}
← expression {"preset":"happy","weight":0.75,...}
← action {"name":"nod","intensity":1.0}
← tts.start {"id":1,"format":"pcm_s16le","lipsync_mode":"mouth","duration":0.94,"volumes":[...],"visemes":[]}
← [binary PCM]×N
← tts.end {"id":1}
← state {"value":"speaking","detail":"sentence 1"}
← llm.done {"text":"Hi there!","usage":{}}
← metrics {...}
← state {"value":"idle"}
```

### A turn the reply-quality guard rejects

Nothing is spoken until the first sentence passes the guard, so a rejected
attempt is silent — only the streaming preview has to be discarded:

```
→ chat {"text":"I love coffee in the morning"}
← state {"value":"thinking"}
← llm.delta "I really love "
← llm.delta "coffee in the morning."      ← the whole first sentence parrots the user
← llm.restart                              ← drop the preview above
← llm.delta "[happy] Tea, then. "
← llm.delta "I never got the taste for coffee."
← llm.sentence {"index":1,"text":"Tea, then.","display_text":"[happy] Tea, then."}
... normal turn from here ...
```

### A spoken turn (server-side ASR)

```
→ audio.start {"sample_rate":16000}
→ [binary PCM]×N            (the browser's mic)
→ audio.end
← state {"value":"listening","detail":"transcribing"}
← asr.final {"text":"how are you","duration":1.31}
← user.text {"text":"how are you"}
... as in the typed turn ...
```

### A spoken turn (client-side ASR)

Identical to the typed turn except that the client marks the source, so the
server can apply the same input gate it applies to its own audio:

```
→ chat {"text":"how are you","source":"asr"}
... as in the typed turn ...
```

### An utterance the transcript gate rejects

With speakers on, the microphone hears the avatar. Nothing reaches the LLM.

```
→ chat {"text":"Huh, I had not considered it that way.","source":"asr"}
← asr.final {"text":"","duration":null,"reason":"echo"}
← state {"value":"idle"}
```

A hallucination from near-silence ("Thanks for watching!") behaves the same way
with `"reason":"noise"`.

### Barge-in

```
→ interrupt {"heard":"It looks lovely"}
← interrupted {}
← state {"value":"idle"}
```

### Patching the voice mid-conversation

```
→ config {"payload":{"tts":{"voice":"en-GB-RyanNeural"}}}
← config.ack {"config":{...},"changed":["tts"]}
```

---

## REST surface (not part of the conversation)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | liveness, active engines, session count, preflight report |
| `GET` | `/api/config` | current config (secrets masked) + character list |
| `POST` | `/api/config/validate` | dry-run a patch; `422` when invalid, never raises |
| `GET` | `/api/characters` | available personas |
| `GET` | `/api/voices?engine=&locale=&limit=` | voices for a TTS engine (cached) |
| `GET` | `/api/vrm-models` | `.vrm` files found in `frontend/public/models` |
| `GET` | `/api/vrm-models/{name}/info` | VRM version, title and `preset_expressions` read straight from the GLB header |

`GET /api/vrm-models/<name>/info` is the quickest way to find out whether a model
can lip-sync at all: if `preset_expressions` has no `aa`/`oh`, it cannot.

---

## Versioning

There is no version negotiation on the socket. Growth is additive: new message
types are ignored by older clients (by rule 3), and new *fields* on existing
messages are optional. Breaking changes require a coordinated release of
`backend/app/schemas.py` and `frontend/src/types/protocol.ts`, which is why both
files carry the same comment and are covered by the same tests.
