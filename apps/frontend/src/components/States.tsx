export function Loading({ label }: { label: string }) {
  return (
    <div className="loading-block" role="status" aria-live="polite" aria-busy="true">
      <p className="meta">{label}…</p>
      <div className="loading-bar" aria-hidden="true" />
      <div className="loading-bar short" aria-hidden="true" />
    </div>
  );
}

export function ErrorAlert({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="alert alert-error" role="alert">
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="secondary" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="alert alert-empty">
      <p>{message}</p>
    </div>
  );
}

export function StaleAlert({ message, onRefresh }: { message: string; onRefresh: () => void }) {
  return (
    <div className="alert alert-stale" role="alert">
      <p>{message}</p>
      <button type="button" className="secondary" onClick={onRefresh}>
        Refresh
      </button>
    </div>
  );
}
