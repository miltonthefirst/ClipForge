import { TestBed } from '@angular/core/testing';
import { SwUpdate, type VersionEvent } from '@angular/service-worker';
import { Subject } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BUILD } from '../build-info';
import { AppUpdateService } from './app-update';

/**
 * Getting the latest build onto the phone without pulling the page out from
 * under someone: at once while nobody is looking, otherwise on the way out.
 */

function fakeSw() {
  const versionUpdates = new Subject<VersionEvent>();
  const unrecoverable = new Subject<{ type: 'UNRECOVERABLE_STATE'; reason: string }>();
  return {
    isEnabled: true,
    versionUpdates,
    unrecoverable,
    checkForUpdate: vi.fn(async () => true),
    activateUpdate: vi.fn(async () => true),
  };
}

const READY = {
  type: 'VERSION_READY',
  currentVersion: { hash: 'old' },
  latestVersion: { hash: 'new' },
} as VersionEvent;

describe('AppUpdateService', () => {
  let sw: ReturnType<typeof fakeSw>;
  let reload: ReturnType<typeof vi.fn<(activate?: boolean) => Promise<void>>>;

  function create(): AppUpdateService {
    TestBed.configureTestingModule({ providers: [{ provide: SwUpdate, useValue: sw }] });
    const service = TestBed.inject(AppUpdateService);
    vi.spyOn(service, 'reload').mockImplementation(reload);
    return service;
  }

  beforeEach(() => {
    sw = fakeSw();
    reload = vi.fn<(activate?: boolean) => Promise<void>>(async () => undefined);
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('asks for an update as soon as the app opens', () => {
    create();
    expect(sw.checkForUpdate).toHaveBeenCalled();
  });

  it('switches at once to a build that arrives right after opening', () => {
    create();
    sw.versionUpdates.next(READY);
    expect(reload).toHaveBeenCalled();
  });

  it('waits while someone is using the app, then switches when they leave it', () => {
    vi.useFakeTimers({ now: Date.now() });
    const service = create();
    vi.advanceTimersByTime(60_000);

    sw.versionUpdates.next(READY);
    expect(reload).not.toHaveBeenCalled();
    expect(service.ready()).toBe(true);

    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(reload).toHaveBeenCalled();
  });

  it('says once that a new version arrived, and not on a first visit', () => {
    expect(create().updatedTo()).toBeNull();
    TestBed.resetTestingModule();

    localStorage.setItem('clipforge.lastVersion', '0.0.1');
    expect(create().updatedTo()).toBe(BUILD.version);
    TestBed.resetTestingModule();

    expect(create().updatedTo()).toBeNull();
  });
});
