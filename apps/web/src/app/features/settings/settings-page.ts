import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import { CLIPFORGE_CONFIG } from '../../core/firebase';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';
import { ThemeService } from '../../core/theme';
import type { ThemeChoice } from '../../core/theme';

/**
 * Profile, appearance, and where this app is actually pointed.
 *
 * The last of those is the part worth having. "Which project am I looking at,
 * and can this browser play a clip?" is otherwise answerable only by reading
 * the page source, and it is the first question worth asking when the queue
 * looks wrong.
 */
@Component({
  selector: 'app-settings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './settings-page.html',
})
export class SettingsPage {
  private readonly session = inject(SessionService);
  private readonly store = inject(ClipForgeStore);
  private readonly theme = inject(ThemeService);
  protected readonly config = inject(CLIPFORGE_CONFIG);

  protected readonly profile = this.session.profile;
  protected readonly themeChoice = this.theme.choice;
  protected readonly themes: readonly ThemeChoice[] = ['system', 'light', 'dark'];

  protected readonly draftName = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly saved = signal(false);

  protected readonly name = computed(() => this.draftName() ?? this.profile()?.displayName ?? '');

  protected readonly canSave = computed(() => {
    const next = this.name().trim();
    return !this.busy() && next.length > 0 && next !== (this.profile()?.displayName ?? '');
  });

  protected setTheme(choice: ThemeChoice): void {
    this.theme.set(choice);
  }

  protected async saveName(): Promise<void> {
    const uid = this.session.uid;
    if (!uid || !this.canSave()) return;

    this.busy.set(true);
    this.error.set(null);
    this.saved.set(false);
    try {
      await this.store.updateOwnProfile(uid, { displayName: this.name().trim() });
      this.saved.set(true);
      this.draftName.set(null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }
}
