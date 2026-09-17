// @ts-check
const eslint = require('@eslint/js');
const { defineConfig } = require('eslint/config');
const tseslint = require('typescript-eslint');
const angular = require('angular-eslint');

module.exports = defineConfig([
  {
    files: ['**/*.ts'],
    extends: [
      eslint.configs.recommended,
      tseslint.configs.recommended,
      tseslint.configs.stylistic,
      angular.configs.tsRecommended,
    ],
    processor: angular.processInlineTemplates,
    rules: {
      '@angular-eslint/directive-selector': [
        'error',
        {
          type: 'attribute',
          prefix: 'app',
          style: 'camelCase',
        },
      ],
      '@angular-eslint/component-selector': [
        'error',
        {
          type: 'element',
          prefix: 'app',
          style: 'kebab-case',
        },
      ],
    },
  },
  {
    // One door to Firestore, and a rule rather than a habit.
    //
    // Reads cost money, documents are not models, and a described query can be
    // its own cache key — all three only hold if `core/firestore` is the single
    // place that knows what a Firestore query is. A convention decays at the
    // first hurry; this fails the build.
    //
    // `core/firebase.ts` is exempt because it owns app initialisation, and
    // `core/documents.ts` because reconciling Timestamps is the conversion the
    // gate performs. Both are part of the door, not callers of it.
    files: ['src/app/**/*.ts'],
    ignores: [
      'src/app/core/firestore/**',
      'src/app/core/firebase.ts',
      'src/app/core/documents.ts',
      '**/*.spec.ts',
    ],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'firebase/firestore',
              message:
                'Go through core/firestore/gateway.ts. Reads bill per document and the caching, ' +
                'bounding and Timestamp reconciliation all live there.',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['**/*.html'],
    extends: [angular.configs.templateRecommended, angular.configs.templateAccessibility],
    rules: {},
  },
]);
