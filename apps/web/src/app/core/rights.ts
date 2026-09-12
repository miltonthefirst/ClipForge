import type { Clip, RightsAttestation, RightsBasis } from '@clipforge/contracts';

/**
 * The rights gate, as the UI sees it.
 *
 * This is the **third** copy of these rules, and the only one that is advisory.
 * The enforcing copies are `firebase/firestore.rules` (which stops a client
 * enqueueing publish work) and `apps/worker/clipforge/publish/rights.py` (which
 * stops the component that actually holds the credentials). Neither of those can
 * explain itself in the interface, and an interface that offered a Publish
 * button and then failed opaquely would be worse than one that never offered it.
 *
 * So: this copy exists to *say why*, immediately and in context. It is not a
 * security boundary and nothing here should ever become one — if this file and
 * the other two disagree, the other two win and this one is the bug.
 *
 * Kept deliberately in step with rights.py, including the refusal codes, so the
 * message a user sees here matches the one a failed job would carry.
 */

export interface RightsRefusal {
  readonly code: string;
  readonly message: string;
}

/** Mirrors `MAX_ATTESTATION_AGE_DAYS` in rights.py. */
export const MAX_ATTESTATION_AGE_DAYS = 365;

/** The choices, in the order they are offered, with the plain-English framing. */
export const RIGHTS_BASES: readonly {
  readonly value: RightsBasis;
  readonly label: string;
  readonly hint: string;
}[] = [
  {
    value: 'OWN_CONTENT',
    label: 'I made this',
    hint: 'You filmed or recorded the source yourself.',
  },
  {
    value: 'LICENSED',
    label: 'I have a licence',
    hint: 'Creative Commons, a stock licence, or a paid licence that permits this use.',
  },
  {
    value: 'PERMISSION_GRANTED',
    label: 'The owner said yes',
    hint: 'Explicit permission from the rights holder. Keep the message.',
  },
  {
    value: 'PUBLIC_DOMAIN',
    label: 'Public domain',
    hint: 'Copyright has expired or was never claimed.',
  },
  {
    value: 'FAIR_USE_ASSERTED',
    label: 'Fair use / fair dealing',
    hint: 'A judgement you are making. Explain your reasoning — it goes in the audit log.',
  },
];

/**
 * Why this clip may not be published, or `null` if it may.
 *
 * Returns rather than throws so a caller can explain a whole queue in one pass.
 */
export function checkPublishable(
  clip: Clip,
  options: { readonly publishingEnabled?: boolean; readonly now?: Date } = {},
): RightsRefusal | null {
  const now = options.now ?? new Date();

  if (clip.review !== 'APPROVED') {
    return {
      code: 'NOT_APPROVED',
      message: `This clip is ${clip.review ?? 'unreviewed'}. Only an approved clip can be published.`,
    };
  }

  const rights = clip.rights as RightsAttestation | null | undefined;
  if (!rights) {
    return {
      code: 'NO_ATTESTATION',
      message: 'Record why you may publish this clip before it can enter the publish queue.',
    };
  }

  if (!RIGHTS_BASES.some((basis) => basis.value === rights.basis)) {
    return { code: 'UNKNOWN_BASIS', message: `“${rights.basis}” is not a recognised basis.` };
  }

  if (!rights.attestedBy) {
    return {
      code: 'NO_ATTESTOR',
      message: 'This attestation records nobody, so it cannot be audited.',
    };
  }

  if (!rights.attestedAt) {
    return {
      code: 'NO_ATTESTATION_TIME',
      message: 'This attestation has no timestamp, so its age cannot be judged.',
    };
  }

  const ageDays = Math.floor((now.getTime() - new Date(rights.attestedAt).getTime()) / 86_400_000);
  if (ageDays > MAX_ATTESTATION_AGE_DAYS) {
    return {
      code: 'ATTESTATION_STALE',
      message: `This attestation is ${ageDays} days old. Rights change — confirm it still holds.`,
    };
  }

  if (rights.basis === 'FAIR_USE_ASSERTED' && !rights.note?.trim()) {
    return {
      code: 'FAIR_USE_NEEDS_REASONING',
      message:
        'A fair-use assertion needs a note explaining the reasoning. It is a judgement, not a status.',
    };
  }

  return null;
}

/** Whether a draft attestation is complete enough to be worth submitting. */
export function draftIsComplete(basis: RightsBasis | null, note: string): boolean {
  if (!basis) return false;
  // The one basis whose reasoning genuinely matters after the fact.
  if (basis === 'FAIR_USE_ASSERTED') return note.trim().length > 0;
  return true;
}
