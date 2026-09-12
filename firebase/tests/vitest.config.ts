import { defineConfig } from 'vitest/config';

/**
 * Rules tests must run one file at a time.
 *
 * Every spec talks to the *same* emulator project and calls `clearFirestore()`
 * between tests. Run in parallel, one file's wipe deletes another file's
 * fixtures mid-test, and the failure surfaces as a rules `get()` returning null
 * — which reads exactly like a broken rule and is not one.
 *
 * This was latent while there were two spec files and became reproducible on the
 * third. Isolating by project id instead would work, but sequential execution
 * costs a couple of seconds and keeps every spec pointed at the same rules the
 * emulator actually loaded.
 */
export default defineConfig({
  test: {
    fileParallelism: false,
  },
});
