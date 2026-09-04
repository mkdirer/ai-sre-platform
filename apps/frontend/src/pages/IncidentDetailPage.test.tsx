import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  evidenceFixture,
  hypothesisFixture,
  incidentFixture,
  knowledgeChunkFixture,
  recommendationFixture,
  reportFixture,
} from "../test-utils/fixtures";
import { IncidentDetailPage } from "./IncidentDetailPage";

const mocks = vi.hoisted(() => ({
  getIncident: vi.fn(),
  getReport: vi.fn(),
  listHypotheses: vi.fn(),
  listRecommendations: vi.fn(),
  listEvidence: vi.fn(),
  evidenceTimeline: vi.fn(),
  auditTimeline: vi.fn(),
  getKnowledgeChunk: vi.fn(),
  approveRecommendation: vi.fn(),
  rejectRecommendation: vi.fn(),
}));

const keyMock = vi.hoisted(() => ({ count: 0 }));

vi.mock("../api/client", () => ({
  api: mocks,
  idempotencyKey: () => `test-key-${++keyMock.count}`,
  ApiError: class ApiError extends Error {
    status: number;
    code: string;
    constructor(status: number, code: string, message: string) {
      super(message);
      this.status = status;
      this.code = code;
    }
  },
}));

function seedAll() {
  mocks.getIncident.mockResolvedValue(incidentFixture);
  mocks.getReport.mockResolvedValue(reportFixture);
  mocks.listHypotheses.mockResolvedValue({ items: [hypothesisFixture], total: 1, limit: 25, offset: 0 });
  mocks.listRecommendations.mockResolvedValue({
    items: [recommendationFixture],
    total: 1,
    limit: 25,
    offset: 0,
  });
  mocks.listEvidence.mockResolvedValue({ items: [evidenceFixture], total: 1, limit: 50, offset: 0 });
  mocks.evidenceTimeline.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
  mocks.auditTimeline.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
  mocks.getKnowledgeChunk.mockResolvedValue(knowledgeChunkFixture);
  mocks.approveRecommendation.mockResolvedValue({ approval: {}, replayed: false });
  mocks.rejectRecommendation.mockResolvedValue({ approval: {}, replayed: false });
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={[`/incidents/${incidentFixture.id}`]}>
      <Routes>
        <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("IncidentDetailPage", () => {
  beforeEach(() => {
    Object.values(mocks).forEach((mock) => mock.mockReset());
    keyMock.count = 0;
    localStorage.clear();
    seedAll();
  });

  it("renders RCA, evidence provenance, hypotheses, knowledge, and risk", async () => {
    renderDetail();
    expect(
      await screen.findByText("deployment regression affecting payment-service"),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/EVD-A1B2C3D4E5F6070811223344/).length).toBeGreaterThan(0);
    expect(screen.getByText("medium risk")).toBeInTheDocument();
    expect(
      await screen.findByText("A recent payment deployment regressed persistence latency"),
    ).toBeInTheDocument();
  });

  it("shows approve controls only while waiting for approval", async () => {
    renderDetail();
    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });

  it("approves with actor and incident version, then reloads", async () => {
    renderDetail();
    await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(mocks.approveRecommendation).toHaveBeenCalledWith(
        recommendationFixture.id,
        { incident_version: 3, actor: "local-demo-approver" },
        expect.any(String),
      ),
    );
  });

  it("rejects with actor and incident version", async () => {
    renderDetail();
    await screen.findByRole("button", { name: "Reject" });
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() =>
      expect(mocks.rejectRecommendation).toHaveBeenCalledWith(
        recommendationFixture.id,
        { incident_version: 3, actor: "local-demo-approver" },
        expect.any(String),
      ),
    );
  });

  it("reuses the same idempotency key when the same decision is retried", async () => {
    renderDetail();
    await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mocks.approveRecommendation).toHaveBeenCalledTimes(1));
    const firstKey = mocks.approveRecommendation.mock.calls[0]?.[2];
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mocks.approveRecommendation).toHaveBeenCalledTimes(2));
    expect(mocks.approveRecommendation.mock.calls[1]?.[2]).toBe(firstKey);
  });

  it("uses a fresh idempotency key when the actor changes", async () => {
    renderDetail();
    await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mocks.approveRecommendation).toHaveBeenCalledTimes(1));
    const firstKey = mocks.approveRecommendation.mock.calls[0]?.[2];
    await userEvent.clear(screen.getByLabelText("Actor"));
    await userEvent.type(screen.getByLabelText("Actor"), "another-approver");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mocks.approveRecommendation).toHaveBeenCalledTimes(2));
    expect(mocks.approveRecommendation.mock.calls[1]?.[2]).not.toBe(firstKey);
    expect(mocks.approveRecommendation.mock.calls[1]?.[1]).toEqual({
      incident_version: 3,
      actor: "another-approver",
    });
  });

  it("shows a replay notice when the API returns a stored decision", async () => {
    mocks.approveRecommendation.mockResolvedValue({ approval: {}, replayed: true });
    renderDetail();
    await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/already recorded.*nothing was duplicated/)).toBeInTheDocument();
  });

  it("requires an actor name before deciding", async () => {
    renderDetail();
    await screen.findByRole("button", { name: "Approve" });
    const input = screen.getByLabelText("Actor");
    await userEvent.clear(input);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Enter an actor name before deciding.")).toBeInTheDocument();
    expect(mocks.approveRecommendation).not.toHaveBeenCalled();
  });

  it("shows actionable errors for approval conflicts and wrong states", async () => {
    for (const [code, pattern] of [
      ["approval_conflict", /already has a recorded decision/],
      ["not_awaiting_approval", /no longer awaiting approval/],
      ["invalid_state", /no longer awaiting approval/],
    ] as const) {
      Object.values(mocks).forEach((mock) => mock.mockReset());
      seedAll();
      const conflict = Object.assign(new Error(code), { status: 409, code });
      mocks.approveRecommendation.mockRejectedValue(conflict);
      const { unmount } = renderDetail();
      await screen.findByRole("button", { name: "Approve" });
      await userEvent.click(screen.getByRole("button", { name: "Approve" }));
      expect(await screen.findByText(pattern)).toBeInTheDocument();
      unmount();
    }
  });

  it("exposes recommendation parameters for review", async () => {
    renderDetail();
    expect(await screen.findByText("Parameters")).toBeInTheDocument();
    expect(screen.getByText(/DEP-A1B2C3D4E5F607081122/)).toBeInTheDocument();
  });

  it("surfaces a stale-update state when the version moved", async () => {
    const stale = Object.assign(new Error("stale"), { status: 409, code: "stale_version" });
    mocks.approveRecommendation.mockRejectedValue(stale);
    renderDetail();
    await screen.findByRole("button", { name: "Approve" });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/changed while you were reviewing/)).toBeInTheDocument();
  });

  it("renders insufficient-evidence state when the report has no root cause", async () => {
    mocks.getReport.mockResolvedValue({ ...reportFixture, root_cause: null, root_cause_summary: null });
    renderDetail();
    expect(await screen.findByText("Insufficient evidence")).toBeInTheDocument();
  });

  it("renders unavailable-source evidence without fabricating results", async () => {
    mocks.listEvidence.mockResolvedValue({
      items: [
        {
          ...evidenceFixture,
          id: "EVD-AAAAAAAAAAAAAAAAAAAAAAAA",
          status: "unavailable",
          error_type: "AdapterUnavailableError",
          error_message: "Tempo is unavailable",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });
    renderDetail();
    expect(await screen.findByText("unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Missing data is not proof/)).toBeInTheDocument();
  });
});
