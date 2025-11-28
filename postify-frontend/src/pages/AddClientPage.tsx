import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE_URL } from "../services/api";
import axios from "axios";
import { Globe, Loader2 } from "lucide-react";

export const AddClientPage = () => {
    const [url, setUrl] = useState("");
    const [loading, setLoading] = useState(false);
    const [status, setStatus] = useState("");
    const navigate = useNavigate();

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!url) return;

        setLoading(true);
        setStatus("🕵️ Crawling website... (This takes about 30s)");

        try {
            const res = await axios.post(`${API_BASE_URL}/api/onboard`, { url });
            setStatus("✅ Analysis complete! Saving...");
            setTimeout(() => {
                navigate(`/clients/${res.data.client_id}`);
            }, 1000);
        } catch (err: any) {
            setStatus("❌ Failed: " + (err.response?.data?.detail || err.message));
            setLoading(false);
        }
    };

    return (
        <div style={{ maxWidth: "600px", margin: "0 auto", textAlign: "center", paddingTop: "4rem" }}>
            <h1 style={{ fontSize: "2rem", marginBottom: "1rem" }}>Add a new brand</h1>
            <p className="muted" style={{ marginBottom: "2rem" }}>
                Enter the website URL. AI will analyze the brand voice, services, and content DNA automatically.
            </p>

            <form onSubmit={handleSubmit} style={{ display: "flex", gap: "0.5rem" }}>
                <div style={{ position: "relative", flex: 1 }}>
                    <Globe size={18} style={{ position: "absolute", left: "12px", top: "14px", color: "#94a3b8" }} />
                    <input
                        type="url"
                        placeholder="https://example.com"
                        value={url}
                        onChange={e => setUrl(e.target.value)}
                        disabled={loading}
                        style={{
                            width: "100%", padding: "0.75rem 0.75rem 0.75rem 2.5rem",
                            borderRadius: "8px", border: "1px solid #cbd5e1", fontSize: "1rem"
                        }}
                        required
                    />
                </div>
                <button className="btn primary" disabled={loading} style={{ minWidth: "120px" }}>
                    {loading ? <Loader2 className="spin" size={20} /> : "Analyze"}
                </button>
            </form>

            {status && (
                <div style={{ marginTop: "2rem", padding: "1rem", background: "#f1f5f9", borderRadius: "8px", color: "#475569" }}>
                    {status}
                </div>
            )}

            <style>{`
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
      `}</style>
        </div>
    );
};
