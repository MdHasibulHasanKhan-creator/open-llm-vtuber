# Animations

Drop animation files in this folder and list them in `manifest.json`. The app
reads the manifest at startup, retargets every clip onto the loaded avatar and
blends it over the procedural idle. **An empty manifest is a valid, fully
working configuration** - the avatar then uses its procedural idle and gestures.

Full instructions (including the Mixamo → Blender → glTF walkthrough) live in
[`docs/ANIMATIONS.md`](../../../docs/ANIMATIONS.md).

## The short version

1. Get a clip from <https://www.mixamo.com> (a *without skin* FBX is the smallest
   download), or any humanoid `.glb` / `.vrma` you already have.
2. Either use the `.fbx`/`.glb` directly (bones are matched by name - Mixamo,
   VRoid, Blender and Rigify names all work) or convert to `.vrma` with
   [`@pixiv/three-vrm-animation`](https://github.com/pixiv/three-vrm) for the
   cleanest result.
3. Put the file here and add an entry to `manifest.json`:

```json
{
  "version": 1,
  "idleSeconds": 14,
  "clips": [
    { "name": "idle",  "url": "/animations/idle.glb",      "kind": "idle" },
    { "name": "wave",  "url": "/animations/wave.glb",      "kind": "gesture" },
    { "name": "nod",   "url": "/animations/agree.glb",     "kind": "gesture" },
    { "name": "think", "url": "/animations/thinking.glb",  "kind": "gesture" }
  ]
}
```

`name` matters for `gesture` clips: the backend asks for `nod`, `shake`, `tilt`,
`wave`, `think`, `sigh`, `laugh` and `lean_in`. Name your file after the gesture
and it will be used automatically; any other name is playable from the settings
drawer (**Motion → Clips**).

## Fields

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | – | Required. Gesture clips should use a backend gesture name. |
| `url` | – | Required. `.vrma`, `.glb`, `.gltf` or `.fbx`, relative to the site root. |
| `kind` | `gesture` | `idle` loops forever, `gesture` plays once. |
| `loop` | `kind === 'idle'` | Force looping on/off. |
| `fadeIn` / `fadeOut` | `0.35` / `0.5` | Blend time in seconds. |
| `intensity` | `1` | Weight, < 1 for a gentler clip. |
| `positionStrength` | `1` | How much hips translation to keep. `0` = stay in place. |
| `positionScale` | `1` | Multiplier for the hips translation (fixes cm-vs-m exports). |
| `rootRotation` | – | `[x, y, z]` degrees applied to the hips, for exports with an odd up-axis. |

> These are example files, not shipped ones - no animation assets are bundled
> with the repository. Make sure the licence of anything you add allows it.
