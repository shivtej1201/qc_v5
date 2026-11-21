// Typed client for /api/v2. Errors carry the server's plain-language title/detail.
export class ApiError extends Error {
  constructor(public status: number, public title: string, public detail: string) { super(detail || title); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const user = localStorageGet("qc6-user");
  if (user) headers["X-QC-User"] = user;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const d = (typeof data === "object" && data) || {};
    throw new ApiError(r.status, (d as any).title || `Request failed (${r.status})`, (d as any).detail || "");
  }
  return data as T;
}

export function localStorageGet(k: string): string | null {
  try { return window.localStorage.getItem(k); } catch { return null; }
}
export function localStorageSet(k: string, v: string): void {
  try { window.localStorage.setItem(k, v); } catch { /* storage blocked */ }
}

export type Severity = "critical" | "high" | "medium" | "low";
export type FixStatus = "open" | "in_progress" | "fixed_pending_check" | "verified" | "wont_fix";
export interface Site { id: number; name: string; base_url: string; primary_host: string; settings: Record<string, any>;
  score?: number | null; open_fixes?: number; last_run?: Run | null }
export interface Fix { id: number; rule: string; severity: Severity; dimension: string; scope: string; title: string; status: FixStatus;
  pages_affected: number; template_name?: string | null; owner_id?: number | null; owner_name?: string | null; regressed: boolean;
  texts: { title: string; why: string; fix: string; verify: string }; example_url?: string | null; source: string;
  ticket_url?: string | null; updated_at: string }
export interface Run { id: number; kind: string; status: string; progress: Record<string, any>; summary: Record<string, any>;
  error?: string | null; created_at: string; started_at?: string | null; finished_at?: string | null }
export interface Member { id: number; name: string; email: string; role: string }
export interface TodayCard { kind: string; title: string; text: string; action: string; fix_id?: number; journey_run_id?: number }
export interface Today { greeting: string; lede: string; cards: TodayCard[]; score: number | null; trend: { score: number; at: string }[];
  checked: string; last_run: Run | null; fixes: { open: number; waiting: number; verified: number }; site: Site }
export interface Step { idx: number; action: string; label: string; status: string; detail: string; screenshot?: string | null; duration_ms: number }
export interface Journey { id: number; name: string; device: string; enabled: boolean; source_yaml: string;
  recent: { id: number; status: string; failed_step: number | null; duration_ms: number; started_at: string }[] }

export const api = {
  me: () => req<{ member: Member; workspace: { id: number; name: string } }>("GET", "/api/v2/me"),
  members: () => req<{ members: Member[] }>("GET", "/api/v2/members"),
  sites: () => req<{ sites: Site[] }>("GET", "/api/v2/sites"),
  createSite: (b: { name: string; base_url: string }) => req<Site>("POST", "/api/v2/sites", b),
  today: (sid: number) => req<Today>("GET", `/api/v2/sites/${sid}/today`),
  fixes: (sid: number, view: string, q: string) =>
    req<{ fixes: Fix[]; counts: Record<string, number> }>("GET", `/api/v2/sites/${sid}/fixes?${new URLSearchParams({ view, q })}`),
  fix: (id: number) => req<{ fix: Fix; pages: string[]; examples: { page_url: string; screenshot: string }[];
    events: { kind: string; detail: any; created_at: string; actor: string | null }[] }>("GET", `/api/v2/fixes/${id}`),
  patchFix: (id: number, b: Record<string, unknown>) => req<Fix>("PATCH", `/api/v2/fixes/${id}`, b),
  runs: (sid: number) => req<{ runs: Run[] }>("GET", `/api/v2/sites/${sid}/runs`),
  run: (id: number) => req<{ run: Run; recent_visits: any[]; found: { id: number; title: string; severity: Severity; pages: number }[] }>("GET", `/api/v2/runs/${id}`),
  startRun: (sid: number, kind: string, options: Record<string, unknown> = {}) =>
    req<{ run_id: number }>("POST", `/api/v2/sites/${sid}/runs`, { kind, options }),
  templates: (sid: number) => req<{ templates: { id: number; name: string; path_pattern: string; page_count: number; visits: number; open_fixes: number }[] }>("GET", `/api/v2/sites/${sid}/templates`),
  pages: (sid: number, q: string) => req<{ pages: { url: string; http_status: number; title: string; template_name: string }[] }>("GET", `/api/v2/sites/${sid}/pages?${new URLSearchParams({ q })}`),
  page: (sid: number, url: string) => req<{ page: any; visit: any; fixes: { id: number; title: string; severity: Severity; status: string }[] }>("GET", `/api/v2/sites/${sid}/page?${new URLSearchParams({ url })}`),
  journeys: (sid: number) => req<{ journeys: Journey[] }>("GET", `/api/v2/sites/${sid}/journeys`),
  saveJourney: (sid: number, yaml: string) => req<{ journey_id: number }>("POST", `/api/v2/sites/${sid}/journeys`, { yaml }),
  journeyRun: (id: number) => req<{ journey_run: any; steps: Step[] }>("GET", `/api/v2/journey-runs/${id}`),
  access: (sid: number) => req<{ header: string; token: string; ua_suffix: string; rules: Record<string, string> }>("GET", `/api/v2/sites/${sid}/access`),
  accessTest: (sid: number) => req<{ ok: boolean; text: string; results: string[] }>("POST", `/api/v2/sites/${sid}/access/test`),
  integrations: () => req<{ integrations: any[]; not_included: { name: string; reason: string }[]; attribution: string }>("GET", "/api/v2/integrations"),
  runIntegration: (id: string, url: string, params: Record<string, unknown> = {}) => req<any>("POST", `/api/v2/integrations/${id}/run`, { url, params }),
  objectUrl: (name: string) => `/api/v2/objects/${encodeURIComponent(name)}`,
  pauseRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/pause`),
  resumeRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/resume`),
  cancelRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/stop`),
  facts: (runId: number, dataset: string, params: Record<string, string | number>) =>
    req<{ total: number; rows: any[]; summary: { n_rows?: number; rows?: number; n_unique?: number; n_broken?: number; n_redirects?: number } }>(
      "GET", `/api/v2/runs/${runId}/facts/${dataset}?${new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))}`),
  factsCsvUrl: (runId: number, dataset: string, filter: string) => `/api/v2/runs/${runId}/export/${dataset}.csv?${new URLSearchParams({ filter })}`,
  patchSite: (sid: number, b: Record<string, unknown>) => req<Site>("PATCH", `/api/v2/sites/${sid}`, b),
  site: (sid: number) => req<Site>("GET", `/api/v2/sites/${sid}`),
  testAlert: (sid: number) => req<{ sent: { channel: string; ok: boolean; status?: number; error?: string }[] }>("POST", `/api/v2/sites/${sid}/alerts/test`),
  convertRecording: (sid: number, code: string, name: string, device: string) =>
    req<{ yaml: string; skipped: string[]; origin: string }>("POST", `/api/v2/sites/${sid}/journeys/convert`, { code, name, device }),
  createTicket: (fixId: number) => req<{ url: string; key: string }>("POST", `/api/v2/fixes/${fixId}/ticket`),
  syncTickets: (sid: number) => req<{ moved_to_waiting: number }>("POST", `/api/v2/sites/${sid}/tickets/sync`),
};
