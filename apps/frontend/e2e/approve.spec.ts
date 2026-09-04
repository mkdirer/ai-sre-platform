import { expect, test } from "@playwright/test";

const INCIDENT_ID = "INC-A1B2C3D4E5F60708";
const REC_ID = "REC-A1B2C3D4E5F6070811223344";
const LIVE = process.env.FRONTEND_E2E_LIVE === "1";

const incident = {
  id: INCIDENT_ID,
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

const recommendation = {
  id: REC_ID,
  action_type: "rollback_deployment",
  target: "payment-service",
  parameters: { deployment_id: "DEP-A1B2C3D4E5F607081122", version: "0.1.0" },
  rationale_evidence_ids: ["EVD-A1B2C3D4E5F6070811223344"],
  risk: "medium",
  reversible: true,
  requires_approval: true,
  status: "waiting_for_approval",
};

test("review a recommendation and approve it", async ({ page }) => {
  if (!LIVE) {
    await page.route("**/api/v1/incidents?**", (route) =>
      route.fulfill({ json: { items: [incident], total: 1, limit: 25, offset: 0 } }),
    );
    await page.route(`**/api/v1/incidents/${INCIDENT_ID}`, (route) =>
      route.fulfill({ json: incident }),
    );
    await page.route(`**/api/v1/incidents/${INCIDENT_ID}/report`, (route) =>
      route.fulfill({
        json: {
          id: "RPT-A1B2C3D4E5F6070811223344",
          incident_id: INCIDENT_ID,
          title: incident.title,
          affected_services: ["payment-service"],
          severity: "warning",
          summary: "deployment regression affecting payment-service",
          root_cause: "bad_deployment",
          root_cause_summary: "deployment regression affecting payment-service",
          confidence: 0.8,
          timeline: [],
          hypotheses: [],
          evidence_references: [],
          knowledge_references: [],
          recommendations: [recommendation],
          related_incident_ids: [],
          limitations: [],
          status: "waiting_for_approval",
          generated_at: "2026-09-06T12:05:00Z",
        },
      }),
    );
    // Register specific timeline paths before the generic evidence prefix so
    // Playwright first-match routing does not capture evidence/timeline calls.
    for (const path of ["evidence/timeline", "timeline", "hypotheses", "evidence"]) {
      const suffix = path === "evidence" ? "/evidence?*" : `/${path}*`;
      await page.route(`**/api/v1/incidents/${INCIDENT_ID}${suffix}`, (route) =>
        route.fulfill({ json: { items: [], total: 0, limit: 25, offset: 0 } }),
      );
    }
    await page.route(`**/api/v1/incidents/${INCIDENT_ID}/recommendations*`, (route) =>
      route.fulfill({ json: { items: [recommendation], total: 1, limit: 25, offset: 0 } }),
    );
    await page.route(`**/api/v1/recommendations/${REC_ID}/approve`, (route) =>
      route.fulfill({
        json: {
          approval: {
            id: "APR-A1B2C3D4E5F6070811223344",
            incident_id: INCIDENT_ID,
            recommendation_id: REC_ID,
            run_id: "7af2ffbd-50fe-42ae-b8be-58ca28fe3f8e",
            report_id: "RPT-A1B2C3D4E5F6070811223344",
            decision: "approved",
            actor: "local-demo-approver",
            incident_version: 3,
            idempotency_key: "e2e-key",
            created_at: "2026-09-06T12:06:00Z",
          },
          replayed: false,
        },
      }),
    );
  }

  await page.goto("/");
  await expect(page.getByText(INCIDENT_ID)).toBeVisible();
  await page.getByText(INCIDENT_ID).click();
  await expect(page.getByText("rollback deployment on payment-service")).toBeVisible();

  const approve = page.getByRole("button", { name: "Approve" });
  await expect(approve).toBeVisible();
  if (!LIVE) {
    // After approval the detail page reloads; replace the initial handler so
    // the approved state is served (stacked routes would leave this dead).
    await page.unroute(`**/api/v1/incidents/${INCIDENT_ID}/recommendations*`);
    await page.route(`**/api/v1/incidents/${INCIDENT_ID}/recommendations*`, (route) =>
      route.fulfill({
        json: {
          items: [{ ...recommendation, status: "approved" }],
          total: 1,
          limit: 25,
          offset: 0,
        },
      }),
    );
  }
  await approve.click();
  await expect(page.getByText("Approved. Awaiting a later remediation stage")).toBeVisible();
});
