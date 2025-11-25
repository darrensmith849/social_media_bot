import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ClientSummary, fetchClients } from "../services/api";

export const DashboardPage = () => {
  const [clients, setClients] = useState<ClientSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const data = await fetchClients();
        if (!cancelled) {
          setClients(data);
        }
      } catch (err: any) {
        if (!cancelled) {
          setError(err?.message || "Failed to load clients");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <p>Loading your brands…</p>;
  if (error) return <p className="error-text">{error}</p>;
  if (!clients.length) {
    return (
      <div>
        <h1>Your brands</h1>
        <p>No clients yet. Once you add brands in the backend, they’ll appear here.</p>
      </div>
    );
  }

  return (
    <div>
      <h1>Your brands</h1>
      <p className="muted">
        Each brand has its own content brain, posting rules, and approvals.
      </p>

      <div className="card-grid">
        {clients.map((c) => (
          <Link
            key={c.id}
            to={`/clients/${c.id}`}
            className="card card-clickable"
          >
            <h2>{c.name}</h2>
            <p className="muted">
              {c.industry} · {c.city}
            </p>
            <p className="pill-row">
              {c.attributes?.content_theme && (
                <span className="pill">
                  {c.attributes.content_theme}
                </span>
              )}
              {Array.isArray(c.attributes?.content_pillars) &&
                c.attributes.content_pillars.slice(0, 3).map((p: string) => (
                  <span key={p} className="pill pill-soft">
                    {p}
                  </span>
                ))}
            </p>
          </Link>
        ))}
      </div>
    </div>
  );
};
