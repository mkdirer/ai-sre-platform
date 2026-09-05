/** Shared timestamp and duration formatting for incident views. */

export function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** Short relative age ("3h ago") for dense tables; absolute time stays in title. */
export function formatRelative(value: string, now: number = Date.now()): string {
  const date = new Date(value);
  const time = date.getTime();
  if (Number.isNaN(time)) return value;
  const seconds = Math.max(0, Math.floor((now - time) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Human duration between two ISO timestamps ("2h 14m"); null when unparseable. */
export function formatDuration(start: string, end: string | null): string | null {
  const from = new Date(start).getTime();
  if (Number.isNaN(from) || end === null) return null;
  const to = new Date(end).getTime();
  if (Number.isNaN(to) || to < from) return null;
  const minutes = Math.floor((to - from) / 60000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    const rest = minutes % 60;
    return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
  }
  return `${Math.floor(hours / 24)}d`;
}

const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  warning: 1,
  info: 2,
};

/** Rank for severity sorting (unknown severities sort last). */
export function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? 99;
}
