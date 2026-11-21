import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles/app.css";
import { api, localStorageGet, localStorageSet, type Site } from "./api";
import { ErrorBox, Icon, ToastProvider, useLoad } from "./ui";
import { TodayView } from "./views/Today";
import { LiveView } from "./views/Live";
import { FixesView } from "./views/Fixes";
import { PageView } from "./views/Page";
import { JourneysView } from "./views/Journeys";
import { IntegrationsView } from "./views/Integrations";
import { AccessView } from "./views/Access";
import { SetupView } from "./views/Setup";
import { LinksView } from "./views/Links";
import { SettingsView } from "./views/Settings";

export type Route = { view: string; params: URLSearchParams };
function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [view, q] = h.split("?");
  return { view: view || "today", params: new URLSearchParams(q || "") };
}
export function go(view: string, params: Record<string, string | number> = {}) {
  const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString();
  window.location.hash = `/${view}${q ? "?" + q : ""}`;
}

const NAV: [string, string, string][] = [
  ["today", "today", "Today"], ["live", "live", "Live run"], ["fixes", "fix", "Fixes"], ["page", "page", "Page"],
  ["links", "layers", "Links & images"], ["journeys", "journey", "Journeys"], ["integrations", "plug", "Integrations"],
  ["access", "shield", "Crawler access"], ["settings", "settings", "Settings"],
];

function App() {
  const [route, setRoute] = useState<Route>(parseHash);
  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const sites = useLoad(() => api.sites(), []);
  const [siteId, setSiteId] = useState<number | null>(() => Number(localStorageGet("qc6-site")) || null);
  const site: Site | undefined = useMemo(() => {
    const list = sites.data?.sites || [];
    return list.find((s) => s.id === siteId) || list[0];
  }, [sites.data, siteId]);
  const [theme, setTheme] = useState<string | null>(() => localStorageGet("qc6-theme"));
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }, [theme]);
  useEffect(() => { document.title = `QC V6 · ${NAV.find((n) => n[0] === route.view)?.[2] || "Today"}`; }, [route.view]);

  const toggleTheme = () => {
    const cur = theme || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    setTheme(next);
    localStorageSet("qc6-theme", next);
  };

  let body;
  if (sites.error) body = <ErrorBox text={sites.error} onRetry={sites.reload} />;
  else if (!sites.data) body = <p className="faint">Loading…</p>;
  else if (!site || route.view === "setup") body = <SetupView onCreated={(s) => { sites.reload(); setSiteId(s.id); localStorageSet("qc6-site", String(s.id)); go("today"); }} />;
  else {
    const p = { site, params: route.params };
    body = {
      today: <TodayView {...p} />, live: <LiveView {...p} />, fixes: <FixesView {...p} />, page: <PageView {...p} />,
      journeys: <JourneysView {...p} />, integrations: <IntegrationsView {...p} />, access: <AccessView {...p} />,
      links: <LinksView {...p} />, settings: <SettingsView {...p} />,
    }[route.view] || <TodayView {...p} />;
  }

  return (
    <div className="app">
      <aside className="rail" aria-label="Main">
        <div className="logo"><div className="logo-mark">QC</div><div><b>QC V6</b><span>Site quality</span></div></div>
        {sites.data && sites.data.sites.length > 0 && (
          <div className="site-switch">
            <label className="sr-only" htmlFor="site-select">Site</label>
            <select id="site-select" value={site?.id} onChange={(e) => { const v = Number(e.target.value); setSiteId(v); localStorageSet("qc6-site", String(v)); }}>
              {sites.data.sites.map((s) => <option key={s.id} value={s.id}>{s.primary_host}</option>)}
            </select>
          </div>
        )}
        {NAV.map(([v, icon, label]) => (
          <a key={v} className="nav" href={`#/${v}`} aria-current={route.view === v ? "page" : undefined}>
            <Icon name={icon} />{label}
            {v === "fixes" && site?.open_fixes ? <span className="count">{site.open_fixes}</span> : null}
          </a>
        ))}
        <div className="foot">
          <a className="nav" href="#/setup" style={{ padding: "4px 0" }}>Add a site</a>
          <button className="btn quiet" onClick={toggleTheme} style={{ justifyContent: "flex-start", padding: 0, minHeight: 0 }}>Switch light / dark</button>
        </div>
      </aside>
      <main key={`${route.view}-${site?.id}`}>{body}</main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode><ToastProvider><App /></ToastProvider></StrictMode>,
);
