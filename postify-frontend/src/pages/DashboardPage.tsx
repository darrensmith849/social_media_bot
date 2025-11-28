import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchClients } from "../services/api";
import type { ClientSummary } from "../services/api";
import { Plus } from "lucide-react";

export const DashboardPage = () => {
  const [clients, setClients] = useState<ClientSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchClients()
      .then(setClients)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const getFavicon = (c: ClientSummary) => {
    if (c.attributes.website) {
      return `https://www.google.com/s2/favicons?domain=${c.attributes.website}&sz=64`;
    }
    return "https://via.placeholder.com/32?text=" + c.name.charAt(0);
  };

  if (loading) return <p>Loading...</p>;
  if (error) return <p className="error-text">{error}</p>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "2rem" }}>
        <div>
          <h1 style={{ marginBottom: "0.25rem", fontSize: "1.5rem" }}>Your brands</h1>
          <p className="muted">Overview of all active clients.</p>
        </div>
        <button className="btn primary" disabled style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <Plus size={16} /> Add Brand
        </button>
      </div>

      <div className="card-grid">
        {clients.map((c) => (
          <Link key={c.id} to={`/clients/${c.id}`} className="card card-clickable" style={{ display: "flex", alignItems: "start", gap: "1rem" }}>

            {/* Small Logo */}
            <img
              src={getFavicon(c)}
              alt={c.name}
              style={{ width: "36px", height: "36px", borderRadius: "6px", objectFit: "contain", border: "1px solid #eee" }}
            />

            {/* Content Area */}
            <div>
              <h3 style={{ margin: "0 0 0.25rem 0", fontSize: "1.1rem" }}>{c.name}</h3>
              <p className="muted" style={{ fontSize: "0.85rem", marginBottom: "0.5rem" }}>
                {c.industry} · {c.city}
              </p>

              <div className="pill-row">
                {c.attributes?.tone && <span className="pill">{c.attributes.tone}</span>}
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
};
