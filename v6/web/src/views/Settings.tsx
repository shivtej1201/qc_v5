import { useEffect, useState } from "react";
import { api, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, useLoad, useToast } from "../ui";

export function SettingsView({ site }: { site: Site; params: URLSearchParams }) {
  const s = useLoad(() => api.site(site.id), [site.id]);
  const ref = useViewMotion<HTMLDivElement>([!!s.data]);
  const toast = useToast();
  const [form, setForm] = useState<Record<string, any>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!s.data) return;
    const st = s.data.settings || {};
    setForm({
      name: s.data.name, rate_rps: st.rate_rps ?? 12, visitors: st.visitors ?? 6, consent: st.consent ?? "accept",
      check_schedule: st.check_schedule ?? "", sitemap: st.sitemap ?? "",
      slack: st.alerts?.slack_webhook ?? "", teams: st.alerts?.teams_webhook ?? "",
      on: st.alerts?.on ?? ["summary", "regressed", "journey_failed", "access"],
      provider: st.tickets?.provider ?? "", repo: st.tickets?.repo ?? "", base_url: st.tickets?.base_url ?? "",
      project: st.tickets?.project ?? "", email: st.tickets?.email ?? "",
    });
  }, [s.data]);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    const tickets = form.provider === "github" ? { provider: "github", repo: form.repo }
      : form.provider === "jira" ? { provider: "jira", base_url: form.base_url, project: form.project, email: form.email } : { provider: "none" };
    try {
      await api.patchSite(site.id, {
        name: form.name, rate_rps: Number(form.rate_rps), visitors: Number(form.visitors), consent: form.consent,
        check_schedule: form.check_schedule, sitemap: form.sitemap || undefined,
        alerts: { slack_webhook: form.slack, teams_webhook: form.teams, on: form.on }, tickets,
      });
      toast("Settings saved.");
      s.reload();
    } catch (x: any) { setErr(x.detail || x.title || x.message); } finally { setBusy(false); }
  };
  const testAlert = async () => {
    try {
      const r = await api.testAlert(site.id);
      toast(r.sent.map((x) => `${x.channel === "slack" ? "Slack" : "Teams"}: ${x.ok ? "delivered" : "failed"}`).join(" · "));
    } catch (x: any) { toast(x.detail || x.message); }
  };

  if (s.error) return <ErrorBox text={s.error} onRetry={s.reload} />;
  if (!s.data) return <p className="faint">Loading…</p>;
  const toggle = (k: string) => setForm({ ...form, on: form.on.includes(k) ? form.on.filter((x: string) => x !== k) : [...form.on, k] });
  return (
    <div className="view" ref={ref} style={{ maxWidth: 860 }}>
      <div className="head"><div><h1>Settings</h1><p className="lede">How QC checks {site.primary_host}, when, and who hears about it.</p></div></div>
      <form className="grid" onSubmit={save}>
        <section className="panel grid" data-reveal>
          <h2>Checking</h2>
          <Field id="s-name" label="Name"><input id="s-name" className="input" value={form.name || ""} onChange={set("name")} /></Field>
          <Field id="s-rate" label="Speed limit" hint="Requests per second to this site, shared by every check. Ask the site's operators what the CDN allows.">
            <input id="s-rate" className="input" type="number" min={0.5} max={200} step={0.5} value={form.rate_rps ?? ""} onChange={set("rate_rps")} /></Field>
          <Field id="s-vis" label="Visitors at a time" hint="People-like browser visits in parallel (1–20). Each reads one page at a time.">
            <input id="s-vis" className="input" type="number" min={1} max={20} value={form.visitors ?? ""} onChange={set("visitors")} /></Field>
          <Field id="s-consent" label="Cookie banner" hint="What visitors answer when a site asks about cookies.">
            <select id="s-consent" className="input" value={form.consent} onChange={set("consent")}><option value="accept">Accept</option><option value="reject">Reject</option></select></Field>
          <Field id="s-sched" label="Check automatically" hint='For example "daily 06:30", "weekdays 07:15", "mondays 09:00", "every 6 hours". Leave empty for manual checks only.'>
            <input id="s-sched" className="input" placeholder="daily 06:30" value={form.check_schedule || ""} onChange={set("check_schedule")} /></Field>
          <Field id="s-sitemap" label="Sitemap" hint="Leave empty to find it from robots.txt.">
            <input id="s-sitemap" className="input" placeholder={`${site.base_url}/sitemap.xml`} value={form.sitemap || ""} onChange={set("sitemap")} /></Field>
        </section>
        <section className="panel grid" data-reveal>
          <h2>Alerts</h2>
          <Field id="s-slack" label="Slack incoming webhook"><input id="s-slack" className="input" placeholder="https://hooks.slack.com/services/…" value={form.slack || ""} onChange={set("slack")} /></Field>
          <Field id="s-teams" label="Microsoft Teams incoming webhook"><input id="s-teams" className="input" placeholder="https://….webhook.office.com/…" value={form.teams || ""} onChange={set("teams")} /></Field>
          <fieldset className="row" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="section-label">Send a message when</legend>
            {[["summary", "a check finishes"], ["regressed", "a fixed problem comes back"], ["journey_failed", "a journey fails"], ["access", "the CDN blocks QC"]].map(([k, l]) => (
              <label key={k} className="row" style={{ gap: 6 }}><input type="checkbox" checked={form.on?.includes(k) || false} onChange={() => toggle(k)} />{l}</label>
            ))}
          </fieldset>
          <div><button type="button" className="btn" onClick={testAlert}>Send a test message</button></div>
        </section>
        <section className="panel grid" data-reveal>
          <h2>Tickets</h2>
          <Field id="s-prov" label="Create tickets in"><select id="s-prov" className="input" value={form.provider} onChange={set("provider")}>
            <option value="">Nowhere</option><option value="github">GitHub Issues</option><option value="jira">Jira Cloud</option></select></Field>
          {form.provider === "github" && <Field id="s-repo" label="Repository" hint="owner/name. The token goes in QC6_GITHUB_TOKEN on the server."><input id="s-repo" className="input" value={form.repo || ""} onChange={set("repo")} /></Field>}
          {form.provider === "jira" && <>
            <Field id="s-jurl" label="Jira address"><input id="s-jurl" className="input" placeholder="https://acme.atlassian.net" value={form.base_url || ""} onChange={set("base_url")} /></Field>
            <Field id="s-jproj" label="Project key"><input id="s-jproj" className="input" placeholder="WEB" value={form.project || ""} onChange={set("project")} /></Field>
            <Field id="s-jmail" label="Account e-mail" hint="The API token goes in QC6_JIRA_TOKEN on the server."><input id="s-jmail" className="input" value={form.email || ""} onChange={set("email")} /></Field>
          </>}
        </section>
        {err && <ErrorBox text={err} />}
        <div><button className="btn primary" type="submit" disabled={busy}>Save settings</button></div>
      </form>
    </div>
  );
}

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grid" style={{ gap: 4 }}>
      <label htmlFor={id}><b>{label}</b></label>
      {children}
      {hint && <span className="faint" style={{ fontSize: 13 }}>{hint}</span>}
    </div>
  );
}
