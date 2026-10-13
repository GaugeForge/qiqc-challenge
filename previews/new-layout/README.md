# New layout preview

Independent Figma new layout study (frame 367:76). Main site source and deployment are unchanged.

Run from the repository root:

```sh
node previews/new-layout/server.mjs
```

Open http://127.0.0.1:4175/. Navigation links return to the main preview on port 4174.

The original Figma image is the loading fallback; the canvas uses the existing site’s two-level Rabi model and palette at full opacity. The Optima heading now sits below the chart, with the two green colors in the latest Figma frame. The y-axis is placed inside the chart with legible labels and ticks. The animation supports pause/resume and prefers-reduced-motion.
