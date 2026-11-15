// Firebase's predeploy hook for the PWA (hosting:bytepic-clipforge): refuses a
// deploy that is not a release. Copied from Sarungano. A release
// (tools/release.py) raised the version,
// committed and tagged it vX.Y.Z, and built from that commit. So the build
// about to go out must carry the repository's VERSION and the commit HEAD
// is on, and HEAD must be tagged with that version. Anything else means the
// version was not raised, which the owner's rule forbids.
//
//   node tools/check-release.mjs apps/web/src/app/build-info.ts
import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const fail = (why) => {
  console.error(`Deploy refused: ${why}\nDeploy with: python tools/release.py (it raises the version).`);
  process.exit(1);
};

const version = readFileSync(resolve(root, 'VERSION'), 'utf8').trim();
const head = execSync('git rev-parse --short HEAD', { cwd: root, encoding: 'utf8' }).trim();
let tag = '';
try {
  tag = execSync('git describe --exact-match --tags HEAD', { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
} catch {
  fail(`this commit (${head}) is not a tagged release.`);
}
if (tag !== `v${version}`) fail(`this commit is tagged ${tag}, but VERSION says ${version}.`);

const buildInfo = process.argv[2] ? resolve(process.cwd(), process.argv[2]) : null;
if (!buildInfo) fail('no build-info.ts given to check.');
const stamped = readFileSync(buildInfo, 'utf8');
const built = (key) => (stamped.match(new RegExp(`"${key}": "([^"]*)"`)) || [])[1];
if (built('version') !== version || built('commit') !== head) {
  fail(`the build says ${built('version')} (${built('commit')}), not ${version} (${head}). Build again from the release.`);
}
console.log(`Release ${version} (${head}) checked.`);
