import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { type ClientSummary, fetchClient, API_BASE_URL } from "../services/api";
import axios from "axios"; // Ensure axios is imported

export const ClientSettingsPage = () => {
  const { clientId } = useParams<{ clientId: string }>();
  const [client, setClient] = useState<ClientSummary | null>(null);
  const [loading, setLoading] = useState(true);

  // Helper to refresh client data after an action
  const refreshClient = () => {
    if (!clientId) return;
    fetchClient(clientId).then(setClient);
  };

  useEffect(() => {
    if (!clientId) return;
    fetchClient(clientId)
      .then(setClient)
      .catch((err) => alert(err.message))
      .finally(() => setLoading(false));
  }, [clientId]);

  const connectPage = async (page: any) => {
    if (!client || !clientId) return;
    if (!confirm(`Connect the page "${page.name}" to this client?`)) return;

    try {
      // We save the specific page token as the "official" one
      await axios.post(`${API_BASE_URL}/clients/${clientId}/attributes/merge`, {
        facebook_page_id: page.id,
        facebook_page_name: page.name,
        facebook_page_token: page.access_token,
        facebook_candidates: null // Clear the list to clean up UI
      });
      alert("Facebook Page Connected!");
      refreshClient();
    } catch (e) {
      alert("Failed to save page selection.");
    }
  };

  if (loading) return <p>Loading...</p>;
  if (!client) return <p>Client not found</p>;

  const getLoginLink = (platform: string) =>
    `${API_BASE_URL}/auth/${platform}/login?client_id=${client.id}`;

  const attrs = client.attributes || {};
  const fbCandidates = attrs.facebook_candidates || [];

  return (
    <div>
      <div className="page-header">
        <h1>Settings · {client.name}</h1>
        <div className="page-header-actions">
          <Link to={`/clients/${client.id}`} className="btn">Back to Overview</Link>
        </div>
      </div>

      <div className="card">
        <h2>Social Connections</h2>
        <p className="muted">Connect your social accounts to allow the bot to post automatically.</p>

        <div className="card-column" style={{ marginTop: "1rem" }}>

          {/* X / Twitter */}
          <div className="connection-row" style={rowStyle}>
            <div>
              <strong>X (Twitter)</strong>
              {attrs.x_access_token ? (
                <div style={connectedStyle}>✅ Connected</div>
              ) : (
                <div style={disconnectedStyle}>Not connected</div>
              )}
            </div>
            {attrs.x_access_token ? (
              <button className="btn" disabled>Connected</button>
            ) : (
              <a href={getLoginLink("x")} className="btn primary">Connect X</a>
            )}
          </div>

          {/* LinkedIn */}
          <div className="connection-row" style={rowStyle}>
            <div>
              <strong>LinkedIn</strong>
              {attrs.linkedin_access_token ? (
                <div style={connectedStyle}>✅ Connected</div>
              ) : (
                <div style={disconnectedStyle}>Not connected</div>
              )}
            </div>
            {attrs.linkedin_access_token ? (
              <button className="btn" disabled>Connected</button>
            ) : (
              <a href={getLoginLink("linkedin")} className="btn primary">Connect LinkedIn</a>
            )}
          </div>

          {/* Facebook */}
          <div className="connection-row" style={rowStyle}>
            <div>
              <strong>Facebook</strong>
              {attrs.facebook_page_token ? (
                <div style={connectedStyle}>✅ Connected to {attrs.facebook_page_name}</div>
              ) : (
                <div style={disconnectedStyle}>Not connected</div>
              )}
            </div>
            {attrs.facebook_page_token ? (
              <button className="btn" disabled>Connected</button>
            ) : (
              <a href={getLoginLink("facebook")} className="btn primary">Connect Facebook</a>
            )}
          </div>

          {/* Facebook Page Selector (Shows up after login if multiple pages found) */}
          {fbCandidates.length > 0 && (
            <div style={{ background: "#f8fafc", padding: "1rem", borderRadius: "8px", border: "1px dashed #cbd5e1" }}>
              <p style={{ margin: "0 0 0.5rem 0", fontWeight: 600 }}>Select a Facebook Page:</p>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                {fbCandidates.map((p: any) => (
                  <button key={p.id} className="btn" onClick={() => connectPage(p)}>
                    {p.name}
                  </button>
                ))}
              </div>
            </div>
          )}

        </div>
      </div>
    </div>
  );
};

const rowStyle = { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "1rem", border: "1px solid #eee", borderRadius: "8px" };
const connectedStyle = { color: "green", fontSize: "0.85rem" };
const disconnectedStyle = { color: "#666", fontSize: "0.85rem" };
