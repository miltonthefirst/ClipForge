import { provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { computed, signal } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';
import type { UserProfile } from '@clipforge/contracts';
import type { User } from 'firebase/auth';

import { App } from './app';
import { SessionService } from './core/session';

/**
 * The shell decides between four states, and confusing any two of them looks
 * like a broken app rather than a considered one:
 *
 *   checking          — the session is not yet known
 *   signed out        — offer sign-in
 *   awaiting approval — signed in, and deliberately shown nothing else
 *   approved          — the app proper
 *
 * The third is the one worth the most care. An unapproved account can read
 * nothing at all, so without an explicit state it would land on an empty page
 * and a console full of permission errors.
 */
class FakeSession {
  readonly user = signal<User | null | undefined>(undefined);
  readonly profile = signal<UserProfile | null | undefined>(undefined);

  readonly signedIn = computed(() => !!this.user());
  readonly checking = computed(() => this.user() === undefined);
  readonly approved = computed(() => this.profile()?.status === 'APPROVED');
  readonly isAdmin = computed(() => this.approved() && this.profile()?.role === 'ADMIN');
  readonly awaitingApproval = computed(() => {
    const profile = this.profile();
    return this.signedIn() && !!profile && profile.status !== 'APPROVED';
  });

  get uid(): string | null {
    return this.user()?.uid ?? null;
  }
  // The shell only ever calls these; none of these tests exercise what they do,
  // which is the auth layer's business and is covered against a real emulator
  // in the E2E suite.
  signInWithGoogle(): Promise<void> {
    return Promise.resolve();
  }
  signInWithPassword(): Promise<void> {
    return Promise.resolve();
  }
  register(): Promise<void> {
    return Promise.resolve();
  }
  resetPassword(): Promise<void> {
    return Promise.resolve();
  }
  signOut(): Promise<void> {
    return Promise.resolve();
  }
}

function profileFor(overrides: Partial<UserProfile> = {}): UserProfile {
  return {
    uid: 'user-1',
    email: 'user@example.com',
    displayName: 'Test User',
    photoUrl: null,
    role: 'MEMBER',
    status: 'APPROVED',
    createdAt: '2026-09-09T00:00:00.000Z',
    decidedAt: '2026-09-09T00:00:00.000Z',
    decidedBy: 'admin-1',
    ...overrides,
  } as UserProfile;
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

  function signedInAs(profile: UserProfile | null): void {
    session.user.set({ uid: 'user-1' } as User);
    session.profile.set(profile);
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

  it('leads with email and password, and offers Google second', () => {
    // Google holds a first sign-in on an unfamiliar device for verification,
    // which is useless on the machine running the worker.
    session.user.set(null);
    const text = render().textContent ?? '';
    expect(text).toContain('Email');
    expect(text).toContain('Password');
    expect(text).toContain('Continue with Google');
  });

  it('explains the wait rather than showing an empty app to a pending account', () => {
    signedInAs(profileFor({ status: 'PENDING', decidedAt: null, decidedBy: null }));
    const text = render().textContent ?? '';
    expect(text).toContain('Waiting for approval');
    expect(text).not.toContain('Sign in to ClipForge');
  });

  it('says so plainly when an account was rejected or disabled', () => {
    signedInAs(profileFor({ status: 'DISABLED' }));
    expect(render().textContent).toContain('Access not available');
  });

  it('shows navigation once approved', () => {
    signedInAs(profileFor());
    const text = render().textContent ?? '';
    expect(text).toContain('Review');
    expect(text).toContain('Jobs');
    expect(text).toContain('Settings');
    expect(text).not.toContain('Sign in to ClipForge');
  });

  it('hides the People page from a member and shows it to an admin', () => {
    signedInAs(profileFor({ role: 'MEMBER' }));
    expect(render().textContent).not.toContain('People');

    signedInAs(profileFor({ role: 'ADMIN' }));
    expect(render().textContent).toContain('People');
  });

  it('shows no navigation at all to an account that is not approved', () => {
    // Nothing to navigate to: every collection is closed to it.
    signedInAs(profileFor({ status: 'PENDING' }));
    const text = render().textContent ?? '';
    expect(text).not.toContain('Review');
    expect(text).not.toContain('Jobs');
  });
});
