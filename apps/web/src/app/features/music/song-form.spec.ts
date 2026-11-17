import { describe, expect, it } from 'vitest';

import { type SongForm, songOptions } from './song-form';

const form = (overrides: Partial<SongForm> = {}): SongForm => ({
  lyricsFrom: 'IDEA',
  idea: 'a slow Sunday',
  notes: '',
  trendId: '',
  style: 'amapiano, log drum',
  vocal: 'ANY',
  seconds: 150,
  takes: 2,
  ...overrides,
});

describe('songOptions', () => {
  it('sends only the words for the source chosen', () => {
    expect(songOptions(form())).toEqual({
      lyricsFrom: 'IDEA',
      style: 'amapiano, log drum',
      seconds: 150,
      takes: 2,
      vocal: 'ANY',
      idea: 'a slow Sunday',
    });
    expect(songOptions(form({ lyricsFrom: 'NOTES', notes: ' cells ' }))?.notes).toBe('cells');
    expect(songOptions(form({ lyricsFrom: 'TREND', trendId: 't1' }))?.trendId).toBe('t1');
  });

  it('waits until the words are there', () => {
    expect(songOptions(form({ idea: '  ' }))).toBeNull();
    expect(songOptions(form({ lyricsFrom: 'NOTES' }))).toBeNull();
    expect(songOptions(form({ lyricsFrom: 'TREND' }))).toBeNull();
    expect(songOptions(form({ style: ' ' }))).toBeNull();
  });

  it('asks an instrumental for no voice and no words', () => {
    const options = songOptions(form({ lyricsFrom: 'NONE' }));
    expect(options).not.toBeNull();
    expect(options).not.toHaveProperty('vocal');
    expect(options).not.toHaveProperty('idea');
  });
});
