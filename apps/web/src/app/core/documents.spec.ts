import { Timestamp } from 'firebase/firestore';
import { describe, expect, it } from 'vitest';

import { fromDocument } from './documents';

/**
 * The bug these pin down was invisible for a release, and worth restating.
 *
 * The worker writes dates through the Admin SDK as Python `datetime` values, so
 * Firestore stores Timestamps. The PWA writes them as `new Date().toISOString()`
 * strings. The contracts declare strings. `snapshot.data() as Clip` asserted the
 * contract and converted nothing, so half the dates in a document were objects
 * claiming to be strings — and `Date.parse` answers `NaN` rather than throwing,
 * which is how it stayed quiet.
 */
describe('fromDocument', () => {
  it('turns a Firestore Timestamp into the ISO string the contracts promise', () => {
    const when = new Date('2026-09-11T20:15:23.205Z');
    const doc = fromDocument<{ playbackExpiresAt: string }>({
      playbackExpiresAt: Timestamp.fromDate(when),
    });

    expect(doc.playbackExpiresAt).toBe('2026-09-11T20:15:23.205Z');
    expect(Date.parse(doc.playbackExpiresAt)).toBe(when.getTime());
  });

  it('leaves dates the PWA itself wrote alone', () => {
    // The same field arrives as a string or a Timestamp depending on who last
    // touched the document, so both have to come out the same way.
    const doc = fromDocument<{ reviewedAt: string }>({
      reviewedAt: '2026-09-11T19:54:56.110Z',
    });
    expect(doc.reviewedAt).toBe('2026-09-11T19:54:56.110Z');
  });

  it('reaches dates nested in arrays, which is where a job keeps them', () => {
    // Job.stages[] carries startedAt/endedAt per stage. Converting only the top
    // level would fix the clip page and leave every stage duration broken.
    const doc = fromDocument<{ stages: { name: string; startedAt: string }[] }>({
      stages: [{ name: 'UPLOAD', startedAt: Timestamp.fromDate(new Date('2026-09-12T06:20:08Z')) }],
    });
    expect(doc.stages[0].startedAt).toBe('2026-09-12T06:20:08.000Z');
  });

  it('reaches dates nested in objects, which is where a job keeps its lease', () => {
    const doc = fromDocument<{ lease: { expiresAt: string; workerId: string } }>({
      lease: { workerId: 'worker-1', expiresAt: Timestamp.fromDate(new Date('2026-09-10Z')) },
    });
    expect(doc.lease.expiresAt).toBe('2026-09-10T00:00:00.000Z');
    expect(doc.lease.workerId).toBe('worker-1');
  });

  it('leaves every other value exactly as it found it', () => {
    const doc = fromDocument<Record<string, unknown>>({
      sizeBytes: 33622309,
      storagePath: 'clips/user-1/clip-1.mp4',
      playbackUrl: null,
      music: undefined,
      tags: ['a', 'b'],
    });

    expect(doc).toEqual({
      sizeBytes: 33622309,
      storagePath: 'clips/user-1/clip-1.mp4',
      playbackUrl: null,
      music: undefined,
      tags: ['a', 'b'],
    });
  });
});
