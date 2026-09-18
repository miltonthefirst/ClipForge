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
    path: 'review/:id',
    title: 'Clip · ClipForge',
    loadComponent: () => import('./features/review/clip-page').then((m) => m.ClipPage),
  },
  {
    path: 'publish',
    title: 'Publish · ClipForge',
    loadComponent: () => import('./features/publish/publish-page').then((m) => m.PublishPage),
  },
  {
    // The publish queue's detail view, in the same shape as `review/:id`: the
    // list decides what to deal with next, this decides what actually goes out
    // and shows what happened when it did.
    path: 'publish/:id',
    title: 'Publish · ClipForge',
    loadComponent: () =>
      import('./features/publish/publication-page').then((m) => m.PublicationPage),
  },
  {
    path: 'jobs',
    title: 'Jobs · ClipForge',
    loadComponent: () => import('./features/jobs/jobs-page').then((m) => m.JobsPage),
  },
  {
    // `withComponentInputBinding` feeds `:id` straight into the component's
    // `id` input, so the page needs no ActivatedRoute and works identically
    // when opened cold from a link.
    path: 'jobs/:id',
    title: 'Job · ClipForge',
    loadComponent: () => import('./features/jobs/job-page').then((m) => m.JobPage),
  },
  {
    path: 'insights',
    title: 'Insights · ClipForge',
    loadComponent: () => import('./features/insights/insights-page').then((m) => m.InsightsPage),
  },
  {
    // `?run=` names a research run, the way `?tab=` names a list on Jobs.
    path: 'trends',
    title: 'Trends · ClipForge',
    loadComponent: () => import('./features/trends/trends-page').then((m) => m.TrendsPage),
  },
  {
    path: 'compile',
    title: 'Compile · ClipForge',
    loadComponent: () => import('./features/compile/compile-page').then((m) => m.CompilePage),
  },
  {
    path: 'settings',
    title: 'Settings · ClipForge',
    loadComponent: () => import('./features/settings/settings-page').then((m) => m.SettingsPage),
  },
  {
    path: 'settings/worker',
    title: 'Worker · ClipForge',
    loadComponent: () => import('./features/worker/worker-page').then((m) => m.WorkerPage),
  },
  {
    path: 'sources',
    title: 'Sources · ClipForge',
    loadComponent: () => import('./features/sources/sources-page').then((m) => m.SourcesPage),
  },
  {
    path: 'settings/storage',
    title: 'Storage · ClipForge',
    loadComponent: () => import('./features/settings/storage-page').then((m) => m.StoragePage),
  },
  {
    path: 'settings/youtube',
    title: 'YouTube · ClipForge',
    loadComponent: () => import('./features/settings/youtube-page').then((m) => m.YouTubePage),
  },
  {
    // Admin-only in the security rules as well as here. The route guard is a
    // courtesy — the listener is what actually refuses a member.
    path: 'admin/users',
    title: 'People · ClipForge',
    loadComponent: () => import('./features/admin/users-page').then((m) => m.UsersPage),
  },
  { path: '**', redirectTo: 'review' },
];
