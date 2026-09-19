import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

/**
 * A searchable, optional pick from a long list.
 *
 * A `<select>` is the right control for fifteen options and the wrong one
 * for a hundred and twenty countries or two hundred categories: nobody scrolls
 * a native menu for "Kenya", they type it. This is the combobox pattern —
 * a text box that filters a listbox — with the keyboard behaviour a person
 * expects of one (arrows, Enter, Escape) and the ARIA wiring that lets a
 * screen reader, and Playwright, find it by its label.
 *
 * Optional is the second half of the brief. The value may be null, a cross
 * clears it, and erasing the text and moving on clears it too; the placeholder
 * is where the page says what "none" means.
 *
 * Deliberately not a form control (no `ControlValueAccessor`): the pages that
 * use it hold signals, and a value in and an event out is the whole contract.
 */
export interface Choice {
  readonly code: string;
  readonly label: string;
  /** Shown beside the label, and searched. */
  readonly group?: string;
  /** Searched, never shown. */
  readonly aliases?: readonly string[];
}

/** Lower-case, accents stripped: "Türkiye" is found by "turk". */
function fold(text: string): string {
  return text.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
}

/** Only the letters and digits: "K-pop" is found by "kpop". */
function compact(text: string): string {
  return fold(text).replace(/[^a-z0-9]/g, '');
}

/**
 * How well a choice answers a query, lower is better; -1 is not at all.
 *
 * The order is what a person expects to see first: the name that starts with
 * what they typed, then a name with a word that does, then the code, then
 * another name it goes by, then a name that merely contains it, and last the
 * whole group it belongs to.
 */
export function matchRank(choice: Choice, query: string): number {
  const q = fold(query.trim());
  if (!q) return 0;
  const label = fold(choice.label);
  if (label.startsWith(q) || compact(choice.label).startsWith(compact(q))) return 0;
  if (label.split(/[^a-z0-9]+/).some((word) => word.startsWith(q))) return 1;
  if (fold(choice.code).startsWith(q)) return 2;
  if ((choice.aliases ?? []).some((alias) => fold(alias).includes(q))) return 3;
  if (label.includes(q)) return 4;
  if (choice.group && fold(choice.group).includes(q)) return 5;
  return -1;
}

/** The choices that answer a query, best first; the whole list, in its own order, for none. */
export function filterChoices<T extends Choice>(choices: readonly T[], query: string): T[] {
  if (!query.trim()) return [...choices];
  return choices
    .map((choice, index) => ({ choice, index, rank: matchRank(choice, query) }))
    .filter((entry) => entry.rank >= 0)
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map((entry) => entry.choice);
}

let nextId = 0;

@Component({
  selector: 'app-combobox',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block', '(focusout)': 'onFocusOut($event)' },
  template: `
    <div class="relative mt-1">
      <input
        #box
        type="text"
        role="combobox"
        autocomplete="off"
        spellcheck="false"
        aria-autocomplete="list"
        aria-haspopup="listbox"
        [attr.aria-label]="label()"
        [attr.aria-expanded]="open()"
        [attr.aria-controls]="listId"
        [attr.aria-activedescendant]="open() && shown().length ? optionId(active()) : null"
        [attr.name]="name() || null"
        [placeholder]="placeholder()"
        [value]="text()"
        class="block w-full rounded-lg border border-line-strong bg-canvas py-2 pr-8 pl-3 text-ink placeholder:text-ink-subtle focus:border-forge-500 focus:outline-none"
        (focus)="openList()"
        (click)="openList()"
        (input)="onInput($event)"
        (keydown)="onKey($event)"
      />
      @if (value()) {
        <button
          type="button"
          class="absolute top-1/2 right-1 -translate-y-1/2 rounded px-2 text-lg leading-none text-ink-subtle hover:text-ink"
          [attr.aria-label]="'Clear ' + label()"
          (mousedown)="$event.preventDefault()"
          (click)="clear()"
        >
          ×
        </button>
      }
      @if (open()) {
        <ul
          [id]="listId"
          role="listbox"
          [attr.aria-label]="label()"
          class="absolute left-0 z-20 mt-1 max-h-64 w-full min-w-56 overflow-y-auto rounded-lg border border-line bg-panel py-1 text-sm shadow-lg"
        >
          @for (choice of shown(); track choice.code; let i = $index) {
            <li
              role="option"
              tabindex="-1"
              [id]="optionId(i)"
              [attr.aria-selected]="choice.code === value()"
              class="cursor-pointer px-3 py-1.5 text-ink"
              [class.bg-forge-600/10]="i === active()"
              (mousedown)="$event.preventDefault()"
              (mouseenter)="active.set(i)"
              (click)="choose(choice)"
              (keydown.enter)="choose(choice)"
            >
              {{ choice.label }}
              @if (choice.group) {
                <span class="ml-1 text-xs text-ink-subtle">· {{ choice.group }}</span>
              }
            </li>
          } @empty {
            <li class="px-3 py-1.5 text-ink-subtle">Nothing matches.</li>
          }
        </ul>
      }
    </div>
  `,
})
export class Combobox {
  readonly options = input.required<readonly Choice[]>();
  readonly value = input<string | null>(null);
  /** The accessible name: what a screen reader, and a test, calls this box. */
  readonly label = input.required<string>();
  readonly name = input('');
  /** What an empty box says, which is where the page explains what "none" means. */
  readonly placeholder = input('');
  readonly valueChange = output<string | null>();

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly box = viewChild.required<ElementRef<HTMLInputElement>>('box');
  protected readonly listId = `combobox-${++nextId}`;

  protected readonly open = signal(false);
  /** What has been typed since the list opened; null until something is. */
  protected readonly typed = signal<string | null>(null);
  protected readonly active = signal(0);

  protected readonly selected = computed(
    () => this.options().find((choice) => choice.code === this.value()) ?? null,
  );
  protected readonly text = computed(() => this.typed() ?? this.selected()?.label ?? '');
  protected readonly shown = computed(() => filterChoices(this.options(), this.typed() ?? ''));

  protected optionId(index: number): string {
    return `${this.listId}-${index}`;
  }

  protected openList(): void {
    if (this.open()) return;
    this.open.set(true);
    this.typed.set(null);
    this.active.set(
      Math.max(
        0,
        this.shown().findIndex((c) => c.code === this.value()),
      ),
    );
  }

  protected onInput(event: Event): void {
    this.typed.set((event.target as HTMLInputElement).value);
    this.open.set(true);
    this.active.set(0);
  }

  protected onKey(event: KeyboardEvent): void {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (this.open()) this.move(1);
        else this.openList();
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (this.open()) this.move(-1);
        break;
      case 'Enter':
        if (!this.open()) return;
        // Not a form submission: Enter on an open list is a pick.
        event.preventDefault();
        this.commit(this.shown()[this.active()] ?? null);
        break;
      case 'Escape':
        if (!this.open()) return;
        event.preventDefault();
        this.close();
        break;
      case 'Tab':
        this.commit(null);
        break;
      default:
        return;
    }
  }

  protected choose(choice: Choice): void {
    this.close();
    if (choice.code !== this.value()) this.valueChange.emit(choice.code);
  }

  protected clear(): void {
    this.close();
    if (this.value()) this.valueChange.emit(null);
    this.box().nativeElement.focus();
  }

  protected onFocusOut(event: FocusEvent): void {
    const next = event.relatedTarget;
    if (next instanceof Node && this.host.nativeElement.contains(next)) return;
    this.commit(null);
  }

  private move(delta: number): void {
    const count = this.shown().length;
    if (!count) return;
    this.active.set((this.active() + delta + count) % count);
    const row = document.getElementById(this.optionId(this.active()));
    // jsdom has no layout and no scrollIntoView; a browser always does.
    if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
  }

  /**
   * What the typed text means once attention leaves it.
   *
   * A highlighted row is the pick. Otherwise an exact name or code is that
   * choice, a single remaining match is that choice ("united k" is the
   * Kingdom), nothing typed at all clears, and anything else is dropped so
   * the box goes back to saying what is actually set.
   */
  private commit(highlighted: Choice | null): void {
    if (highlighted) {
      this.choose(highlighted);
      return;
    }
    const typed = this.typed();
    if (typed === null) {
      this.close();
      return;
    }
    const query = typed.trim();
    if (!query) {
      this.close();
      if (this.value()) this.valueChange.emit(null);
      return;
    }
    const wanted = fold(query);
    const exact = this.options().find(
      (choice) => fold(choice.label) === wanted || fold(choice.code) === wanted,
    );
    const only = this.shown().length === 1 ? this.shown()[0] : undefined;
    const pick = exact ?? only;
    if (pick) this.choose(pick);
    else this.close();
  }

  private close(): void {
    this.open.set(false);
    this.typed.set(null);
  }
}
