import { useEffect, useRef, useState } from "react";
import { api, type Fix, type Member, type Site } from "../api";
import { go } from "../main";
import { flash, slideIn, useViewMotion } from "../motion";
import { Avatar, ErrorBox, Sev, count, day, statusPill, useLoad, useToast } from "../ui";

const VIEWS: [string, string][] = [["needs", "Needs you"], ["verify", "Waiting for check"], ["verified", "Verified"], ["all", "All"]];

export function FixesView({ site, params }: { site: Site; params: URLSearchParams }) {
  const [view, setView] = useState("needs");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const list = useLoad(() => api.fixes(site.id, view, query), [site.id, view, query]);
  const members = useLoad(() => api.members(), []);
  const openId = Number(params.get("fix")) || null;
  const ref = useViewMotion<HTMLDivElement>([]);
  useEffect(() => { const t = window.setTimeout(() => setQuery(q.trim()), 300); return () => window.clearTimeout(t); }, [q]);

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Fixes</h1>
          <p className="lede">Each row is one thing to fix, however many pages it touches. Worst first.</p>
        </div>
      </div>
      <div className="toolbar" data-reveal>
        <div className="seg" role="group" aria-label="Show">
          {VIEWS.map(([k, label]) => (
            <button key={k} aria-pressed={view === k} onClick={() => setView(k)}>
              {label}{list.data ? ` · ${count(list.data.counts[k])}` : ""}
            </button>
          ))}
        </div>
        <label className="sr-only" htmlFor="fix-q">Find a fix</label>
        <input id="fix-q" className="search" type="search" placeholder="Find a fix or page type" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {list.error ? <ErrorBox text={list.error} onRetry={list.reload} /> : (
        <div className="table-wrap" data-reveal>
          <table>
            <thead><tr><th>Severity</th><th>What's wrong</th><th>Page type</th><th className="num">Pages</th><th>Owner</th><th>Status</th></tr></thead>
            <tbody>
              {!list.data && <tr><td colSpan={6} className="faint">Loading…</td></tr>}
              {list.data && list.data.fixes.length === 0 && (
                <tr><td colSpan={6} className="empty">{view === "needs" ? "Nothing needs you here. We'll keep checking." : "Nothing in this list."}</td></tr>
              )}
              {list.data?.fixes.map((f) => (
                <tr key={f.id} className="click" tabIndex={0} data-fix={f.id}
                  onClick={() => go("fixes", { fix: f.id })} onKeyDown={(e) => { if (e.key === "Enter") go("fixes", { fix: f.id }); }}>
                  <td><Sev s={f.severity} /></td>
                  <td className="title">{f.title}<span className="sub">{f.texts.why}</span></td>
                  <td>{f.template_name ? <span className="chip">{f.template_name}</span> : <span className="faint">Site-wide</span>}</td>
                  <td className="num">{count(f.pages_affected)}</td>
                  <td><Avatar name={f.owner_name} /></td>
                  <td>{statusPill(f)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {openId && <FixDrawer id={openId} members={members.data?.members || []} onChanged={list.reload} />}
    </div>
  );
}

function FixDrawer({ id, members, onChanged }: { id: number; members: Member[]; onChanged: () => void }) {
  const d = useLoad(() => api.fix(id), [id]);
  const panel = useRef<HTMLElement>(null);
  const back = useRef<HTMLDivElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);
  const toast = useToast();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const close = () => go("fixes");

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    slideIn(panel.current, back.current);
    closeBtn.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("keydown", esc); prev?.focus?.(); };
  }, []);

  const change = async (body: Record<string, unknown>, msg: string) => {
    setBusy(true);
    try {
      await api.patchFix(id, body);
      toast(msg);
      d.reload(); onChanged();
      flash(document.querySelector(`tr[data-fix="${id}"]`));
    } catch (e: any) { toast(e.detail || e.message); } finally { setBusy(false); }
  };

  const f: Fix | undefined = d.data?.fix;
  return (
    <>
      <div className="drawer-back" ref={back} onClick={close} />
      <aside className="drawer" ref={panel} role="dialog" aria-modal="true" aria-labelledby="fix-title">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "nowrap" }}>
          <div>{f && <Sev s={f.severity} />}<h2 id="fix-title" style={{ marginTop: 6 }}>{f ? f.title : "Loading…"}</h2></div>
          <button className="btn quiet" ref={closeBtn} onClick={close} aria-label="Close">Close</button>
        </div>
        {d.error && <ErrorBox text={d.error} onRetry={d.reload} />}
        {f && d.data && (
          <>
            <div className="row" style={{ marginTop: 12 }}>
              <Avatar name={f.owner_name} />
              <label className="sr-only" htmlFor="owner">Owner</label>
              <select id="owner" className="input" style={{ flex: "0 1 220px" }} value={f.owner_id ?? ""} disabled={busy}
                onChange={(e) => e.target.value ? change({ owner_id: Number(e.target.value) }, `${members.find((m) => m.id === Number(e.target.value))?.name} now owns this fix.`)
                  : change({ unassign: true }, "Nobody owns this fix now.")}>
                <option value="">Unassigned</option>
                {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
              {statusPill(f)}
            </div>
            <div className="qa">
              <div><h3>What's wrong</h3><p>{f.texts.title}. {count(f.pages_affected)} page{f.pages_affected === 1 ? "" : "s"} affected.</p></div>
              <div><h3>Why it matters</h3><p>{f.texts.why}</p></div>
              <div><h3>How to fix</h3><p>{f.texts.fix}</p></div>
              <div><h3>How we'll know</h3><p>{f.texts.verify}</p></div>
            </div>
            <div className="row">
              {f.status !== "in_progress" && f.status !== "verified" && <button className="btn" disabled={busy} onClick={() => change({ status: "in_progress" }, "Marked as in progress.")}>Start working</button>}
              {["open", "in_progress"].includes(f.status) && <button className="btn primary" disabled={busy} onClick={() => change({ status: "fixed_pending_check" }, "Marked fixed. The next check will confirm it.")}>I've fixed it</button>}
              {f.status !== "wont_fix" && f.status !== "verified" && <button className="btn quiet" disabled={busy} onClick={() => change({ status: "wont_fix" }, "Marked as won't fix.")}>Won't fix</button>}
              {(f.status === "wont_fix" || f.status === "verified") && <button className="btn" disabled={busy} onClick={() => change({ status: "open" }, "Reopened.")}>Reopen</button>}
              {f.ticket_url ? <a className="btn quiet" href={f.ticket_url} target="_blank" rel="noopener noreferrer">Open ticket</a>
                : <button className="btn quiet" disabled={busy} onClick={async () => {
                    setBusy(true);
                    try { const t = await api.createTicket(id); toast(`Ticket ${t.key} created.`); d.reload(); onChanged(); }
                    catch (e: any) { toast(e.detail || e.message); } finally { setBusy(false); }
                  }}>Create ticket</button>}
            </div>
            {d.data.examples.length > 0 && (
              <>
                <p className="section-label" style={{ marginTop: 20 }}>What a visitor saw</p>
                <div className="thumbs">
                  {d.data.examples.map((x) => <img key={x.screenshot} className="shot" src={api.objectUrl(x.screenshot)} alt={`Screenshot of ${x.page_url}`} loading="lazy" />)}
                </div>
              </>
            )}
            <p className="section-label" style={{ marginTop: 20 }}>Pages ({count(f.pages_affected)})</p>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
              {d.data.pages.slice(0, 8).map((u) => <li key={u}><a href={`#/page?url=${encodeURIComponent(u)}`} className="mono">{u}</a></li>)}
            </ul>
            <p className="section-label" style={{ marginTop: 20 }}>History</p>
            <ul className="timeline">
              {d.data.events.map((e, i) => <li key={i}><span className="faint mono">{day(e.created_at)}</span><span>{eventText(e, members)}</span></li>)}
            </ul>
            <form className="row" style={{ marginTop: 16 }} onSubmit={(e) => { e.preventDefault(); if (comment.trim()) { change({ comment }, "Comment added."); setComment(""); } }}>
              <label className="sr-only" htmlFor="comment">Comment</label>
              <input id="comment" className="input" placeholder="Add a comment for the team" value={comment} onChange={(e) => setComment(e.target.value)} />
              <button className="btn" type="submit" disabled={busy || !comment.trim()}>Comment</button>
            </form>
          </>
        )}
      </aside>
    </>
  );
}

function eventText(e: { kind: string; detail: any; actor: string | null }, members: Member[]): string {
  const who = e.actor || "The check";
  switch (e.kind) {
    case "found": return `Found on ${count(e.detail?.pages)} page${e.detail?.pages === 1 ? "" : "s"}`;
    case "assigned": return e.detail?.owner_id ? `${who} gave it to ${members.find((m) => m.id === e.detail.owner_id)?.name || "someone"}` : `${who} unassigned it`;
    case "status": return `${who} moved it to ${String(e.detail?.to || "").replace(/_/g, " ")}`;
    case "regressed": return "Found again after it was verified";
    case "verified": return "The check confirmed it's fixed";
    case "comment": return `${who}: ${e.detail?.text}`;
    case "ticket": return `${who} linked a ticket`;
    default: return e.kind;
  }
}
