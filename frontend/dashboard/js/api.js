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
  seo: (id) => req("GET", `/api/on-page/audits/${enc(id)}/seo`),
  discoverSitemaps: (inputs, maxUrls = 50000) => req("POST", "/api/on-page/sitemaps/discover", { inputs, max_urls: maxUrls }),
  sitemapPreview: (importId, sel) => req("POST", `/api/on-page/sitemaps/${enc(importId)}/preview`, { import_id: importId, ...sel }),
  profiles: () => req("GET", "/api/on-page/sitemap-profiles"),
  saveProfile: (p, id) => (id ? req("PUT", `/api/on-page/sitemap-profiles/${enc(id)}`, p) : req("POST", "/api/on-page/sitemap-profiles", p)),
  deleteProfile: (id) => req("DELETE", `/api/on-page/sitemap-profiles/${enc(id)}`),
  runProfile: (id) => req("POST", `/api/on-page/sitemap-profiles/${enc(id)}/run`),
  progress: (id) => req("GET", `/api/on-page/audits/${enc(id)}/progress`),
  pause: (id) => req("POST", `/api/on-page/audits/${enc(id)}/pause`),
  cancel: (id) => req("POST", `/api/on-page/audits/${enc(id)}/cancel`),
  score: (id) => req("GET", `/api/on-page/audits/${enc(id)}/score`),
  urls: (id, params) => req("GET", `/api/on-page/audits/${enc(id)}/urls?${new URLSearchParams(params)}`),
  urlDetail: (id, url) => req("GET", `/api/on-page/audits/${enc(id)}/url-detail?${new URLSearchParams({ url })}`),
  links: (id, params) => req("GET", `/api/on-page/audits/${enc(id)}/links?${new URLSearchParams(params)}`),
  images: (id, params) => req("GET", `/api/on-page/audits/${enc(id)}/images?${new URLSearchParams(params)}`),
  issueDetail: (id, key) => req("GET", `/api/on-page/audits/${enc(id)}/issues/${enc(key)}`),
  setIssueStatus: (id, key, status, note = "") => req("PATCH", `/api/on-page/audits/${enc(id)}/issues/${enc(key)}/status`, { status, note }),
  remediations: (id, params) => req("GET", `/api/jobs/${enc(id)}/remediations?${new URLSearchParams(params)}`),
  generateRemediations: (id) => req("POST", `/api/jobs/${enc(id)}/remediations/generate`),
  setRemediationStatus: (id, rid, status) => req("PATCH", `/api/jobs/${enc(id)}/remediations/${enc(rid)}`, { status }),
  fileText: (id, name) => req("GET", `/api/on-page/audits/${enc(id)}/files/${enc(name)}`),
  integrations: () => req("GET", "/api/integrations"),
  runIntegration: (iid, url, params = {}) => req("POST", `/api/integrations/${enc(iid)}/run`, { url, params }),
  googleUpdates: (q = {}) => req("GET", `/api/integrations/google-updates?${new URLSearchParams(q)}`),
  schemaTemplates: () => req("GET", "/api/integrations/schema-templates"),
  packageUrl: (id) => `/api/on-page/audits/${enc(id)}/package`,
  fileUrl: (id, name) => `/api/on-page/audits/${enc(id)}/files/${enc(name)}`,
};
