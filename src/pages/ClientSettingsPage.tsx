import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { ClientSummary, fetchClient, API_BASE_URL } from "../services/api";

export const ClientSettingsPage = () => {
  const { clientId } = useParams<{ clientId: string }>();
  const [client, setClient] = useState<ClientSummary | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!clientId) return;
    fetchClient(clientId)
      .then(setClient)
      .catch((err) => alert(err.message))
      .finally(() => setLoading(false));
  }, [clientId]);

  if (loading) return <p>Loading...</p>;
  if (!client) return <p>Client not found</p>;

  // Helper to build login links
  // This sends the user to your Python Backend to start the OAuth dance
  const getLoginLink = (platform: string) => 
    `${API_BASE_URL}/auth/${platform}/login?client_id=${client.id}`;

  const attrs = client.attributes || {};

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
          
          {/* X / Twitter Button */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "1rem", border: "1px solid #eee", borderRadius: "8px" }}>
            <div>
              <strong>X (Twitter)</strong>
              {attrs.x_access_token ? (
                <div style={{ color: "green", fontSize: "0.85rem" }}>✅ Connected</div>
              ) : (
                 <div style={{ color: "#666", fontSize: "0.85rem" }}>Not connected</div>
              )}
            </div>
            {attrs.x_access_token ? (
               <button className="btn" disabled>Connected</button>
            ) : (
               <a href={getLoginLink("x")} className="btn primary">Connect X</a>
            )}
          </div>

          {/* Placeholders for Next Week */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "1rem", border: "1px solid #eee", borderRadius: "8px", opacity: 0.5 }}>
            <div><strong>LinkedIn</strong> (Coming Soon)</div>
            <button className="btn" disabled>Connect</button>
          </div>
          
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "1rem", border: "1px solid #eee", borderRadius: "8px", opacity: 0.5 }}>
            <div><strong>Facebook</strong> (Coming Soon)</div>
            <button className="btn" disabled>Connect</button>
          </div>

        </div>
      </div>
    </div>
  );
};
