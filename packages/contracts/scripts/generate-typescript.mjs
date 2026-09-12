// Generates the TypeScript half of the contracts from schemas/clipforge.json.
//
// Run `npm run generate` to write the output, or `npm run check` to verify the
// committed output is current. CI runs the check, which is what stops a schema
// edit from landing without its regenerated types (docs/PLAN.md Phase 1, exit
// criterion 4).

import { compileFromFile } from 'json-schema-to-typescript';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const input = resolve(root, 'schemas/clipforge.json');
const output = resolve(root, 'generated/typescript/index.d.ts');

const BANNER = `/**
 * ClipForge contracts — GENERATED FILE, DO NOT EDIT.
 *
 * Source of truth: packages/contracts/schemas/clipforge.json
 * Regenerate with: npm --prefix packages/contracts run generate
 *
 * Editing this file by hand is pointless: CI regenerates it and fails on any
 * difference. Change the schema instead.
 */`;

const generated = await compileFromFile(input, {
  bannerComment: BANNER,
  additionalProperties: false,
  declareExternallyReferenced: true,
  enableConstEnums: false,
  style: { singleQuote: true, printWidth: 100 },
});

const isCheck = process.argv.includes('--check');

if (isCheck) {
  let current;
  try {
    current = await readFile(output, 'utf8');
  } catch {
    console.error(`FAIL: ${output} does not exist. Run: npm --prefix packages/contracts run generate`);
    process.exit(1);
  }
  if (current !== generated) {
    console.error(
      'FAIL: generated TypeScript contracts are stale.\n' +
        '      The schema changed but the generated output was not regenerated.\n' +
        '      Run: npm --prefix packages/contracts run generate',
    );
    process.exit(1);
  }
  console.log('PASS: generated TypeScript contracts are up to date.');
} else {
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, generated, 'utf8');
  console.log(`Wrote ${output}`);
}
