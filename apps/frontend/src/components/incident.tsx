import type {
  AuditEvent,
  EvidenceItem,
  Hypothesis,
  IncidentDetail,
  KnowledgeChunk,
  TimelineEvent,
} from "../api/types";
import { CopyButton, Mono } from "./Code";
import { ConfidenceBadge, SeverityBadge, StatusBadge } from "./StatusBadge";
import { EmptyState } from "./States";
import { formatDuration, formatRelative, formatTime } from "../utils/format";

/** Header facts: everything needed to answer what/where/when at a glance. */
export function SummaryFacts({ incident }: { incident: IncidentDetail }) {
  const duration = formatDuration(incident.started_at, incident.completed_at);
  return (
    <div className="panel">
      <div>
        <Mono>{incident.id}</Mono>
        <CopyButton value={incident.id} label="incident ID" />{" "}
        <StatusBadge status={incident.status} /> <SeverityBadge severity={incident.severity} />{" "}
        <ConfidenceBadge status={incident.status} confidence={incident.confidence} />
      </div>
      <dl className="facts">
        <div>
          <dt>Service</dt>
          <dd>
            <Mono>{incident.service}</Mono>
          </dd>
        </div>
        <div>
          <dt>Affected services</dt>
          <dd>{incident.affected_services.join(", ") || "—"}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd className="num" title={formatTime(incident.started_at)}>
            {formatTime(incident.started_at)} ({formatRelative(incident.started_at)})
          </dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd className="num">
            {incident.completed_at ? (duration ?? "—") : "ongoing"}
          </dd>
        </div>
        <div>
          <dt>Investigation window</dt>
          <dd className="meta num">
            {formatTime(incident.investigation_window_start)} →{" "}
            {formatTime(incident.investigation_window_end)}
          </dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd className="num">v{incident.version}</dd>
        </div>
        <div>
          <dt>Occurrences</dt>
          <dd className="num">{incident.occurrence_count}</dd>
        </div>
        <div>
          <dt>Completed</dt>
          <dd className="num">
            {incident.completed_at ? formatTime(incident.completed_at) : "—"}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function EvidenceIdList({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <ul className="evidence-ids" aria-label="Evidence references">
      {ids.map((id) => (
        <li key={id}>
          <Mono>{id}</Mono>
        </li>
      ))}
    </ul>
  );
}

export function HypothesisCard({ hypothesis }: { hypothesis: Hypothesis }) {
  return (
    <article className="item" data-testid={`hypothesis-${hypothesis.id}`}>
      <h3>
        {hypothesis.category.replaceAll("_", " ")} · {hypothesis.status} · confidence{" "}
        {(hypothesis.confidence * 100).toFixed(0)}%
      </h3>
      <p>{hypothesis.description}</p>
      <p>{hypothesis.reasoning_summary}</p>
      {hypothesis.supporting_evidence_ids.length > 0 ? (
        <>
          <p className="meta">Supporting:</p>
          <EvidenceIdList ids={hypothesis.supporting_evidence_ids} />
        </>
      ) : null}
      {hypothesis.contradicting_evidence_ids.length > 0 ? (
        <>
          <p className="meta">Contradicting:</p>
          <EvidenceIdList ids={hypothesis.contradicting_evidence_ids} />
        </>
      ) : null}
      {hypothesis.supporting_evidence_ids.length === 0 &&
      hypothesis.contradicting_evidence_ids.length === 0 ? (
        <p className="meta">No linked evidence.</p>
      ) : null}
      {hypothesis.status === "rejected" ? (
        <p className="meta">
          Rejected: contradicted by current telemetry or lacking support. Historical similarity
          alone never sustains a hypothesis.
        </p>
      ) : null}
    </article>
  );
}

const EVIDENCE_STATUS_CLASS: Record<string, string> = {
  collected: "badge-good",
  empty: "badge-neutral",
  unavailable: "badge-bad",
  failed: "badge-bad",
  timed_out: "badge-bad",
};

/** Dense evidence table: one row per item, provenance behind details. */
export function EvidenceTable({ items }: { items: EvidenceItem[] }) {
  if (items.length === 0) {
    return <EmptyState message="No evidence has been collected for this incident yet." />;
  }
  return (
    <div className="table-wrap">
      <table className="grid">
        <caption className="visually-hidden">
          Collected and attempted telemetry evidence for this incident
        </caption>
        <thead>
          <tr>
            <th scope="col">Evidence</th>
            <th scope="col">Source</th>
            <th scope="col">Status</th>
            <th scope="col">Observed</th>
            <th scope="col">Summary</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                <Mono>{item.id}</Mono>
                <div className="meta mono">{item.query_template}</div>
              </td>
              <td className="nowrap meta">
                {item.source} · {item.type}
              </td>
              <td className="nowrap">
                <span className={`badge ${EVIDENCE_STATUS_CLASS[item.status] ?? "badge-neutral"}`}>
                  {item.status.replaceAll("_", " ")}
                </span>
              </td>
              <td className="nowrap meta num">
                <span aria-hidden="true">{formatRelative(item.observed_at)}</span>
                <span className="visually-hidden">{formatTime(item.observed_at)}</span>
              </td>
              <td>
                {item.summary}
                {item.status === "collected" || item.status === "empty" ? null : (
                  <p className="meta">
                    Source unavailable or failed{item.error_type ? `: ${item.error_type}` : ""}.
                    {item.error_message ? ` ${item.error_message}` : ""} Missing data is not
                    proof that an event did not occur.
                  </p>
                )}
                <details>
                  <summary>Provenance</summary>
                  <pre className="meta">
                    {JSON.stringify(
                      {
                        provenance: item.provenance,
                        query_parameters: item.query_parameters,
                        payload_sha256: item.payload_sha256,
                      },
                      null,
                      2,
                    )}
                  </pre>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function KnowledgeList({
  chunks,
  unavailable,
}: {
  chunks: KnowledgeChunk[];
  unavailable: boolean;
}) {
  return (
    <>
      {chunks.length === 0 && !unavailable ? (
        <EmptyState message="No historical context was retrieved for this incident." />
      ) : null}
      {unavailable ? (
        <p className="meta">
          Some referenced knowledge chunks could not be loaded; citations below are partial.
        </p>
      ) : null}
      {chunks.map((chunk) => (
        <article className="item" key={chunk.id}>
          <h3>
            <Mono>{chunk.id}</Mono> · {chunk.doc_type} · <span className="meta">{chunk.source_path}</span>
          </h3>
          <p>{chunk.text}</p>
          <p className="meta">
            Historical context only — similarity to past incidents never proves the current root
            cause.
          </p>
        </article>
      ))}
    </>
  );
}

export function EvidenceTimelineList({ events }: { events: TimelineEvent[] }) {
  if (events.length === 0) {
    return <EmptyState message="The evidence timeline is empty." />;
  }
  return (
    <ol className="timeline">
      {events.map((event) => (
        <li key={event.id}>
          <span className="meta num" title={formatTime(event.timestamp)}>
            {formatTime(event.timestamp)} ·{" "}
          </span>
          {event.summary}{" "}
          <span className="meta">
            ({event.source}, <Mono>{event.evidence_id}</Mono>)
          </span>
        </li>
      ))}
    </ol>
  );
}

export function AuditTimelineList({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <EmptyState message="No audit events are recorded yet." />;
  }
  return (
    <ol className="timeline">
      {events.map((event) => (
        <li
          key={event.id}
          className={
            event.to_status === "resolved"
              ? "timeline-good"
              : event.to_status === "investigation_failed"
                ? "timeline-bad"
                : undefined
          }
        >
          <span className="meta num" title={formatTime(event.created_at)}>
            {formatTime(event.created_at)} ·{" "}
          </span>
          <Mono>{event.event_type}</Mono> by {event.actor}
          {event.from_status || event.to_status ? (
            <span className="meta">
              {" "}
              ({event.from_status ?? "—"} → {event.to_status ?? "—"})
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
