// Copied from the logging blueprint's reference/log-export.util.ts. ClipForge's
// adaptation: its own types, timestamps that arrive as ISO strings (core/documents.ts
// normalises Firestore Timestamps), and occurrences that carry only `extra`.
import type { LogGroup, LogOccurrence } from '@clipforge/contracts';

/** What a ClipForge Logs list is filtered to. */
export interface LogGroupFilter {
  readonly app: string;
  readonly unresolvedOnly: boolean;
}

/** Named in each export's header, so a pasted export says where it came from. */
const EXPORT_SOURCE = 'ClipForge Logs page';

/** A log group bundled with its stored occurrence history, as exported. */
export interface LogGroupExport extends LogGroup {
  occurrences: LogOccurrence[];
}

/** What an export was scoped to, recorded in its header so the reader knows what is (and isn't) in it. */
export type LogExportScope = LogGroupFilter;

const MARKDOWN_STACK_LINES = 40;

function iso(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function serializeOccurrence(occurrence: LogOccurrence) {
  return {
    timestamp: iso(occurrence.timestamp),
    extra: occurrence.extra ?? null,
  };
}

function serializeGroup(group: LogGroupExport) {
  return {
    fingerprint: group.fingerprint,
    app: group.app,
    level: group.level,
    category: group.category,
    status: group.status,
    watched: group.watched,
    message: group.message,
    count: group.count,
    firstSeenAt: iso(group.firstSeenAt),
    lastSeenAt: iso(group.lastSeenAt),
    resolvedAt: iso(group.resolvedAt),
    stack: group.stack ?? null,
    lastOccurrence: group.lastOccurrence ?? {},
    occurrences: group.occurrences.map(serializeOccurrence),
  };
}

/**
 * Machine-readable export: every field the admin portal has on each group,
 * dates as ISO-8601 UTC strings. Meant to be handed to a developer (or an
 * AI assistant) as-is for triage.
 */
export function buildLogExportJson(
  groups: LogGroupExport[],
  scope: LogExportScope | null,
  exportedAt: Date,
): string {
  return JSON.stringify(
    {
      exportedAt: exportedAt.toISOString(),
      source: `${EXPORT_SOURCE} - log groups`,
      scope,
      groupCount: groups.length,
      groups: groups.map(serializeGroup),
    },
    null,
    2,
  );
}

function inlineCode(text: string): string {
  return '`' + text.replace(/`/g, "'") + '`';
}

function fencedBlock(text: string): string {
  // Longer fence than any backtick run in the content, so a stack trace that
  // happens to contain ``` can't close the block early.
  const longestRun = Math.max(2, ...(text.match(/`+/g) ?? []).map((run) => run.length));
  const fence = '`'.repeat(longestRun + 1);
  return `${fence}text\n${text}\n${fence}`;
}

function truncateStack(stack: string): string {
  const lines = stack.split('\n');
  if (lines.length <= MARKDOWN_STACK_LINES) return stack;
  // A Python traceback runs outermost call first: the frame that raised and
  // the exception line are at its END, so that is the part kept (since 1.2.0).
  if (stack.trimStart().startsWith('Traceback') || stack.includes('\n  File "')) {
    return [
      `... (${lines.length - MARKDOWN_STACK_LINES} earlier lines)`,
      ...lines.slice(-MARKDOWN_STACK_LINES),
    ].join('\n');
  }
  return [
    ...lines.slice(0, MARKDOWN_STACK_LINES),
    `... (${lines.length - MARKDOWN_STACK_LINES} more lines)`,
  ].join('\n');
}

function occurrenceLine(occurrence: LogOccurrence): string {
  const parts = [iso(occurrence.timestamp) ?? 'unknown time'];
  if (occurrence.extra && Object.keys(occurrence.extra).length > 0)
    parts.push(inlineCode(JSON.stringify(occurrence.extra)));
  return `- ${parts.join(' · ')}`;
}

function groupMarkdown(group: LogGroupExport, maxOccurrences: number): string {
  const lines: string[] = [];
  lines.push(`## [${group.level}] ${group.app}: ${group.message.split('\n')[0]}`);
  lines.push('');
  lines.push(`- Fingerprint: ${inlineCode(group.fingerprint)}`);
  if (group.category) lines.push(`- Category: ${group.category}`);
  lines.push(`- Status: ${group.status}${group.watched ? ' (watched)' : ''}`);
  lines.push(`- Occurrences: ${group.count}`);
  lines.push(`- First seen: ${iso(group.firstSeenAt) ?? 'unknown'}`);
  lines.push(`- Last seen: ${iso(group.lastSeenAt) ?? 'unknown'}`);

  if (group.message.includes('\n')) {
    lines.push('', '### Full message', '', fencedBlock(group.message));
  }

  lines.push('', '### Stack trace', '');
  lines.push(group.stack ? fencedBlock(truncateStack(group.stack)) : '_No stack trace captured._');

  const shown = group.occurrences.slice(0, maxOccurrences);
  lines.push(
    '',
    `### Recent occurrences (${shown.length} of ${group.occurrences.length} stored)`,
    '',
  );
  lines.push(
    shown.length > 0 ? shown.map(occurrenceLine).join('\n') : '_No occurrence history stored._',
  );

  return lines.join('\n');
}

/**
 * Human-readable export, sized to paste into a chat: one section per group
 * with its message, stats, stack trace, and the most recent occurrences.
 */
export function buildLogExportMarkdown(
  groups: LogGroupExport[],
  scope: LogExportScope | null,
  exportedAt: Date,
  maxOccurrences = 10,
): string {
  const header = [`# Log export (${groups.length} group${groups.length === 1 ? '' : 's'})`, ''];
  header.push(`Exported ${exportedAt.toISOString()} from the ${EXPORT_SOURCE}.`);
  if (scope) {
    header.push(
      `Scope: app = ${scope.app}, ${scope.unresolvedOnly ? 'unresolved only' : 'all statuses'}.`,
    );
  }
  if (groups.length === 0) {
    header.push('', '_No log groups matched._');
  }
  return (
    [header.join('\n'), ...groups.map((group) => groupMarkdown(group, maxOccurrences))].join(
      '\n\n---\n\n',
    ) + '\n'
  );
}

/** e.g. `logs-functions-unresolved-2026-10-10T08-15-00Z.json` */
export function logExportFilename(
  label: string,
  extension: 'json' | 'md',
  exportedAt: Date,
): string {
  const stamp = exportedAt
    .toISOString()
    .replace(/\.\d{3}Z$/, 'Z')
    .replace(/:/g, '-');
  const safeLabel = label
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return `logs-${safeLabel}-${stamp}.${extension}`;
}
