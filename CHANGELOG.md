# Changelog

Notable changes to **Open VRM VTuber**. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

---

## [Unreleased] — 2026-10-20

> **The short version:** Ai now speaks Bengali, English and Banglish with the
> right voice, keeps her personality in a local file you can edit from the UI,
> and moves like a person instead of a puppet — a softer mouth, a calmer idle,
> readable expressions, and gestures that never fight each other.

### Added

- **Bengali · English · Banglish conversation.** Per-turn language detection
  (`bn`, `bn-Latn`, `en`) chooses the voice, viseme set and lip-sync timing for
  that reply. A new reply never inherits the previous reply's language.
- **Editable persona.** *Settings → Persona* writes a git-ignored
  `characters/<id>.user.json`; edits apply live over the WebSocket without
  dropping the conversation. **Delete** restores the shipped YAML.
- **Mood-driven body language.** `MOOD_POSTURE` gives listening, happy, sad,
  angry, surprised, thinking and shy their own shoulder, chest and head
  posture, eased in slowly instead of snapped.
- **Ambient expression layer.** A thinking or listening face sits under the
  sentence mood and is suppressed automatically when a real emotion arrives.
- **Reaction overshoot.** A *changed* mood gets a 240 ms 1.18× transient;
  repeated moods stay steady, so the face reacts without twitching.
- **Settings → Look.** Live sliders for hair, eye and shirt tint, blush
  intensity and lip-sync openness, with a migration for the old grey shirt.

### Changed

- **Lip-sync is a mouth, not a letterbox.** A peak-normalised envelope with a
  4% floor, 0.8 curve, 120 ms release, 60 ms minimum dwell and a hard
  `mouth_max` ceiling. `visemes` mode obeys the same ceiling.
- **Idle motion is quieter.** Roughly half the previous amplitude and slower
  periods. Gestures fade in over 140 ms and out over 260 ms, with a 600 ms
  global gap, so two gestures can no longer overlap.
- **Expressions read at a glance.** Open cheerful eyes use `Fcl_EYE_Fun`;
  surprise no longer opens the mouth; a trailing `?` is curiosity, not shock.
- **`think` is arm-free.** The old hand-to-chin raise clipped on the sample
  avatar; it is now a head and chest tilt with no IK.
- **Voice energy drives the chest, not the head.** A low-passed energy signal
  adds a small breathing sway while speaking.
- **Gestures use their own protocol channel.** Action frames are independent of
  expression frames, so a neutral `(nods)` still nods and no gesture plays
  twice.

### Fixed

- Mouth opening far too wide, visible jitter on quiet syllables, and the
  "surprised at every question" reaction.
- Double-played gestures and restarted `(smiles)` transients after `tts.start`.
- Expression, posture and blush state now clear on barge-in, reset and
  disconnect instead of sticking to the next reply.
- Banglish is detected only when no Indic script is present, and the quality
  guard no longer discards valid Bengali text because of combining marks.

### Security & privacy

- API keys live only in the backend (`secrets.json` / `.env`), are never sent
  to the browser, and are shown in the UI only as `gsk_****abcd`.
- Logs redact `gsk_…`, Bearer tokens and `api_key` fields, including traces.
- `.env`, `.env.local`, `secrets.json`, local memory, logs and virtualenvs are
  ignored by Git and Docker; `scripts/check_secrets.py` passes.
- The server binds `127.0.0.1` by default; CORS and WebSocket origins are
  restricted to the local UI. Memory stays on disk, with a one-click
  **Delete all my data** action.
- Cloud (Groq) and local (Ollama) providers are labelled in the UI. No
  telemetry, no analytics.

### Verification

- `pytest` — 350 passed; `ruff check` clean.
- `npm run typecheck` and `npm run build` clean.
- `scripts/check_secrets.py` — clean (141 files scanned).
- Live probes: Groq first token in ~200–370 ms; a Bengali reply synthesised
  with `bn-BD-NabanitaNeural`; cross-origin state changes rejected with `403`.

### Notes

- First run downloads Whisper weights on demand; a GPU is not required.
- The sample avatar loads from the three-vrm CDN. Drop your own `.vrm` into
  `frontend/public/models/` to use it.
- Mixamo `.vrma` clips are optional; the procedural idle needs no assets.
