import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import type { AppNotification } from '@clipforge/contracts';

import { KINDS, NotificationsService, type KindInfo } from '../../core/notifications';
import { SessionService } from '../../core/session';

/**
 * Turning notifications on for this device, choosing which, and what was sent
 * (core/notifications.ts, apps/worker/clipforge/notify).
 */
@Component({
  selector: 'app-notifications-page',
  imports: [DatePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './notifications-page.html',
})
export class NotificationsPage {
  protected readonly notifications = inject(NotificationsService);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);

  protected readonly kinds = computed<readonly KindInfo[]>(() =>
    KINDS.filter((info) => !info.adminOnly || this.session.isAdmin()),
  );
  protected readonly testState = signal<'idle' | 'asking' | 'asked' | string>('idle');

  protected async toggle(info: KindInfo): Promise<void> {
    await this.notifications.setKind(info.kind, !this.notifications.isOn(info));
  }

  protected async test(): Promise<void> {
    this.testState.set('asking');
    try {
      await this.notifications.sendTest();
      this.testState.set('asked');
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.testState.set(
        /permission/i.test(message)
          ? 'A test was asked for less than 30 seconds ago. Wait a moment.'
          : message,
      );
    }
  }

  protected async open(item: AppNotification): Promise<void> {
    await this.notifications.markRead(item);
    if (item.link) await this.router.navigateByUrl(item.link);
  }
}
