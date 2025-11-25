import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ClientSummary, fetchClient } from "../services/api";

export const ClientOverviewPage = () => {
  const { clientId } = useParams<{ clientId: string }>();
  const [client, setClient] = useState<ClientSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!clientId) return;
    let cancelled = false;

    const load = async () => {
      try {
        const data = await fetchClient(clientId);
        if (!cancelled) {
          setClient(data);
        }
      } catch (err: any) {
        if (!cancelled) {
          setError(err?.message || "Failed to load client");
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
  }, [clientId]);

  if (loading) return <p>Loading client…</p>;
  if (error) return <p className="error-text">{error}</p>;
  if (!client) return <p>Client not found.</p>;

  const a = client.attributes || {};

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>{client.name}</h1>
          <p className="muted">
            {client.industry} · {client.city}
          </p>
        </div>
        <div className="page-header-actions">
          <Link to={`/clients/${client.id}/approvals`} className="btn primary">
            View approvals
          </Link>
        </div>
      </div>

      <div className="card-grid">
        <section className="card">
          <h2>Brand DNA</h2>
          <p>
            <strong>Theme:</strong>{" "}
            {a.content_theme || <span className="muted">Not set</span>}
          </p>
          <p>
            <strong>Pillars:</strong>{" "}
            {Array.isArray(a.content_pillars) && a.content_pillars.length ? (
              a.content_pillars.join(" · ")
            ) : (
              <span className="muted">Not set</span>
            )}
          </p>
          <p>
            <strong>Tone:</strong>{" "}
            {a.tone || <span className="muted">Not set</span>}
          </p>
          <p>
            <strong>Negative constraints:</strong>
            <br />
            {a.negative_constraints || (
              <span className="muted">None defined yet.</span>
            )}
          </p>
        </section>

        <section className="card">
          <h2>Posting rules</h2>
          <p>
            <strong>Suggested posts/week:</strong>{" "}
            {a.suggested_posts_per_week ?? <span className="muted">Not set</span>}
          </p>
          <p>
            <strong>Cooldown days:</strong>{" "}
            {a.cooldown_days ?? <span className="muted">Default</span>}
          </p>
          <p>
            <strong>Max posts/month:</strong>{" "}
            {a.max_posts_per_month ?? <span className="muted">Default</span>}
          </p>
          <p>
            <strong>Approval mode:</strong>{" "}
            {a.approval_mode || "always"}
          </p>
          <p>
            <strong>Timeout behaviour:</strong>{" "}
            {a.on_approval_timeout || "auto_post"}
          </p>
        </section>

        <section className="card">
          <h2>Ecommerce</h2>
          <p>
            <strong>Is ecommerce:</strong>{" "}
            {a.is_ecommerce ? "Yes" : "No / Not detected"}
          </p>
          <p>
            <strong>Platform:</strong>{" "}
            {a.ecommerce_platform || <span className="muted">Unknown</span>}
          </p>
          <p>
            <strong>Product categories:</strong>{" "}
            {Array.isArray(a.product_categories) &&
            a.product_categories.length ? (
              a.product_categories.join(" · ")
            ) : (
              <span className="muted">None detected</span>
            )}
          </p>
        </section>
      </div>

      {/* Later: controls to override Brand DNA & posting rules */}
    </div>
  );
};
