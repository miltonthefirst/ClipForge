import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import type { LyricsSource } from '@clipforge/contracts';

import { SONG_STYLES, TracksRepository } from '../../core/data/tracks';
import { SessionService } from '../../core/session';
import { IDEA_LIMIT, NOTES_LIMIT, type SongForm, type Vocal, songOptions } from './song-form';

/** Where the words come from, as the form offers it. */
const SOURCES: readonly { readonly value: LyricsSource; readonly label: string }[] = [
  { value: 'IDEA', label: 'An idea' },
  { value: 'TREND', label: 'A trend' },
  { value: 'NOTES', label: 'My notes' },
  { value: 'NONE', label: 'No words' },
];

const LENGTHS = [
  { seconds: 60, label: '1 min' },
  { seconds: 150, label: '2½ min' },
  { seconds: 210, label: '3½ min' },
];

/**
 * Ask the PC for a new song (docs/MUSIC-PLAN.md, M2 and M5).
 *
 * The local model writes a title and lyrics from an idea, a trend or a
 * person's notes, and the music model sings them, a take per seed. The takes
 * arrive in the list below as NEW, for a person to keep the best. A trend song
 * is about the feeling under the headline and names nobody: the worker checks
 * that, because a song goes out under the channel's name.
 */
@Component({
  selector: 'app-make-track',
  imports: [FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="mt-6 rounded-xl border border-line bg-panel p-4">
      <div class="flex flex-wrap items-baseline justify-between gap-2">
        <h2 class="font-medium text-ink">Make a track</h2>
        <button
          type="button"
          class="text-sm text-forge-600 underline"
          [attr.aria-expanded]="open()"
          (click)="open.set(!open())"
        >
          {{ open() ? 'Close' : 'Open' }}
        </button>
      </div>
      @if (!open()) {
        <p class="mt-1 text-sm text-ink-muted">
          A song written and sung on the PC, from an idea, a trend or your notes.
        </p>
      } @else {
        <form class="mt-3 space-y-4" (ngSubmit)="submit()">
          <fieldset>
            <legend class="text-sm font-medium text-ink">The words come from</legend>
            <div class="mt-2 flex flex-wrap gap-2">
              @for (source of sources; track source.value) {
                <button
                  type="button"
                  class="rounded-full border px-3 py-1 text-sm"
                  [class]="
                    lyricsFrom() === source.value
                      ? 'border-forge-600 bg-forge-600 text-white'
                      : 'border-line-strong text-ink hover:bg-line'
                  "
                  [attr.aria-pressed]="lyricsFrom() === source.value"
                  (click)="lyricsFrom.set(source.value)"
                >
                  {{ source.label }}
                </button>
              }
            </div>
          </fieldset>

          @switch (lyricsFrom()) {
            @case ('IDEA') {
              <label class="block text-sm text-ink">
                The idea
                <input
                  class="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-3 py-2 text-ink placeholder:text-ink-subtle focus:border-forge-500 focus:outline-none"
                  name="idea"
                  [maxlength]="ideaLimit"
                  placeholder="Sunday evening with family after a long week"
                  [ngModel]="idea()"
                  (ngModelChange)="idea.set($event)"
                />
              </label>
            }
            @case ('TREND') {
              <label class="block text-sm text-ink">
                The trend
                <select
                  class="mt-1 block w-full rounded-lg border border-line-strong bg-canvas px-3 py-2 text-ink"
                  name="trend"
                  [ngModel]="trendId()"
                  (ngModelChange)="trendId.set($event)"
                >
                  <option value="">Choose one…</option>
                  @for (trend of trends() ?? []; track trend.id) {
                    <option [value]="trend.id">{{ trend.topic }}</option>
                  }
                </select>
              </label>
              <p class="text-xs text-ink-subtle">
                The song is about the feeling under the headline, never the people in it. No trends
                yet? Run one on the <a class="underline" routerLink="/trends">Trends</a> page.
              </p>
            }
            @case ('NOTES') {
              <label class="block text-sm text-ink">
                Your notes
                <textarea
                  class="mt-1 w-full rounded-lg border border-line-strong bg-canvas px-3 py-2 text-ink placeholder:text-ink-subtle focus:border-forge-500 focus:outline-none"
                  rows="6"
                  name="notes"
                  [maxlength]="notesLimit"
                  placeholder="Paste a lecture's notes or a chapter summary. The song teaches what they say, and only that."
                  [ngModel]="notes()"
                  (ngModelChange)="notes.set($event)"
                ></textarea>
              </label>
              <p class="text-xs text-ink-subtle">
                {{ notes().length }} / {{ notesLimit }} characters. The notes are kept with the job
                and read by the model on the PC; they are not sent anywhere else.
              </p>
            }
            @case ('NONE') {
              <p class="text-sm text-ink-muted">An instrumental: the music only.</p>
            }
          }

          <fieldset>
            <legend class="text-sm font-medium text-ink">The sound</legend>
            <div class="mt-2 flex flex-wrap gap-2">
              @for (preset of styles; track preset.name) {
                <button
                  type="button"
                  class="rounded-full border px-3 py-1 text-sm"
                  [class]="
                    style() === preset.style
                      ? 'border-forge-600 bg-forge-600 text-white'
                      : 'border-line-strong text-ink hover:bg-line'
                  "
                  [attr.aria-pressed]="style() === preset.style"
                  (click)="style.set(preset.style)"
                >
                  {{ preset.name }}
                </button>
              }
            </div>
            <input
              class="mt-2 w-full rounded-lg border border-line-strong bg-canvas px-3 py-2 text-sm text-ink focus:border-forge-500 focus:outline-none"
              name="style"
              maxlength="200"
              aria-label="The sound, in words"
              [ngModel]="style()"
              (ngModelChange)="style.set($event)"
            />
            <p class="mt-1 text-xs text-ink-subtle">
              Genre, instruments and mood, in words. Never an artist's name.
            </p>
          </fieldset>

          <div class="flex flex-wrap gap-4">
            <label class="text-sm text-ink">
              Voice
              <select
                class="mt-1 block rounded-lg border border-line-strong bg-canvas px-3 py-2 text-ink"
                name="vocal"
                [disabled]="lyricsFrom() === 'NONE'"
                [ngModel]="vocal()"
                (ngModelChange)="vocal.set($event)"
              >
                <option value="ANY">One of each</option>
                <option value="FEMALE">Female</option>
                <option value="MALE">Male</option>
              </select>
            </label>
            <label class="text-sm text-ink">
              Length
              <select
                class="mt-1 block rounded-lg border border-line-strong bg-canvas px-3 py-2 text-ink"
                name="seconds"
                [ngModel]="seconds()"
                (ngModelChange)="seconds.set(+$event)"
              >
                @for (length of lengths; track length.seconds) {
                  <option [value]="length.seconds">{{ length.label }}</option>
                }
              </select>
            </label>
            <label class="text-sm text-ink">
              Takes
              <select
                class="mt-1 block rounded-lg border border-line-strong bg-canvas px-3 py-2 text-ink"
                name="takes"
                [ngModel]="takes()"
                (ngModelChange)="takes.set(+$event)"
              >
                <option [value]="1">1</option>
                <option [value]="2">2</option>
                <option [value]="3">3</option>
              </select>
            </label>
          </div>

          @if (error(); as message) {
            <p class="text-sm text-danger-ink">{{ message }}</p>
          }
          @if (started(); as jobId) {
            <p class="text-sm text-ink-muted">
              The PC is writing and singing it. The takes appear below, and you get a notification
              (<a class="underline" [routerLink]="['/jobs', jobId]">follow the job</a>).
            </p>
          }

          <button
            type="submit"
            class="rounded-lg bg-forge-600 px-4 py-2 text-sm font-medium text-white hover:bg-forge-700 disabled:opacity-50"
            [disabled]="!ready() || sending()"
          >
            {{ sending() ? 'Sending…' : 'Make it' }}
          </button>
        </form>
      }
    </section>
  `,
})
export class MakeTrack implements OnDestroy {
  private readonly repository = inject(TracksRepository);
  private readonly session = inject(SessionService);
  private readonly liveTrends = this.repository.recentTrends();

  protected readonly sources = SOURCES;
  protected readonly styles = SONG_STYLES;
  protected readonly lengths = LENGTHS;
  protected readonly notesLimit = NOTES_LIMIT;
  protected readonly ideaLimit = IDEA_LIMIT;
  protected readonly trends = this.liveTrends.data;

  protected readonly open = signal(false);
  protected readonly lyricsFrom = signal<LyricsSource>('IDEA');
  protected readonly idea = signal('');
  protected readonly notes = signal('');
  protected readonly trendId = signal('');
  protected readonly style = signal(SONG_STYLES[0]!.style);
  protected readonly vocal = signal<Vocal>('ANY');
  protected readonly seconds = signal(150);
  protected readonly takes = signal(2);
  protected readonly sending = signal(false);
  protected readonly started = signal<string | null>(null);
  protected readonly error = signal<string | null>(null);

  protected readonly ready = computed(() => songOptions(this.snapshot()) !== null);

  ngOnDestroy(): void {
    this.liveTrends.release();
  }

  protected async submit(): Promise<void> {
    const uid = this.session.uid;
    const options = songOptions(this.snapshot());
    if (!uid || !options) return;
    this.sending.set(true);
    this.error.set(null);
    try {
      this.started.set(await this.repository.makeSong(uid, options));
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.sending.set(false);
    }
  }

  private snapshot(): SongForm {
    return {
      lyricsFrom: this.lyricsFrom(),
      idea: this.idea(),
      notes: this.notes(),
      trendId: this.trendId(),
      style: this.style(),
      vocal: this.vocal(),
      seconds: this.seconds(),
      takes: this.takes(),
    };
  }
}
