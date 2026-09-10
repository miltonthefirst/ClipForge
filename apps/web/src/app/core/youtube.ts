import type { PublishPrivacy } from '@clipforge/contracts';

/**
 * The YouTube vocabulary the UI has to speak: categories and privacy.
 *
 * Here rather than on either page because both need it — Settings sets a
 * channel's default and the publish screen overrides it for one upload — and a
 * category list that disagreed between the two would be a genuinely confusing
 * bug: the same clip filed under different names depending on which screen the
 * operator used.
 */

/**
 * A shortlist, not the API's full set.
 *
 * YouTube publishes around thirty categories and most are irrelevant to short
 * clips of talking. A picker with thirty entries is one nobody reads, so this is
 * the useful subset with the commonest first. The ids are YouTube's own and are
 * stable; the labels are theirs too, so what the operator picks here matches
 * what they see in YouTube Studio afterwards.
 */
export const CATEGORIES: readonly { readonly id: string; readonly label: string }[] = [
  { id: '22', label: 'People & Blogs' },
  { id: '27', label: 'Education' },
  { id: '28', label: 'Science & Technology' },
  { id: '24', label: 'Entertainment' },
  { id: '23', label: 'Comedy' },
  { id: '26', label: 'Howto & Style' },
  { id: '20', label: 'Gaming' },
  { id: '10', label: 'Music' },
  { id: '25', label: 'News & Politics' },
  { id: '17', label: 'Sport' },
  { id: '1', label: 'Film & Animation' },
];

/** The category shown when nothing else says otherwise. Mirrors the worker's. */
export const DEFAULT_CATEGORY_ID = '22';

/**
 * The three privacy states, with what each one actually means for the operator.
 *
 * Ordered least public first, and that is the order the radio group renders in:
 * the option nearest the top is the one a mis-tap lands on, and of the three,
 * `public` is the only one that cannot be undone — a link that was public may
 * already have been scraped.
 */
export const PRIVACY_OPTIONS: readonly {
  readonly value: PublishPrivacy;
  readonly label: string;
  readonly hint: string;
}[] = [
  { value: 'private', label: 'Private', hint: 'Only you. Nobody with the link can watch it.' },
  {
    value: 'unlisted',
    label: 'Unlisted',
    hint: 'Anyone with the link can watch it. It will not appear in search or on your channel.',
  },
  {
    value: 'public',
    label: 'Public',
    hint: 'Listed on your channel and in search. This is the one you cannot take back.',
  },
];

/** The label for a category id, or the id itself if it is one we do not list. */
export function categoryLabel(id: string | null | undefined): string {
  if (!id) return categoryLabel(DEFAULT_CATEGORY_ID);
  return CATEGORIES.find((category) => category.id === id)?.label ?? id;
}
