import { describe, expect, it } from 'vitest';

import {
  ASSISTANT_PROMPT,
  BRIEF_LIMIT,
  NOTES_INLINE_LIMIT,
  type SongForm,
  notesFileProblem,
  safeFileName,
  songOptions,
  uploadNeeded,
} from './song-form';

const form = (overrides: Partial<SongForm> = {}): SongForm => ({
  lyricsFrom: 'IDEA',
  idea: 'a slow Sunday',
  notesMode: 'TEXT',
  notes: '',
  notesFileName: null,
  focus: '',
  trendId: '',
  brief: '',
  style: 'amapiano, log drum',
  vocal: 'ANY',
  maxSeconds: 240,
  takes: 2,
  ...overrides,
});

describe('songOptions', () => {
  it('sends only the words for the source chosen', () => {
    expect(songOptions(form())).toEqual({
      lyricsFrom: 'IDEA',
      style: 'amapiano, log drum',
      maxSeconds: 240,
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
    expect(songOptions(form({ lyricsFrom: 'NOTES', notesMode: 'FILE' }))).toBeNull();
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

describe('a pasted brief', () => {
  it('sends the brief and the takes, and leaves the sound, voice and length to it', () => {
    expect(
      songOptions(form({ lyricsFrom: 'BRIEF', brief: '  [Verse 1]\nhello\n', takes: 3 })),
    ).toEqual({ lyricsFrom: 'BRIEF', brief: '[Verse 1]\nhello', takes: 3 });
  });

  it('waits for a brief, and refuses one longer than a job carries', () => {
    expect(songOptions(form({ lyricsFrom: 'BRIEF', brief: '   ' }))).toBeNull();
    expect(
      songOptions(form({ lyricsFrom: 'BRIEF', brief: 'x'.repeat(BRIEF_LIMIT + 1) })),
    ).toBeNull();
    expect(songOptions(form({ lyricsFrom: 'BRIEF', brief: 'x', style: ' ' }))).not.toBeNull();
  });

  it('asks the assistant for lyrics with their sections in brackets', () => {
    expect(ASSISTANT_PROMPT).toContain('[Verse 1], [Chorus]');
    expect(ASSISTANT_PROMPT.endsWith('My song idea: ')).toBe(true);
  });
});

describe('notes of any length', () => {
  it('uploads a chosen file, and long typed notes, instead of putting them in the job', () => {
    const file = form({ lyricsFrom: 'NOTES', notesMode: 'FILE', notesFileName: 'Week 3.pdf' });
    expect(uploadNeeded(file)).toBe('FILE');
    expect(songOptions(file)).not.toHaveProperty('notes');

    const long = form({ lyricsFrom: 'NOTES', notes: 'x'.repeat(NOTES_INLINE_LIMIT + 1) });
    expect(uploadNeeded(long)).toBe('TEXT');
    expect(songOptions(long)).not.toHaveProperty('notes');

    expect(uploadNeeded(form({ lyricsFrom: 'NOTES', notes: 'short' }))).toBeNull();
    expect(uploadNeeded(form())).toBeNull();
  });

  it('carries the focus with the notes', () => {
    const options = songOptions(
      form({ lyricsFrom: 'NOTES', notesMode: 'FILE', notesFileName: 'a.pdf', focus: ' enzymes ' }),
    );
    expect(options?.focus).toBe('enzymes');
  });

  it('accepts what the PC can read and explains the rest', () => {
    expect(notesFileProblem('Week 3.PDF', 1000)).toBeNull();
    expect(notesFileProblem('slides.pptx', 1000)).toBeNull();
    expect(notesFileProblem('old.doc', 1000)).toMatch(/save it as \.docx/);
    expect(notesFileProblem('old.ppt', 1000)).toMatch(/save it as \.pptx/);
    expect(notesFileProblem('photo.jpg', 1000)).toMatch(/PDF, Word/);
    expect(notesFileProblem('huge.pdf', 200 * 1024 * 1024)).toMatch(/100 MB/);
  });

  it('makes a name safe for a bucket path and keeps its extension', () => {
    expect(safeFileName('Week 3/part #2.pdf')).toBe('Week 3-part -2.pdf');
    const long = safeFileName(`${'a'.repeat(300)}.docx`);
    expect(long.length).toBeLessThanOrEqual(150);
    expect(long.endsWith('.docx')).toBe(true);
  });
});
