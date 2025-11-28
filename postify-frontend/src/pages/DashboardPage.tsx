import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ClientSummary, fetchClients } from "../services/api";

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

  const getClientImage = (c: ClientSummary) => {
    // 1. Try Hero Image from DB
    if (c.attributes.hero_image_url) return c.attributes.hero_image_url;

    // 2. Try Favicon from Website (Google Service)
    if (c.attributes.website) {
      // Remove protocol for cleaner domain usage if needed, but Google handles full URLs well
      return `https://www.google.com/s2/favicons?domain=${c.attributes.website}&sz=256`;
    }

    // 3. Fallback Placeholder
    return "https://via.placeholder.com/150?text=" + c.name.charAt(0);
  };

  if (loading) return <p>Loading...</p>;
  if (error) return <p className="error-text">{error}</p>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "2rem" }}>
        <div>
          <h1 style={{ marginBottom: "0.5rem" }}>Your brands</h1>
          <p className="muted">Overview of all active clients.</p>
        </div>
        {/* We will wire this up next */}
        <button className="btn primary" disabled>+ Add Brand</button>
      </div>

      <div className="card-grid">
        {clients.map((c) => (
          <Link key={c.id} to={`/clients/${c.id}`} className="card card-clickable" style={{ padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>

            {/* Image Area */}
            <div style={{
              height: "140px",
              width: "100%",
              background: "#f1f5f9",
              backgroundImage: `url(${getClientImage(c)})`,
              backgroundSize: "cover",
              backgroundPosition: "center"
            }} />

            {/* Content Area */}
            <div style={{ padding: "1.25rem" }}>
              <h3 style={{ marginTop: 0, marginBottom: "0.25rem" }}>{c.name}</h3>
              <p className="muted" style={{ fontSize: "0.85rem", marginBottom: "1rem" }}>
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
