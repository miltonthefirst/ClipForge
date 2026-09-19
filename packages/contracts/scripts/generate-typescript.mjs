// Generates the TypeScript half of the contracts from schemas/clipforge.json,
// and the category catalogue from data/categories.json.
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
const catalogueInput = resolve(root, 'data/categories.json');
const catalogueJs = resolve(root, 'generated/typescript/categories.js');
const catalogueDts = resolve(root, 'generated/typescript/categories.d.ts');

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

// The category catalogue is data, not a type, and it is needed at runtime on
// both sides: the PWA lists it, the worker acts on it. It is rendered as a
// plain ES module plus a declaration file — the shape a compiled package has —
// because that is what both consumers accept: `tsc` refuses a `.ts` source
// outside the app's root but reads a `.d.ts` from anywhere, and the bundler
// follows the same path mapping to the `.js`.
const CATALOGUE_BANNER = `/**
 * ClipForge category catalogue — GENERATED FILE, DO NOT EDIT.
 *
 * Source of truth: packages/contracts/data/categories.json
 * Regenerate with: npm --prefix packages/contracts run generate
 *
 * Editing this file by hand is pointless: CI regenerates it and fails on any
 * difference. Change the catalogue instead.
 */`;

const NL = '\n';

function quote(value) {
  return `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
}

function strings(values) {
  return `[${values.map(quote).join(', ')}]`;
}

function renderCatalogueJs(json) {
  const { categories } = JSON.parse(json);
  const rows = categories.map((c) =>
    [
      '  {',
      `    code: ${quote(c.code)},`,
      `    label: ${quote(c.label)},`,
      `    group: ${quote(c.group)},`,
      `    hint: ${quote(c.hint)},`,
      `    aliases: ${strings(c.aliases)},`,
      `    terms: ${strings(c.terms)},`,
      `    subreddits: ${strings(c.subreddits)},`,
      '  },',
    ].join(NL),
  );
  return [
    CATALOGUE_BANNER,
    '',
    "/** @type {readonly import('./categories').Category[]} */",
    'export const CATEGORIES = Object.freeze([',
    ...rows,
    ']);',
    '',
  ].join(NL);
}

function renderCatalogueDts() {
  return [
    CATALOGUE_BANNER,
    '',
    '/** One entry of the catalogue. `code` is what travels on `ResearchOptions.category`. */',
    'export interface Category {',
    '  readonly code: string;',
    '  readonly label: string;',
    '  readonly group: string;',
    '  /** Appended to a feed phrase when videos are looked up for it. */',
    '  readonly hint: string;',
    '  /** Other words a person might search the list by. */',
    '  readonly aliases: readonly string[];',
    '  /** Searched on YouTube when a run has no topics of its own. */',
    '  readonly terms: readonly string[];',
    '  /** Read when a run names no subreddits of its own. */',
    '  readonly subreddits: readonly string[];',
    '}',
    '',
    'export declare const CATEGORIES: readonly Category[];',
    '',
  ].join(NL);
}

const catalogueSource = await readFile(catalogueInput, 'utf8');
const catalogueJsText = renderCatalogueJs(catalogueSource);
const catalogueDtsText = renderCatalogueDts();

const isCheck = process.argv.includes('--check');

async function checkCurrent(path, want, what) {
  let current;
  try {
    current = await readFile(path, 'utf8');
  } catch {
    console.error(`FAIL: ${path} does not exist. Run: npm --prefix packages/contracts run generate`);
    process.exit(1);
  }
  if (current !== want) {
    console.error(
      `FAIL: generated ${what} is stale.\n` +
        '      The source changed but the generated output was not regenerated.\n' +
        '      Run: npm --prefix packages/contracts run generate',
    );
    process.exit(1);
  }
}

if (isCheck) {
  await checkCurrent(output, generated, 'TypeScript contracts');
  await checkCurrent(catalogueJs, catalogueJsText, 'category catalogue module');
  await checkCurrent(catalogueDts, catalogueDtsText, 'category catalogue declarations');
  console.log('PASS: generated TypeScript contracts and catalogue are up to date.');
} else {
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, generated, 'utf8');
  console.log(`Wrote ${output}`);
  await writeFile(catalogueJs, catalogueJsText, 'utf8');
  console.log(`Wrote ${catalogueJs}`);
  await writeFile(catalogueDts, catalogueDtsText, 'utf8');
  console.log(`Wrote ${catalogueDts}`);
}
