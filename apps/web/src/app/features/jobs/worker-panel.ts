import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { AgentDesired, AgentReport, WorkerHeartbeat } from '@clipforge/contracts';

import { WorkersRepository } from '../../core/data/workers';
import { CLIPFORGE_CONFIG } from '../../core/firebase';
import type { Live } from '../../core/firestore/gateway';
import { LocalApiService, type WorkerSelfReport } from '../../core/local-api';
import { PresenceService, agentPresence, workerPresence } from '../../core/presence';
import { SessionService } from '../../core/session';
import { WorkerControlService } from '../../core/worker-control';

/**
 * Is anything actually going to run this queue?
 *
 * The question that makes this panel worth its space. A job sitting at QUEUED
 * has two very different explanations — "the worker is busy with the one in
 * front of it" and "there is no worker" — and the job document cannot tell them
 * apart. Before this, the second one looked like the first for as long as it
 * took someone to think of checking.
 *
 * ## Two signals, because each is blind where the other sees
 *
 * - The **worker document** in Firestore is visible from anywhere, phone
 *   included, and says what the worker last reported doing. It is written when
 *   something happens, never on a timer, so on its own it cannot say whether
 *   the worker is there *now* — a worker that was killed leaves it saying ONLINE
 *   for ever. Whether it is there is asked: see {@link workerPresence}.
 * - The **process state** from the desktop shell is instant and works with no
 *   network at all, but only describes this machine.
 * - The **agent** in `agents/{machineId}` is visible from anywhere *and* answers
 *   when no worker is running, which neither of the others can do: a worker
 *   that is not running cannot say so, and a browser tab has no process table.
 *   It is also the only one of the three that can *act* from a phone.
 *
 * ## Asked, not watched
 *
 * Opening the panel pings the machine (`core/presence.ts`): a token written to
 * the agent's document, echoed back by the agent and — relayed over loopback —
 * by the worker. The panel says "checking" until the token comes back or fifteen
 * seconds pass, then says when it last checked. It never re-asks on its own;
 * "Check now" does, and so does pressing Start or Stop. The ninety- and
 * hundred-and-eighty-second staleness windows this replaced needed the machine
 * to write every half-minute whether anyone was looking or not.
 *
 * None is dropped in favour of another: on the desktop they are shown together,
 * and a disagreement between them is itself informative — a running process with
 * a stale heartbeat means the worker cannot reach Firestore.
 *
 * ## Which button the buttons press
 *
 * One pair of controls, two routes, chosen by what is actually available:
 *
 * - In the desktop shell, Tauri owns the child process, so it starts and stops
 *   it — instantly, and with no network at all.
 * - Everywhere else, and that includes the phone this was built for, the wish
 *   is written to the agent document and the agent does it.
 *
 * The desktop route *also* writes the wish, and that is not redundancy. The
 * agent reconciles what is running against what is wanted, so a worker started
 * behind its back under a standing `STOPPED` would be a worker it reports as
 * foreign rather than one it adopts. Writing both keeps the two in agreement
 * about what the person actually asked for.
 */
@Component({
  selector: 'app-worker-panel',
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './worker-panel.html',
})
export class WorkerPanel implements OnDestroy {
  private readonly data = inject(WorkersRepository);
  private readonly config = inject(CLIPFORGE_CONFIG);
  private readonly local = inject(LocalApiService);
  private readonly session = inject(SessionService);
  private readonly presence = inject(PresenceService);
  protected readonly control = inject(WorkerControlService);

  /** How many jobs are waiting, so the panel can say what that means. */
  readonly queued = input(0);

  private stopWatching: (() => void) | null = null;
  private ticker: ReturnType<typeof setInterval> | null = null;
  /** Whether this panel has pinged since it opened. */
  private asked = false;

  /** What the worker on this machine says about itself, if one answers. */
  protected readonly selfReport = signal<WorkerSelfReport | null>(null);

  /**
   * A worker is running, but against the other database.
   *
   * The app spawns workers with its own mode pinned, so this cannot happen by
   * that route — but a worker started from a terminal takes whatever .env says,
   * and then the app reads one database while the worker writes the other. The
   * heartbeat cannot report it: a worker writing somewhere else looks exactly
   * like no worker at all, which is the most misleading state the panel has.
   */
  protected readonly modeMismatch = computed(() => {
    const report = this.selfReport();
    if (!report) return null;
    if (report.useEmulators === this.config.useEmulators) return null;
    return {
      worker: report.useEmulators
        ? 'the local emulators'
        : `the live project (${report.projectId})`,
      app: this.config.useEmulators ? 'the local emulators' : 'the live project',
    };
  });

  /**
   * The heartbeats as delivered, freshest first. `null` until one arrives.
   *
   * Three states, not two: `null` is "nothing has been read yet", an empty
   * array is "read, and no worker has ever reported in", and
   * {@link heartbeatError} is "the read failed". The panel says a different
   * thing for each, because saying "No worker running" over a query that never
   * answered is the exact lie this panel exists to stop telling.
   */
  protected readonly heartbeats = signal<WorkerHeartbeat[] | null>(null);
  protected readonly heartbeatError = signal<string | null>(null);

  /** Same three states for the agents, freshest first. */
  protected readonly agents = signal<AgentReport[] | null>(null);

  /**
   * Why the machines are unknown, when they are never going to be known.
   *
   * A failed agent read used to be recorded as an empty list, which put "Nothing
   * on the worker machine is listening for a start request. Install the agent
   * there…" on screen for a machine that was very likely listening. The install
   * instructions are for an answered query that came back empty; this is for one
   * that did not come back.
   */
  protected readonly agentError = signal<string | null>(null);

  /**
   * Nothing has been read yet, or the machine has been asked and has not
   * answered yet, so the panel has nothing to report.
   *
   * Distinct from offline: the headline renders this instead of the state,
   * because an unanswered question and a worker that is genuinely not running
   * look the same from {@link live} and mean opposite things.
   */
  protected readonly checking = computed(
    () =>
      (this.heartbeats() === null && this.heartbeatError() === null) ||
      this.workerPresence() === 'checking',
  );

  /** Set between the click and the agent document catching up with it. */
  protected readonly submitting = signal<AgentDesired | null>(null);
  protected readonly wishError = signal<string | null>(null);
  protected readonly logOpen = signal(false);
  protected readonly repoDraft = signal('');

  /**
   * A clock, so "checked 40s ago" keeps counting up. Local only: nothing here
   * asks Firestore anything on a timer.
   */
  private readonly now = signal(Date.now());

  /**
   * The worker on the machine this panel is talking to, else the freshest. The
   * worker and its agent share an id by construction (`Settings.worker_id`).
   */
  protected readonly heartbeat = computed(() => {
    const beats = this.heartbeats();
    if (!beats?.length) return null;
    const machine = this.machine();
    return beats.find((beat) => beat.workerId === machine?.agentId) ?? beats[0];
  });

  /** Whether the agent on that machine answered the last ping. */
  protected readonly agentPresence = computed(() =>
    agentPresence(this.presence.check(), this.machine()),
  );

  /** Whether a worker on that machine answered the last ping. */
  protected readonly workerPresence = computed(() =>
    workerPresence(this.presence.check(), this.machine(), this.heartbeat()),
  );

  /** When the question that is now answered was asked, on this device's clock. */
  protected readonly lastChecked = computed(() => {
    const check = this.presence.check();
    return check && this.agentPresence() !== 'checking' ? check.askedAt : null;
  });

  /**
   * What to show as the headline state, the ping's answer and the worker's own
   * report reconciled.
   *
   * `unknown` is a worker document with nothing to ask about it — no agent on
   * that machine to ping — so the panel repeats what it last reported and says
   * that is all it is. "offline" is an answer about a question that was asked,
   * so the template settles "not read yet" and "could not be read" before it
   * asks — see {@link checking} and {@link heartbeatError}.
   */
  protected readonly live = computed<'busy' | 'online' | 'offline' | 'unknown'>(() => {
    const beat = this.heartbeat();
    const presence = this.workerPresence();
    if (!beat || presence === 'offline' || beat.status === 'OFFLINE') return 'offline';
    if (presence === 'unknown' || presence === 'checking') return 'unknown';
    return beat.status === 'BUSY' || (beat.activeJobIds?.length ?? 0) > 0 ? 'busy' : 'online';
  });

  /**
   * The machine this panel offers to start, when there is one.
   *
   * The most recently seen agent rather than a configured name: ClipForge's
   * premise is one machine with a GPU, and picking the freshest is right for
   * the one case and harmless for the two-machine case a later phase will have
   * to give a picker.
   *
   * The first one, because the repository already delivers them freshest first
   * — and sorts a timestamp that will not parse to the back, which a comparator
   * written here did not.
   */
  protected readonly machine = computed(() => this.agents()?.[0] ?? null);

  /**
   * An agent that answered the last ping, and so could act on a request.
   *
   * As of that ping: the panel does not re-ask on a timer, and pressing Start
   * or Stop asks again first.
   */
  protected readonly reachable = computed(() => this.agentPresence() === 'online');

  /** Whether this device can start the worker at all, by either route. */
  protected readonly canControl = computed(() => this.control.available || this.reachable());

  /** What the agent is doing about the last request, if anything. */
  protected readonly agentBusy = computed(() => {
    const state = this.machine()?.state;
    return !!this.submitting() || state === 'STARTING' || state === 'STOPPING';
  });

  /** True while a worker is running by any account this panel trusts. */
  protected readonly anyRunning = computed(() => {
    if (this.control.available) return this.running();
    const state = this.machine()?.state;
    if (state === 'RUNNING' || state === 'FOREIGN' || state === 'STARTING') return true;
    const live = this.live();
    return live === 'online' || live === 'busy';
  });

  /**
   * Whether pressing Start could do anything, as opposed to merely being there.
   *
   * The agent can always be asked — it is a document write, and the agent
   * reports what came of it. The shell knows more: it has looked for the
   * checkout and for `uv`, and says so before the button is pressed rather than
   * after.
   */
  protected readonly canStart = computed(() => {
    if (this.reachable()) return true;
    return !!this.control.status()?.canStart;
  });

  /**
   * One label for two routes and three sources of "in progress".
   *
   * Worth computing rather than nesting in the template: "Starting…" while the
   * agent is stopping is the sort of wrong that makes someone press the button
   * again.
   */
  protected readonly actionLabel = computed(() => {
    const state = this.machine()?.state;
    const asked = this.submitting();
    if (asked === 'RUNNING' || state === 'STARTING') return 'Starting…';
    if (asked === 'STOPPED' || state === 'STOPPING') return 'Stopping…';
    if (this.control.busy()) return this.anyRunning() ? 'Stopping…' : 'Starting…';
    return this.anyRunning() ? 'Stop' : 'Start worker';
  });

  /**
   * Jobs are waiting and there is nothing to run them. The actionable case.
   *
   * Only once the machine has actually been asked and the answer is no. A
   * question still in flight, a query that has not delivered and one that
   * failed all look like "not running" from `live()`, and raising a warning
   * border and "N jobs waiting with nothing to run them" on the strength of any
   * of them is an alarm about the worker raised by a page that has not managed
   * to look at it.
   */
  protected readonly stalled = computed(
    () =>
      this.queued() > 0 &&
      this.heartbeats() !== null &&
      (this.workerPresence() === 'offline' || this.heartbeats()?.length === 0),
  );

  /** Asking is in progress, so asking again would only wait on the same answer. */
  protected readonly asking = computed(() => this.agentPresence() === 'checking');

  /** Why the last ping could not be written, if it could not. */
  protected readonly pingError = this.presence.error;

  protected readonly running = computed(() => {
    const state = this.control.status()?.state;
    return state === 'running' || state === 'foreign';
  });

  /** Newest line first, for the `flex-col-reverse` log pane. */
  protected readonly newestFirst = computed(() => {
    const log = this.control.status()?.log ?? [];
    return [...log].reverse();
  });

  /**
   * The two listeners held, released when the signed-in user changes or the
   * panel goes.
   *
   * Signals rather than fields because the delivering effects below *read*
   * them: a plain field is not tracked, so those effects would run once against
   * no listener and never again, and the panel would sit at "Checking the
   * worker" for ever.
   */
  private readonly heldWorkers = signal<Live<WorkerHeartbeat[]> | null>(null);
  private readonly heldAgents = signal<Live<AgentReport[]> | null>(null);

  constructor() {
    effect(() => {
      // Reading the uid is what re-subscribes at sign-in. Both collections are
      // readable only to a signed-in user, and a listener opened without one
      // fails permanently — `onSnapshot` does not retry — which the gate would
      // then keep warm and hand to the session that follows.
      const uid = this.session.uid;
      // Cleared first, and unconditionally: a failure belongs to the listener
      // that produced it, and these two are about to be replaced — including
      // with no listener at all, on the way out of the app.
      this.heartbeatError.set(null);
      this.agentError.set(null);

      // `untracked`, or this effect depends on the signals it is about to write
      // and re-runs itself for ever — releasing and re-opening both listeners
      // on every pass, which is a hang rather than a leak. Releasing is not
      // unsubscribing: the gate keeps each listener warm for fifteen minutes,
      // so leaving the Jobs page and coming back costs nothing.
      untracked(() => {
        this.heldWorkers()?.release();
        this.heldAgents()?.release();
      });
      this.heldWorkers.set(null);
      this.heldAgents.set(null);
      this.heartbeats.set(null);
      this.agents.set(null);
      // A new session asks its own question; an answer is about who asked.
      this.asked = false;
      untracked(() => this.presence.scope(uid ?? null));

      if (!uid) return;
      this.heldWorkers.set(this.data.heartbeats());
      this.heldAgents.set(this.data.agents());
    });

    // Separate from the subscription above so that a delivery does not re-open
    // a listener: these read the held signals and nothing else, and reading
    // `heldWorkers` inside the effect that assigns it would make every snapshot
    // a reason to re-subscribe.
    effect(() => {
      const held = this.heldWorkers();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.heartbeatError.set(failure.message);
        return;
      }
      const workers = held.data();
      // Still loading is not the same as delivered-and-empty, and the panel
      // says a different thing for each. Only the second means no worker has
      // ever reported in.
      if (workers === null) return;
      this.heartbeats.set(workers);
      this.heartbeatError.set(null);
    });

    effect(() => {
      const held = this.heldAgents();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.agentError.set(failure.message);
        return;
      }
      const agents = held.data();
      if (agents === null) return;
      this.agents.set(agents);
      this.agentError.set(null);

      // Opening the panel is a question: is the machine there? Asked once per
      // panel, on the first delivery that names a machine — not on a timer.
      if (!this.asked && agents.length) {
        this.asked = true;
        untracked(() => void this.ping());
      }

      // The agent has taken the request on board; the button can stop saying
      // "Starting…" on its own account and start reflecting what the machine
      // actually reports. `untracked` because this effect writes `submitting`
      // as well as reading it, and only a fresh snapshot is news — a press of
      // Start is not a reason to re-examine the agents already in hand.
      const asked = untracked(() => this.submitting());
      if (asked && agents.some((agent) => agent.desired === asked)) {
        this.submitting.set(null);
      }
    });

    this.stopWatching = this.control.watch();
    void this.refreshSelfReport();
    this.ticker = setInterval(() => {
      this.now.set(Date.now());
      void this.refreshSelfReport();
    }, 5_000);
  }

  ngOnDestroy(): void {
    this.stopWatching?.();
    this.heldWorkers()?.release();
    this.heldAgents()?.release();
    if (this.ticker) clearInterval(this.ticker);
  }

  /** Ask the machine again, now. The "Check now" button. */
  protected async checkNow(): Promise<void> {
    await this.ping();
  }

  /**
   * Ping the machine this panel is talking to. Quietly does nothing without
   * one, or without a signed-in user — the rules would refuse it anyway.
   */
  private async ping(): Promise<void> {
    const agent = this.machine();
    const uid = this.session.user()?.uid;
    if (!agent || !uid) return;
    await this.presence.ping(agent, this.heartbeat(), uid);
  }

  /** Silent on failure: no worker answering is the normal case here. */
  private async refreshSelfReport(): Promise<void> {
    try {
      this.selfReport.set(await this.local.workerSelfReport());
    } catch {
      this.selfReport.set(null);
    }
  }

  protected async start(): Promise<void> {
    await this.act('RUNNING', () => this.control.start(this.config.useEmulators));
  }

  protected async stop(): Promise<void> {
    await this.act('STOPPED', () => this.control.stop());
  }

  /**
   * Do it here if this is the machine, and record the wish either way.
   *
   * The local route runs first and is awaited, because when it works it is the
   * one the person is watching: the log pane fills in, the pid appears, and the
   * document write that follows is bookkeeping. When there is no local route the
   * wish *is* the action.
   */
  private async act(desired: AgentDesired, locally: () => Promise<void>): Promise<void> {
    this.wishError.set(null);
    if (this.control.available) await locally();

    const agent = this.machine();
    const uid = this.session.user()?.uid;
    if (!agent || !uid) return;

    this.submitting.set(desired);
    try {
      // Asked first, so the panel's answer about the machine is as fresh as the
      // request it is about to make. Not awaited for the answer: the wish is
      // worth writing either way, and the agent acts on it whenever it hears.
      await this.ping();
      await this.data.wish(agent.agentId, uid, desired);
    } catch (error) {
      this.submitting.set(null);
      // Only worth surfacing when the wish was the whole action. In the desktop
      // shell the worker has already started or stopped, and an alarming red
      // box about a document write would describe a real but secondary problem.
      if (!this.control.available) {
        this.wishError.set(error instanceof Error ? error.message : 'Could not reach the machine.');
      }
    }
  }

  protected async saveRepo(): Promise<void> {
    const path = this.repoDraft().trim();
    if (path) await this.control.setRepo(path);
  }

  /**
   * What the agent is up to, in words rather than an enum.
   *
   * FOREIGN earns the longest phrase because it is the one that would otherwise
   * be read as a bug: the panel is reporting a running worker next to a machine
   * that says it did not start one, and the reason — somebody started it at the
   * PC, or from a terminal — is the whole explanation.
   */
  protected agentState(agent: AgentReport): string {
    switch (agent.state) {
      case 'STARTING':
        return 'starting the worker';
      case 'RUNNING':
        return 'worker running';
      case 'FOREIGN':
        return 'a worker it did not start is running';
      case 'STOPPING':
        return 'stopping the worker';
      case 'FAILED':
        return 'the worker would not stay up';
      default:
        return 'ready, worker stopped';
    }
  }

  /** "4s", "3m", "2h" — short enough to sit inline without wrapping. */
  protected since(iso: string | null | undefined): string {
    if (!iso) return '—';
    return this.ago(Date.parse(iso));
  }

  protected sinceMs(ms: number | null | undefined): string {
    return ms ? this.ago(ms) : '—';
  }

  private ago(at: number): string {
    const seconds = Math.max(0, Math.round((this.now() - at) / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 3600)}h`;
  }

  protected vram(beat: WorkerHeartbeat): string | null {
    if (!beat.gpu) return null;
    const free = (beat.gpu.vramFreeMb / 1024).toFixed(1);
    const total = (beat.gpu.vramTotalMb / 1024).toFixed(1);
    return `${beat.gpu.name} · ${free}/${total} GB free`;
  }
}
