import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { FirebaseService } from './core/firebase';
import { SessionService } from './core/session';
import { ThemeService } from './core/theme';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
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
   * reason redirect exists here is that a silent failure made the button look
   * dead, and a silent failure of the fallback would be the same bug again.
   */
  protected readonly signInError = this.firebase.redirectError;

  /** Distinguishes "still checking" from "signed out" — see SessionService. */
  protected readonly checking = computed(() => this.session.user() === undefined);
  protected readonly signedIn = computed(() => !!this.session.user());

  protected readonly themeChoice = this.theme.choice;

  /**
   * The label names the *current* state, not the next one.
   *
   * A control that announces what it will become gives a screen reader user no
   * way to find out what is true now — and the icon is doing the same job, so
   * the two would disagree.
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

  protected cycleTheme(): void {
    this.theme.cycle();
  }

  protected signIn(): void {
    void this.session.signIn();
  }

  protected signOut(): void {
    void this.session.signOut();
  }
}
