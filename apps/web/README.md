# ClipForge PWA

The Angular front end. Submit a video, watch the pipeline progress live, and review
and approve the clips the worker produces — installable, and usable one-handed on a
phone.

See [`docs/PLAN.md`](../../docs/PLAN.md) for the architecture and the repository
[`README.md`](../../README.md) for setup.

> **Status: Phase 0.** This is a shell. The dashboard, submission flow and review
> queue land in Phase 7, which is what ships v0.1.0.

## Commands

```bash
npm start              # dev server on http://localhost:4200
npm run lint
npm run typecheck      # tsc --noEmit across app and spec configs
npm run format:check   # prettier
npm run test:ci        # vitest + jsdom, no watch
npm run build          # production bundle
```

CI runs `lint`, `typecheck`, `format:check`, `test:ci` and `build` — the same five
commands, so nothing fails only on the server.

## Stack notes

- **Angular 22** standalone components with signals; no NgModules.
- **Tailwind 4** via `@tailwindcss/postcss`, configured in
  [`.postcssrc.json`](.postcssrc.json). Design tokens live in the `@theme` block in
  [`src/styles.css`](src/styles.css) rather than a `tailwind.config.js`.
- **Vitest + jsdom** for tests, so CI needs no browser.
- Firebase project ids and emulator hosts come from `.env` only, never from source —
  see the Phase 1 addendum in `docs/PLAN.md`.
