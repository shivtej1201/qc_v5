import { useEffect, useState } from "react";
import { api, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, count, useLoad } from "../ui";

const FILTERS: Record<string, [string, string][]> = {
  links: [["", "All"], ["broken", "Broken"], ["redirect", "Redirects"], ["external", "Other sites"]],
  images: [["", "All"], ["broken", "Broken"], ["missing_alt", "No description (alt)"], ["external", "Other sites"]],
};
const PAGE = 100;

export function LinksView({ site, params }: { site: Site; params: URLSearchParams }) {
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const dataset = params.get("d") === "images" ? "images" : "links";
  const [filter, setFilter] = useState(params.get("f") || "");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const run = runs.data?.runs.find((r) => r.status === "completed" && (r.kind === "full" || r.kind === "coverage"));
  const data = useLoad(() => (run ? api.facts(run.id, dataset, { filter, q: query, offset, limit: PAGE, sort: "page" }) : Promise.resolve(null)),
    [run?.id, dataset, filter, query, offset]);
  const ref = useViewMotion<HTMLDivElement>([dataset]);
  useEffect(() => { const t = window.setTimeout(() => { setQuery(q.trim()); setOffset(0); }, 300); return () => window.clearTimeout(t); }, [q]);
  useEffect(() => { setOffset(0); }, [filter, dataset]);

  if (runs.error) return <ErrorBox text={runs.error} onRetry={runs.reload} />;
  if (runs.data && !run) return <div className="view"><h1>Links and images</h1><p className="lede">This appears after the first complete check.</p></div>;
  const s = data.data?.summary;
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>{dataset === "links" ? "Links" : "Images"}</h1>
          <p className="lede">Every {dataset === "links" ? "link" : "image"} on every page from the last complete check
            {s ? <> — <b>{count(s.n_rows)}</b> placements of <b>{count(s.n_unique)}</b> different addresses, <b>{count(s.n_broken)}</b> broken.</> : "."}</p>
        </div>
        <div className="seg" role="group" aria-label="Show">
          <button aria-pressed={dataset === "links"} onClick={() => go("links", { d: "links" })}>Links</button>
          <button aria-pressed={dataset === "images"} onClick={() => go("links", { d: "images" })}>Images</button>
        </div>
      </div>
      <div className="toolbar" data-reveal>
        <div className="seg" role="group" aria-label="Filter">
          {FILTERS[dataset].map(([k, label]) => <button key={k} aria-pressed={filter === k} onClick={() => setFilter(k)}>{label}</button>)}
        </div>
        <label className="sr-only" htmlFor="links-q">Search addresses</label>
        <input id="links-q" className="search" type="search" placeholder="Search page or target address" value={q} onChange={(e) => setQ(e.target.value)} />
        {run && <a className="btn" href={api.factsCsvUrl(run.id, dataset, filter)}>Download CSV</a>}
      </div>
      {data.error ? <ErrorBox text={data.error} onRetry={data.reload} /> : (
        <div className="table-wrap" data-reveal>
          <table>
            <thead><tr><th>On page</th><th>{dataset === "links" ? "Links to" : "Image"}</th><th>{dataset === "links" ? "Text" : "Description (alt)"}</th><th className="num">Status</th></tr></thead>
            <tbody>
              {!data.data && <tr><td colSpan={4} className="faint">Loading…</td></tr>}
              {data.data && data.data.rows.length === 0 && <tr><td colSpan={4} className="empty">Nothing matches.</td></tr>}
              {data.data?.rows.map((r, i) => (
                <tr key={i}>
                  <td className="mono" style={{ maxWidth: 280, overflowWrap: "anywhere" }}><a href={`#/page?url=${encodeURIComponent(r.source_page_url)}`}>{path(r.source_page_url)}</a></td>
                  <td className="mono" style={{ maxWidth: 340, overflowWrap: "anywhere" }}>{r.url}{r.final_url && r.final_url !== r.url ? <span className="sub">→ {r.final_url}</span> : null}</td>
                  <td style={{ maxWidth: 220, overflowWrap: "anywhere" }}>{dataset === "links" ? r.anchor_text || <span className="faint">no text</span> : (r.alt_present ? r.alt_text || <span className="faint">empty</span> : <span className="pill warn">missing</span>)}</td>
                  <td className="num"><span className={`pill ${r.http_status >= 400 ? "bad" : r.http_status >= 300 ? "warn" : r.http_status ? "good" : ""}`}>{r.http_status ?? "—"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.data && data.data.total > PAGE && (
        <div className="row" style={{ marginTop: 12, justifyContent: "space-between" }}>
          <span className="faint">{count(offset + 1)}–{count(Math.min(offset + PAGE, data.data.total))} of {count(data.data.total)}</span>
          <div className="row">
            <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <button className="btn" disabled={offset + PAGE >= data.data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        </div>
      )}
    </div>
  );
}

function path(u: string) { try { const x = new URL(u); return x.pathname + x.search; } catch { return u; } }
