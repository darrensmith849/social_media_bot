import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE_URL } from "../services/api";
import axios from "axios";
import { Globe, Loader2, CheckCircle2 } from "lucide-react";

const LOADING_MSGS = [
    "📡 Connecting to website...",
    "nw Downloading page content...",
    "🧠 Analyzing brand voice & tone...",
    "🔍 Identifying core services...",
    "💎 Extracting content pillars...",
    "🛍️ Checking e-commerce capabilities...",
    "✨ Finalizing Brand DNA...",
    "💾 Saving to database..."
];

export const AddClientPage = () => {
    const [url, setUrl] = useState("");
    const [loading, setLoading] = useState(false);
    const [msgIndex, setMsgIndex] = useState(0);
    const [status, setStatus] = useState(""); // For final success/error
    const navigate = useNavigate();

    // Cycle through messages while loading
    useEffect(() => {
        if (!loading) return;
        setMsgIndex(0);
        const interval = setInterval(() => {
            setMsgIndex((prev) => (prev + 1) % LOADING_MSGS.length);
        }, 3500);
        return () => clearInterval(interval);
    }, [loading]);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!url) return;

        setLoading(true);
        setStatus(""); // Clear previous errors

        try {
            const res = await axios.post(`${API_BASE_URL}/api/onboard`, { url });
            setLoading(false);
            setStatus("SUCCESS");
            setTimeout(() => {
                navigate(`/clients/${res.data.client_id}`);
            }, 1000);
        } catch (err: any) {
            setLoading(false);
            setStatus("ERROR: " + (err.response?.data?.detail || err.message));
        }
    };

    return (
        <div style={{ maxWidth: "600px", margin: "0 auto", textAlign: "center", paddingTop: "6rem" }}>
            <h1 style={{ fontSize: "2rem", marginBottom: "1rem", fontWeight: "800" }}>Add a new brand</h1>
            <p className="muted" style={{ marginBottom: "2.5rem", fontSize: "1.1rem" }}>
                Enter the website URL. AI will analyze the brand voice, services, and content DNA automatically.
            </p>

            <form onSubmit={handleSubmit} style={{ display: "flex", gap: "0.75rem", maxWidth: "500px", margin: "0 auto" }}>
                <div style={{ position: "relative", flex: 1 }}>
                    <Globe size={18} style={{ position: "absolute", left: "14px", top: "16px", color: "#94a3b8" }} />
                    <input
                        type="url"
                        placeholder="https://example.com"
                        value={url}
                        onChange={e => setUrl(e.target.value)}
                        disabled={loading}
                        style={{
                            width: "100%", padding: "0.85rem 0.85rem 0.85rem 2.75rem",
                            borderRadius: "12px", border: "1px solid #cbd5e1", fontSize: "1rem",
                            boxShadow: "0 2px 4px rgba(0,0,0,0.02)"
                        }}
                        required
                    />
                </div>
                <button className="btn primary" disabled={loading} style={{ minWidth: "120px", borderRadius: "12px", fontWeight: "600" }}>
                    {loading ? <Loader2 className="spin" size={20} /> : "Analyze"}
                </button>
            </form>

            <div style={{ marginTop: "3rem", minHeight: "60px" }}>
                {loading && (
                    <div className="fade-in" key={msgIndex} style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "10px", color: "#64748b" }}>
                        <Loader2 className="spin" size={18} />
                        <span>{LOADING_MSGS[msgIndex]}</span>
                    </div>
                )}

                {status === "SUCCESS" && (
                    <div style={{ color: "green", display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", fontWeight: "600" }}>
                        <CheckCircle2 size={20} />
                        <span>Analysis complete! Redirecting...</span>
                    </div>
                )}

                {status.startsWith("ERROR") && (
                    <div style={{ color: "#ef4444", background: "#fef2f2", padding: "1rem", borderRadius: "8px", display: "inline-block" }}>
                        {status}
                    </div>
                )}
            </div>

            <style>{`
                .spin { animation: spin 1s linear infinite; }
                @keyframes spin { 100% { transform: rotate(360deg); } }
                .fade-in { animation: fadeIn 0.5s ease-in-out; }
                @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
            `}</style>
        </div>
    );
};
