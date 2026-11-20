# QC V5 Command Center — Design System

Audience: designers and front-end engineers. Source of truth: `frontend/dashboard/css/tokens.css`.

![Command Center, live audit](screenshots/command-center-live.png)

## Principles

1. **Data first, decoration never.** Every visual encodes something: the 3D core is a progress map, ring speed is
   the request rate, colors are severities. If it doesn't help someone decide what to fix, it doesn't ship.
2. **Fix-first.** Views open on what matters most: HIGH issues, biggest wins, worst pages.
3. **Honest numbers.** Estimates say what they assume; live projections explain gaps (e.g. "50% of the budget is
   validating non-sitemap links"); WAF results are labeled "not the origin's status".
4. **Calm motion.** Short, purposeful, and fully disabled under `prefers-reduced-motion`. Polling refreshes never
   replay entrance animations.

## Tokens

| Group | Tokens | Notes |
|---|---|---|
| Brand | `--brand-500` cyan, `--accent-500` violet | primary actions, progress, data series |
| Severity | `--sev-high` red, `--sev-medium` amber, `--sev-low` sky, `--sev-info` slate | identical meaning in badges, charts, 3D |
| State | `--ok`, `--warn`, `--danger`, `--active` (in-flight) | state pills, 3D in-flight dots |
| Surface | `--bg`, `--bg-elev`, `--glass`, `--glass-strong`, `--stroke` | glass panels over a soft gradient backdrop |
| Text | `--text`, `--text-2`, `--text-3` | primary / secondary / tertiary |
| Type | system UI stack; `--fs-xs` … `--fs-2xl`; tabular numerals for metrics | no web-font download |
| Space / shape | `--s-1` … `--s-8` (4 px grid), `--r-sm/md/lg/pill` | |
| Motion | `--t-fast` 140 ms, `--t-med` 260 ms, `--t-slow` 600 ms, `--ease` | all 0 ms under reduced motion |

Themes: dark (default, "mission control") and light (`[data-theme="light"]`, follows the OS on first visit,
toggle persisted per browser).

## Components

| Component | Use |
|---|---|
| **Crawl Core (3D)** | Each dot ≈ N pages: dim outer shell = pending, bright band = in flight, inner orbit = done (teal clean / red HIGH issue); rings and comets follow req/s; core turns amber on WAF pushback. Drag to rotate. Pauses off-screen; static under reduced motion; text fallback without WebGL. |
| **Target gauge** | Projected or actual minutes on a 0–40 scale with the 15–20 min window marked in green. |
| KPI card | Label, animated value (counts from previous value), sub-text, optional progress bar against its own population. |
| Insight | Icon + title + one-sentence finding; kinds: danger, warning, info, success. |
| Severity donut, bar list, area chart | Dependency-free SVG; animated once per view. |
| Issue explorer | Severity segmented control (opens on HIGH), issue-type select, debounced search, paging; each row shows URL, the page it's on, anchor/alt text, status, hop count, detail and fix. |
| Wizard | Pages (**from sitemap**, paste or file) → Speed (presets, rate slider, live estimate) → Launch. Sitemap mode: discover, sitemap tree with health tags (index, URLs, duplicates, lastmod, robots, HTTP errors), checkboxes per sitemap, include/exclude patterns, page cap, live selection preview with "left out because…", save as profile. |
| SEO panel | SEO score gauge (green ≥ 80), coverage bars (share of pages passing each check), issues grouped by area as chips (click → issue explorer filtered to that type), sitemap health table. |
| Profiles table | Saved sitemap configurations: selection summary, speed, last run (links to the audit), Run now / Review & run / Delete. |
| Tag | Small pill for metadata in dense lists; neutral, warn (amber) and bad (red) variants. |
| State pill | RUNNING (pulsing), COMPLETED/PASS, STOPPED/PARTIAL, FAILED. |

![Issue explorer](screenshots/issue-explorer.png)
![Sitemap configuration](screenshots/wizard-sitemap.png)
![SEO panel](screenshots/seo-panel.png)
![Sitemap profiles](screenshots/sitemaps-profiles.png)
![New audit wizard](screenshots/new-audit-wizard.png)

## Motion system (GSAP 3.12.5, vendored)

`frontend/dashboard/vendor/gsap.min.js` (GreenSock standard no-charge license, see `GSAP_LICENSE.txt`), loaded
locally; no CDN. Primitives live in `js/motion.js` and are the only place GSAP is called:

| Primitive | Used for |
|---|---|
| `PageTransition`, `CardReveal`, `StaggerReveal` | view and card entrance (once per view) |
| `CounterAnimation` | KPI values count from the previous value |
| `ProgressAnimation` | KPI bars, score dimension bars |
| `StatusTransition` | glow when the lifecycle state changes (e.g. RUNNING → COMPLETED) |
| `IssuePulse` | FAILED / INTERRUPTED lifecycle pill |
| `ModalTransition` | wizard, issue detail |
| `ChartReveal` | charts |

Rules: every tween belongs to the current view's `gsap.context` and is reverted on navigation (no leaked timelines);
`fromTo` with explicit end values only (a `from` tween reads computed styles, which breaks while a CSS animation runs);
nothing animates under `prefers-reduced-motion` or during a polling refresh; table rows are never animated.
3D stays where it carries data (the crawl core: pending / in flight / done / HIGH-issue pages, rings follow req/s).

## Large-data components

| Component | Behaviour |
|---|---|
| **Virtualized grid** (`js/grid.js`) | Only visible rows (+ overscan) are in the DOM (< 80 nodes for 300,000 rows, verified in E2E); 200-row pages fetched on demand from the API; server-side filter / sort / search; column chooser (persisted per grid); keyboard Up/Down/Enter; `role="grid"` with `aria-rowcount` / `aria-rowindex` |
| **Audit tabs** | Overview · URLs · Links · Images · Issues · Remediation · Reports, one shared header with the lifecycle pill and Pause / Resume / Cancel |
| **URL detail** | Overview, HTTP, redirect chain, canonical & robots, metadata, headings & content, structured data & hreflang, performance & security, links & images, issues with evidence, raw stored rows |
| **Issue detail** (modal) | evidence, recommended fix, lifecycle status + note (OPEN / IN_PROGRESS / VERIFIED / RESOLVED / DISMISSED) |
| **Score card** | gauge + five weighted dimensions + `formula_version` |
| **Reports** | trust gate + manifest (rows, size, SHA-256, status per file) + downloads |

Themes: dark and light (the Watermelon theme is intentionally not added).

![Audit overview with health score](screenshots/audit-overview-score.png)
![Link audit (virtualized grid)](screenshots/link-audit-grid.png)
![Issue detail](screenshots/issue-detail.png)
![Reports and manifest](screenshots/reports-manifest.png)

## Accessibility

Keyboard reachable (rows are focusable, Enter opens), visible focus rings, `aria-label`s on charts summarizing the
data, skip link, modal with `aria-modal` and Escape to close, live region only for toasts (not for polling
refreshes), color never the only signal (severity text labels everywhere), reduced motion respected.

## Responsive

Breakpoints at 1280 / 1080 / 860 / 640 / 480 px: KPI grid 6 → 3 → 2 columns, side nav becomes a top bar, hero
grows taller and stat chips compact on phones. Verified at 390 × 844 with no horizontal overflow.

![Mobile, light theme](screenshots/mobile-light.png)

## Security rules for UI code

Crawled pages control anchor text, alt text and URLs. All data is inserted with `h()` (text nodes) — never
`innerHTML`. External links open with `rel="noopener noreferrer nofollow"`.
