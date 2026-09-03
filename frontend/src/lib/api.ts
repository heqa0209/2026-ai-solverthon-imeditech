import type {
  AnnouncementDetail, AnnouncementListResponse, ApiErrorBody, CompanyProfileInput, CompanyResponse,
  InterestStatus, QueueResponse, Region, User,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE || "/api/v1").replace(/\/$/, "");
let csrfToken: string | null = null;

export class ApiError extends Error {
  constructor(public status: number, public body: ApiErrorBody) {
    super(body.message || `요청에 실패했습니다. (${status})`);
  }
}

type RequestOptions = Omit<RequestInit, "body"> & { body?: unknown; csrf?: boolean; redirectOnUnauthorized?: boolean };

async function parseError(response: Response): Promise<ApiErrorBody> {
  try { return (await response.json()) as ApiErrorBody; }
  catch { return { code: `HTTP_${response.status}`, message: `요청에 실패했습니다. (${response.status})` }; }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<{ data: T; response: Response }> {
  const headers = new Headers(options.headers);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.csrf) headers.set("X-CSRF-Token", await getCsrfToken());
  const response = await fetch(`${API_BASE}${path}`, {
    ...options, headers, credentials: "include",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    const error = new ApiError(response.status, await parseError(response));
    if (response.status === 401 && options.redirectOnUnauthorized !== false) window.dispatchEvent(new CustomEvent("app:unauthorized"));
    throw error;
  }
  const data = response.status === 204 ? undefined : await response.json();
  return { data: data as T, response };
}

export async function getCsrfToken(force = false): Promise<string> {
  if (csrfToken && !force) return csrfToken;
  const { data } = await request<{ csrfToken: string }>("/auth/csrf");
  csrfToken = data.csrfToken;
  return csrfToken;
}

export function clearCsrfToken() { csrfToken = null; }

export const api = {
  login: async (username: string, password: string) => {
    const result = await request<{ user: User }>("/auth/login", { method: "POST", body: { username, password }, redirectOnUnauthorized: false });
    await getCsrfToken(true);
    return result.data;
  },
  me: async () => (await request<{ user: User }>("/auth/me")).data,
  logout: async () => { await request<void>("/auth/logout", { method: "POST", csrf: true }); clearCsrfToken(); },
  company: async () => {
    const result = await request<CompanyResponse>("/company");
    return { ...result.data, etag: result.response.headers.get("ETag") || `"${result.data.version}"` };
  },
  updateCompany: async (input: CompanyProfileInput, etag: string) => {
    const result = await request<CompanyResponse>("/company", { method: "PUT", csrf: true, headers: { "If-Match": etag }, body: input });
    return { ...result.data, etag: result.response.headers.get("ETag") || `"${result.data.version}"` };
  },
  regions: async (query: string) => (await request<{ items: Region[] }>(`/regions?query=${encodeURIComponent(query)}`)).data.items,
  announcements: async (params: URLSearchParams) => (await request<AnnouncementListResponse>(`/announcements?${params.toString()}`)).data,
  announcement: async (id: string) => (await request<AnnouncementDetail>(`/announcements/${encodeURIComponent(id)}`)).data,
  setInterest: async (id: string, status: InterestStatus) => (await request<{ status: InterestStatus; updatedAt: string }>(`/announcements/${encodeURIComponent(id)}/interest`, { method: "PUT", csrf: true, body: { status } })).data,
  setRole: async (id: string, announcementVersionId: string, roleKey: string | null) => (await request<QueueResponse>(`/announcements/${encodeURIComponent(id)}/role`, { method: "PUT", csrf: true, body: { announcementVersionId, roleKey } })).data,
  answer: async (id: string, input: { announcementVersionId: string; conditionId: string; value: unknown; source: string; memo?: string }) => (await request<QueueResponse>(`/announcements/${encodeURIComponent(id)}/answers`, { method: "POST", csrf: true, body: input })).data,
  reevaluate: async (id: string, announcementVersionId: string) => (await request<QueueResponse>(`/announcements/${encodeURIComponent(id)}/reevaluate`, { method: "POST", csrf: true, body: { announcementVersionId } })).data,
};

export function fileUrl(announcementId: string, fileId: string) {
  return `${API_BASE}/announcements/${encodeURIComponent(announcementId)}/files/${encodeURIComponent(fileId)}`;
}
