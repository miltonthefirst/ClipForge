import {
  ChangeDetectionStrategy,
  Component,
  computed,
  HostListener,
  inject,
  signal,
} from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { SignInPage } from './features/auth/sign-in-page';
import { FirebaseService } from './core/firebase';
import { SessionService } from './core/session';
import { ThemeService } from './core/theme';

interface NavItem {
  readonly path: string;
  readonly label: string;
  readonly adminOnly?: boolean;
}

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, SignInPage],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly session = inject(SessionService);
  private readonly theme = inject(ThemeService);
  private readonly firebase = inject(FirebaseService);

  /**
   * A redirect sign-in that failed. Shown rather than swallowed: the whole
   * reason redirect exists is that a silent failure made the button look dead,
   * and a silent failure of the fallback would be the same bug again.
   */
  protected readonly signInError = this.firebase.redirectError;

  protected readonly checking = this.session.checking;
  protected readonly signedIn = this.session.signedIn;
  protected readonly approved = this.session.approved;
  protected readonly awaitingApproval = this.session.awaitingApproval;
  protected readonly isAdmin = this.session.isAdmin;
  protected readonly profile = this.session.profile;

  /** Sidebar on small screens: a drawer rather than a permanent column. */
  protected readonly navOpen = signal(false);
  protected readonly menuOpen = signal(false);

  protected readonly themeChoice = this.theme.choice;

  protected readonly navigation = computed<NavItem[]>(() => {
    const items: NavItem[] = [
      { path: '/review', label: 'Review' },
      { path: '/publish', label: 'Publish' },
      { path: '/jobs', label: 'Jobs' },
      { path: '/settings', label: 'Settings' },
      { path: '/settings/youtube', label: 'YouTube' },
    ];
    if (this.isAdmin()) {
      items.push({ path: '/admin/users', label: 'People', adminOnly: true });
    }
    return items;
  });

  /** What to call the person, in order of how much they chose it. */
  protected readonly displayName = computed(
    () => this.profile()?.displayName || this.profile()?.email || 'Account',
  );

  protected readonly initials = computed(() => {
    const source = this.profile()?.displayName || this.profile()?.email || '?';
    const parts = source.split(/[\s@._-]+/).filter(Boolean);
    return (parts[0]?.[0] ?? '?').toUpperCase() + (parts[1]?.[0]?.toUpperCase() ?? '');
  });

  /**
   * The label names the *current* state, not the next one. A control that
   * announces what it will become gives a screen reader user no way to find out
   * what is true now — and the icon is doing the same job, so the two would
   * disagree.
   */
  protected readonly themeLabel = computed(() => {
    switch (this.themeChoice()) {
      case 'light':
        return 'Theme: light. Activate to use dark.';
      case 'dark':
        return 'Theme: dark. Activate to follow your system.';
      default:
        return 'Theme: following your system. Activate to use light.';
    }
  });

  /** Any click outside closes the account menu; Escape does too. */
  @HostListener('document:click')
  protected closeMenu(): void {
    this.menuOpen.set(false);
  }

  @HostListener('document:keydown.escape')
  protected onEscape(): void {
    this.menuOpen.set(false);
    this.navOpen.set(false);
  }

  protected toggleMenu(event: Event): void {
    event.stopPropagation();
    this.menuOpen.update((open) => !open);
  }

  protected toggleNav(): void {
    this.navOpen.update((open) => !open);
  }

  protected cycleTheme(event: Event): void {
    event.stopPropagation();
    this.theme.cycle();
  }

  protected signOut(): void {
    this.menuOpen.set(false);
    void this.session.signOut();
  }
}
