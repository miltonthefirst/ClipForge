import type { Routes } from '@angular/router';

/**
 * Lazy-loaded routes.
 *
 * Each feature is its own chunk so the initial bundle stays small — this is a
 * PWA that has to be usable one-handed on a phone over a mobile connection, and
 * the review queue is the only screen most sessions ever reach.
 */
export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'review' },
  {
    path: 'review',
    title: 'Review · ClipForge',
    loadComponent: () => import('./features/review/review-page').then((m) => m.ReviewPage),
  },
  {
    path: 'publish',
    title: 'Publish · ClipForge',
    loadComponent: () => import('./features/publish/publish-page').then((m) => m.PublishPage),
  },
  {
    path: 'jobs',
    title: 'Jobs · ClipForge',
    loadComponent: () => import('./features/jobs/jobs-page').then((m) => m.JobsPage),
  },
  { path: '**', redirectTo: 'review' },
];
