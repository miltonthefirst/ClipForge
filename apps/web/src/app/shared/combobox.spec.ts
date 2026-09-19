import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';

import { Combobox, filterChoices, matchRank, type Choice } from './combobox';

/**
 * The searchable pick: what the filter ranks first, and what the box does
 * with a keyboard, a click, and a blur.
 *
 * The blur rules matter most, because they are where a value is silently
 * set or silently dropped: "united k" and away must be the Kingdom, an
 * erased box must mean none, and half a word that matches nothing must leave
 * the previous value alone.
 */

const CHOICES: Choice[] = [
  { code: 'AE', label: 'United Arab Emirates' },
  { code: 'GB', label: 'United Kingdom' },
  { code: 'US', label: 'United States' },
  { code: 'TR', label: 'Türkiye' },
  { code: 'football', label: 'Football (soccer)', group: 'Sports', aliases: ['soccer', 'futbol'] },
  { code: 'k-pop', label: 'K-pop', group: 'Music', aliases: ['bts'] },
  { code: 'esports', label: 'Esports', group: 'Sports' },
];

describe('filterChoices', () => {
  it('returns everything, in order, for an empty query', () => {
    expect(filterChoices(CHOICES, '  ').map((c) => c.code)).toEqual(CHOICES.map((c) => c.code));
  });

  it('puts a name that starts with the query before one that contains it', () => {
    expect(filterChoices(CHOICES, 'united k').map((c) => c.code)).toEqual(['GB']);
    expect(filterChoices(CHOICES, 'states').map((c) => c.code)).toEqual(['US']);
    expect(filterChoices(CHOICES, 'sport').map((c) => c.code)).toEqual(['esports', 'football']);
  });

  it('finds by alias, code, accent-free spelling and punctuation-free spelling', () => {
    expect(filterChoices(CHOICES, 'soccer').map((c) => c.code)).toEqual(['football']);
    expect(filterChoices(CHOICES, 'gb').map((c) => c.code)).toEqual(['GB']);
    expect(filterChoices(CHOICES, 'turk').map((c) => c.code)).toEqual(['TR']);
    expect(filterChoices(CHOICES, 'kpop').map((c) => c.code)).toEqual(['k-pop']);
    expect(matchRank(CHOICES[5]!, 'bts')).toBe(3);
  });

  it('is empty for something nobody would mean', () => {
    expect(filterChoices(CHOICES, 'zzz')).toEqual([]);
  });
});

describe('Combobox', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [Combobox] }).compileComponents();
  });

  function mount(value: string | null = null) {
    const fixture = TestBed.createComponent(Combobox);
    fixture.componentRef.setInput('options', CHOICES);
    fixture.componentRef.setInput('label', 'Where');
    fixture.componentRef.setInput('placeholder', 'Anywhere');
    fixture.componentRef.setInput('value', value);
    const emitted: (string | null)[] = [];
    fixture.componentInstance.valueChange.subscribe((next) => emitted.push(next));
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    const input = host.querySelector('input') as HTMLInputElement;
    const options = () => Array.from(host.querySelectorAll('[role="option"]'));
    const type = (text: string) => {
      input.value = text;
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
    };
    const key = (name: string) => {
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true }),
      );
      fixture.detectChanges();
    };
    const blur = () => {
      host.dispatchEvent(new FocusEvent('focusout', { relatedTarget: null }));
      fixture.detectChanges();
    };
    return { fixture, host, input, options, type, key, blur, emitted };
  }

  it('shows the selected name, is labelled, and offers the whole list on focus', () => {
    const { input, options, fixture } = mount('GB');
    expect(input.value).toBe('United Kingdom');
    expect(input.getAttribute('aria-label')).toBe('Where');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    input.dispatchEvent(new Event('focus'));
    fixture.detectChanges();
    expect(input.getAttribute('aria-expanded')).toBe('true');
    expect(options()).toHaveLength(CHOICES.length);
    expect(options()[1]?.getAttribute('aria-selected')).toBe('true');
  });

  it('filters as you type and picks with a click', () => {
    const { options, type, emitted } = mount();
    type('king');
    expect(options().map((o) => o.textContent?.trim())).toEqual(['United Kingdom']);
    (options()[0] as HTMLElement).click();
    expect(emitted).toEqual(['GB']);
  });

  it('walks the list with the arrows and picks with Enter, without submitting', () => {
    const { input, key, type, emitted, host } = mount();
    let submitted = false;
    host.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.defaultPrevented) submitted = true;
    });
    type('united');
    key('ArrowDown');
    key('ArrowDown');
    expect(input.getAttribute('aria-activedescendant')).toMatch(/-2$/);
    key('Enter');
    expect(emitted).toEqual(['US']);
    expect(submitted).toBe(false);
  });

  it('on blur, one remaining match is the pick and an exact name is too', () => {
    const first = mount();
    first.type('united k');
    first.blur();
    expect(first.emitted).toEqual(['GB']);

    const second = mount();
    second.type('esports');
    second.blur();
    expect(second.emitted).toEqual(['esports']);
  });

  it('on blur, an erased box means none and a half-word that matches nothing keeps the value', () => {
    const erased = mount('GB');
    erased.type('');
    erased.blur();
    expect(erased.emitted).toEqual([null]);

    const stray = mount('GB');
    stray.type('zzz');
    stray.blur();
    expect(stray.emitted).toEqual([]);
    expect(stray.input.value).toBe('United Kingdom');
  });

  it('has a clear button only when something is set, and it clears', () => {
    const empty = mount();
    expect(empty.host.querySelector('button')).toBeNull();

    const set = mount('GB');
    const button = set.host.querySelector('button') as HTMLButtonElement;
    expect(button.getAttribute('aria-label')).toBe('Clear Where');
    button.click();
    expect(set.emitted).toEqual([null]);
  });

  it('Escape closes without changing anything', () => {
    const { input, key, type, emitted, fixture } = mount('GB');
    type('unit');
    key('Escape');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    expect(emitted).toEqual([]);
    fixture.detectChanges();
    expect(input.value).toBe('United Kingdom');
  });
});
