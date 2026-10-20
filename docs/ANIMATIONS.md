# Animations

The avatar always has *some* motion: `MotionController` runs a procedural idle
(breathing, weight shift, head drift, arm rest, and hand-written gestures) that
needs no assets at all. Authored clips from Mixamo sit **on top** of it, and the
app is fully usable with none of them.

This document is the whole pipeline: where to get clips, how to get them into the
app, how the retargeting works, and what to check when a clip looks wrong.

---

## 1. The 60-second version

1. Download a clip from <https://www.mixamo.com> as **FBX for Unity** (or
   "FBX Binary", *without* skin — a skinless export is ~100 KB instead of 5 MB).
2. Either use the `.fbx` as-is, or convert it to `.glb` in Blender (§3) or to
   `.vrma` (§4) for the best quality.
3. Drop the file in `frontend/public/animations/`.
4. Add it to `frontend/public/animations/manifest.json`:

```json
{
  "version": 1,
  "idleSeconds": 14,
  "clips": [
    { "name": "idle", "url": "/animations/idle.glb", "kind": "idle" },
    { "name": "wave", "url": "/animations/wave.glb", "kind": "gesture" }
  ]
}
```

5. Reload the page, or press **Motion → Reload clips** in the settings drawer.
   The status line above the buttons tells you exactly what loaded:

```
2 clips · 1 idle / 1 gesture · 41 bones driven · idle: idle
```

If it instead says *"No clips loaded — using procedural motion"*, the problems
list right under it says why (missing file, unmapped skeleton, bad JSON…).

---

## 2. Where clips live in the runtime

```
                    ┌──────────────────────────┐
  manifest.json ───▶ │  AnimationController     │
                    │  ┌────────────────────┐  │
  .vrma/.glb/.fbx ─▶│  │ AnimationMixer     │  │──▶ bone quaternions
                    │  └────────────────────┘  │
                    │  ┌────────────────────┐  │
                    │  │ MotionController   │  │──▶ bone quaternions
                    │  │ (procedural idle)  │  │
                    │  └────────────────────┘  │
                    └──────────────────────────┘
```

* **Idle clips** loop forever. If the manifest lists more than one, the
  controller rotates between them every `idleSeconds` (±25 % jitter, and it skips
  the clip it is already playing), cross-fading as it goes.
* **Gesture clips** play once, hold their last frame, and fade out. A new gesture
  *interrupts* the current one instead of queuing behind it.
* During a gesture the idle is ducked to 35 % so the gesture reads.
* The backend asks for gestures by name (`nod`, `shake`, `tilt`, `wave`, `think`,
  `sigh`, `laugh`, `lean_in`). A gesture clip whose manifest `name` matches is
  used; anything else falls back to the procedural gesture, so a missing clip is
  never an error.

### How the two layers share a skeleton

`AnimationMixer` writes the properties it animates; the procedural layer runs
*after* it, once per frame, and decides per bone:

| Ownership (from the loudest clip) | What the procedural layer does |
| --- | --- |
| `0` | writes its own pose outright |
| `1` | does not touch the bone at all |
| `0 < w < 1` | slerps its own pose over the mixer's output by `1 - w` |

That last row is what makes a fade-in a *blend* rather than a snap: as a clip's
weight ramps up, the procedural layer smoothly hands the bone over instead of
fighting it. And because a clip's first activation happens while the procedural
layer still owns the bone, the mixer's internal "original state" snapshot is the
*procedural idle pose* — so even a slow fade never dips through the model's
T-pose on the way in.

---

## 3. Mixamo → Blender → glTF (recommended manual path)

Mixamo's own exports are fine, but a Blender round trip buys you: a smaller file,
a predictable up-axis, and the option to trim the first/last frames.

1. **Import.** `File ▸ Import ▸ FBX`, pick the Mixamo file. In the import panel set
   *Automatic Bone Orientation* on and leave the scale at 0.01 (Mixamo writes
   centimetres).
2. **Check the root.** Select the armature and look at its rotation. If the
   character faces the wrong way in Blender, you will fix it in the app with
   `rootRotation` instead of editing the rig (§6).
3. **Delete everything but the skeleton+mesh** if you exported with a skin, or
   keep the empty mesh — the app only reads the armature.
4. *Optional trim:* in the Dope Sheet, cut the frames you do not want. Leaving a
   2-second lead-in of nothing makes the avatar look asleep before it moves.
5. **Export.** `File ▸ Export ▸ glTF 2.0`:
   * Format: **glTF Binary (.glb)**
   * Include: **Selected Objects** (armature + mesh only)
   * Transform: **+Y up** (this is Blender's default and what three.js expects)
   * Animation: **on**, with *NLA strips* or *all actions* — either works, but
     **export one clip per file** (see §5).
6. Drop the `.glb` into `frontend/public/animations/` and reference it.

> Bone names do not matter: `mixamorig:LeftForeArm`, `J_Bip_L_LowerArm`,
> `DEF-forearm.L` and `Bip01 L Forearm` are all recognised (§5).

---

## 4. `.vrma`: the highest-quality path

`.vrma` is the VRM Animation format. It stores *normalised-rig* rotations, which
makes it model independent and removes the one approximation the retargeter has
to make — so use it when the clip is important and you have the tooling.

```bash
# From a VRM + a BVH/FBX, in a Node script:
npm install --save-dev @pixiv/three-vrm-animation three
```

```js
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { VRMAnimationLoaderPlugin, createVRMAnimationClip } from '@pixiv/three-vrm-animation';

const loader = new GLTFLoader();
loader.register((parser) => new VRMAnimationLoaderPlugin(parser));

const gltf = await loader.loadAsync('/animations/idle.vrma');
const clip = createVRMAnimationClip(gltf.userData.vrmAnimations[0], vrm);
```

Then export the clip with `THREE.AnimationClip` → glTF. In practice the easiest
route is the *VRM Animation* exporter in
[VRoid Studio](https://vroid.com/en/studio) or the community
`vrma-exporter` Blender add-on; either way the app just loads the `.vrma` and
skips retargeting.

`.vrma` expression and look-at tracks are **deliberately dropped**: the app's own
`ExpressionController` and `LookAtController` own the face, and a clip that
fights them produces a twitching avatar.

---

## 5. Bone names, and what "unmapped" means

Retargeting matches bones by *meaning*, not by spelling. One canonicaliser strips
the namespace prefix, splits on separators and case boundaries, drops noise
tokens (`mixamorig`, `j`, `bip01`, `def`, `ctrl`, `ik`, `nub`, `end`…), and then
matches the remaining tokens:

* **Side:** `l`/`left`/`_L` → left, `r`/`right`/`_R` → right.
* **Segment:** `upperarm`/`arm`/`shoulder` → upper arm, `forearm`/`lowerarm` →
  forearm, `hand` → hand, `thigh`/`upleg` → upper leg, `shin`/`calf`/`leg` →
  lower leg, `foot`/`toe`…
* **Fingers:** `thumb1`/`index2`/`middle3`/`ring`/`little` with `proximal`/
  `intermediate`/`distal` numbering, in both VRoid and Mixamo conventions.
* **Twist/helper joints** (`*_twist`, `*_roll`, `*_helper`, IK targets) are
  rejected rather than guessed at.

If the status panel says something like:

```
"idle" drives no bone of this avatar - check the file's skeleton (unmapped: Root, Hips, Spine)
```

then the file's bones really are unrecognisable — most often because the export
contains *objects* named after bones but no armature, or because a custom rig used
proprietary names. Check it in the Mixamo→Blender path instead of guessing at the
manifest.

Partial mappings are normal and fine:

```
"idle": ignored 9 unmapped track(s) (NeckTwist01, Eye_L, Jaw)
```

---

## 6. Manifest reference

```json
{
  "version": 1,
  "idleSeconds": 14,
  "clips": [
    {
      "name": "idle",
      "url": "/animations/idle.glb",
      "kind": "idle",
      "loop": true,
      "fadeIn": 0.4,
      "fadeOut": 0.6,
      "intensity": 1,
      "positionStrength": 0,
      "positionScale": 1,
      "rootRotation": [0, 0, 0]
    }
  ]
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `version` | `1` | Manifest revision; informational. |
| `idleSeconds` | `14` | Seconds between random idle clips. `0` keeps one idle. |
| `clips[]` | – | The list. An empty array (or no file at all) is valid. |
| `name` | – | **Required.** Gesture clips should use a backend gesture name. |
| `url` | – | **Required.** Relative to the site root (`/animations/…`). |
| `kind` | `gesture` | `idle` loops, `gesture` plays once. |
| `loop` | `kind === "idle"` | Force looping on/off. |
| `fadeIn` | `0.35` | Blend-in seconds (procedural → clip). |
| `fadeOut` | `0.5` | Blend-out seconds (clip → procedural). |
| `intensity` | `1` | Weight multiplier for this clip. |
| `positionStrength` | `1` | How much hips *translation* to keep. `0` = in place. |
| `positionScale` | `1` | Multiplier on hips translation (cm-vs-m exports). |
| `rootRotation` | – | `[x, y, z]` **degrees** premultiplied onto the hips delta. |

Rotation-only retargeting means **root motion never moves the avatar by
default**: the hips' translation is converted into a delta from the clip's own
first frame, so a walk cycle keeps its footfalls but does not walk away from the
camera. Set `positionStrength: 0` to ignore it entirely.

### `rootRotation` — the "my avatar is lying down" fix

Rotations are transferred as *deltas from the clip's own rest pose*, so a clip
authored in Blender (−Y forward, +Z up) maps correctly, while an odd export can
arrive rotated by 90°. Fix it in the manifest, not in Blender:

| Symptom | `rootRotation` |
| --- | --- |
| Face down / lying on the floor | `[−90, 0, 0]` |
| Leaning 90° sideways | `[0, 0, 90]` |
| Facing away from the camera | `[0, 180, 0]` |

If nothing in the list helps, use the Blender path in §3 with `+Y up`.

### Multi-clip files

A manifest entry maps to **one** clip. If a file contains several (Blender's
"all actions" export), the entry uses the clip whose name matches `name`, else the
first one, and reports:

```
"walk" (walk.glb) holds 3 clips; using the first (`Walk`). Add one manifest entry per clip.
```

Split the file, or accept the first clip.

---

## 7. Tuning the look

Everything below is in **Settings → Motion**, per browser, no backend involved.

| Control | What it does |
| --- | --- |
| **Use clips** | Master switch. Off = pure procedural idle. |
| **Manifest** | Where the manifest is fetched from (default `/animations/manifest.json`). |
| **Idle variety** | Seconds between idle changes (`0` = keep one). |
| **Gesture strength** | Global multiplier on *clip* gestures. |
| **Idle motion / Breathing / Body sway** | The procedural layer's components. |
| **Relaxed arms / Arm angle** | Rotates a T-pose model's arms down. The angle is detected per model; the slider is for tuning. |
| **Intensity** | Global multiplier on the procedural layer. |

**"My clips look stiff."** Give an idle clip more presence by raising *Gesture
strength* and lowering *Idle motion*, or ship two idles and set *Idle variety*
to 8–15 s so the avatar is not holding one loop for a whole conversation.

**"The arms snap when a gesture ends."** The clip's final frame is far from the
idle pose. Increase `fadeOut`, or end the clip nearer the neutral stance.

**"It moves around the room."** `positionStrength: 0`.

**"It plays a T-pose for half a second when a gesture starts."** The clip's first
frame is a T-pose (Mixamo's "rest" clip). Trim it in Blender (§3 step 4), or add
`fadeIn` so the blend covers it.

---

## 8. Bundled assets

**None.** The repository ships an empty manifest and no animation files: Mixamo's
terms are not redistribution-friendly and VRM animations have their own licences.
Check the licence of anything you add.

Placeholders that need no assets:

* the procedural idle (default), and
* `frontend/public/models/` for avatars — see
  [`../frontend/public/models/README.md`](../frontend/public/models/README.md).
