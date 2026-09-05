import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { incidentFixture } from "../test-utils/fixtures";
import { IncidentListPage } from "./IncidentListPage";

const listMock = vi.fn();

vi.mock("../api/client", () => ({
  api: {
    listIncidents: (...args: unknown[]) => listMock(...args),
  },
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

function page(items = [incidentFixture], total = 1) {
  return { items, total, limit: 25, offset: 0 };
}

describe("IncidentListPage", () => {
  beforeEach(() => {
    listMock.mockReset();
  });

  it("renders severity, service, status, timestamps, and confidence indicator", async () => {
    listMock.mockResolvedValue(page());
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("INC-A1B2C3D4E5F60708")).toBeInTheDocument();
    expect(screen.getByText("payment-service")).toBeInTheDocument();
    expect(screen.getByText("waiting for approval")).toBeInTheDocument();
    expect(screen.getByText("warning")).toBeInTheDocument();
    expect(screen.getByText("confidence 80%")).toBeInTheDocument();
  });

  it("shows an empty state when no incidents match", async () => {
    listMock.mockResolvedValue(page([], 0));
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("No incidents match this filter.")).toBeInTheDocument();
  });

  it("shows an error with retry when loading fails", async () => {
    const error = Object.assign(new Error("boom"), { status: 503, code: "persistence_unavailable" });
    listMock.mockRejectedValue(error);
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("boom")).toBeInTheDocument();
    listMock.mockResolvedValue(page());
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("INC-A1B2C3D4E5F60708")).toBeInTheDocument());
  });

  it("shows a data-gap indicator for insufficient-evidence incidents", async () => {
    listMock.mockResolvedValue(
      page([{ ...incidentFixture, status: "insufficient_evidence", confidence: 0 }]),
    );
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("data gaps")).toBeInTheDocument();
  });

  it("shows an overview strip derived from loaded incidents", async () => {
    listMock.mockResolvedValue(page());
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText("Critical severity")).toBeInTheDocument();
    expect(screen.getByText(/of 1 loaded/)).toBeInTheDocument();
  });

  it("sorts oldest first on request", async () => {
    const older = { ...incidentFixture, id: "INC-OLD", started_at: "2026-09-05T12:00:00Z" };
    listMock.mockResolvedValue(page([incidentFixture, older], 2));
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    await screen.findByText("INC-A1B2C3D4E5F60708");
    await userEvent.selectOptions(screen.getByLabelText("Sort"), "started_asc");
    const rows = screen.getAllByRole("row");
    // Header row first, then the older incident.
    expect(rows[1]?.textContent).toContain("INC-OLD");
  });

  it("filters by severity on the client", async () => {
    listMock.mockResolvedValue(page());
    render(
      <MemoryRouter>
        <IncidentListPage />
      </MemoryRouter>,
    );
    await screen.findByText("INC-A1B2C3D4E5F60708");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "critical");
    expect(screen.getByText("No incidents match this filter.")).toBeInTheDocument();
  });
});
