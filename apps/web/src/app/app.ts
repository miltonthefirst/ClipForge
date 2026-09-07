import { Component, computed, signal } from '@angular/core';
import { RouterOutlet } from '@angular/router';

/** A milestone from docs/PLAN.md, rendered on the Phase 0 shell. */
export interface Milestone {
  readonly id: string;
  readonly label: string;
  readonly ships: string;
  readonly done: boolean;
}

@Component({
  selector: 'app-root',
  imports: [RouterOutlet],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly title = signal('ClipForge');

  /** Mirrors the milestone table in docs/PLAN.md §4. */
  protected readonly milestones = signal<readonly Milestone[]>([
    { id: 'M0', label: 'Foundations', ships: 'Control plane + resumable worker', done: false },
    { id: 'M1', label: 'Pipeline', ships: 'URL in, rendered vertical clip out', done: false },
    { id: 'M2', label: 'Product', ships: 'v0.1.0 — the phone review loop', done: false },
    { id: 'M3', label: 'Feedback loop', ships: 'v0.2.0 — publish and measure', done: false },
    { id: 'M4', label: 'Autonomy', ships: 'v0.3.0 — trend-driven sourcing', done: false },
    { id: 'M5', label: 'Release', ships: 'Public open-source launch', done: false },
  ]);

  protected readonly completed = computed(
    () => this.milestones().filter((milestone) => milestone.done).length,
  );
}
