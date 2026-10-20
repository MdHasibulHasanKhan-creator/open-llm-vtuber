# Streaming: taking the avatar to OBS, Twitch and YouTube

This app is the *brain and the body*. It is deliberately not a capture tool, so
the last step - getting the avatar into your stream - is done by OBS, exactly the
way the free VTuber methods people use (VRoid Studio, Booth assets, Nizima Live,
VTube Studio) all end up doing it.

There is one thing this app does that a face-tracked VTuber rig does not: the
avatar is driven by a **language model**, not by your webcam. So there is no
tracking step and no tracker calibration - the interesting setup is entirely
about (a) which `.vrm` you use and (b) how OBS sees it.

---

## 1. The five free methods, mapped to this project

The pipeline in "How To Make A VTuber For FREE" has five options. Here is what
each one means for *this* app.

| Method | What it gives you | Status here |
| --- | --- | --- |
| **VRoid Studio** (`vroid.com/en/studio`) | Free 3D anime character creator; exports `.vrm` | **Supported.** Export, then drop the file in `frontend/public/models/` and pick it in *Settings → Avatar*. This is the recommended path. |
| **Booth** (`booth.pm`) | Free/paid `.vrm` models, outfits, `.vrma` animations | **Supported.** Same as above for models; `.vrma` clips go in `frontend/public/animations/` (see `docs/ANIMATIONS.md`). |
| **Mannequin Character Generator** (Steam, AR14 Works) | Auto-rigged mannequin + PuppetString | **Partial.** It exports a `.vrm`, so it loads - but its charm is its own rigging/animation tooling, which we do not use. |
| **Nizima Live** (Live2D Inc.) | Plug-and-play Live2D VTuber with camera tracking | **Not applicable.** It is a closed Live2D *tracking* product; a `.vrm`/3D pipeline cannot plug into it. Use it instead of this app if you want face tracking with no AI conversation. |
| **VTuber Kit** (`kyuppin.itch.io/vtuber-kit`) | Free 2D rig with Twitch-chat interaction | **Not applicable** for the character itself (it is 2D, not VRM). Its *chat-reading* idea is orthogonal - see "Audience interaction" below. |

Two more pieces from that world are worth naming, because they are where this
project fits:

* **VTube Studio / VBridger** - Live2D/VRM viewers driven by webcam tracking.
  They are the *opposite* input to this app (you→avatar rather than
  model→avatar), so a VRM you own can be used in either, but not at once.
* **Vupechat** (`vupechat.com`) - "VTuber Omegle", and it now accepts a virtual
  camera input, which is how people put a VTuber in front of strangers. It works
  with the OBS virtual camera described below.

## 2. The two ways OBS can see the avatar

### A. Browser source (recommended - cleanest, transparent)

This app is a web page, so OBS can render it directly. No virtual camera, no
green screen, no cropping.

1. Make sure the backend is serving the built frontend on one port:

   ```powershell
   cd frontend; npm run build
   cd ..\backend; .\.venv\Scripts\python.exe main.py
   ```

2. In OBS: **Sources → + → Browser**, and set URL to:

   ```
   http://localhost:8000/?streamer=1
   ```

   Width/height whatever your canvas is (e.g. 1920×1080). Tick **Shutdown source
   when not visible** so the avatar reloads when you switch scenes.

3. `?streamer=1` does three things:
   * hides the top bar, chat panel, toggles and every control - the avatar is
     alone on the page;
   * drops the app's decorative backdrop so the page is **transparent** (the
     WebGL renderer is already `alpha: true` with a clear alpha of 0), which
     means you can key the avatar over a gameplay capture or a background image;
   * auto-starts the session, because capture tools normally permit autoplay.

   If the capture tool *does* block autoplay, a single "Start talking" button is
   shown at the bottom - click it once through the OBS preview
   (**right-click the source → Interact**).

4. Add a colour source *behind* the browser source if you want a flat background
   instead of transparency.

Accepted spellings: `?streamer=1`, `?mode=streamer`, `?clean`, `?obs`. The
autostart can also be requested on its own with `?autostart=1`.

### B. Virtual camera (when you need a video feed)

Use this when you want the avatar somewhere a browser source cannot reach - a
different app, Vupechat, a capture card, or a second machine.

1. Run this app in streamer mode in a normal browser window (so you can see it):
   `http://localhost:8000/?streamer=1`.
2. In OBS: **Start Virtual Camera** (Controls panel, bottom right).
3. Select **OBS Virtual Camera** as the camera in the other application.

The trade-off versus a browser source: the feed is opaque, so you must key it
(chroma key a flat colour background) instead of getting transparency for free.

### Turning the toggle on from the UI

*Settings → Avatar → Streaming (OBS)* sets the same thing as the URL parameter,
persisted per browser. The URL parameter wins on a fresh browser profile, which
is what an OBS source always is.

## 3. Voice

The microphone is **this app's** input, not the stream's. OBS does not need to
hear you: the backend runs speech recognition and the language model, and the
result is spoken by the configured TTS engine.

* If the TTS engine is `edge` or `openai`, audio comes from the **browser
  source**. In OBS, right-click the source and use **Advanced Audio Properties**
  to route it to your stream mix and/or your monitor.
* If the TTS engine is `browser`, the page speaks with the Web Speech API. That
  still plays inside the browser source, but the voice list and quality are the
  browser's, not Edge's.
* Keep the app's own volume at 100 % (*Settings → Avatar → Output volume*) and
  set the level in OBS instead - there is one volume control, and it should not
  be in two places.

## 4. Audience interaction

The free methods above get engagement from **reading the chat**. This app's
conversation is driven by the microphone and the typed box, which is right for a
1-to-1 companion and wrong for a busy stream.

If chat-reading is what you want, the honest answer is that it is not built in:
you would bridge it yourself by POSTing chat lines into the same WebSocket
`chat` message the UI uses (`docs/PROTOCOL.md`). Treat that as a small
integration project, not a setting.

## 5. Checklist

- [ ] `.vrm` in `frontend/public/models/`, selected in *Settings → Avatar*.
- [ ] Model connected (*Settings → Model → Detect models* / a preset). If the
      banner says "Offline demo mode", your replies are a script until you fix this.
- [ ] TTS engine chosen and a voice you like (*Settings → Voice*).
- [ ] `npm run build` run at least once so the backend can serve the page.
- [ ] OBS browser source at `http://localhost:8000/?streamer=1`.
- [ ] Autoplay works (or you clicked "Start talking" once via *Interact*).
- [ ] Audio routed in *Advanced Audio Properties*.
