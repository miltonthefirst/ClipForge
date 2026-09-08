import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { SessionService } from './core/session';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly session = inject(SessionService);

  /** Distinguishes "still checking" from "signed out" — see SessionService. */
  protected readonly checking = computed(() => this.session.user() === undefined);
  protected readonly signedIn = computed(() => !!this.session.user());

  protected signIn(): void {
    void this.session.signIn();
  }

  protected signOut(): void {
    void this.session.signOut();
  }
}
