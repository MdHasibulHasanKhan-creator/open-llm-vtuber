# Avatars

Drop your `.vrm` files in this folder and they show up in the settings drawer
(**Avatar → Model**) as `/models/<file>.vrm`.

Nothing here yet? That is fine: the app falls back to the placeholder avatar
(`VITE_VRM_URL`, or three-vrm's sample VRM on jsDelivr).

## Where to get a VRM

* **VRoid Studio** – build your own character and export a `.vrm`:
  <https://vroid.com/en/studio>
* **VRM sample models** – three-vrm's own samples (used by the placeholder):
  <https://github.com/pixiv/three-vrm/tree/dev/packages/three-vrm/examples/models>
* **VRM Consortium** – spec + a list of public avatars:
  <https://vrm.dev/en/>

## What the app needs from a model

| Feature | Used for | Required? |
| --- | --- | --- |
| `aa ih ou ee oh` mouth presets | lip-sync | yes, otherwise the mouth cannot move |
| `blink` (or `blinkLeft`/`blinkRight`) | blinking | recommended |
| `happy angry sad relaxed surprised` | emotion | recommended |
| Humanoid bones (`head`, `chest`, `hips`, arms) | idle motion + gestures | recommended |
| Look-at (VRM 1.0 lookAt / VRM 0.x blend shapes) | eye tracking | optional |

Both **VRM 0.x** and **VRM 1.0** are supported: three-vrm converts 0.x models on
load (including `a → aa`, `joy → happy`, `blink_l → blinkLeft`, ...).

> Licensed assets only – check the model's terms before redistributing it.
