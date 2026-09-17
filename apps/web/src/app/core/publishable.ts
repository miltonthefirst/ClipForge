import type { Clip } from '@clipforge/contracts';

/**
 * The publish gate, as the UI sees it.
 *
 * This is the **third** copy of these rules, and the only one that is advisory.
 * The enforcing copies are `firebase/firestore.rules` (which stops a client
 * enqueueing publish work) and `apps/worker/clipforge/publish/gate.py` (which
 * stops the component that actually holds the credentials). Neither of those can
 * explain itself in the interface, and an interface that offered a Publish
 * button and then failed opaquely would be worse than one that never offered it.
 *
 * So: this copy exists to *say why*, immediately and in context. It is not a
 * security boundary and nothing here should ever become one — if this file and
 * the other two disagree, the other two win and this one is the bug.
 *
 * Kept deliberately in step with gate.py, including the refusal codes, so the
 * message a user sees here matches the one a failed job would carry.
 */

export interface PublishRefusal {
  readonly code: string;
  readonly message: string;
}

/**
 * Why this clip may not be published, or `null` if it may.
 *
 * Returns rather than throws so a caller can explain a whole queue in one pass.
 *
 * It takes no clock and no settings. `check_publishable` in gate.py takes a
 * `publishing_enabled` flag as well — a worker setting this app has no way to
 * read — so a clip can pass here and be refused there. That is the right way
 * round: the advisory copy may be optimistic, never permissive.
 */
export function checkPublishable(clip: Clip): PublishRefusal | null {
  if (clip.review !== 'APPROVED') {
    return {
      code: 'NOT_APPROVED',
      message: `This clip is ${clip.review ?? 'unreviewed'}. Only an approved clip can be published.`,
    };
  }

  return null;
}
