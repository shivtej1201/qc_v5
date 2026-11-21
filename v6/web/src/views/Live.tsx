import { useEffect, useRef, useState } from "react";
import { api, type Run, type Site } from "../api";
import { go } from "../main";
import { fadeText, useViewMotion } from "../motion";
import { ErrorBox, Icon, Sev, ago, count, useLoad, useToast } from "../ui";

const STATUS: Record<string, string> = { running: "Running", queued: "Waiting to start", pausing: "Pausing…", paused: "Paused",
  completed: "Completed", failed: "Stopped", cancelled: "Cancelled" };
const STEP_TEXT: Record<string, string> = {
  open: "Loading the page", settled: "Page settled", consent: "Answered the cookie banner", read: "Reading", scrolled: "Scrolling down",
  measured: "Measuring", done: "Done, moving on",
};

type Lane = { url: string; device: string; status: string; steps: string[] };

export function LiveView({ site, params }: { site: Site; params: URLSearchParams }) {
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const runId = Number(params.get("run")) || runs.data?.runs[0]?.id || 0;
  const detail = useLoad(() => (runId ? api.run(runId) : Promise.resolve(null)), [runId]);
  const [run, setRun] = useState<Run | null>(null);
  const [lanes, setLanes] = useState<Lane[]>([]);
  const [found, setFound] = useState<{ id: number; title: string; severity: any; pages: number }[]>([]);
  const ref = useViewMotion<HTMLDivElement>([!!detail.data]);
  const lede = useRef<HTMLParagraphElement>(null);
  const toast = useToast();

  useEffect(() => {
    if (!detail.data) return;
    setRun(detail.data.run);
    setFound(detail.data.found);
    setLanes(detail.data.recent_visits.slice(0, 8).map((v: any) => ({ url: v.page_url, device: v.device, status: v.status,
      steps: (v.timeline || []).map((s: any) => s.step) })));
  }, [detail.data]);

  useEffect(() => {
    if (!run || !["queued", "running", "pausing"].includes(run.status)) return;
    const es = new EventSource(`/api/v2/stream?run=${run.id}`);
    let lane = 0;
    const onProgress = (e: MessageEvent) => {
      const d = JSON.parse(e.data);
      setRun((r) => (r ? { ...r, status: "running", progress: { ...r.progress, ...d } } : r));
      fadeText(lede.current);
    };
    es.addEventListener("progress", onProgress);
    es.addEventListener("visit", (e: MessageEvent) => {
      const d = JSON.parse(e.data);
      onProgress(e);
      setLanes((ls) => {
        const next = [...ls];
        next[(d.lane ?? lane++) % 8] = { url: d.url, device: d.device, status: d.status, steps: d.steps || [] };
        return next;
      });
    });
    es.addEventListener("journey", (e: MessageEvent) => {
      const d = JSON.parse(e.data);
      toast(`Journey "${d.name}" ${d.status === "passed" ? "passed" : `failed at step ${d.failed_step}`}.`);
    });
    const refresh = window.setInterval(() => { api.run(run.id).then((d) => setFound(d.found)).catch(() => {}); }, 8000);
    const finish = () => { es.close(); window.clearInterval(refresh); detail.reload(); runs.reload(); };
    es.addEventListener("completed", finish);
    es.addEventListener("failed", finish);
    es.addEventListener("paused", finish);
    return () => { es.close(); window.clearInterval(refresh); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, run?.status === "running" || run?.status === "queued"]);

  if (runs.error || detail.error) return <ErrorBox text={(runs.error || detail.error)!} onRetry={detail.reload} />;
  if (!runs.data) return <p className="faint">Loading…</p>;
  if (!runId || !run) return (
    <div className="view"><h1>Live run</h1>
      <p className="lede">No check has run yet. Start one from Today.</p>
      <button className="btn primary" style={{ marginTop: 16 }} onClick={() => go("today")}>Go to Today</button></div>
  );

  const p = run.progress || {};
  const pagesDone = p.pages_done ?? run.summary?.pages ?? 0, pagesTotal = p.pages_total ?? run.summary?.pages ?? 0;
  const visitsDone = p.visits_done ?? run.summary?.visited ?? 0, visitsTotal = p.visits_total ?? run.summary?.visits_planned ?? 0;
  const live = ["queued", "running", "pausing"].includes(run.status);
  const control = async (what: "pause" | "resume" | "cancel") => {
    try {
      if (what === "pause") { await api.pauseRun(run.id); toast("Pausing. Visitors finish the page they're on; nothing already checked is lost."); }
      if (what === "resume") { await api.resumeRun(run.id); toast("Resuming where it stopped."); }
      if (what === "cancel") { await api.cancelRun(run.id); toast("Check cancelled."); }
      detail.reload(); runs.reload();
    } catch (e: any) { toast(e.detail || e.title || e.message); }
  };
  const frac = p.stage === "visits" ? (visitsTotal ? visitsDone / visitsTotal : 0) : (pagesTotal ? pagesDone / pagesTotal : 0);
  const sentence = live
    ? (p.stage === "visits" ? `Visitors have checked ${count(visitsDone)} of ${count(visitsTotal)} sample pages in a real browser.`
      : `${count(pagesDone)} of ${count(pagesTotal)} pages checked.`)
    : run.status === "paused" ? "Paused. Resume to carry on where it stopped." : run.status === "completed" ? `Finished ${ago(run.finished_at)}. ${count(run.summary?.pages)} pages checked, ${count(run.summary?.visited)} visited in a real browser.`
      : run.status === "failed" ? `This check stopped: ${run.error}` : `This check was ${run.status}.`;

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>{live ? `Checking ${site.primary_host}` : `Check of ${site.primary_host}`}</h1>
          <p className="lede" ref={lede} aria-live="polite">{sentence}</p>
        </div>
        <div className="row">
          {run.status === "running" && <button className="btn" onClick={() => control("pause")}><Icon name="pause" />Pause</button>}
          {run.status === "paused" && <button className="btn primary" onClick={() => control("resume")}><Icon name="play" />Resume</button>}
          {["paused", "queued"].includes(run.status) && <button className="btn quiet" onClick={() => control("cancel")}>Cancel</button>}
          <span className={`pill ${run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "warn"}`}>
            {STATUS[run.status] || run.status}</span>
        </div>
      </div>
      {live && <div className="progress-line" aria-hidden="true"><i style={{ width: `${(frac * 100).toFixed(1)}%` }} /></div>}
      <p className="faint" style={{ fontSize: 13, margin: "0 0 20px" }}>
        Stage: {run.status === "paused" ? `paused after ${(p.stages_done || []).length ? (p.stages_done || []).join(", ") : "part of the page check"}` : p.stage === "visits" ? "real-visitor checks" : p.stage === "coverage" ? "every page and link" : live ? "starting" : "finished"} ·
        Each visitor reads one page at a time and pauses like a person.
      </p>
      <div className="grid g-main">
        <div className="panel" data-reveal>
          <div className="panel-head"><h2>Visitors</h2><span className="faint" style={{ fontSize: 13 }}>{live ? "right now" : "last pages visited"}</span></div>
          {lanes.length === 0 ? <p className="empty">Visitors start after the pages are listed.</p> : (
            <div className="lanes">
              {lanes.filter(Boolean).map((l, i) => (
                <div className="lane" key={i}>
                  <Icon name={l.device === "mobile" ? "mobile" : "desktop"} />
                  <span className="path" title={l.url}>{safePath(l.url)}</span>
                  <span className="act">{l.status === "challenged" ? "Stopped: bot check" : l.status === "failed" ? "Couldn't load" :
                    STEP_TEXT[l.steps[l.steps.length - 1]] || "Loading"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="panel" data-reveal>
          <div className="panel-head"><h2>Found in this check</h2><span className="faint" style={{ fontSize: 13 }}>grouped into fixes</span></div>
          {found.length === 0 ? <p className="empty">Nothing yet.</p> : (
            <ul className="found">
              {found.map((f) => (
                <li key={f.id}><Sev s={f.severity} /><a href={`#/fixes?fix=${f.id}`} style={{ color: "inherit" }}>{f.title}</a></li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function safePath(u: string) {
  try { const x = new URL(u); return x.pathname + x.search; } catch { return u; }
}
