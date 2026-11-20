# QC V6 — Product Design Spec

| | |
|---|---|
| **Audience** | Product designer, front-end team, dev lead |
| **Status** | Proposal for approval |
| **Prototype** | `prototype.html` (clickable: Today, Live run, Fixes, Page, Journeys, Integrations) |
| **Architecture** | [ARCHITECTURE.md](ARCHITECTURE.md) |

---

## 1. Who uses it and what they need

| Person | Typical day | What they need from QC |
|---|---|---|
| **SEO lead** (primary) | Owns site health, reports to marketing heads | "What changed, what's broken, who's fixing it, did it work" in one screen |
| **Developer** | Receives tickets | Exactly where, why, how to reproduce, how to verify; no noise |
| **Content editor** | Updates pages | Which of *my* pages need text changes, in plain words |
| **QA / release manager** | Before and after releases | Did the journeys pass, did anything regress |
| **Head of digital** (viewer) | Weekly | One number, one trend, three sentences |

**Design principle that follows:** the product is organised around **fixes and people**, not around crawl data.
Crawl data (pages, links, images) is one click deeper, always available, never the front door.

## 2. Voice: human, not robotic

| Instead of (V5) | Write (V6) |
|---|---|
| `H1_MISSING` · SOURCE_PAGE · 41,200 rows | **41,200 product pages have no main heading** |
| Run COMPLETED, final_qc_status PASS | **Checked 3,00,000 pages in 3 h 12 min. Everything adds up.** |
| 158 HIGH, 6,021 MEDIUM | **3 things need you today.** 12 more can wait for the next sprint. |
| WAF_BLOCKED 403 | **Akamai stopped our visitors on www.example.com.** Ask your CDN team to allow QC — here's the exact rule. |
| No results | **Nothing broken here.** We'll keep checking every night. |
| Error: 500 | **We couldn't load this page's history.** Try again; if it keeps happening, the API log has request `a91f…`. |

Rules:
* Lead with the consequence for a person, then the count. Second person ("your product pages"), active voice.
* Every problem card answers **What's wrong · Why it matters · How to fix · How we'll know it's fixed**.
* Numbers use the Indian system by default (`3,00,000`), with a setting for international (`300,000`).
* Time is relative when recent ("12 min ago"), absolute when old ("14 Nov, 06:30").
* Never blame ("you broke"); describe ("since Tuesday's release, …").
* No exclamation marks, no emoji, no internal codes in primary text. Codes appear only in a "Technical details" disclosure.

## 3. Information architecture

```
Today                         ← home: what needs attention, narrated
Sites ─ Site ─┬─ Overview     ← score, trend, last runs, templates
              ├─ Fixes        ← inbox (default view: "Needs you")
              ├─ Pages        ← explorer: every page, filter by template / issue / status
              ├─ Templates    ← page types with sample visits and results
              ├─ Journeys     ← list, builder, runs with filmstrip
              ├─ Runs         ← history; live view when running
              └─ Settings     ← hosts, speed, crawler access (allowlist), schedule, team
Integrations                  ← connect free services; each shows what it adds
Reports                       ← exports, scheduled digests
```

Navigation: left rail with 5 items (Today, Sites, Integrations, Reports, Settings) and a site switcher at the top.
Inside a site, tabs. Command palette (`Ctrl/⌘ K`) jumps to any site, page URL, fix or journey.

## 4. Key screens

### 4.1 Today
* **Greeting line with the state of the world**, e.g. *"Good morning, Priya. Last night's check of
  www.example.com finished at 03:42 — 3 things need you."*
* **Needs you** (max 5 cards): new regressions, fixes waiting for verification, journeys failing, access problems.
  Each card: one sentence, affected count, owner avatar or "Unassigned", primary action ("Assign", "Open fix",
  "Send rule to CDN team").
* **Site health** strip: score with 30-day sparkline per site, change since last run in words ("up 4 since the
  Monday release").
* **What we checked**: pages, visits, journeys, integrations — as a sentence, with the run link.

### 4.2 Live run
* Top: plain progress sentence ("1,24,500 of 3,00,000 pages checked · about 1 h 40 min left").
* **Visitors panel**: up to 20 visitor lanes; each shows device icon, current page path, what the visitor is doing
  (Loading · Reading · Scrolling · Checking accessibility), and its pause timer. This is the human-like crawl made
  visible, and it doubles as a debugging view. Motion: a lane's activity label cross-fades; the scroll progress bar
  fills with the real scroll position.
* **Pace**: requests per second vs the site's budget, as a calm line chart with the budget as a band. Challenges
  appear as markers with an explanation.
* **Found so far**: fixes appearing during the run (deduplicated), newest first.
* Controls: Pause (finishes in-flight visits), Stop, and "Slow down" (−25% budget) as a one-tap safety valve.

### 4.3 Fixes (inbox)
* Default filter **Needs you**: open + regressed + waiting for verification, sorted by impact
  (pages affected × severity × traffic when GA4 is connected).
* Row: severity mark, sentence title, template chip, pages affected, owner, status, last change.
* Bulk: assign, change status, create tickets.
* Views: list (default), board by status.
* **Fix detail drawer**: What's wrong / Why it matters / How to fix / How we'll know; example pages (5, with
  screenshots from visits); full page list (virtualised); history timeline (found → assigned → fixed → verified or
  regressed); comments; "Create GitHub issue".

### 4.4 Page
* Header: URL, template, status, last visited.
* **Two views side by side**: what the server sent (raw) vs what a visitor saw (rendered screenshot), with a
  difference summary ("4 links and the H1 only appear after JavaScript").
* Experience: LCP, CLS, INP with good/needs-work/poor bands; console errors; accessibility violations with the
  element highlighted on the screenshot.
* SEO: title, description, headings, canonical, robots, structured data, hreflang.
* Links and images on this page (virtualised table), fixes on this page, history.

### 4.5 Journeys
* List: name, device, schedule, last result, trend of the last 14 runs as dots.
* Run view: **filmstrip** of step screenshots; the failing step is expanded with the human-written failure text,
  console errors and a "Watch trace" link.
* Builder: steps as a vertical list (Open, Click, Type, Choose, Wait, Check); each step shows the element it targets
  by its visible name; "Record in browser" (phase 2).

### 4.6 Integrations
* Cards: name, what it adds in one sentence ("Real visitor speed from Chrome users"), cost ("Free"), status
  (Connected · Needs a key · Not connected), and where its data shows up in the product.
* Connecting is a 2–4 step guided panel with copyable values and a **Test connection** button.

### 4.7 Settings → Crawler access
* Egress IPs, header name and token (copy), ready-to-send rule text for Akamai/Cloudflare, **Test access** (20 paced
  requests → result in words), visitor manners (visitors per host, think time, quiet hours, device mix).

## 5. Visual language

**Concept: "Survey instrument."** The product measures a site the way a surveyor measures land: calm paper-white
ground, ink-blue text, a single teal measuring colour, and data set in a monospaced face like readings on an
instrument. Severity colours are reserved for state and never decorate.

### 5.1 Colour tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ground` | `#F5F7F6` | `#0F1519` | page background |
| `--surface` | `#FFFFFF` | `#151D22` | cards, drawers |
| `--surface-2` | `#EEF2F1` | `#1B252B` | inputs, table stripes |
| `--line` | `#D8E0DE` | `#28343B` | borders, dividers |
| `--ink` | `#14212B` | `#E6EDEF` | primary text |
| `--ink-2` | `#4A5A63` | `#A6B4BA` | secondary text |
| `--ink-3` | `#76858C` | `#7A8A91` | tertiary, captions |
| `--measure` (accent) | `#0B7A75` | `#3CC4BB` | primary actions, focus, selected |
| `--measure-soft` | `#DDF1EF` | `#123634` | selected backgrounds |
| `--marker` | `#D99A1E` | `#F0B840` | highlights in screenshots, "new" markers |
| `--critical` | `#B42318` | `#F97066` | severity: fix now |
| `--high` | `#C4320A` | `#FB8A5A` | severity: high |
| `--medium` | `#B54708` | `#FDB022` | severity: medium |
| `--low` | `#3E6A8A` | `#7CB4DA` | severity: low |
| `--good` | `#067647` | `#47CD89` | passing, verified |

Contrast: every text token on `--ground` and `--surface` meets WCAG AA (4.5:1); `--measure` on white is 5.2:1.

### 5.2 Type

| Role | Face | Sizes |
|---|---|---|
| Display / headings | **Bricolage Grotesque** 600 | 32 / 24 / 20 |
| Body and UI | **Instrument Sans** 400/500/600 | 15 (base) / 14 / 13 |
| Data, URLs, codes | **JetBrains Mono** 400/500 | 13 / 12 |

Line length 60–72 characters in prose. Numbers in tables use tabular figures.

### 5.3 Space, shape, depth
* 4-pt grid: 4 / 8 / 12 / 16 / 24 / 32 / 48.
* Radius: 6 (controls), 10 (cards), 999 (pills). Not every block is a card: lists and tables sit on the ground.
* Depth: borders first; one soft shadow only on overlays (drawers, menus, command palette).

### 5.4 Iconography
Lucide icons (ISC licence), 1.75 stroke, 18 px in UI, never as decoration in headings.

### 5.5 Data display
* Severity is a 3 px left mark + word, never colour alone.
* Charts: one series in `--measure`, comparisons in `--ink-3`, budgets as a light band, events as small markers with
  text labels. No 3D, no pie charts for more than 3 parts.

## 6. Motion (GSAP)

Motion explains change; it never decorates.

| Moment | Motion | Duration / ease |
|---|---|---|
| Screen change | content fades and rises 8 px | 0.28 s `power2.out` |
| List arrival (fix inbox, today cards) | stagger 0.04 s, max 8 items animated | 0.32 s `power2.out` |
| Number change (score, counts) | count from previous value | 0.6 s `power1.out` |
| Visitor lane activity | label cross-fade; scroll bar follows real position | 0.2 s |
| Fix status change | row flashes `--measure-soft` once | 0.6 s |
| Drawer open | slides 24 px + fade, backdrop fade | 0.3 s `power3.out` |

Rules: all timelines live in a per-view `gsap.context()` and are reverted on navigation; `prefers-reduced-motion`
turns every tween into an instant set; polling/SSE refreshes never replay entrance animations.

## 7. Components

Button (primary / secondary / quiet / danger), Icon button, Input, Select, Combobox, Checkbox, Toggle, Tabs,
Pill (status), Severity mark, Avatar + owner picker, Card, Drawer, Dialog, Toast, Tooltip, Command palette,
Data table (virtualised, sortable, column chooser, sticky header), Sparkline, Line chart with band, Filmstrip,
Screenshot viewer with highlight overlay, Progress sentence, Visitor lane, Empty state, Skeleton.

Every component: keyboard operable, visible focus ring (2 px `--measure` + 2 px offset), labelled for screen readers,
works at 200% zoom, has light and dark specs.

## 8. Accessibility

WCAG 2.2 AA. Targets ≥ 24×24 px. All motion has a reduced-motion path. Live run updates are announced politely
(`aria-live="polite"`, at most every 15 s). Tables expose sort state. Screenshots have text alternatives that list
the highlighted problem.

## 9. Responsive

Desktop first (1280–1600), tablet (768–1279: rail collapses to icons), phone (< 768: rail becomes a bottom bar;
Today, Fixes and Journeys are fully usable; explorers show cards instead of tables).

## 10. Usability test plan (before build of phase 2)

Five SEO leads and three developers, 30 minutes each, on the prototype:
1. "What needs your attention today?" (success: names the top card within 20 s)
2. "Give the missing-heading fix to Rahul and tell him how to check it." (≤ 60 s)
3. "Why did the loan journey fail last night?" (finds failing step and reason)
4. "Our CDN is blocking QC. What do you send the CDN team?" (finds and copies the rule)
Success bar: 4 of 5 tasks without help for 80% of participants.
