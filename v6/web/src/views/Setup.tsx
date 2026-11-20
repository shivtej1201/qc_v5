import { useState } from "react";
import { api, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox } from "../ui";

export function SetupView({ onCreated }: { onCreated: (s: Site) => void }) {
  const ref = useViewMotion<HTMLDivElement>([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try { onCreated(await api.createSite({ name: name || url, base_url: url })); }
    catch (x: any) { setErr(x.detail || x.title || x.message); } finally { setBusy(false); }
  };
  return (
    <div className="view" ref={ref} style={{ maxWidth: 640 }}>
      <h1>Add a site</h1>
      <p className="lede">QC checks every page in the sitemap, visits a sample in a real browser like a person would, and walks the journeys you add.</p>
      <form className="panel grid" style={{ marginTop: 20 }} onSubmit={submit} data-reveal>
        <label htmlFor="site-url"><b>Address</b></label>
        <input id="site-url" className="input" required placeholder="https://www.example.com" value={url} onChange={(e) => setUrl(e.target.value)} />
        <label htmlFor="site-name"><b>Name</b> <span className="faint">(optional)</span></label>
        <input id="site-name" className="input" placeholder="Main website" value={name} onChange={(e) => setName(e.target.value)} />
        {err && <ErrorBox text={err} />}
        <div><button className="btn primary" type="submit" disabled={busy || !url}>Add site</button></div>
      </form>
    </div>
  );
}
