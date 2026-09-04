const STATUS_CLASS: Record<string, string> = {
  queued: "badge-neutral",
  investigating: "badge-info",
  waiting_for_approval: "badge-warning",
  remediating: "badge-info",
  verifying: "badge-info",
  resolved: "badge-good",
  insufficient_evidence: "badge-neutral",
  investigation_failed: "badge-bad",
  rejected: "badge-bad",
  closed: "badge-neutral",
};

export function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_CLASS[status] ?? "badge-neutral";
  return <span className={`badge ${cls}`}>{status.replaceAll("_", " ")}</span>;
}

export function SeverityBadge({ severity }: { severity: string }) {
  const cls =
    severity === "critical"
      ? "badge-critical"
      : severity === "warning"
        ? "badge-warning"
        : "badge-info";
  return <span className={`badge ${cls}`}>{severity}</span>;
}

/** Confidence / data-gap indicator for list and detail views. */
export function ConfidenceBadge({
  status,
  confidence,
}: {
  status: string;
  confidence: number | null;
}) {
  if (status === "insufficient_evidence") {
    return <span className="badge badge-neutral">data gaps</span>;
  }
  if (confidence === null || confidence === undefined) {
    return <span className="badge badge-neutral">no RCA yet</span>;
  }
  return <span className="badge badge-neutral">confidence {(confidence * 100).toFixed(0)}%</span>;
}
