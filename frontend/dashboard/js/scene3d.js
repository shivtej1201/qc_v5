// QC V5 Dashboard — "Crawl Core": a live 3D view of an audit (Three.js r170, vendored, MIT).
//
// Encoding (data, not decoration):
//   outer shell dots  = pages still PENDING           (dim)
//   bright band       = pages being fetched right now  (frontier)
//   inner orbit dots  = pages DONE: teal = clean, red = has a HIGH issue
//   ring / comet speed= live request rate; core tint amber/red on WAF pushback
// Each dot represents ceil(total / N) pages. Pauses when hidden; static under reduced motion.
import * as THREE from "../vendor/three.module.min.js";
import { reducedMotion } from "./ui.js";

const N = 2400;
const SHELL_R = 3.25, ORBIT_R = 1.85;

function cssColor(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  try { return new THREE.Color(v || fallback); } catch { return new THREE.Color(fallback); }
}

function dotTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.35, "rgba(255,255,255,0.85)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 64, 64);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export function createCrawlCore(container) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  } catch (e) {
    container.append(Object.assign(document.createElement("div"), {
      className: "scene-fallback", textContent: "3D view unavailable (WebGL disabled). All metrics are shown alongside.",
    }));
    return { update() {}, setTheme() {}, dispose() {}, ok: false };
  }
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.append(renderer.domElement);
  renderer.domElement.setAttribute("aria-hidden", "true");

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.set(0, 0.6, 9.2);
  const world = new THREE.Group();
  scene.add(world);

  let palette = {};
  const readPalette = () => {
    palette = {
      pending: cssColor("--text-3", "#6b7a93"),
      active: cssColor("--active", "#e0fbff"),
      done: cssColor("--brand-500", "#22d3ee"),
      issue: cssColor("--sev-high", "#f43f5e"),
      accent: cssColor("--accent-500", "#a78bfa"),
      warn: cssColor("--sev-medium", "#f59e0b"),
      light: document.documentElement.dataset.theme === "light",
    };
  };
  readPalette();

  // ---- particles (fibonacci sphere: even coverage)
  const pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
  const home = new Float32Array(N * 3), inner = new Float32Array(N * 3), phase = new Float32Array(N);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2, r = Math.sqrt(1 - y * y), th = golden * i;
    const x = Math.cos(th) * r, z = Math.sin(th) * r;
    const jitter = 1 + (Math.random() - 0.5) * 0.06;
    home.set([x * SHELL_R * jitter, y * SHELL_R * jitter, z * SHELL_R * jitter], i * 3);
    const ir = ORBIT_R * (0.85 + Math.random() * 0.3);
    inner.set([x * ir, y * ir * 0.62, z * ir], i * 3);         // flattened inner orbit (a "disc" of done work)
    pos.set(home.subarray(i * 3, i * 3 + 3), i * 3);
    phase[i] = Math.random() * Math.PI * 2;
  }
  // assign pages to dots in a shuffled order so progress spreads over the sphere
  const order = Array.from({ length: N }, (_, i) => i);
  for (let i = N - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; [order[i], order[j]] = [order[j], order[i]]; }
  const rank = new Int32Array(N);
  order.forEach((dot, k) => { rank[dot] = k; });

  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geom.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const pmat = new THREE.PointsMaterial({ size: 0.075, map: dotTexture(), vertexColors: true, transparent: true,
    depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true });
  const points = new THREE.Points(geom, pmat);
  world.add(points);
  // additive glow reads well on dark; on the light theme it washes out, so use normal blending there
  const applyBlending = () => { pmat.blending = palette.light ? THREE.NormalBlending : THREE.AdditiveBlending; pmat.needsUpdate = true; };
  applyBlending();

  // ---- core
  const coreMat = new THREE.MeshBasicMaterial({ color: palette.done, transparent: true, opacity: 0.22 });
  const core = new THREE.Mesh(new THREE.SphereGeometry(0.62, 48, 48), coreMat);
  const wireMat = new THREE.MeshBasicMaterial({ color: palette.accent, wireframe: true, transparent: true, opacity: 0.45 });
  const wire = new THREE.Mesh(new THREE.IcosahedronGeometry(0.95, 1), wireMat);
  world.add(core, wire);

  // ---- orbital rings + comets
  const rings = [];
  const comets = [];
  const ringDefs = [[2.55, 0.35, 0], [2.85, -0.6, 0.8], [3.7, 1.15, -0.4]];
  for (const [r, rx, rz] of ringDefs) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r, 0.006, 6, 220),
      new THREE.MeshBasicMaterial({ color: palette.done, transparent: true, opacity: 0.35 }));
    ring.rotation.set(Math.PI / 2 + rx, 0, rz);
    world.add(ring);
    rings.push(ring);
    const comet = new THREE.Mesh(new THREE.SphereGeometry(0.045, 12, 12), new THREE.MeshBasicMaterial({ color: palette.active }));
    ring.add(comet);
    comets.push({ mesh: comet, r, a: Math.random() * Math.PI * 2 });
  }

  // ---- state driven by data
  const state = { total: 0, done: 0, high: 0, rps: 0, waf: 0, running: false, target: { done: 0, high: 0 } };
  const shown = { done: 0, high: 0 };

  function paint(dt, t) {
    // ease displayed counts toward data (smooth even with 2 s polling)
    const k = reducedMotion() ? 1 : Math.min(1, dt * 2.5);
    shown.done += (state.target.done - shown.done) * k;
    shown.high += (state.target.high - shown.high) * k;
    const doneDots = Math.round(shown.done * N);
    const highDots = Math.round(shown.high * N);
    const frontier = state.running ? Math.max(8, Math.round(N * 0.015)) : 0;
    const pr = palette;
    for (let i = 0; i < N; i++) {
      const rk = rank[i], o = i * 3;
      let tx, ty, tz, c;
      if (rk < doneDots) {
        tx = inner[o]; ty = inner[o + 1]; tz = inner[o + 2];
        c = rk < highDots ? pr.issue : pr.done;
      } else if (rk < doneDots + frontier) {
        const f = 0.8 + 0.2 * Math.sin(t * 6 + phase[i]);
        tx = home[o] * f; ty = home[o + 1] * f; tz = home[o + 2] * f;
        c = pr.active;
      } else {
        tx = home[o]; ty = home[o + 1]; tz = home[o + 2];
        c = pr.pending;
      }
      const m = reducedMotion() ? 1 : Math.min(1, dt * 3.2);
      pos[o] += (tx - pos[o]) * m; pos[o + 1] += (ty - pos[o + 1]) * m; pos[o + 2] += (tz - pos[o + 2]) * m;
      const dim = rk >= doneDots + frontier ? (pr.light ? 0.75 : 0.42) : 1;
      col[o] = c.r * dim; col[o + 1] = c.g * dim; col[o + 2] = c.b * dim;
    }
    geom.attributes.position.needsUpdate = true;
    geom.attributes.color.needsUpdate = true;
  }

  // ---- interaction: drag to rotate, gentle auto-spin otherwise
  let dragging = false, lx = 0, ly = 0, vy = 0, vx = 0;
  const el = renderer.domElement;
  el.style.touchAction = "none";
  el.addEventListener("pointerdown", (e) => { dragging = true; lx = e.clientX; ly = e.clientY; el.setPointerCapture(e.pointerId); });
  el.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    vy = (e.clientX - lx) * 0.005; vx = (e.clientY - ly) * 0.005; lx = e.clientX; ly = e.clientY;
    world.rotation.y += vy; world.rotation.x = Math.max(-0.8, Math.min(0.8, world.rotation.x + vx));
  });
  el.addEventListener("pointerup", () => { dragging = false; });

  // ---- sizing + loop
  const resize = () => {
    const w = container.clientWidth || 600, hgt = container.clientHeight || 360;
    renderer.setSize(w, hgt, false);
    camera.aspect = w / hgt;
    camera.position.z = w < 520 ? 11 : 9.2;
    camera.updateProjectionMatrix();
  };
  const ro = new ResizeObserver(resize);
  ro.observe(container);
  resize();

  let visible = true, raf = 0, last = performance.now(), t = 0;
  const io = new IntersectionObserver((ents) => { visible = ents.some((e) => e.isIntersecting); if (visible) loop(); });
  io.observe(container);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loop(); });

  function frame(now) {
    raf = 0;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    t += dt;
    const speed = 0.15 + Math.min(1.5, state.rps / 10);           // rings/comets follow the request rate
    if (!dragging && !reducedMotion()) world.rotation.y += dt * 0.08;
    rings.forEach((r, i) => { r.rotation.z += dt * speed * (i % 2 ? -0.35 : 0.25); });
    comets.forEach((c, i) => {
      c.a += dt * speed * (1.4 + i * 0.3);
      c.mesh.position.set(Math.cos(c.a) * c.r, Math.sin(c.a) * c.r, 0);
      c.mesh.visible = state.running;
    });
    const pulse = state.running ? 1 + 0.06 * Math.sin(t * (2 + speed * 2)) : 1;
    core.scale.setScalar(pulse);
    wire.rotation.x += dt * 0.2 * speed; wire.rotation.y += dt * 0.3 * speed;
    coreMat.color.copy(state.waf > 0 ? palette.warn : palette.done);
    paint(dt, t);
    renderer.render(scene, camera);
    if (!reducedMotion() && visible && !document.hidden) loop();
  }
  function loop() { if (!raf) raf = requestAnimationFrame(frame); }
  loop();

  return {
    ok: true,
    /** total pages, done pages, pages with HIGH issues, live req/s, WAF events, running? */
    update({ total = 0, done = 0, high = 0, rps = 0, waf = 0, running = false }) {
      Object.assign(state, { total, done, high, rps, waf, running });
      state.target.done = total ? Math.min(1, done / total) : 0;
      state.target.high = total ? Math.min(state.target.done, high / total) : 0;
      if (reducedMotion()) { last = performance.now(); frame(last); }
      else loop();
    },
    pagesPerDot(total) { return total ? Math.ceil(total / N) : 0; },
    setTheme() {
      readPalette(); applyBlending();
      wireMat.color.copy(palette.accent); rings.forEach((r) => r.material.color.copy(palette.done)); loop();
    },
    dispose() {
      cancelAnimationFrame(raf); ro.disconnect(); io.disconnect();
      geom.dispose(); pmat.dispose(); renderer.dispose(); renderer.domElement.remove();
    },
  };
}
