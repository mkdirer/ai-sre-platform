import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, idempotencyKey } from "../api/client";
import {
  apiErrorCode,
  apiErrorMessage,
  type AuditEvent,
  type EvidenceItem,
  type Hypothesis,
  type IncidentDetail,
  type IncidentReport,
  type KnowledgeChunk,
  type Recommendation,
  type TimelineEvent,
} from "../api/types";
import { Mono } from "../components/Code";
import {
  AuditTimelineList,
  EvidenceTimelineList,
  EvidenceTable,
  HypothesisCard,
  KnowledgeList,
  SummaryFacts,
} from "../components/incident";
import { EmptyState, ErrorAlert, Loading, StaleAlert } from "../components/States";

interface DetailData {
  incident: IncidentDetail;
  report: IncidentReport | null;
  hypotheses: Hypothesis[];
  recommendations: Recommendation[];
  evidence: EvidenceItem[];
  evidenceTimeline: TimelineEvent[];
  audit: AuditEvent[];
  knowledge: KnowledgeChunk[];
  knowledgeUnavailable: boolean;
}

const ACTOR_STORAGE_KEY = "sre-approver-actor";
const DEFAULT_ACTOR =
  (import.meta.env.VITE_APPROVAL_ACTOR as string | undefined) ?? "local-demo-approver";

export function IncidentDetailPage() {
  const { incidentId = "" } = useParams();
  const [data, setData] = useState<DetailData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState<string | null>(null);
  const [actor, setActor] = useState(
    () => localStorage.getItem(ACTOR_STORAGE_KEY) ?? DEFAULT_ACTOR,
  );
  const [decisionPending, setDecisionPending] = useState<string | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [decisionNotice, setDecisionNotice] = useState<string | null>(null);
  // Stable idempotency keys per recommendation+decision so a lost response can
  // be retried with the same key and replay safely on the API side.
  const decisionKeys = useRef(new Map<string, string>());

  function keyFor(
    recommendationId: string,
    decision: "approve" | "reject",
    actor: string,
    incidentVersion: number,
  ): string {
    // Scope the key to actor+version: replaying with the same key returns the
    // stored decision, so a changed actor must not silently reuse it.
    const mapKey = `${recommendationId}:${decision}:${actor}:${incidentVersion}`;
    const existing = decisionKeys.current.get(mapKey);
    if (existing) return existing;
    const fresh = idempotencyKey();
    decisionKeys.current.set(mapKey, fresh);
    return fresh;
  }

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStale(null);
    try {
      const incident = await api.getIncident(incidentId);
      const [report, hypotheses, recommendations, evidence, evidenceTimeline, audit] =
        await Promise.all([
          api.getReport(incidentId),
          api.listHypotheses(incidentId),
          api.listRecommendations(incidentId),
          api.listEvidence(incidentId),
          api.evidenceTimeline(incidentId),
          api.auditTimeline(incidentId),
        ]);
      const knowledge: KnowledgeChunk[] = [];
      let knowledgeUnavailable = false;
      const refs = report?.knowledge_references ?? [];
      if (refs.length > 0) {
        const settled = await Promise.allSettled(refs.map((id) => api.getKnowledgeChunk(id)));
        for (const result of settled) {
          if (result.status === "fulfilled") knowledge.push(result.value);
          else knowledgeUnavailable = true;
        }
      }
      setData({
        incident,
        report,
        hypotheses: hypotheses.items,
        recommendations: recommendations.items,
        evidence: evidence.items,
        evidenceTimeline: evidenceTimeline.items,
        audit: audit.items,
        knowledge,
        knowledgeUnavailable,
      });
    } catch (err) {
      if (apiErrorCode(err) === "incident_not_found") {
        setError("Incident was not found. It may have been removed.");
      } else {
        setError(apiErrorMessage(err, "Failed to load incident detail"));
      }
    } finally {
      setLoading(false);
    }
  }, [incidentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = useCallback(
    async (recommendation: Recommendation, decision: "approve" | "reject") => {
      if (!data) return;
      const trimmedActor = actor.trim();
      if (!trimmedActor) {
        setDecisionError("Enter an actor name before deciding.");
        return;
      }
      localStorage.setItem(ACTOR_STORAGE_KEY, trimmedActor);
      setDecisionPending(recommendation.id);
      setDecisionError(null);
      setDecisionNotice(null);
      setStale(null);
      try {
        const body = { incident_version: data.incident.version, actor: trimmedActor };
        const key = keyFor(recommendation.id, decision, trimmedActor, data.incident.version);
        const response =
          decision === "approve"
            ? await api.approveRecommendation(recommendation.id, body, key)
            : await api.rejectRecommendation(recommendation.id, body, key);
        if (response.replayed) {
          setDecisionNotice(
            "This decision was already recorded. Showing the stored result; nothing was duplicated.",
          );
        }
        await load();
      } catch (err) {
        const code = apiErrorCode(err);
        if (code === "stale_version") {
          setStale(
            "This incident changed while you were reviewing. Refresh to see the current version, then decide again.",
          );
        } else if (code === "approval_conflict") {
          setDecisionError(
            "This recommendation already has a recorded decision. Refresh to see the current state.",
          );
        } else if (code === "not_awaiting_approval") {
          setDecisionError(
            "This recommendation is no longer awaiting approval. Refresh to see the current state.",
          );
        } else if (code === "invalid_state") {
          setDecisionError(
            "The incident is no longer awaiting approval, so no decision can be recorded. Refresh to see the current state.",
          );
        } else {
          setDecisionError(apiErrorMessage(err, "Decision failed unexpectedly."));
        }
      } finally {
        setDecisionPending(null);
      }
    },
    [actor, data, load],
  );

  if (loading) return <Loading label="Loading incident detail" />;
  if (error)
    return (
      <section aria-labelledby="detail-error-title">
        <h1 id="detail-error-title">Incident detail</h1>
        <ErrorAlert message={error} onRetry={() => void load()} />
        <p>
          <Link to="/">Back to incidents</Link>
        </p>
      </section>
    );
  if (!data) return <EmptyState message="No incident data available." />;

  const { incident, report } = data;
  const showRca = report !== null && report.root_cause !== null;

  return (
    <div>
      <p>
        <Link to="/">← Back to incidents</Link>
      </p>
      <section aria-labelledby="incident-title">
        <div className="page-head">
          <h1 id="incident-title">{incident.title}</h1>
        </div>
        <SummaryFacts incident={incident} />
      </section>

      {stale ? <StaleAlert message={stale} onRefresh={() => void load()} /> : null}
      {decisionError ? <ErrorAlert message={decisionError} /> : null}
      {decisionNotice ? (
        <div className="alert" role="status">
          <p>{decisionNotice}</p>
        </div>
      ) : null}

      <section aria-labelledby="rca-title" className="panel">
        <div className="panel-head">
          <h2 id="rca-title">AI root cause analysis</h2>
          {report ? (
            <span className="meta num">confidence {(report.confidence * 100).toFixed(0)}%</span>
          ) : null}
        </div>
        {report === null ? (
          <EmptyState message="No investigation report exists yet. Evidence below reflects the current collection state." />
        ) : showRca ? (
          <div>
            <h3>
              <Mono>{report.root_cause?.replaceAll("_", " ")}</Mono>
            </h3>
            <p>{report.root_cause_summary}</p>
            <p className="meta">{report.summary}</p>
            {report.evidence_references.length > 0 ? (
              <>
                <p className="meta">Cited evidence (current telemetry, not history):</p>
                <ul className="evidence-ids" aria-label="Cited evidence">
                  {report.evidence_references.map((id) => (
                    <li key={id}>
                      <Mono>{id}</Mono>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        ) : (
          <div>
            <h3>Insufficient evidence</h3>
            <p>
              No verified hypothesis met the evidence threshold, so no root cause is claimed.
            </p>
          </div>
        )}
        {report !== null && report.limitations.length > 0 ? (
          <div>
            <h3>Data gaps</h3>
            <ul>
              {report.limitations.map((gap) => (
                <li key={gap}>{gap}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section aria-labelledby="recommendations-title" className="panel">
        <div className="panel-head">
          <h2 id="recommendations-title">Recommendations</h2>
          <span className="meta num">{data.recommendations.length}</span>
        </div>
        {data.recommendations.length === 0 ? (
          <EmptyState message="No recommendations were proposed for this incident." />
        ) : null}
        {data.recommendations.map((recommendation) => (
          <article className="item" key={recommendation.id}>
            <h3>
              {recommendation.action_type.replaceAll("_", " ")} on {recommendation.target}
            </h3>
            <p>
              <span
                className={`badge badge-${recommendation.risk === "low" ? "good" : recommendation.risk === "medium" ? "warning" : "bad"}`}
              >
                {recommendation.risk} risk
              </span>
              <span className="badge badge-neutral">
                {recommendation.status.replaceAll("_", " ")}
              </span>
              {recommendation.reversible ? (
                <span className="badge badge-neutral">reversible</span>
              ) : (
                <span className="badge badge-bad">irreversible</span>
              )}
            </p>
            <p className="meta">
              Rationale evidence: {recommendation.rationale_evidence_ids.join(", ") || "none"}
            </p>
            <details>
              <summary>Parameters</summary>
              <pre className="meta">{JSON.stringify(recommendation.parameters, null, 2)}</pre>
            </details>
            {recommendation.status === "waiting_for_approval" &&
            incident.status === "waiting_for_approval" ? (
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                }}
              >
                <label htmlFor={`actor-${recommendation.id}`}>Actor</label>{" "}
                <input
                  id={`actor-${recommendation.id}`}
                  value={actor}
                  maxLength={64}
                  onChange={(event) => setActor(event.target.value)}
                />
                <div className="form-actions">
                  <button
                    type="button"
                    disabled={decisionPending === recommendation.id}
                    onClick={() => void decide(recommendation, "approve")}
                  >
                    {decisionPending === recommendation.id ? "Working…" : "Approve"}
                  </button>
                  <button
                    type="button"
                    className="danger"
                    disabled={decisionPending === recommendation.id}
                    onClick={() => void decide(recommendation, "reject")}
                  >
                    Reject
                  </button>
                </div>
                <p className="meta">
                  Approval records the decision and resumes incident state. It never executes
                  remediation.
                </p>
              </form>
            ) : (
              <p className="meta">
                {recommendation.status === "approved"
                  ? "Approved. Awaiting a later remediation stage; nothing was executed."
                  : recommendation.status === "rejected"
                    ? "Rejected by a human reviewer."
                    : "Not awaiting approval, so no decision controls are shown."}
              </p>
            )}
          </article>
        ))}
      </section>

      <section aria-labelledby="hypotheses-title" className="panel">
        <div className="panel-head">
          <h2 id="hypotheses-title">Competing hypotheses</h2>
          <span className="meta num">{data.hypotheses.length}</span>
        </div>
        {data.hypotheses.length === 0 ? (
          <EmptyState message="No hypotheses were recorded for this incident." />
        ) : null}
        {data.hypotheses.map((hypothesis) => (
          <HypothesisCard key={hypothesis.id} hypothesis={hypothesis} />
        ))}
      </section>

      <section aria-labelledby="evidence-title" className="panel">
        <div className="panel-head">
          <h2 id="evidence-title">Evidence</h2>
          <span className="meta num">{data.evidence.length}</span>
        </div>
        <EvidenceTable items={data.evidence} />
      </section>

      <section aria-labelledby="knowledge-title" className="panel">
        <div className="panel-head">
          <h2 id="knowledge-title">Related knowledge</h2>
          <span className="meta">history, not proof</span>
        </div>
        <KnowledgeList chunks={data.knowledge} unavailable={data.knowledgeUnavailable} />
      </section>

      <section aria-labelledby="timeline-title" className="panel">
        <div className="panel-head">
          <h2 id="timeline-title">Correlated timeline</h2>
          <span className="meta num">{data.evidenceTimeline.length}</span>
        </div>
        <EvidenceTimelineList events={data.evidenceTimeline} />
      </section>

      <section aria-labelledby="audit-title" className="panel">
        <div className="panel-head">
          <h2 id="audit-title">Audit status</h2>
          <span className="meta num">{data.audit.length}</span>
        </div>
        <AuditTimelineList events={data.audit} />
      </section>
    </div>
  );
}
