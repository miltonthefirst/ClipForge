import type { GenerateMusicOptions, LyricsSource } from '@clipforge/contracts';

/** The "Make a track" form's values, and the job options they become. */

export type Vocal = 'FEMALE' | 'MALE' | 'ANY';

export const NOTES_LIMIT = 8000;
export const IDEA_LIMIT = 300;

export interface SongForm {
  readonly lyricsFrom: LyricsSource;
  readonly idea: string;
  readonly notes: string;
  readonly trendId: string;
  readonly style: string;
  readonly vocal: Vocal;
  readonly seconds: number;
  readonly takes: number;
}

/** The options the rules accept for this form, or null while something is missing. */
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
      const notes = form.notes.trim().slice(0, NOTES_LIMIT);
      return notes ? { ...base, notes } : null;
    }
    case 'TREND':
      return form.trendId ? { ...base, trendId: form.trendId } : null;
    default:
      return base;
  }
}
