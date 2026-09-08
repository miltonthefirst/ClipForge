import { provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';
import type { User } from 'firebase/auth';

import { App } from './app';
import { SessionService } from './core/session';

/**
 * The shell has one job worth testing: distinguishing "still checking" from
 * "signed out". Collapsing those would flash the sign-in screen at every
 * signed-in user on every load, which is the kind of thing that reads as broken.
 */
class FakeSession {
  readonly user = signal<User | null | undefined>(undefined);
  get uid(): string | null {
    return this.user()?.uid ?? null;
  }
  async signIn(): Promise<void> {
    return;
  }
  async signOut(): Promise<void> {
    return;
  }
}

describe('App shell', () => {
  let session: FakeSession;

  beforeEach(async () => {
    session = new FakeSession();
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    }).compileComponents();
  });

  function render(): HTMLElement {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows a checking state before the session is known', () => {
    expect(render().textContent).toContain('Checking your session');
  });

  it('offers sign-in once the session is known to be absent', () => {
    session.user.set(null);
    const text = render().textContent ?? '';
    expect(text).toContain('Sign in to ClipForge');
    expect(text).not.toContain('Checking your session');
  });

  it('explains the local-first constraint on the sign-in screen', () => {
    // The one place to set the expectation before someone wonders why a clip
    // will not play on their phone.
    session.user.set(null);
    expect(render().textContent).toContain('stay on the machine');
  });

  it('shows navigation once signed in', () => {
    session.user.set({ uid: 'user-1' } as User);
    const text = render().textContent ?? '';
    expect(text).toContain('Review');
    expect(text).toContain('Jobs');
    expect(text).not.toContain('Sign in to ClipForge');
  });
});
