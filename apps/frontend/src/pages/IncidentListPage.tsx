import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage, type IncidentSummary } from "../api/types";
import { ConfidenceBadge, SeverityBadge, StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorAlert, Loading } from "../components/States";

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function IncidentListPage() {
  const [statusFilter, setStatusFilter] = useState("");
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.listIncidents(25, 0, statusFilter || undefined);
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

  return (
    <section aria-labelledby="incidents-title">
      <h1 id="incidents-title">Incidents</h1>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <label htmlFor="status-filter">Status</label>{" "}
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
        </select>{" "}
        <button type="submit">Filter</button>
      </form>

      {loading ? <Loading label="Loading incidents" /> : null}
      {error ? <ErrorAlert message={error} onRetry={() => void load()} /> : null}
      {!loading && !error && incidents.length === 0 ? (
        <EmptyState message="No incidents match this filter." />
      ) : null}
      {!loading && !error && incidents.length > 0 ? (
        <>
          <p className="meta" aria-live="polite">
            Showing {incidents.length} of {total} incidents.
          </p>
          <table className="incidents">
            <thead>
              <tr>
                <th scope="col">Incident</th>
                <th scope="col">Severity</th>
                <th scope="col">Service</th>
                <th scope="col">Status</th>
                <th scope="col">Started</th>
                <th scope="col">Assessment</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((incident) => (
                <tr key={incident.id}>
                  <td>
                    <Link to={`/incidents/${incident.id}`}>{incident.id}</Link>
                    <div className="meta">{incident.title}</div>
                  </td>
                  <td>
                    <SeverityBadge severity={incident.severity} />
                  </td>
                  <td>{incident.service}</td>
                  <td>
                    <StatusBadge status={incident.status} />
                  </td>
                  <td className="meta">{formatTime(incident.started_at)}</td>
                  <td>
                    <ConfidenceBadge status={incident.status} confidence={incident.confidence} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </section>
  );
}
