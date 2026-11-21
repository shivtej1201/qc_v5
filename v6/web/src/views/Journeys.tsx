import { useState } from "react";
import { api, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, ago, useLoad, useToast } from "../ui";

const EXAMPLE = `name: Check loan eligibility
device: mobile
steps:
  - open: /personal-loan
    expect:
      - visible: "role=heading[name=/Personal Loan/i]"
  - click: "role=link[name='Check eligibility']"
  - fill:
      "label=Monthly income": "85000"
  - click: "role=button[name='Continue']"
    expect:
      - visible: "text=/eligible/i"
      - no_console_errors: true
  - stop_before: "role=button[name='Submit application']"
`;

export function JourneysView({ site, params }: { site: Site; params: URLSearchParams }) {
  const list = useLoad(() => api.journeys(site.id), [site.id]);
  const runParam = Number(params.get("run")) || 0;
  const firstRun = list.data?.journeys.find((j) => j.recent.length)?.recent.slice(-1)[0]?.id || 0;
  const runId = runParam || firstRun;
  const jr = useLoad(() => (runId ? api.journeyRun(runId) : Promise.resolve(null)), [runId]);
  const ref = useViewMotion<HTMLDivElement>([!!list.data]);
  const [editing, setEditing] = useState(false);
  const [yaml, setYaml] = useState(EXAMPLE);
  const [err, setErr] = useState<string | null>(null);
  const [rec, setRec] = useState("");
  const [recName, setRecName] = useState("");
  const convert = async () => {
    setErr(null);
    try {
      const out = await api.convertRecording(site.id, rec, recName || "Recorded journey", "desktop");
      setYaml(out.yaml);
      toast(out.skipped.length ? `Converted. ${out.skipped.length} line(s) weren't understood and were left out.` : "Converted. Check the steps, then save.");
    } catch (e: any) { setErr(e.detail || e.message); }
  };
  const toast = useToast();

  const save = async () => {
    setErr(null);
    try { await api.saveJourney(site.id, yaml); toast("Journey saved. It runs with the next check."); setEditing(false); list.reload(); }
    catch (e: any) { setErr(e.detail || e.message); }
  };

  if (list.error) return <ErrorBox text={list.error} onRetry={list.reload} />;
  const js = list.data?.journeys || [];
  const passed = js.filter((j) => j.recent.length && j.recent[j.recent.length - 1].status === "passed").length;
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Journeys</h1>
          <p className="lede">Paths your customers take, walked at a person's pace with every check.
            {js.length ? <> <b>{passed} of {js.length} passed last time.</b></> : null}</p>
        </div>
        <button className="btn primary" onClick={() => setEditing(!editing)}>{editing ? "Close the editor" : "Add a journey"}</button>
      </div>
      {editing && (
        <div className="panel" data-reveal style={{ marginBottom: 18 }}>
          <div className="panel-head"><h2>New journey</h2><span className="faint" style={{ fontSize: 13 }}>Buttons like Submit or Pay are never pressed; use stop_before.</span></div>
          <details style={{ marginBottom: 12 }}>
            <summary style={{ cursor: "pointer" }}>Record it instead of writing it</summary>
            <p className="faint" style={{ fontSize: 13 }}>Run <span className="mono">npx playwright codegen --target python {site.base_url}</span> on your computer,
              click through the journey, then paste the generated code here.</p>
            <label className="sr-only" htmlFor="rec-name">Journey name</label>
            <input id="rec-name" className="input" placeholder="Journey name" value={recName} onChange={(e) => setRecName(e.target.value)} style={{ marginBottom: 8, width: "100%" }} />
            <label className="sr-only" htmlFor="rec">Recorded code</label>
            <textarea id="rec" className="input" style={{ minHeight: 120 }} placeholder="page.goto(...)" value={rec} onChange={(e) => setRec(e.target.value)} />
            <div className="row" style={{ marginTop: 8 }}><button className="btn" type="button" disabled={rec.length < 10} onClick={convert}>Turn into steps</button></div>
          </details>
          <label className="sr-only" htmlFor="yaml">Journey steps</label>
          <textarea id="yaml" className="input" value={yaml} onChange={(e) => setYaml(e.target.value)} spellCheck={false} />
          {err && <div style={{ marginTop: 10 }}><ErrorBox text={err} /></div>}
          <div className="row" style={{ marginTop: 10 }}><button className="btn primary" onClick={save}>Save journey</button></div>
        </div>
      )}
      <div className="table-wrap" data-reveal style={{ marginBottom: 18 }}>
        <table>
          <thead><tr><th>Journey</th><th>Device</th><th>Recent runs</th><th>Last run</th></tr></thead>
          <tbody>
            {js.length === 0 && <tr><td colSpan={4} className="empty">No journeys yet. Add one to check a path like "apply for a loan".</td></tr>}
            {js.map((j) => {
              const last = j.recent[j.recent.length - 1];
              return (
                <tr key={j.id} className="click" tabIndex={0} onClick={() => last && go("journeys", { run: last.id })}
                  onKeyDown={(e) => e.key === "Enter" && last && go("journeys", { run: last.id })}>
                  <td className="title">{j.name}</td>
                  <td>{j.device === "mobile" ? "Mobile" : "Desktop"}</td>
                  <td><span className="dots" aria-label={`${j.recent.filter((r) => r.status === "passed").length} passed of ${j.recent.length}`}>
                    {j.recent.map((r) => <i key={r.id} className={r.status === "passed" ? "" : "f"} />)}</span></td>
                  <td>{last ? <span className={`pill ${last.status === "passed" ? "good" : "bad"}`}>
                    {last.status === "passed" ? `Passed · ${(last.duration_ms / 1000).toFixed(0)} s` : `Failed at step ${last.failed_step}`}</span> : <span className="faint">Not run yet</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {jr.data && (
        <div className="panel" data-reveal>
          <div className="panel-head">
            <h2>{jr.data.journey_run.name} · {ago(jr.data.journey_run.started_at)} · {jr.data.journey_run.device}</h2>
            <span className={`pill ${jr.data.journey_run.status === "passed" ? "good" : "bad"}`}>{jr.data.journey_run.status === "passed" ? "Passed" : "Failed"}</span>
          </div>
          <div className="film">
            {jr.data.steps.map((s) => (
              <div className={`frame ${s.status}`} key={s.idx}>
                {s.screenshot ? <img src={api.objectUrl(s.screenshot)} alt={`After step ${s.idx}: ${s.label}`} loading="lazy" /> : <div className="ph" />}
                <b>{s.idx} {s.label}</b>
                <span className={s.status === "failed" ? "" : "faint"} style={s.status === "failed" ? { color: "var(--critical)" } : undefined}>
                  {s.status === "passed" ? `${(s.duration_ms / 1000).toFixed(1)} s` : s.status === "failed" ? "Didn't work" : "Not reached"}</span>
              </div>
            ))}
          </div>
          {jr.data.journey_run.failure_text && (
            <div className="failure"><h3>Step {jr.data.journey_run.failed_step} didn't work</h3><p>{jr.data.journey_run.failure_text}</p></div>
          )}
        </div>
      )}
    </div>
  );
}
