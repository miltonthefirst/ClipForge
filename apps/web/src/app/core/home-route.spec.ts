import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import type { AgentReport } from '@clipforge/contracts';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkersRepository } from './data/workers';
import { HOME_SERVICE, HomeRouteService, bypassServiceWorker } from './home-route';
import { SessionService } from './session';

/**
 * The home wifi route, seen from the phone: when it believes it is home, and
 * what it asks the PC for. docs/HOME-NETWORK-PLAN.md.
 */

const URL = 'https://clipforge-home.example.dev:8768';

function agent(overrides: Partial<AgentReport> = {}): AgentReport {
  return {
    agentId: 'tower',
    desired: 'STOPPED',
    state: 'STOPPED',
    version: '0.0.3',
    lastSeenAt: '2026-11-15T09:00:00Z',
    home: { state: 'SERVING', url: URL },
    ...overrides,
  } as AgentReport;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('HomeRouteService', () => {
  const agents = signal<AgentReport[] | null>(null);
  const approved = signal(true);
  const user = signal<{ getIdToken: () => Promise<string> } | null>({
    getIdToken: async () => 'id-token',
  });
  let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;

  function service(): HomeRouteService {
    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: { approved, user } },
        {
          provide: WorkersRepository,
          useValue: {
            agents: () => ({
              data: agents,
              loading: signal(false),
              error: signal(null),
              release: () => undefined,
            }),
          },
        },
      ],
    });
    const created = TestBed.inject(HomeRouteService);
    TestBed.tick();
    return created;
  }

  beforeEach(() => {
    agents.set([agent()]);
    approved.set(true);
    fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  it('is home when the advertised address answers as this PC', async () => {
    fetchMock.mockImplementation(async () => json({ service: HOME_SERVICE, station: 'tower' }));
    const route = service();

    await route.check(fetchMock);

    expect(fetchMock).toHaveBeenCalledWith(`${URL}/hello?ngsw-bypass=true`, expect.anything());
    expect(route.home()).toBe(true);
    expect(route.label()).toBe('Home wifi');
  });

  it('is not home when something else answers at that address', async () => {
    fetchMock.mockImplementation(async () => json({ service: 'sarungano-home', station: 'tower' }));
    const route = service();

    await route.check(fetchMock);

    expect(route.home()).toBe(false);
    expect(route.label()).toBe('Via cloud');
  });

  it('is not home when another PC answers', async () => {
    fetchMock.mockImplementation(async () => json({ service: HOME_SERVICE, station: 'laptop' }));
    const route = service();

    await route.check(fetchMock);

    expect(route.home()).toBe(false);
  });

  it('is not home when nothing answers, which is what away from home looks like', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    const route = service();

    await route.check(fetchMock);

    expect(route.home()).toBe(false);
  });

  it('offers nothing, and asks nothing, until the PC advertises a route', async () => {
    agents.set([agent({ home: { state: 'OFF', url: null } })]);
    const route = service();

    await route.check(fetchMock);

    expect(route.label()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(await route.clipUrl('clip-1', fetchMock)).toBeNull();
  });

  it('asks the PC for a clip link, signed in, while home', async () => {
    fetchMock.mockImplementation(async (input) =>
      String(input).includes('/hello')
        ? json({ service: HOME_SERVICE, station: 'tower' })
        : json({
            url: `${URL}/media/abc`,
            expiresAt: new Date(Date.now() + 12 * 3600_000).toISOString(),
          }),
    );
    const route = service();

    const link = await route.clipUrl('clip-1', fetchMock);

    expect(link).toBe(`${URL}/media/abc?ngsw-bypass=true`);
    const [, sent] = fetchMock.mock.calls.find(([url]) => String(url).includes('/media-links'))!;
    const init = sent as RequestInit & { headers: Record<string, string>; body: string };
    expect(init.method).toBe('POST');
    expect(init.headers['Authorization']).toBe('Bearer id-token');
    expect(JSON.parse(init.body)).toEqual({ kind: 'clip', id: 'clip-1' });
  });

  it('asks for a track by its own kind, cached apart from a clip with the same id', async () => {
    fetchMock.mockImplementation(async (input, init) =>
      String(input).includes('/hello')
        ? json({ service: HOME_SERVICE, station: 'tower' })
        : json({
            url: `${URL}/media/${JSON.parse(String(init?.body)).kind}`,
            expiresAt: new Date(Date.now() + 12 * 3600_000).toISOString(),
          }),
    );
    const route = service();

    expect(await route.mediaUrl('track', 'same-id', fetchMock)).toBe(
      `${URL}/media/track?ngsw-bypass=true`,
    );
    expect(await route.mediaUrl('clip', 'same-id', fetchMock)).toBe(
      `${URL}/media/clip?ngsw-bypass=true`,
    );
  });

  it('reuses a link until it is close to expiring', async () => {
    fetchMock.mockImplementation(async (input) =>
      String(input).includes('/hello')
        ? json({ service: HOME_SERVICE, station: 'tower' })
        : json({
            url: `${URL}/media/abc`,
            expiresAt: new Date(Date.now() + 12 * 3600_000).toISOString(),
          }),
    );
    const route = service();

    await route.clipUrl('clip-1', fetchMock);
    await route.clipUrl('clip-1', fetchMock);

    const links = fetchMock.mock.calls.filter(([url]) => String(url).includes('/media-links'));
    expect(links).toHaveLength(1);
  });

  it('falls back quietly when the PC says no to one clip, and stays home', async () => {
    fetchMock.mockImplementation(async (input) =>
      String(input).includes('/hello')
        ? json({ service: HOME_SERVICE, station: 'tower' })
        : json({ detail: "That clip's file is not on this PC any more." }, 404),
    );
    const route = service();

    expect(await route.clipUrl('gone', fetchMock)).toBeNull();
    expect(route.home()).toBe(true);
  });

  it('gives up on the route when the PC stops answering mid-way', async () => {
    fetchMock.mockImplementation(async (input) => {
      if (String(input).includes('/hello'))
        return json({ service: HOME_SERVICE, station: 'tower' });
      throw new TypeError('Failed to fetch');
    });
    const route = service();

    expect(await route.clipUrl('clip-1', fetchMock)).toBeNull();
    expect(route.home()).toBe(false);
  });

  it('is never used by an account that is not approved', async () => {
    approved.set(false);
    const route = service();

    expect(route.label()).toBeNull();
    expect(await route.clipUrl('clip-1', fetchMock)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('HomeRouteService.diagnose', () => {
  const agents = signal<AgentReport[] | null>(null);

  function service(): HomeRouteService {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: SessionService,
          useValue: {
            approved: signal(true),
            user: signal({ getIdToken: async () => 'id-token' }),
          },
        },
        {
          provide: WorkersRepository,
          useValue: {
            agents: () => ({
              data: agents,
              loading: signal(false),
              error: signal(null),
              release: () => undefined,
            }),
          },
        },
      ],
    });
    const created = TestBed.inject(HomeRouteService);
    TestBed.tick();
    return created;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  function fetchWith(linkStatus: number) {
    return vi.fn<typeof fetch>(async (input) =>
      String(input).includes('/hello')
        ? json({ service: HOME_SERVICE, station: 'tower', version: '0.0.7' })
        : json({ detail: 'x' }, linkStatus),
    );
  }

  it('passes every step on the home wifi with an approved account', async () => {
    agents.set([agent()]);
    const fetchMock = fetchWith(404);
    vi.stubGlobal('fetch', fetchMock);

    const steps = await service().diagnose(fetchMock);

    expect(steps.map((s) => [s.id, s.state])).toEqual([
      ['advertised', 'pass'],
      ['reach', 'pass'],
      ['secure', 'pass'],
      ['signin', 'pass'],
    ]);
  });

  it('says so when the account is not approved', async () => {
    agents.set([agent()]);
    const fetchMock = fetchWith(403);
    vi.stubGlobal('fetch', fetchMock);

    const steps = await service().diagnose(fetchMock);

    expect(steps.at(-1)).toMatchObject({ id: 'signin', state: 'fail' });
    expect(steps.at(-1)?.detail).toContain('not approved');
  });

  it('stops at the first step when the PC is not offering the route', async () => {
    agents.set([agent({ home: { state: 'OFF', url: null } })]);
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal('fetch', fetchMock);

    const steps = await service().diagnose(fetchMock);

    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({ id: 'advertised', state: 'fail' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('blames a blocked browser permission when that is what it is', async () => {
    agents.set([agent()]);
    const fetchMock = vi.fn<typeof fetch>(async () => {
      throw new TypeError('Failed to fetch');
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('navigator', {
      ...navigator,
      permissions: { query: async () => ({ state: 'denied' }) },
    });

    const steps = await service().diagnose(fetchMock);

    expect(steps.at(-1)).toMatchObject({ id: 'reach', state: 'fail' });
    expect(steps.at(-1)?.detail).toContain('blocking');
  });
});

describe('bypassServiceWorker', () => {
  it("keeps every request to the PC away from the app's service worker", () => {
    expect(bypassServiceWorker('https://h:8768/hello')).toBe(
      'https://h:8768/hello?ngsw-bypass=true',
    );
    expect(bypassServiceWorker('https://h:8768/media/t?x=1')).toBe(
      'https://h:8768/media/t?x=1&ngsw-bypass=true',
    );
  });
});
