# QIQC Challenge website

This repository contains the public English GaugeForge QIQC Challenge website
and the files intentionally published for participants. It does not contain
production credentials, databases, private task manifests, rollout data,
server configuration or deployment state.

## Local preview

Node.js 20 or newer is required. The build has no npm runtime dependencies.

```sh
npm run build
PORT=4174 npm start
```

Open `http://127.0.0.1:4174/` for the static preview. The participant portal
and staff operations are served by the separately managed competition service;
this static preview does not provide account, upload or evaluation operations.

## Source layout

- `generate.mjs` generates the public homepage from `src/facts.json`.
- `src/` contains the public website source and assets.
- `src/downloads/participant-manual.en.*` contains the English participant
  manual published for the event.
- `src/downloads/participant-guide.*` and `src/downloads/partner-handbook.*`
  are retained historical public downloads.
- `dist/` is the generated static snapshot and is checked together with its
  source.

The historical `documents.html` page and its original download links are
preserved. The build copies that page and the original download files without
rewriting them.

## Checks

```sh
node --check generate.mjs
node --check src/app.js
npm run build
git diff --check
```

Review the generated diff before publication. Do not add databases, runtime
directories, provider configuration, server keys, compiled binaries, rollouts,
test evidence or local virtual environments.

See `THIRD-PARTY-NOTICES.md` for asset attribution and licensing information.
