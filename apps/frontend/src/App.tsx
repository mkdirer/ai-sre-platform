import { Link, NavLink, Route, Routes } from "react-router-dom";
import { IncidentDetailPage } from "./pages/IncidentDetailPage";
import { IncidentListPage } from "./pages/IncidentListPage";

export function App() {
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            AI SRE Incidents
          </Link>
          <nav className="topnav" aria-label="Primary">
            <NavLink to="/" end>
              Incidents
            </NavLink>
          </nav>
          <span className="env-chip" title="This UI is served with the local demo stack">
            local demo
          </span>
        </div>
      </header>
      <div className="container">
        <main id="main-content">
          <Routes>
            <Route path="/" element={<IncidentListPage />} />
            <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </div>
    </>
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
