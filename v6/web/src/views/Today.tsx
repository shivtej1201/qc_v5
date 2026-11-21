import { useEffect, useRef, useState } from "react";
import { api, type Site, type TodayCard } from "../api";
import { go } from "../main";
import { countTo, useViewMotion } from "../motion";
import { ErrorBox, Icon, Spark, copyText, count, useLoad, useToast } from "../ui";

const CARD_ICON: Record<string, [string, boolean]> = {
  regressed: ["up", true], journey: ["journey", true], access: ["shield", false], unassigned: ["fix", false],
};

export function TodayView({ site }: { site: Site; params: URLSearchParams }) {
  const t = useLoad(() => api.today(site.id), [site.id]);
  const ref = useViewMotion<HTMLDivElement>([!!t.data]);
  const scoreRef = useRef<HTMLElement>(null);
  const toast = useToast();
  const [starting, setStarting] = useState(false);
  useEffect(() => { if (t.data?.score != null) countTo(scoreRef.current, t.data.score, (n) => n.toFixed(0)); }, [t.data?.score]);

  if (t.error) return <ErrorBox text={t.error} onRetry={t.reload} />;
  if (!t.data) return <p className="faint">Loading…</p>;
  const d = t.data;
  const running = d.last_run && ["queued", "running", "pausing", "paused"].includes(d.last_run.status);
  const changedTpl: any[] = d.last_run?.summary?.templates_changed || [];

  const act = async (c: TodayCard) => {
    if (c.kind === "journey") go("journeys", { run: c.journey_run_id ?? "" });
    else if (c.kind === "access") {
      const a = await api.access(site.id).catch(() => null);
      const ok = a && (await copyText(a.rules.akamai));
      toast(ok ? "Rule copied. Send it to your CDN team." : "Open Crawler access to copy the rule.");
      if (!ok) go("access");
    } else if (c.fix_id) go("fixes", { fix: c.fix_id });
  };
  const start = async () => {
    setStarting(true);
    try {
      const r = await api.startRun(site.id, "full");
      toast("Check started. You can watch it live.");
      go("live", { run: r.run_id });
    } catch (e: any) { toast(e.detail || e.message); setStarting(false); }
  };

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>{d.greeting}</h1>
          <p className="lede">{d.lede}</p>
        </div>
        {running ? <button className="btn primary" onClick={() => go("live", { run: d.last_run!.id })}><Icon name="live" />{d.last_run!.status === "paused" ? "Resume the check" : "Watch the check"}</button>
          : <button className="btn primary" disabled={starting} onClick={start}><Icon name="play" />Check the site now</button>}
      </div>
      <div className="grid g-main">
        <div className="panel" data-reveal>
          <p className="section-label">Needs you</p>
          {d.cards.length === 0 && changedTpl.length === 0 && <p className="empty">Nothing needs you right now. We'll keep checking.</p>}
          {changedTpl.slice(0, 2).map((t) => (
            <div className="need" key={`v${t.template_id}`}>
              <div className="icon"><Icon name="page" /></div>
              <div><h3>{t.template} look different since the last check</h3>
                <p>{Math.round(t.changed * 100)}% of the page changed on the {t.pages_compared} pages compared. If that wasn't a planned release, have a look.</p></div>
              <button className="btn" onClick={() => go("fixes")}>See fixes</button>
            </div>
          ))}
          {d.cards.map((c, i) => {
            const [icon, alert] = CARD_ICON[c.kind] || ["fix", false];
            return (
              <div className="need" key={i}>
                <div className={`icon ${alert ? "alert" : ""}`}><Icon name={icon} /></div>
                <div><h3>{c.title}</h3><p>{c.text}</p></div>
                <button className={`btn ${i === 0 ? "primary" : ""}`} onClick={() => act(c)}>{c.action}</button>
              </div>
            );
          })}
        </div>
        <div className="grid">
          <div className="panel" data-reveal>
            <div className="panel-head"><h2>Site health</h2><span className="faint" style={{ fontSize: 13 }}>qcv6-health-1.0</span></div>
            <div className="score"><b ref={scoreRef} className="num">{d.score != null ? Math.round(d.score) : "—"}</b>
              <span className="dim">{d.trend.length > 1 ? trendText(d.trend.map((x) => x.score)) : "out of 100"}</span></div>
            <Spark values={d.trend.map((x) => x.score)} />
          </div>
          <div className="panel" data-reveal>
            <p className="section-label">What we checked</p>
            <p style={{ margin: 0 }}>{d.checked}</p>
            <div className="facts">
              <div className="fact"><b className="num">{count(d.fixes.open)}</b><span>open fixes</span></div>
              <div className="fact"><b className="num">{count(d.fixes.waiting)}</b><span>waiting for the next check</span></div>
              <div className="fact"><b className="num">{count(d.fixes.verified)}</b><span>verified fixed</span></div>
              <div className="fact"><b className="num">{d.last_run?.summary?.trust_gate === "PASS" ? "0" : "—"}</b><span>numbers that don't add up</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function trendText(v: number[]): string {
  const diff = Math.round(v[v.length - 1] - v[v.length - 2]);
  if (diff === 0) return "same as the check before";
  return `${diff > 0 ? "up" : "down"} ${Math.abs(diff)} since the check before`;
}
