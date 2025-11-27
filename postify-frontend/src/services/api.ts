import axios from "axios";

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8080";

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 10000,
});

export interface ClientSummary {
  id: string;
  name: string;
  industry: string;
  city: string;
  attributes: Record<string, any>;
}

export interface PostCandidate {
  id: number;
  client_id: string;
  template_key: string;
  text_body: string;
  media_url: string | null;
  slot_time: string;
  status: string;
  rejection_reason: string | null;
  metadata: Record<string, any>;
}

/**
 * Fetch all clients visible to the logged-in user.
 * Backend: e.g. GET /api/clients
 */
export async function fetchClients(): Promise<ClientSummary[]> {
  const res = await api.get("/api/clients");
  return res.data.clients || res.data || [];
}

/**
 * Fetch a single client by ID.
 * Backend: e.g. GET /api/clients/{client_id}
 */
export async function fetchClient(clientId: string): Promise<ClientSummary> {
  const res = await api.get(`/api/clients/${clientId}`);
  return res.data;
}

/**
 * Fetch pending post candidates for a client.
 * Backend: e.g. GET /api/clients/{client_id}/candidates?status=PENDING
 */
export async function fetchPendingCandidates(
  clientId: string
): Promise<PostCandidate[]> {
  const res = await api.get(`/api/clients/${clientId}/candidates`, {
    params: { status: "PENDING" },
  });
  return res.data.candidates || res.data || [];
}

/**
 * Approve a candidate.
 * Backend: e.g. POST /api/candidates/{id}/approve
 */
export async function approveCandidate(id: number): Promise<void> {
  await api.post(`/api/candidates/${id}/approve`);
}

/**
 * Reject a candidate, optionally with a reason.
 * Backend: e.g. POST /api/candidates/{id}/reject
 */
export async function rejectCandidate(
  id: number,
  reason?: string
): Promise<void> {
  await api.post(`/api/candidates/${id}/reject`, { reason });
}

export async function generatePost(clientId: string): Promise<void> {
  await api.post(`/api/clients/${clientId}/generate`);
}
