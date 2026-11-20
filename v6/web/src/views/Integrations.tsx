import { useState } from "react";
import { api, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, Sev, useLoad } from "../ui";

const SEV_MAP: Record<string, "critical" | "high" | "medium" | "low"> = { HIGH: "high", MEDIUM: "medium", LOW: "low", INFO: "low" };

export function IntegrationsView({ site }: { site: Site; params: URLSearchParams }) {
  const cat = useLoad(() => api.integrations(), []);
  const ref = useViewMotion<HTMLDivElement>([!!cat.data]);
  const [url, setUrl] = useState(site.base_url + "/");
  const [results, setResults] = useState<Record<string, any>>({});

  const run = async (id: string) => {
    setResults((r) => ({ ...r, [id]: { status: "running" } }));
    try { const out = await api.runIntegration(id, url); setResults((r) => ({ ...r, [id]: out })); }
    catch (e: any) { setResults((r) => ({ ...r, [id]: { status: "error", error: e.detail || e.message } })); }
  };

  if (cat.error) return <ErrorBox text={cat.error} onRetry={cat.reload} />;
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Integrations</h1>
          <p className="lede">Free services that add search and real-user data to what we measure ourselves.</p>
        </div>
      </div>
      <div className="row" style={{ marginBottom: 16 }} data-reveal>
        <label className="sr-only" htmlFor="int-url">Page to check</label>
        <input id="int-url" className="input" value={url} onChange={(e) => setUrl(e.target.value)} />
      </div>
      <div className="grid g-3">
        {(cat.data?.integrations || []).map((i: any) => {
          const r = results[i.id];
          return (
            <div className="panel int" key={i.id} data-reveal>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h3>{i.name}</h3>
                <span className={`pill ${i.configured ? "good" : "warn"}`}>{i.configured ? "Ready" : "Needs a key"}</span>
              </div>
              <p>{i.description}</p>
              <span className="faint" style={{ fontSize: 12.5 }}>{i.category} · {i.cost}</span>
              <div><button className="btn" disabled={r?.status === "running"} onClick={() => run(i.id)}>{r?.status === "running" ? "Checking…" : "Check this page"}</button></div>
              {r && r.status !== "running" && (
                r.status === "ok" ? (
                  r.findings?.length ? (
                    <ul className="found" style={{ marginTop: 6 }}>
                      {r.findings.slice(0, 4).map((f: any) => <li key={f.id}><Sev s={SEV_MAP[f.severity] || "low"} /><span>{f.title}</span></li>)}
                    </ul>
                  ) : <p>No problems found.</p>
                ) : <p style={{ color: "var(--ink-2)" }}>{r.error || r.status}</p>
              )}
            </div>
          );
        })}
      </div>
      {cat.data && <p className="faint" style={{ fontSize: 12.5, marginTop: 16 }}>{cat.data.attribution} Not included: {cat.data.not_included.map((n) => n.name).join("; ")}.</p>}
    </div>
  );
}
