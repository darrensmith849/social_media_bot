import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchClient, API_BASE_URL } from "../services/api";
import type { ClientSummary } from "../services/api";
import axios from "axios";

export const ClientSettingsPage = () => {
  const { clientId } = useParams<{ clientId: string }>();
  const [client, setClient] = useState<ClientSummary | null>(null);
  const [loading, setLoading] = useState(true);

  // Form State
  const [tone, setTone] = useState("");
  const [tips, setTips] = useState("");
  const [myths, setMyths] = useState("");
  const [story, setStory] = useState("");

  useEffect(() => {
    if (!clientId) return;
    fetchClient(clientId)
      .then((data) => {
        setClient(data);
        // Pre-fill form
        const a = data.attributes || {};
        setTone(a.tone || "");
        setTips(Array.isArray(a.tips) ? a.tips.join("\n") : "");
        setMyths(Array.isArray(a.myths) ? a.myths.join("\n") : "");
        setStory(a.content_atoms?.story_mission || "");
      })
      .catch((err) => alert(err.message))
      .finally(() => setLoading(false));
  }, [clientId]);

  const handleSaveDNA = async () => {
    if (!clientId) return;
    try {
      // Split text areas into lists
      const tipsList = tips.split("\n").map(s => s.trim()).filter(Boolean);
      const mythsList = myths.split("\n").map(s => s.trim()).filter(Boolean);

      const payload = {
        tone,
        tips: tipsList,
        myths: mythsList,
        content_atoms: {
          ...(client?.attributes?.content_atoms || {}),
          story_mission: story
        }
      };

      await axios.post(`${API_BASE_URL}/api/clients/${clientId}/attributes/merge`, payload);
      alert("✅ Brand DNA Saved!");
      // Refresh
      const updated = await fetchClient(clientId);
      setClient(updated);
    } catch (e: any) {
      alert("Failed to save: " + e.message);
    }
  };

  if (loading) return <p>Loading...</p>;
  if (!client) return <p>Client not found</p>;

  const getLoginLink = (platform: string) =>
    `${API_BASE_URL}/auth/${platform}/login?client_id=${client.id}`;

  const attrs = client.attributes || {};

  return (
    <div>
      <div className="page-header">
        <h1>Settings · {client.name}</h1>
        <div className="page-header-actions">
          <Link to={`/clients/${client.id}/approvals`} className="btn">Back to Approvals</Link>
          <Link to={`/clients/${client.id}`} className="btn">Back to Overview</Link>
        </div>
      </div>

      <div className="card-grid">
        {/* LEFT COLUMN: SOCIAL CONNECTIONS */}
        <div className="card">
          <h2>Social Connections</h2>
          <p className="muted">Connect accounts to enable auto-posting.</p>

          <div className="card-column" style={{ marginTop: "1rem" }}>
            {/* X / Twitter */}
            <div className="connection-row">
              <div>
                <strong>X (Twitter)</strong>
                {attrs.x_access_token ? <div className="conn-active">✅ Connected</div> : <div className="conn-inactive">Not connected</div>}
              </div>
              {attrs.x_access_token ? <button className="btn" disabled>Connected</button> : <a href={getLoginLink("x")} className="btn primary">Connect X</a>}
            </div>

            {/* LinkedIn */}
            <div className="connection-row">
              <div>
                <strong>LinkedIn</strong>
                {attrs.linkedin_access_token ? <div className="conn-active">✅ Connected</div> : <div className="conn-inactive">Not connected</div>}
              </div>
              {attrs.linkedin_access_token ? <button className="btn" disabled>Connected</button> : <a href={getLoginLink("linkedin")} className="btn primary">Connect LinkedIn</a>}
            </div>

            {/* Facebook */}
            <div className="connection-row">
              <div>
                <strong>Facebook</strong>
                {attrs.facebook_page_token ? <div className="conn-active">✅ Connected</div> : <div className="conn-inactive">Not connected</div>}
              </div>
              {attrs.facebook_page_token ? <button className="btn" disabled>Connected</button> : <a href={getLoginLink("facebook")} className="btn primary">Connect Facebook</a>}
            </div>
          </div>
        </div>

        {/* RIGHT COLUMN: BRAND DNA EDITING */}
        <div className="card">
          <h2>Edit Brand DNA</h2>
          <p className="muted">Required for generating posts.</p>

          <div className="card-column" style={{ marginTop: "1rem" }}>

            <label className="label">
              <strong>Tone of Voice</strong>
              <input
                className="input"
                value={tone}
                onChange={e => setTone(e.target.value)}
                placeholder="e.g. Professional, Friendly, Witty"
              />
            </label>

            <label className="label">
              <strong>Tips (One per line)</strong>
              <textarea
                className="input"
                rows={4}
                value={tips}
                onChange={e => setTips(e.target.value)}
                placeholder="e.g. Drink more water&#10;Stretch daily"
              />
            </label>

            <label className="label">
              <strong>Myths (One per line)</strong>
              <textarea
                className="input"
                rows={4}
                value={myths}
                onChange={e => setMyths(e.target.value)}
                placeholder="e.g. Carbs are bad&#10;Lifting makes you bulky"
              />
            </label>

            <label className="label">
              <strong>Mission / Origin Story</strong>
              <textarea
                className="input"
                rows={3}
                value={story}
                onChange={e => setStory(e.target.value)}
                placeholder="e.g. We started in 2010 to help..."
              />
            </label>

            <button className="btn primary" onClick={handleSaveDNA} style={{ alignSelf: "flex-start" }}>
              💾 Save Brand DNA
            </button>
          </div>
        </div>
      </div>

      {/* Styles inline for simplicity in this file */}
      <style>{`
        .connection-row { display: flex; align-items: center; justify-content: space-between; padding: 1rem; border: 1px solid #eee; borderRadius: 8px; }
        .conn-active { color: green; font-size: 0.85rem; }
        .conn-inactive { color: #666; font-size: 0.85rem; }
        .label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.9rem; }
        .input { padding: 0.5rem; border: 1px solid #ccc; border-radius: 6px; font-family: inherit; }
      `}</style>
    </div>
  );
};
