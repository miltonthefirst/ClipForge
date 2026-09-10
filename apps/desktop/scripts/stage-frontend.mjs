/**
 * Stage the Angular build for Tauri to bundle.
 *
 * The desktop app and the deployed site are the *same* Angular build with
 * different `window.__clipforge` blocks — the bundle itself is
 * project-agnostic, which is the property ADR-0004 exists to protect.
 *
 * They get a separate copy rather than sharing `apps/web/dist`. Injecting
 * desktop configuration into the directory that `tools/deploy.ps1` publishes is
 * one mistimed command away from shipping `serviceWorker: false` and a
 * `127.0.0.1` playback origin to a phone. A copy costs a few megabytes on disk
 * and removes the whole class of mistake.
 *
 *   node scripts/stage-frontend.mjs              # the real Firebase project
 *   node scripts/stage-frontend.mjs --emulators  # the local Emulator Suite
 */

import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const desktopRoot = resolve(here, '..');
const repoRoot = resolve(desktopRoot, '..', '..');
const webDist = join(repoRoot, 'apps', 'web', 'dist', 'web', 'browser');
const staged = join(desktopRoot, 'frontend');

const useEmulators = process.argv.includes('--emulators');

function readEnv() {
  const path = join(repoRoot, '.env');
  if (!existsSync(path)) return {};
  const config = {};
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const split = trimmed.indexOf('=');
    if (split < 1) continue;
    config[trimmed.slice(0, split).trim()] = trimmed.slice(split + 1).trim();
  }
  return config;
}

const env = readEnv();

const localServerOrigin = env['CLIPFORGE_WEB_LOCAL_SERVER_ORIGIN'] || 'http://127.0.0.1:8770';

/**
 * Wrap the configuration fields in the assignment that sets them.
 *
 * The opening and the closing live here together on purpose. They used to be
 * written out at each call site, and when the assignment grew into an
 * `Object.assign` one of the two closings kept its old `};` — an unbalanced
 * paren that made the whole block a syntax error. Nothing failed at build time;
 * the browser simply skipped the script, `window.__clipforge` never existed, and
 * the app ran on its emulator defaults. A live sign-in then reported itself as
 * "No network", which is not a clue anyone can follow.
 *
 * Merged rather than assigned, for the same reason as index.html: anything set
 * before this runs has to survive.
 */
function merge(fields) {
  return [
    '      window.__clipforge = Object.assign({}, window.__clipforge, {',
    ...fields,
    '      });',
  ].join('\n');
}

function desktopConfig() {
  // The one genuinely different thing about the desktop build: it runs on the
  // machine that rendered the clips, so playback branch 2 resolves and the
  // video actually plays. On a phone the same code falls through to the poster.
  // See docs/adr/0009-spark-tier-local-artefacts.md.
  const shared = [
    `        localServerOrigin: '${localServerOrigin}',`,
    // Never in the desktop shell. A service worker here would cache the app
    // shell from Tauri's custom protocol and then serve it back after a rebuild,
    // which turns "I just rebuilt" into "why is it still the old one".
    `        serviceWorker: false,`,
    // Tauri's WebView2 blocks `window.open`, so Firebase's popup never opens
    // and sign-in fails with `auth/popup-blocked`. Measured, not assumed — see
    // the note in apps/web/src/app/core/firebase.ts. The app would fall back on
    // its own; asking for redirect up front skips a guaranteed failure.
    `        authFlow: 'redirect',`,
  ];

  if (useEmulators) {
    return merge(shared);
  }

  const projectId = env['CLIPFORGE_FIREBASE_PROJECT_ID'];
  const apiKey = env['CLIPFORGE_WEB_API_KEY'];
  const authDomain = env['CLIPFORGE_WEB_AUTH_DOMAIN'];
  const appId = env['CLIPFORGE_WEB_APP_ID'];
  // Optional: without a bucket the desktop app plays clips from the worker's
  // file server, which is the machine it is running on anyway.
  const storageBucket = env['CLIPFORGE_FIREBASE_STORAGE_BUCKET'] ?? '';

  const missing = Object.entries({
    CLIPFORGE_FIREBASE_PROJECT_ID: projectId,
    CLIPFORGE_WEB_API_KEY: apiKey,
    CLIPFORGE_WEB_AUTH_DOMAIN: authDomain,
    CLIPFORGE_WEB_APP_ID: appId,
  })
    .filter(([, value]) => !value)
    .map(([name]) => name);

  if (missing.length) {
    console.error(
      `\nCannot stage a live desktop build: ${missing.join(', ')} not set in .env.\n\n` +
        'Without these the app keeps its development default and points at the local\n' +
        'Emulator Suite. If that is what you want, pass --emulators and mean it.\n',
    );
    process.exit(1);
  }

  return merge([
    '        useEmulators: false,',
    '        firebase: {',
    `          projectId: '${projectId}',`,
    `          apiKey: '${apiKey}',`,
    `          authDomain: '${authDomain}',`,
    `          appId: '${appId}',`,
    `          storageBucket: '${storageBucket}',`,
    '        },',
    ...shared,
  ]);
}

if (!existsSync(webDist)) {
  console.error(
    `\nNo Angular build at ${webDist}.\nRun: npm --prefix apps/web run build\n`,
  );
  process.exit(1);
}

await rm(staged, { recursive: true, force: true });
await mkdir(staged, { recursive: true });
await cp(webDist, staged, { recursive: true });

const indexPath = join(staged, 'index.html');
const html = await readFile(indexPath, 'utf8');

const pattern = /\/\* CLIPFORGE-CONFIG-START \*\/[\s\S]*?\/\* CLIPFORGE-CONFIG-END \*\//;
if (!pattern.test(html)) {
  console.error(
    '\nCould not find the CLIPFORGE-CONFIG markers in the built index.html.\n' +
      'They are what this script replaces, so without them the desktop app would\n' +
      'silently keep its emulator defaults. Restore them in apps/web/src/index.html\n' +
      'rather than removing this check.\n',
  );
  process.exit(1);
}

const config = desktopConfig();

// Parse it before it ships. A syntax error here costs nothing at build time and
// everything at run time: the browser skips the whole script, so the app finds no
// configuration at all, falls back to the Emulator Suite, and reports a live
// sign-in as "No network" against an emulator that was never running.
try {
  new Function(config);
} catch (error) {
  console.error(`\nGenerated an invalid configuration block:\n\n${config}\n\n${error}\n`);
  process.exit(1);
}

const replaced = html.replace(
  pattern,
  `/* CLIPFORGE-CONFIG-START */\n${config}\n      /* CLIPFORGE-CONFIG-END */`,
);
await writeFile(indexPath, replaced, 'utf8');

// The service worker cannot help here and its manifest would only go stale.
await rm(join(staged, 'ngsw-worker.js'), { force: true });
await rm(join(staged, 'ngsw.json'), { force: true });
await rm(join(staged, 'safety-worker.js'), { force: true });
await rm(join(staged, 'worker-basic.min.js'), { force: true });

const target = useEmulators
  ? 'the local Emulator Suite'
  : `${env['CLIPFORGE_FIREBASE_PROJECT_ID']} (live)`;
console.log(`staged ${staged}`);
console.log(`  target           ${target}`);
console.log(`  playback origin  ${localServerOrigin}`);
console.log(`  service worker   removed`);
