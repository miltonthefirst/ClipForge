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
/** A pasted brief: a chat assistant's whole answer, lyrics and all, fits easily. */
export const BRIEF_LIMIT = 10000;

/**
 * What to ask a chat assistant for, so its answer reads well as a brief: the
 * parts the PC looks for, lyrics with their sections in brackets. The person
 * types their idea after the last line. Any other answer works too; this one
 * leaves the least to guess.
 */
export const ASSISTANT_PROMPT = `Write a complete song for me to make with an AI music generator. Reply with these parts, in this order:

Title:
Style: the genre, instruments, mood and energy, in plain words (no artist names)
Vocals: female, male, duet, choir or instrumental
Language:
Tempo: in BPM
Length: in minutes (at most 8)
Video picture: one scene for the background of the video: a place, its light, colours and objects, with no people, logos or writing
Lyrics: each section's name on its own line in square brackets, like [Verse 1], [Chorus], [Bridge], [Outro]. Short lines, and a catchy chorus that repeats word for word. No real people, brands or other songs.

My song idea: `;
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
  /** A BRIEF song's pasted text: it says the sound, the voice and the length too. */
  readonly brief: string;
  readonly style: string;
  readonly vocal: Vocal;
  /** The longest the song may be; the words decide within it. */
  readonly maxSeconds: number;
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
  if (form.lyricsFrom === 'BRIEF') {
    // Only the brief and how many takes: the PC reads the sound, the voice
    // and the length out of the brief itself.
    const brief = form.brief.trim();
    if (!brief || brief.length > BRIEF_LIMIT) return null;
    return { lyricsFrom: 'BRIEF', brief, takes: form.takes };
  }
  const style = form.style.trim().slice(0, 200);
  if (!style) return null;
  const base: GenerateMusicOptions = {
    lyricsFrom: form.lyricsFrom,
    style,
    maxSeconds: form.maxSeconds,
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
