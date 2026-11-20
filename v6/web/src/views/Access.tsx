import { useState } from "react";
import { api, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, copyText, useLoad, useToast } from "../ui";

export function AccessView({ site }: { site: Site; params: URLSearchParams }) {
  const a = useLoad(() => api.access(site.id), [site.id]);
  const ref = useViewMotion<HTMLDivElement>([!!a.data]);
  const toast = useToast();
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [testing, setTesting] = useState(false);

  const copy = async (text: string) => toast((await copyText(text)) ? "Copied." : "Copy blocked. Select the text instead.");
  const runTest = async () => {
    setTesting(true);
    try { setTest(await api.accessTest(site.id)); } catch (e: any) { setTest({ ok: false, text: e.detail || e.message }); } finally { setTesting(false); }
  };

  if (a.error) return <ErrorBox text={a.error} onRetry={a.reload} />;
  if (!a.data) return <p className="faint">Loading…</p>;
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Crawler access</h1>
          <p className="lede">QC tells your site who it is on every request. If your CDN's bot protection stops it, ask the CDN team to allow it with the rule below. QC never tries to get around bot protection.</p>
        </div>
      </div>
      <div className="grid g-main">
        <div className="panel" data-reveal>
          <p className="section-label">How QC identifies itself</p>
          <dl className="kv">
            <dt>Header</dt><dd className="mono">{a.data.header}: {a.data.token}</dd>
            <dt>User agent ends with</dt><dd className="mono">{a.data.ua_suffix}</dd>
            <dt>Speed</dt><dd>{site.settings.rate_rps} requests per second at most, shared by every check; {site.settings.visitors} visitors at a time</dd>
          </dl>
          <p className="section-label" style={{ marginTop: 20 }}>Rule for Akamai</p>
          <div className="code-box">{a.data.rules.akamai}</div>
          <div className="row" style={{ marginTop: 8 }}><button className="btn" onClick={() => copy(a.data!.rules.akamai)}>Copy the Akamai rule</button></div>
          <p className="section-label" style={{ marginTop: 20 }}>Rule for Cloudflare</p>
          <div className="code-box">{a.data.rules.cloudflare}</div>
          <div className="row" style={{ marginTop: 8 }}><button className="btn" onClick={() => copy(a.data!.rules.cloudflare)}>Copy the Cloudflare rule</button></div>
        </div>
        <div className="panel" data-reveal>
          <p className="section-label">Test access</p>
          <p style={{ marginTop: 0 }}>Sends 20 requests, half a second apart, to the homepage with QC's identity and reports whether any were stopped.</p>
          <button className="btn primary" onClick={runTest} disabled={testing}>{testing ? "Testing… (about 15 s)" : "Test access"}</button>
          {test && <p role="status" style={{ marginTop: 12, color: test.ok ? "var(--good)" : "var(--critical)" }}>{test.text}</p>}
        </div>
      </div>
    </div>
  );
}
