/**
 * Test-only fixtures for component tests and story-like development.
 * Never imported by production runtime paths.
 */
import type {
  EvidenceItem,
  Hypothesis,
  IncidentDetail,
  IncidentReport,
  KnowledgeChunk,
  Recommendation,
} from "../api/types";

export const incidentFixture: IncidentDetail = {
  id: "INC-A1B2C3D4E5F60708",
  status: "waiting_for_approval",
  title: "Payment latency is high",
  service: "payment-service",
  affected_services: ["payment-service"],
  severity: "warning",
  started_at: "2026-09-06T12:00:00Z",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:05:00Z",
  completed_at: null,
  alert_fingerprint: "a".repeat(64),
  version: 3,
  occurrence_count: 1,
  root_cause: "bad_deployment",
  confidence: 0.8,
  investigation_window_start: "2026-09-06T11:50:00Z",
  investigation_window_end: "2026-09-06T12:05:00Z",
};

export const recommendationFixture: Recommendation = {
  id: "REC-A1B2C3D4E5F6070811223344",
  action_type: "rollback_deployment",
  target: "payment-service",
  parameters: { deployment_id: "DEP-A1B2C3D4E5F607081122", version: "0.1.0" },
  rationale_evidence_ids: ["EVD-A1B2C3D4E5F6070811223344"],
  risk: "medium",
  reversible: true,
  requires_approval: true,
  status: "waiting_for_approval",
};

export const hypothesisFixture: Hypothesis = {
  id: "HYP-A1B2C3D4E5F6070811223344",
  incident_id: incidentFixture.id,
  category: "bad_deployment",
  description: "A recent payment deployment regressed persistence latency",
  status: "verified",
  confidence: 0.8,
  supporting_evidence_ids: ["EVD-A1B2C3D4E5F6070811223344"],
  contradicting_evidence_ids: [],
  reasoning_summary: "Latency coincides with the deployment window",
};

export const reportFixture: IncidentReport = {
  id: "RPT-A1B2C3D4E5F6070811223344",
  incident_id: incidentFixture.id,
  title: incidentFixture.title,
  affected_services: ["payment-service"],
  severity: "warning",
  summary: "Payment latency is high: deployment regression affecting payment-service",
  root_cause: "bad_deployment",
  root_cause_summary: "deployment regression affecting payment-service",
  confidence: 0.8,
  timeline: [],
  hypotheses: [hypothesisFixture],
  evidence_references: ["EVD-A1B2C3D4E5F6070811223344"],
  knowledge_references: [],
  recommendations: [recommendationFixture],
  related_incident_ids: [],
  limitations: [],
  status: "waiting_for_approval",
  generated_at: "2026-09-06T12:05:00Z",
};

export const evidenceFixture: EvidenceItem = {
  id: "EVD-A1B2C3D4E5F6070811223344",
  incident_id: incidentFixture.id,
  source: "prometheus",
  type: "metric",
  status: "collected",
  observed_at: "2026-09-06T12:00:00Z",
  window: { start: "2026-09-06T11:50:00Z", end: "2026-09-06T12:05:00Z" },
  summary: "Payment p95 latency is 2.5 seconds",
  payload: { value: 2.5 },
  query_template: "metric.service_latency_p95",
  query_parameters: { service: "payment-service" },
  provenance: { adapter: "prometheus" },
  error_type: null,
  error_message: null,
  payload_sha256: "b".repeat(64),
  collected_at: "2026-09-06T12:00:00Z",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
};

export const knowledgeChunkFixture: KnowledgeChunk = {
  id: "KNW-A1B2C3D4E5F6070811223344",
  document_id: "DOC-AAAAAAAAAAAAAAAAAAAA",
  source_path: "knowledge/runbooks/payment_database_runbook.md",
  doc_type: "runbook",
  version: "v1",
  chunk_index: 0,
  text: "Disable the fault and verify p95 recovers.",
  embedding: [],
  token_estimate: 12,
  created_at: "2026-09-06T12:00:00Z",
};
