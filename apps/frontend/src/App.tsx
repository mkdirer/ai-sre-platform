import { Link, Route, Routes } from "react-router-dom";
import { IncidentDetailPage } from "./pages/IncidentDetailPage";
import { IncidentListPage } from "./pages/IncidentListPage";

export function App() {
  return (
    <div className="app">
      <header className="app-header">
        <nav aria-label="Primary">
          <Link to="/" className="brand">
            AI SRE Incidents
          </Link>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<IncidentListPage />} />
          <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}

function NotFound() {
  return (
    <section aria-labelledby="not-found-title">
      <h1 id="not-found-title">Page not found</h1>
      <p>
        <Link to="/">Back to incidents</Link>
      </p>
    </section>
  );
}
