import {
  ApiError,
  type ApiErrorBody,
  type ApprovalResponse,
  type AuditEvent,
  type EvidenceItem,
  type Hypothesis,
  type IncidentDetail,
  type IncidentPage,
  type IncidentReport,
  type KnowledgeChunk,
  type Page,
  type Recommendation,
  type TimelineEvent,
} from "./types";

const BASE = (import.meta.env.VITE_INCIDENT_API_URL as string | undefined) ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "network_unavailable", "Incident API is unreachable");
  }
  if (response.ok) {
    return (await response.json()) as T;
  }
  let code = "unexpected_error";
  let message = `Request failed with status ${response.status}`;
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.message === "string") message = body.message;
  } catch {
    // Keep the default status-based message when the body is not JSON.
  }
  throw new ApiError(response.status, code, message);
}

function idempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `key-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

export const api = {
  listIncidents(limit = 25, offset = 0, status?: string): Promise<IncidentPage> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (status) params.set("status", status);
    return request<IncidentPage>(`/api/v1/incidents?${params}`);
  },

  getIncident(id: string): Promise<IncidentDetail> {
    return request<IncidentDetail>(`/api/v1/incidents/${id}`);
  },

  getReport(id: string): Promise<IncidentReport | null> {
    return request<IncidentReport>(`/api/v1/incidents/${id}/report`).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    });
  },

  listHypotheses(id: string): Promise<Page<Hypothesis>> {
    return request(`/api/v1/incidents/${id}/hypotheses?limit=25&offset=0`);
  },

  listRecommendations(id: string): Promise<Page<Recommendation>> {
    return request(`/api/v1/incidents/${id}/recommendations?limit=25&offset=0`);
  },

  listEvidence(id: string): Promise<Page<EvidenceItem>> {
    return request(`/api/v1/incidents/${id}/evidence?limit=50&offset=0`);
  },

  evidenceTimeline(id: string): Promise<Page<TimelineEvent>> {
    return request(`/api/v1/incidents/${id}/evidence/timeline?limit=50&offset=0`);
  },

  auditTimeline(id: string): Promise<Page<AuditEvent>> {
    return request(`/api/v1/incidents/${id}/timeline?limit=50&offset=0`);
  },

  getKnowledgeChunk(chunkId: string): Promise<KnowledgeChunk> {
    return request(`/api/v1/knowledge/chunks/${chunkId}`);
  },

  approveRecommendation(
    recommendationId: string,
    body: { incident_version: number; actor: string },
    key: string = idempotencyKey(),
  ): Promise<ApprovalResponse> {
    return request(`/api/v1/recommendations/${recommendationId}/approve`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": key },
    });
  },

  rejectRecommendation(
    recommendationId: string,
    body: { incident_version: number; actor: string },
    key: string = idempotencyKey(),
  ): Promise<ApprovalResponse> {
    return request(`/api/v1/recommendations/${recommendationId}/reject`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": key },
    });
  },
};

export { idempotencyKey };
