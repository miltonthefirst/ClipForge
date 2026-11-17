import type { GenerateMusicOptions, LyricsSource } from '@clipforge/contracts';

/** The "Make a track" form's values, and the job options they become. */

export type Vocal = 'FEMALE' | 'MALE' | 'ANY';

/** How notes are given: a file (PDF, Word, PowerPoint, text) or typed in. */
export type NotesMode = 'FILE' | 'TEXT';

/**
 * Typed notes up to this travel in the job itself; longer ones are uploaded
 * as a text file, like any other notes file. There is no limit on notes
 * (the owner, 2026-11-18): only on how much a job document carries.
 */
export const NOTES_INLINE_LIMIT = 8000;
export const IDEA_LIMIT = 300;
export const FOCUS_LIMIT = 300;
/** storage.rules allows up to 100 MB. */
export const NOTES_FILE_MAX_BYTES = 100 * 1024 * 1024;

/** What storage.rules accepts, by extension, with the type each is uploaded as. */
export const NOTES_TYPES: Readonly<Record<string, string>> = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  txt: 'text/plain',
  md: 'text/markdown',
};

export const NOTES_ACCEPT = Object.keys(NOTES_TYPES)
  .map((extension) => `.${extension}`)
  .join(',');

export interface SongForm {
  readonly lyricsFrom: LyricsSource;
  readonly idea: string;
  readonly notesMode: NotesMode;
  readonly notes: string;
  /** The chosen file's name, when one is chosen and acceptable. */
  readonly notesFileName: string | null;
  readonly focus: string;
  readonly trendId: string;
  readonly style: string;
  readonly vocal: Vocal;
  readonly seconds: number;
  readonly takes: number;
}

/** The extension of a file name, lower case, without the dot. */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot === -1 ? '' : name.slice(dot + 1).toLowerCase();
}

/** Why a chosen file cannot be used, or null when it can. */
export function notesFileProblem(name: string, size: number): string | null {
  const extension = extensionOf(name);
  if (extension === 'doc') return 'This is the old Word format. Open it and save it as .docx.';
  if (extension === 'ppt')
    return 'This is the old PowerPoint format. Open it and save it as .pptx.';
  if (!NOTES_TYPES[extension]) return 'Use a PDF, Word (.docx), PowerPoint (.pptx) or text file.';
  if (size > NOTES_FILE_MAX_BYTES) return 'The file is over 100 MB.';
  if (size === 0) return 'The file is empty.';
  return null;
}

/** A name that is safe as the last part of a bucket path, keeping its extension. */
export function safeFileName(name: string): string {
  const cleaned = name
    .replace(/[/\\#?[\]*]/g, '-')
    .replace(/\s+/g, ' ')
    .trim();
  if (cleaned.length <= 150) return cleaned || 'notes.txt';
  const extension = extensionOf(cleaned);
  return `${cleaned.slice(0, 140)}.${extension}`;
}

/**
 * What has to be uploaded before the job can be written: the chosen file,
 * typed notes too long to travel in the job, or nothing.
 */
export function uploadNeeded(form: SongForm): 'FILE' | 'TEXT' | null {
  if (form.lyricsFrom !== 'NOTES') return null;
  if (form.notesMode === 'FILE') return 'FILE';
  return form.notes.trim().length > NOTES_INLINE_LIMIT ? 'TEXT' : null;
}

/**
 * The options the rules accept for this form, or null while something is
 * missing. An upload's `notesFile` is added once it is in the bucket.
 */
export function songOptions(form: SongForm): GenerateMusicOptions | null {
  const style = form.style.trim().slice(0, 200);
  if (!style) return null;
  const base: GenerateMusicOptions = {
    lyricsFrom: form.lyricsFrom,
    style,
    seconds: form.seconds,
    takes: form.takes,
    ...(form.lyricsFrom === 'NONE' ? {} : { vocal: form.vocal }),
  };
  switch (form.lyricsFrom) {
    case 'IDEA': {
      const idea = form.idea.trim().slice(0, IDEA_LIMIT);
      return idea ? { ...base, idea } : null;
    }
    case 'NOTES': {
      const focus = form.focus.trim().slice(0, FOCUS_LIMIT);
      const notesBase = focus ? { ...base, focus } : base;
      if (form.notesMode === 'FILE') return form.notesFileName ? notesBase : null;
      const notes = form.notes.trim();
      if (!notes) return null;
      return notes.length > NOTES_INLINE_LIMIT ? notesBase : { ...notesBase, notes };
    }
    case 'TREND':
      return form.trendId ? { ...base, trendId: form.trendId } : null;
    default:
      return base;
  }
}
