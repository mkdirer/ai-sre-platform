import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage, type IncidentSummary } from "../api/types";
import { CopyButton, Mono } from "../components/Code";
import { ConfidenceBadge, SeverityBadge, StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorAlert, Loading } from "../components/States";
import { formatRelative, formatTime, severityRank } from "../utils/format";

const PAGE_LIMIT = 100;

const ATTENTION_STATUSES = new Set([
  "queued",
  "investigating",
  "waiting_for_approval",
  "remediating",
  "verifying",
]);

const REVIEW_STATUSES = new Set(["insufficient_evidence", "investigation_failed", "rejected"]);

type SortKey = "started_desc" | "started_asc" | "severity";

function sortIncidents(incidents: IncidentSummary[], sort: SortKey): IncidentSummary[] {
  const rows = [...incidents];
  if (sort === "started_asc") {
    rows.sort((a, b) => a.started_at.localeCompare(b.started_at));
  } else if (sort === "severity") {
    rows.sort(
      (a, b) => severityRank(a.severity) - severityRank(b.severity) || b.started_at.localeCompare(a.started_at),
    );
  } else {
    rows.sort((a, b) => b.started_at.localeCompare(a.started_at));
  }
  return rows;
}

export function IncidentListPage() {
  const [statusFilter, setStatusFilter] = useState("");
  const [severityFilter, setSeverityFilter] = useState("");
  const [sort, setSort] = useState<SortKey>("started_desc");
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.listIncidents(PAGE_LIMIT, 0, statusFilter || undefined);
      setIncidents(page.items);
      setTotal(page.total);
    } catch (err) {
      setError(apiErrorMessage(err, "Failed to load incidents"));
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = useMemo(() => {
    const filtered = severityFilter
      ? incidents.filter((incident) => incident.severity === severityFilter)
      : incidents;
    return sortIncidents(filtered, sort);
  }, [incidents, severityFilter, sort]);

  const attention = incidents.filter((incident) => ATTENTION_STATUSES.has(incident.status)).length;
  const resolved = incidents.filter(
    (incident) => incident.status === "resolved" || incident.status === "closed",
  ).length;
  const needsReview = incidents.filter((incident) => REVIEW_STATUSES.has(incident.status)).length;
  const critical = incidents.filter((incident) => incident.severity === "critical").length;

  return (
    <section aria-labelledby="incidents-title">
      <div className="page-head">
        <h1 id="incidents-title">Incidents</h1>
        <p className="meta" aria-live="polite">
          Showing {visible.length} of {incidents.length} loaded incidents ({total} total).
        </p>
      </div>

      {!loading && !error && incidents.length > 0 ? (
        <ul className="stats" aria-label="Incident overview for the loaded page">
          <li>
            <div className="stat-value num">{attention}</div>
            <div className="stat-label">Needs attention</div>
          </li>
          <li>
            <div className="stat-value num">{critical}</div>
            <div className="stat-label">Critical severity</div>
          </li>
          <li>
            <div className="stat-value num">{needsReview}</div>
            <div className="stat-label">Needs review</div>
          </li>
          <li>
            <div className="stat-value num">{resolved}</div>
            <div className="stat-label">Resolved or closed</div>
          </li>
        </ul>
      ) : null}

      <div className="toolbar" role="group" aria-label="Incident filters">
        <div className="field">
          <label htmlFor="status-filter">Status</label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="">All</option>
            <option value="queued">Queued</option>
            <option value="investigating">Investigating</option>
            <option value="waiting_for_approval">Waiting for approval</option>
            <option value="remediating">Remediating</option>
            <option value="verifying">Verifying</option>
            <option value="insufficient_evidence">Insufficient evidence</option>
            <option value="investigation_failed">Investigation failed</option>
            <option value="resolved">Resolved</option>
            <option value="rejected">Rejected</option>
            <option value="closed">Closed</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="severity-filter">Severity</label>
          <select
            id="severity-filter"
            value={severityFilter}
            onChange={(event) => setSeverityFilter(event.target.value)}
          >
            <option value="">All</option>
            <option value="critical">Critical</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="sort-order">Sort</label>
          <select
            id="sort-order"
            value={sort}
            onChange={(event) => setSort(event.target.value as SortKey)}
          >
            <option value="started_desc">Newest first</option>
            <option value="started_asc">Oldest first</option>
            <option value="severity">Severity</option>
          </select>
        </div>
      </div>

      {loading ? <Loading label="Loading incidents" /> : null}
      {error ? <ErrorAlert message={error} onRetry={() => void load()} /> : null}
      {!loading && !error && visible.length === 0 ? (
        <EmptyState message="No incidents match this filter." />
      ) : null}
      {!loading && !error && visible.length > 0 ? (
        <div className="table-wrap">
          <table className="grid">
            <caption className="visually-hidden">
              Incidents sorted by {sort === "started_asc" ? "oldest" : sort === "severity" ? "severity" : "newest"} first
            </caption>
            <thead>
              <tr>
                <th scope="col">Incident</th>
                <th scope="col">Status</th>
                <th scope="col">Severity</th>
                <th scope="col">Service</th>
                <th scope="col">Started</th>
                <th scope="col">Analysis</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((incident) => (
                <tr key={incident.id}>
                  <td>
                    <Link to={`/incidents/${incident.id}`}>
                      <Mono>{incident.id}</Mono>
                    </Link>
                    <CopyButton value={incident.id} label="incident ID" />
                    <div className="meta">{incident.title}</div>
                  </td>
                  <td className="nowrap">
                    <StatusBadge status={incident.status} />
                  </td>
                  <td className="nowrap">
                    <SeverityBadge severity={incident.severity} />
                  </td>
                  <td>
                    <Mono>{incident.service}</Mono>
                  </td>
                  <td className="nowrap meta num">
                    <span aria-hidden="true">{formatRelative(incident.started_at)}</span>
                    <span className="visually-hidden">{formatTime(incident.started_at)}</span>
                  </td>
                  <td className="nowrap">
                    <ConfidenceBadge status={incident.status} confidence={incident.confidence} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
