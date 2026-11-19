// QC V5 Dashboard — API client (same-origin; the dashboard is served by the FastAPI app).
async function req(method, path, body, isForm = false) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await r.json() : await r.text();
  if (!r.ok) {
    const err = new Error((data && data.detail) || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

const enc = encodeURIComponent;
export const api = {
  presets: () => req("GET", "/api/on-page/presets"),
  estimate: (pages, rps, imagesOnPrimary = false) =>
    req("GET", `/api/on-page/estimate?pages=${pages}&rps=${rps}&images_on_primary=${imagesOnPrimary}`),
  audits: () => req("GET", "/api/on-page/audits"),
  audit: (id) => req("GET", `/api/on-page/audits/${enc(id)}`),
  start: (payload) => req("POST", "/api/on-page/audits", payload),
  upload: (form) => req("POST", "/api/on-page/audits/upload", form, true),
  stop: (id) => req("POST", `/api/on-page/audits/${enc(id)}/stop`),
  resume: (id) => req("POST", `/api/on-page/audits/${enc(id)}/resume`),
  insights: (id) => req("GET", `/api/on-page/audits/${enc(id)}/insights`),
  overview: (id) => req("GET", `/api/on-page/audits/${enc(id)}/issues/overview`),
  issues: (id, params) => req("GET", `/api/on-page/audits/${enc(id)}/issues?${new URLSearchParams(params)}`),
  pages: (id, limit = 15) => req("GET", `/api/on-page/audits/${enc(id)}/pages?limit=${limit}`),
  packageUrl: (id) => `/api/on-page/audits/${enc(id)}/package`,
  fileUrl: (id, name) => `/api/on-page/audits/${enc(id)}/files/${enc(name)}`,
};
