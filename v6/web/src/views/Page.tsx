import { useEffect, useState } from "react";
import { api, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, Sev, count, useLoad } from "../ui";

const BANDS: Record<string, [number, number, (v: number) => string]> = {
  LCP: [2500, 4000, (v) => `${(v / 1000).toFixed(1)} s`], CLS: [0.1, 0.25, (v) => v.toFixed(2)], INP: [200, 500, (v) => `${Math.round(v)} ms`],
};
const LABEL: Record<string, string> = { LCP: "Main content appears", CLS: "Layout shift", INP: "Response to input" };

export function PageView({ site, params }: { site: Site; params: URLSearchParams }) {
  const url = params.get("url") || "";
  const [q, setQ] = useState("");
  const list = useLoad(() => api.pages(site.id, q), [site.id, q]);
  const page = useLoad(() => (url ? api.page(site.id, url) : Promise.resolve(null)), [site.id, url]);
  const ref = useViewMotion<HTMLDivElement>([url, !!page.data]);
  useEffect(() => { setQ(""); }, [url]);

  if (!url) return (
    <div className="view" ref={ref}>
      <div className="head"><div><h1>Page</h1><p className="lede">Pick a page to see what the server sent and what a visitor saw.</p></div></div>
      <input className="search" type="search" placeholder="Find a page by address" aria-label="Find a page" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%", marginBottom: 14 }} />
      {list.error ? <ErrorBox text={list.error} /> : (
        <div className="table-wrap" data-reveal>
          <table>
            <thead><tr><th>Page</th><th>Type</th><th>Status</th></tr></thead>
            <tbody>
              {(list.data?.pages || []).map((p) => (
                <tr key={p.url} className="click" tabIndex={0} onClick={() => go("page", { url: p.url })} onKeyDown={(e) => e.key === "Enter" && go("page", { url: p.url })}>
                  <td className="title mono">{p.url}<span className="sub">{p.title}</span></td>
                  <td>{p.template_name ? <span className="chip">{p.template_name}</span> : "—"}</td>
                  <td className="num">{p.http_status}</td>
                </tr>
              ))}
              {list.data && list.data.pages.length === 0 && <tr><td colSpan={3} className="empty">No pages match. Run a check first if this site is new.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );

  if (page.error) return <ErrorBox text={page.error} onRetry={page.reload} />;
  if (!page.data) return <p className="faint">Loading…</p>;
  const { page: p, visit: v, fixes } = page.data;
  const diff = v?.render_diff || {};
  const summary = !v ? "No real-visitor check yet: this page wasn't in the sample for its page type."
    : diff.h1_only_after_js ? "The main heading only appears after JavaScript runs."
      : diff.links_only_after_js >= 5 ? `${diff.links_only_after_js} links only appear after JavaScript runs.`
        : "What the server sent and what a visitor saw match.";
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1 className="mono" style={{ fontFamily: "var(--mono)", fontSize: 20, overflowWrap: "anywhere" }}>{p.path}</h1>
          <p className="lede">{p.template_name || "Page"} · {v ? `visited ${new Date(v.started_at).toLocaleString("en-IN")} on ${v.device}` : `HTTP ${p.http_status}`} · <b>{summary}</b></p>
        </div>
        <button className="btn" onClick={() => go("page")}>Another page</button>
      </div>
      <div className="grid g-main">
        <div className="panel" data-reveal>
          <div className="panel-head"><h2>What a visitor saw</h2></div>
          {v?.screenshot ? <img className="shot" style={{ aspectRatio: "auto" }} src={api.objectUrl(v.screenshot)} alt={`Screenshot of ${p.url} as a ${v.device} visitor saw it`} />
            : <p className="empty">No screenshot yet.</p>}
          {v && (
            <dl className="kv" style={{ marginTop: 14 }}>
              <dt>Main heading</dt><dd>{diff.h1_text || "none"} {diff.h1_only_after_js && <span className="pill warn">only after JavaScript</span>}</dd>
              <dt>Links</dt><dd>{count(diff.raw_links)} in the server HTML, {count(diff.rendered_links)} after JavaScript</dd>
              <dt>Images loaded</dt><dd>{count(diff.images_loaded)} of {count(diff.images)}</dd>
              <dt>Cookie banner</dt><dd>{v.consent?.found ? `answered "${v.consent.clicked}"` : "none seen"}</dd>
              <dt>Visitor steps</dt><dd>{(v.timeline || []).map((s: any) => s.step).join(" → ")}</dd>
            </dl>
          )}
        </div>
        <div className="grid">
          {v && (
            <div className="panel" data-reveal>
              <p className="section-label">Experience</p>
              <div className="grid" style={{ gap: 14 }}>
                {Object.entries(BANDS).map(([k, [good, poor, fmt]]) => {
                  const val = v.vitals?.[k];
                  const pos = val == null ? 0 : Math.min(98, val <= good ? (val / good) * 50 : val <= poor ? 50 + ((val - good) / (poor - good)) * 30 : 80 + Math.min(18, ((val - poor) / poor) * 18));
                  return (
                    <div className="metric" key={k}>
                      <div className="row" style={{ justifyContent: "space-between" }}><span>{LABEL[k]}</span><b className="num">{val == null ? "—" : fmt(val)}</b></div>
                      {val != null && <div className="band" aria-hidden="true"><i style={{ left: `${pos}%` }} /></div>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          <div className="panel" data-reveal>
            <p className="section-label">Fixes on this page</p>
            {fixes.length === 0 ? <p className="empty">Nothing to fix here.</p> : (
              <ul className="found">{fixes.map((f) => <li key={f.id}><Sev s={f.severity} /><a href={`#/fixes?fix=${f.id}`} style={{ color: "inherit" }}>{f.title}</a></li>)}</ul>
            )}
            {v && v.a11y?.length > 0 && (
              <>
                <p className="section-label" style={{ marginTop: 14 }}>Accessibility</p>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13.5 }}>
                  {v.a11y.slice(0, 6).map((a: any) => <li key={a.id}>{a.help} <span className="faint">({a.impact}, {a.count})</span></li>)}
                </ul>
              </>
            )}
            {v && v.console?.length > 0 && <><p className="section-label" style={{ marginTop: 14 }}>Browser errors</p><p className="mono" style={{ margin: 0, overflowWrap: "anywhere" }}>{v.console[0]}</p></>}
          </div>
        </div>
      </div>
    </div>
  );
}
