# PETRON Brain V0.1

The entire PETRON interface is the Brain. **Nothing else appears on screen.**

- Pure black full-viewport background.
- A single volumetric, irregular, monochrome particle sphere.
- Slow continuous motion, responsive pointer parallax, and a discreet pulse on touch/click.
- No navigation, panels, buttons, labels, remotely hosted assets, network calls, analytics, voice recording, model calls, or dependencies.
- Generated local visual texture plus procedural HTML Canvas 2D particle animation; adaptive pixel density/particle count, target ~30 FPS and reduced-motion support. This is a visual prototype, not a true 3D renderer.
- Works without a development server: open `index.html` directly.

## Integration into PexisOS

Copy the `apps/petron/brain` directory into the PexisOS monorepo. It is deliberately standalone and does not import `src/pexislink` or call the PETRON Python runtime.

An optional host can change the animation with:

```js
window.PetronBrain.setState('thinking'); // idle | thinking | working | success | error
window.PetronBrain.setActivity(0.7); // from 0 to 1
window.PetronBrain.getState();
window.PetronBrain.pause();
window.PetronBrain.resume();
```

These states alter only the visual animation. There is **no live PETRON connection** and the Brain cannot claim a real mission status on its own. When wired to the runtime later, connect the trusted status events to these calls. Do not create an unprotected command endpoint merely to drive the animation.

## Test

No build step required. For convenience, serve the directory with `python3 -m http.server 8000`, then visit `http://127.0.0.1:8000`. The code remains entirely local and makes no network requests.
