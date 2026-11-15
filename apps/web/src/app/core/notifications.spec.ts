import { describe, expect, it } from 'vitest';

import { KINDS, deviceId, deviceLabel } from './notifications';

describe('notifications', () => {
  it('names a device well enough to tell two apart', () => {
    expect(
      deviceLabel(
        'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36',
      ),
    ).toBe('Android · Chrome');
    expect(
      deviceLabel(
        'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1',
      ),
    ).toBe('iPhone · Safari');
    expect(deviceLabel('Mozilla/5.0 (Windows NT 10.0) Chrome/140.0 Safari/537.36 Edg/140.0')).toBe(
      'Windows · Edge',
    );
  });

  it('derives the same short id from the same token, and a different one otherwise', async () => {
    const one = await deviceId('token-a');
    expect(one).toMatch(/^[0-9a-f]{20}$/);
    expect(await deviceId('token-a')).toBe(one);
    expect(await deviceId('token-b')).not.toBe(one);
  });

  it('lists every kind the PC sends except the test, with the admin ones marked', () => {
    const kinds = KINDS.map((info) => info.kind);
    expect(kinds).toEqual(
      expect.arrayContaining(['CLIPS_READY', 'JOB_FAILED', 'JOB_WAITING', 'PUBLISHED']),
    );
    expect(kinds).not.toContain('TEST');
    expect(
      KINDS.filter((info) => info.adminOnly)
        .map((info) => info.kind)
        .sort(),
    ).toEqual(['SIGNUP', 'WORKER_DOWN', 'YOUTUBE_SIGN_IN']);
  });
});
