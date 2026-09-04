/**
 * Typed contracts mirroring the Incident API OpenAPI schemas.
 * Checked against /openapi.json by tests/contract/test_approval_contract.py
 * (report/hypotheses/recommendations/approve/reject/chunk paths).
 */

export interface IncidentSummary {
  id: string;
  status: string;
  title: string;
  service: string;
  affected_services: string[];
  severity: "info" | "warning" | "critical";
  started_at: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  alert_fingerprint: string;
  version: number;
  occurrence_count: number;
  root_cause: string | null;
  confidence: number | null;
}

export interface IncidentDetail extends IncidentSummary {
  investigation_window_start: string;
  investigation_window_end: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type IncidentPage = Page<IncidentSummary>;

export interface TimelineEvent {
  id: string;
  evidence_id: string;
  incident_id: string;
  timestamp: string;
  source: string;
  type: string;
  status: string;
  summary: string;
  attributes: Record<string, unknown>;
}

export interface EvidenceItem {
  id: string;
  incident_id: string;
  source: string;
  type: string;
  status: "collected" | "empty" | "unavailable" | "failed" | "timed_out";
  observed_at: string;
  window: { start: string; end: string };
  summary: string;
  payload: Record<string, unknown>;
  query_template: string;
  query_parameters: Record<string, unknown>;
  provenance: Record<string, unknown>;
  error_type: string | null;
  error_message: string | null;
  payload_sha256: string;
  collected_at: string;
  created_at: string;
  updated_at: string;
}

export interface Hypothesis {
  id: string;
  incident_id: string;
  category: string;
  description: string;
  status: "proposed" | "verified" | "rejected" | "inconclusive";
  confidence: number;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  reasoning_summary: string;
}

export interface Recommendation {
  id: string;
  action_type: string;
  target: string;
  parameters: Record<string, unknown>;
  rationale_evidence_ids: string[];
  risk: "low" | "medium" | "high";
  reversible: boolean;
  requires_approval: boolean;
  status: "proposed" | "waiting_for_approval" | "approved" | "rejected";
}

export interface IncidentReport {
  id: string;
  incident_id: string;
  title: string;
  affected_services: string[];
  severity: string;
  summary: string;
  root_cause: string | null;
  root_cause_summary: string | null;
  confidence: number;
  timeline: TimelineEvent[];
  hypotheses: Hypothesis[];
  evidence_references: string[];
  knowledge_references: string[];
  recommendations: Recommendation[];
  related_incident_ids: string[];
  limitations: string[];
  status: "complete" | "insufficient_evidence" | "waiting_for_approval";
  generated_at: string;
}

export interface KnowledgeChunk {
  id: string;
  document_id: string;
  source_path: string;
  doc_type: string;
  version: string;
  chunk_index: number;
  text: string;
  embedding: number[];
  token_estimate: number;
  created_at: string;
}

export interface AuditEvent {
  id: string;
  incident_id: string;
  event_type: string;
  actor: string;
  from_status: string | null;
  to_status: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface ApprovalRecord {
  id: string;
  incident_id: string;
  recommendation_id: string;
  run_id: string;
  report_id: string;
  decision: "approved" | "rejected";
  actor: string;
  incident_version: number;
  idempotency_key: string;
  created_at: string;
}

export interface ApprovalResponse {
  approval: ApprovalRecord;
  replayed: boolean;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** Duck-typed error code access that survives module mocks and serialization. */
export function apiErrorCode(error: unknown): string | null {
  if (typeof error === "object" && error !== null && "code" in error) {
    const code = (error as { code?: unknown }).code;
    return typeof code === "string" ? code : null;
  }
  return null;
}

export function apiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}
