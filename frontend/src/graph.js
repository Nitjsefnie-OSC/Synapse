/**
 * Obsidian-style force graph (vanilla Canvas2D) — shared by dashboard and explorer.
 * Visual language (ARIA, kit REV 2.2): hue = repo · brightness & size = connectedness ·
 * soft convex-hull blobs group each repo (the sibling spaghetti is hidden by default) ·
 * curved edges · glowing draggable repo hubs (children move along) · zoom-adaptive labels ·
 * double-click zooms to a node.
 */
export function createGraph(canvas, { tooltipEl, infoEl, onNodeClick }) {
  const ctx = canvas.getContext('2d');
  let nodes = [], edges = [];
  let sim = { p: new Map(), alpha: 0, hover: null, focus: null, drag: null, view: { x: 0, y: 0, k: 1 }, moved: false, lastW: 0 };
  let repoHue = new Map();
  const pinned = new Set();                   // dragged nodes/hubs STAY where you put them
  const hiddenEdges = new Set(['sibling']);   // grouping is shown as hulls, not spaghetti
  const hiddenRepos = new Set();
  let matchSet = null;   // Set of note ids matching the filter (null = no filter)
  let pathIds = null;    // ordered node ids of a traced path (Epic G) — drawn as a glowing route

  // Semantic zoom (D-8): the z-axis of a 2D map — zoom level = importance altitude. The
  // long tail (notes outside the importance window) lives here as STATIC dots haloed around
  // their repo hub: no physics, drawn only past REVEAL_K, hoverable + clickable (a click
  // promotes the note into the living graph via the host's onNodeClick → pull-in).
  const REVEAL_K = 1.5;
  // When the host is showing the ENTIRE brain (explorer `?cap=all`) the per-frame edge
  // ceiling has to follow it — otherwise "all nodes" renders with most of its links
  // silently missing, which is exactly the dishonesty the budgets exist to avoid.
  // Viewport culling and big-mode edge-typing still apply; only the ceiling moves.
  // (the URL is read here too — the host writes the localStorage key AFTER this module is
  // constructed, so a first load with `?cap=all` would otherwise render the thinned view)
  const SHOW_ALL = new URLSearchParams(location.search).get('cap') === 'all'
    || (localStorage.getItem('synapse.windowCap') ?? '') === 'all';
  let statics = [];        // [{ n, dx, dy }] — deterministic halo offset from the repo hub
  let staticsDrawn = 0;    // last-frame count (exposed via state() for honest verification)
  let layerCounts = {};    // last-frame per-layer draw counts (edges/labels) — state() too

  const W = () => canvas.clientWidth, H = () => canvas.clientHeight || 340;
  // Beyond the POC budget (~2k nodes) the O(n²) physics + full edge draw would freeze the tab:
  // degrade honestly — spring/gravity-only layout, wikilink-only edges until zoomed, no hulls.
  // MEMOISED (recomputed in setData): this used to re-filter the whole node array on every
  // call — and it is called once PER NODE inside draw(). On an 11k-note brain that is ~120M
  // comparisons a frame, and it dominated the profile (53% of samples) long before any
  // stroke did. The threshold semantics are unchanged.
  let noteCount = 0;
  const big = () => noteCount > 3000;
  const hue = (repo) => repoHue.get(repo) ?? 220;
  const repoColor = (n) => `hsl(${hue(n.repo)} 65% ${52 + Math.min(n.in_degree + n.out_degree, 12) * 2.2}%)`;

  /* ── NODE CLASSES (founder ask 2026-08-04) ──────────────────────────────────
   * hue=repo says only WHERE a note came from — useless in a one-repo brain, where an
   * article, a LinkedIn post and a hero image all render identically. A class assigns
   * {colour, shape, size} by matching the node's own path/name/tag, so the canvas says
   * what a node IS. Served by GET /api/v1/node-classes, CLI-managed (`synapse classes`),
   * FIRST MATCH WINS. Empty list ⇒ every node falls back to the original repo-hue circle,
   * so this is additive: nothing regresses when no classes are configured. */
  let nodeClasses = [];
  const classCache = new Map();
  function classOf(n) {
    if (!nodeClasses.length || n.kind === 'repo' || n.kind === 'ghost') return null;
    if (classCache.has(n.id)) return classCache.get(n.id);
    const path = n.source_path || '';
    const name = path.slice(path.lastIndexOf('/') + 1);
    let hit = null;
    for (const c of nodeClasses) {
      const m = c.match || {};
      if (m.path_contains && !path.includes(m.path_contains)) continue;
      if (m.name_contains && !name.toLowerCase().includes(String(m.name_contains).toLowerCase())) continue;
      if (m.tag && !(n.tags || []).includes(m.tag)) continue;
      if (!m.path_contains && !m.name_contains && !m.tag) continue;   // a ruleless class matches nothing
      hit = c; break;
    }
    classCache.set(n.id, hit);
    return hit;
  }
  const noteColor = (n) => classOf(n)?.color ?? repoColor(n);

  /** Draw one node's symbol. Shapes are how a class reads at a glance when colours are
   *  close together (and the only cue that survives a colour-blind viewer). */
  function drawSymbol(ctx, shape, x, y, r) {
    ctx.beginPath();
    switch (shape) {
      case 'square':   ctx.rect(x - r, y - r, r * 2, r * 2); break;
      case 'diamond':  ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath(); break;
      case 'triangle': ctx.moveTo(x, y - r); ctx.lineTo(x + r, y + r * 0.8); ctx.lineTo(x - r, y + r * 0.8); ctx.closePath(); break;
      case 'star': {
        for (let i = 0; i < 10; i++) {
          const rad = i % 2 ? r * 0.46 : r, a = (Math.PI / 5) * i - Math.PI / 2;
          i ? ctx.lineTo(x + Math.cos(a) * rad, y + Math.sin(a) * rad)
            : ctx.moveTo(x + Math.cos(a) * rad, y + Math.sin(a) * rad);
        }
        ctx.closePath(); break;
      }
      case 'hexagon': {
        for (let i = 0; i < 6; i++) {
          const a = (Math.PI / 3) * i - Math.PI / 2;
          i ? ctx.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r)
            : ctx.moveTo(x + Math.cos(a) * r, y + Math.sin(a) * r);
        }
        ctx.closePath(); break;
      }
      default: ctx.arc(x, y, r, 0, 7);
    }
    ctx.fill();
  }

  function setData(n, e, { preserve = false } = {}) {
    // preserve=true (incremental pull-in): keep the camera, every existing position and every
    // pin — promoting a zoom-revealed note must NOT snap the view to k=1 and reshuffle the
    // layout the user was working in (second-opinion finding #3). Default (false) is a fresh
    // layout — that's what refresh and ⟲ reset mean.
    nodes = n; edges = e;
    noteCount = nodes.reduce((c, x) => c + (x.kind === 'note' ? 1 : 0), 0);
    const repos = nodes.filter(x => x.kind === 'repo');
    repoHue = new Map(repos.map((r, i) => [r.repo, (i * 137.5) % 360]));
    if (!preserve) sim.p = new Map();
    repos.forEach((r, i) => {
      if (!sim.p.has(r.id)) sim.p.set(r.id, { x: W() * (i + 1) / (repos.length + 1), y: H() / 2, vx: 0, vy: 0 });
    });
    for (const nd of nodes) {
      if (nd.kind === 'repo' || sim.p.has(nd.id)) continue;
      // ghosts have no repo hub — seed beside their first referrer (Epic N), else the hub
      const hub = (nd.seedNear && sim.p.get(nd.seedNear))
        ?? sim.p.get(`repo:${nd.repo}`) ?? { x: W() / 2, y: H() / 2 };
      sim.p.set(nd.id, { x: hub.x + Math.sin(nd.id.length * 7 + nd.id.charCodeAt(0)) * 90,
                         y: hub.y + Math.cos(nd.id.length * 13) * 90, vx: 0, vy: 0 });
    }
    if (!preserve) sim.view = { x: 0, y: 0, k: 1 };
    // a focused note that no longer exists (deleted distill) must not keep spotlight-dimming
    // the whole brain (second-opinion finding #4)
    if (sim.focus && !nodes.some(x => x.id === sim.focus)) sim.focus = null;
    // a traced path must not outlive its data (Codex P2): fresh layouts drop it, and a
    // preserved refresh drops it if any hop vanished from the new node set
    if (pathIds && (!preserve || !pathIds.every(id => nodes.some(x => x.id === id)))) {
      pathIds = null; matchSet = null;
    }
    kick(preserve ? 0.3 : 1);
  }

  function setStatics(list) {
    statics = list.map((n) => {
      let h = 0;
      for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) >>> 0;
      const ang = (h % 3600) / 3600 * Math.PI * 2;
      // uniform-density annulus OUTSIDE the animated cluster — sqrt spread keeps a 20k-note
      // single-root long tail from piling into one thin ring
      const rad = 170 + Math.sqrt(((h >>> 8) % 1000) / 1000) * 620;
      return { n, dx: Math.cos(ang) * rad, dy: Math.sin(ang) * rad };
    });
    draw();
  }

  const visibleNode = (n) => n.kind === 'repo' ? !hiddenRepos.has(n.repo) : !hiddenRepos.has(n.repo);
  const visibleEdge = (e) => !hiddenEdges.has(e.type)
    && visibleNode(nodeById(e.src) ?? {}) && visibleNode(nodeById(e.dst) ?? {});
  const byId = () => new Map(nodes.map(n => [n.id, n]));
  let _byId = null;
  const nodeById = (id) => (_byId ??= byId()).get(id);

  function kick(a) { const was = sim.alpha; sim.alpha = Math.max(sim.alpha, a); _byId = null; _deg = null; if (was <= 0.02) requestAnimationFrame(tick); }

  function tick() {
    const byGroup = new Map();
    for (const n of nodes) {
      const p = sim.p.get(n.id); if (!p) continue;
      const g = n.kind === 'repo' ? '__hubs__' : n.repo;
      if (big() && g !== '__hubs__') continue;   // big mode: springs+gravity only (O(E+N))
      (byGroup.get(g) ?? byGroup.set(g, []).get(g)).push(p);
    }
    for (const [g, pts] of byGroup) {
      const strength = g === '__hubs__' ? 5200 : 110;
      for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
        const a = pts[i], b = pts[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy || 1; if (d2 > 14000 && g !== '__hubs__') continue;
        const f = strength / d2;
        dx *= f; dy *= f; a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
      }
    }
    const degOf = _degMap();
    for (const e of edges) {
      const a = sim.p.get(e.src), b = sim.p.get(e.dst); if (!a || !b) continue;
      const dsum = (degOf.get(e.src) ?? 0) + (degOf.get(e.dst) ?? 0);
      const k = e.type === 'sibling' ? 0.03 : 0.012 / (1 + dsum / 24);
      const rest = e.type === 'sibling' ? 85 : 60 + Math.min(90, dsum * 1.4);
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const f = k * (d - rest) / d;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
    const w = W(), h = H();
    for (const [id, p] of sim.p) {
      if (sim.drag && p === sim.drag.p) continue;
      if (pinned.has(id)) { p.vx = 0; p.vy = 0; continue; }   // pinned = immovable anchor
      // only HUBS feel canvas-centering — notes orbit their hub (sibling springs), so a
      // dragged-away cluster STAYS apart instead of being herded back to the middle
      if (id.startsWith('repo:')) { p.vx += (w / 2 - p.x) * 0.002; p.vy += (h / 2 - p.y) * 0.005; }
      else { p.vx += (w / 2 - p.x) * 0.0006; p.vy += (h / 2 - p.y) * 0.0015; }
      if (p.x < 24) p.vx += (24 - p.x) * 0.06; if (p.x > w - 24) p.vx -= (p.x - (w - 24)) * 0.06;
      if (p.y < 24) p.vy += (24 - p.y) * 0.06; if (p.y > h - 24) p.vy -= (p.y - (h - 24)) * 0.06;
      p.vx = Math.max(-55, Math.min(55, p.vx)); p.vy = Math.max(-55, Math.min(55, p.vy));
      p.x += p.vx * sim.alpha; p.y += p.vy * sim.alpha; p.vx *= 0.8; p.vy *= 0.8;
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) { p.x = w / 2; p.y = h / 2; p.vx = p.vy = 0; }
    }
    sim.alpha *= 0.99;
    draw();
    if (sim.alpha > 0.02) requestAnimationFrame(tick);
  }

  let _deg = null;
  function _degMap() {
    if (_deg) return _deg;
    _deg = new Map(nodes.map(n => [n.id, (n.in_degree ?? 0) + (n.out_degree ?? 0)]));
    return _deg;
  }

  function neighborsOf(id) {
    const set = new Set([id]);
    for (const e of edges) {
      if (e.type === 'sibling') continue;
      if (e.src === id) set.add(e.dst);
      if (e.dst === id) set.add(e.src);
    }
    return set;
  }

  /** Clicking a hub focuses its WHOLE cluster (all notes of that repo + the hub). */
  function clusterSet(repo) {
    const set = new Set([`repo:${repo}`]);
    for (const n of nodes) if (n.kind === 'note' && n.repo === repo) set.add(n.id);
    return set;
  }

  /** Re-fit the layout when the canvas size changes (panel drag/collapse/window resize):
   *  rescale horizontally to the new width, then let the physics settle it. Without this the
   *  layout stays sized for the OLD canvas and gets clipped under the panels. */
  function reflow() {
    // Called continuously during a panel drag: each frame rescales by (newW / prevW), and the
    // per-frame ratios compound to exactly (finalW / startW) — so the graph slides WITH the
    // panel edge instead of getting clipped under it. draw() refreshes sim.lastW afterwards.
    const w = W();
    if (sim.lastW && w > 50 && Math.abs(w - sim.lastW) > 0.5) {
      const ratio = w / sim.lastW;
      for (const [, p] of sim.p) p.x *= ratio;
    }
    kick(0.4);
  }

  function convexHull(pts) {
    if (pts.length < 3) return pts;
    const s = [...pts].sort((a, b) => a.x - b.x || a.y - b.y);
    const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
    const lower = [], upper = [];
    for (const p of s) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
    for (const p of s.reverse()) { while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }

  function drawHulls() {
    if (big()) return;   // 18k-point hulls per frame are not worth the burn
    const byRepo = new Map();
    for (const n of nodes) {
      if (n.kind !== 'note' || hiddenRepos.has(n.repo)) continue;
      const p = sim.p.get(n.id); if (!p) continue;
      (byRepo.get(n.repo) ?? byRepo.set(n.repo, []).get(n.repo)).push(p);
    }
    for (const [repo, pts] of byRepo) {
      if (pts.length < 3) continue;
      const hull = convexHull(pts);
      const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
      const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
      ctx.beginPath();
      hull.forEach((p, i) => {
        const dx = p.x - cx, dy = p.y - cy, d = Math.hypot(dx, dy) || 1;
        const x = p.x + dx / d * 26, y = p.y + dy / d * 26;   // pad the blob outward
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.closePath();
      ctx.fillStyle = `hsl(${hue(repo)} 60% 60% / 0.055)`;
      ctx.strokeStyle = `hsl(${hue(repo)} 60% 60% / 0.16)`;
      ctx.lineWidth = 1.2; ctx.lineJoin = 'round';
      ctx.fill(); ctx.stroke();
    }
  }

  function draw() {
    const dpr = devicePixelRatio;
    canvas.width = W() * dpr; canvas.height = H() * dpr;
    sim.lastW = W();
    ctx.setTransform(dpr * sim.view.k, 0, 0, dpr * sim.view.k, dpr * sim.view.x, dpr * sim.view.y);
    ctx.clearRect(-sim.view.x / sim.view.k, -sim.view.y / sim.view.k, canvas.width / (dpr * sim.view.k), canvas.height / (dpr * sim.view.k));
    // hover-highlight is transient; CLICK-focus persists until you click empty canvas
    const focusSet = sim.focus
      ? (sim.focus.startsWith('repo:') ? clusterSet(sim.focus.slice(5)) : neighborsOf(sim.focus))
      : null;
    // hovering a STATIC (long-tail) dot must not spotlight-dim the whole living graph
    const hood = (sim.hover && sim.p.has(sim.hover)) ? neighborsOf(sim.hover) : focusSet;

    drawHulls();   // soft per-repo blobs — the grouping layer

    // map-engine LOD discipline: symbols and text keep a ~constant SCREEN size while the
    // world zooms underneath, labels declutter on a screen-space grid (no label soup), and
    // every z-layer has a HARD per-frame budget (founder ruling) + viewport culling.
    const K = sim.view.k;
    const BUDGET = { edges: SHOW_ALL ? 60000 : 3500, labels: 80, statics: 800 };
    let edgesDrawn = 0, labelsDrawn = 0;
    const vMinX = -sim.view.x / K, vMinY = -sim.view.y / K;
    const vMaxX = vMinX + W() / K, vMaxY = vMinY + H() / K;
    const inView = (x, y, m = 70) => x > vMinX - m && x < vMaxX + m && y > vMinY - m && y < vMaxY + m;
    const labelCells = new Set();
    const labelFits = (x, y) => {
      if (labelsDrawn >= BUDGET.labels) return false;
      const cell = (((x * K + sim.view.x) / 160) | 0) + ':' + (((y * K + sim.view.y) / 30) | 0);
      if (labelCells.has(cell)) return false;
      labelCells.add(cell); labelsDrawn++; return true;
    };
    // zooming IN keeps symbols at constant screen size (map convention); zooming OUT only
    // softens shrink so the brain stays visible from afar
    const symScale = K < 1 ? Math.sqrt(K) : K;

    // curved edges (slight deterministic bow — reads organic, no two overlap exactly)
    const EDGE = { wikilink: '#7c9eff', relative: '#8b93a6', pathref: '#3f8f81', sibling: '#2c3342', semantic: '#c084fc', ghost: '#6b7280' };
    const BASE = { wikilink: 0.5, relative: 0.36, pathref: 0.18, sibling: 0.13, semantic: 0.45, ghost: 0.3 };
    ctx.lineWidth = 0.75 / Math.sqrt(K);
    const bigMode = big();
    const straight = edges.length > 2000;   // dense brains: straight lines (~2× cheaper strokes)
    for (const e of edges) {
      if (!visibleEdge(e)) continue;
      const lit0 = hood && (e.src === sim.hover || e.dst === sim.hover) && e.type !== 'sibling';
      // big-mode thinning normally keeps only wikilinks until you zoom in — but a brain
      // whose links are mostly pathref/relative would then read as "no connections at all".
      // With SHOW_ALL the type gate lifts; culling + the edge budget still bound the frame.
      if (bigMode && !SHOW_ALL && !lit0 && e.type !== 'wikilink' && sim.view.k < 1.2) continue;
      const a = sim.p.get(e.src), b = sim.p.get(e.dst); if (!a || !b) continue;
      if (!lit0) {
        if (!inView(a.x, a.y) && !inView(b.x, b.y)) continue;   // off-screen — skip
        if (edgesDrawn >= BUDGET.edges) continue;               // layer budget
        edgesDrawn++;
      }
      const lit = lit0;
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      ctx.strokeStyle = lit ? '#e6e6e6' : EDGE[e.type] ?? '#333';
      ctx.globalAlpha = hood ? (lit ? 0.95 : 0.04) : (BASE[e.type] ?? 0.3) * Math.min(1, 240 / d);
      // schema v3 honesty: an AI-derived (INFERRED/AMBIGUOUS) edge never looks identical
      // to an extracted one — uncertainty reads as a dashed line
      const uncertain = (e.confidence && e.confidence !== 'EXTRACTED') || e.type === 'ghost';
      if (uncertain) ctx.setLineDash([5 / K, 4 / K]);
      ctx.beginPath(); ctx.moveTo(a.x, a.y);
      if (straight) ctx.lineTo(b.x, b.y);
      else {
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const bow = Math.min(d * 0.12, 22) * (e.src < e.dst ? 1 : -1);
        ctx.quadraticCurveTo(mx - dy / d * bow, my + dx / d * bow, b.x, b.y);
      }
      ctx.stroke();
      if (uncertain) ctx.setLineDash([]);
    }
    ctx.globalAlpha = 1;

    // path overlay (Epic G): the traced route glows on top of everything, hop by hop
    if (pathIds && pathIds.length > 1) {
      ctx.strokeStyle = '#ffd479'; ctx.lineWidth = 2.6 / K; ctx.globalAlpha = 0.9;
      ctx.beginPath();
      for (let i = 0; i < pathIds.length - 1; i++) {
        const a = sim.p.get(pathIds[i]), b = sim.p.get(pathIds[i + 1]);
        if (!a || !b) continue;
        ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      }
      ctx.stroke(); ctx.globalAlpha = 1;
    }

    for (const n of nodes) {
      const p = sim.p.get(n.id); if (!p || !visibleNode(n)) continue;
      if (n.kind !== 'repo' && !inView(p.x, p.y) && sim.focus !== n.id) continue;   // culled
      if (n.kind === 'repo') {
        // the draggable hub: glowing ring + label (drag it — its children come along)
        ctx.globalAlpha = hood ? 0.4 : 0.95;
        ctx.strokeStyle = `hsl(${hue(n.repo)} 70% 65% / 0.5)`;
        ctx.lineWidth = 2;
        ctx.shadowColor = `hsl(${hue(n.repo)} 70% 60%)`; ctx.shadowBlur = 14;
        ctx.beginPath(); ctx.arc(p.x, p.y, 9, 0, 7); ctx.stroke();
        ctx.shadowBlur = 0;
        ctx.fillStyle = '#e6e6e6'; ctx.font = `600 ${12 / K}px system-ui`; ctx.textAlign = 'center';
        ctx.fillText(n.title, p.x, p.y - 15 / K);
        ctx.globalAlpha = 1;
        continue;
      }
      if (n.kind === 'ghost') {   // Epic N: hollow dashed circle — a note that isn't (yet)
        const gr = 3.5 / symScale;
        ctx.globalAlpha = hood && !hood.has(n.id) ? 0.08 : 0.55;
        ctx.strokeStyle = '#9aa3b2'; ctx.lineWidth = 1 / K;
        ctx.setLineDash([3 / K, 3 / K]);
        ctx.beginPath(); ctx.arc(p.x, p.y, gr, 0, 7); ctx.stroke();
        ctx.setLineDash([]);
        if (K >= 1.2 && labelFits(p.x, p.y)) {
          ctx.fillStyle = '#7d8590'; ctx.font = `${10 / K}px system-ui`; ctx.textAlign = 'center';
          ctx.fillText(`👻 ${n.title}`, p.x, p.y - 8 / K);
        }
        ctx.globalAlpha = 1;
        continue;
      }
      const deg = n.in_degree + n.out_degree;
      const inMatch = matchSet?.has(n.id) ?? false;
      const dim = (hood && !hood.has(n.id)) || (matchSet && !inMatch);
      ctx.globalAlpha = dim ? 0.12 : 1;
      if (inMatch) { ctx.shadowColor = '#7c9eff'; ctx.shadowBlur = 14; }
      ctx.fillStyle = noteColor(n);
      if (!dim && deg >= 8) { ctx.shadowColor = `hsl(${hue(n.repo)} 70% 60%)`; ctx.shadowBlur = 10; }
      const cls = classOf(n);
      const r = (2.8 + Math.min(deg, 14) * 0.5) * (cls?.size ?? 1) / symScale;
      if (big() && !inMatch && sim.hover !== n.id) {
        ctx.shadowBlur = 0;
        ctx.fillRect(p.x - 1.5, p.y - 1.5, 3, 3);   // fast path: 18k arcs/frame won't fly
      } else if (cls?.shape && cls.shape !== 'circle') {
        drawSymbol(ctx, cls.shape, p.x, p.y, r);
      } else {
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7); ctx.fill();
      }
      ctx.shadowBlur = 0;
      // asset sidecars (sprint 05): a small 📷/📄 glyph rides the node once zoomed enough
      // to read — assets are visually distinct from prose notes at a glance
      if (!dim && K >= 0.55 && n.tags?.length && !big()) {
        const glyph = n.tags.includes('asset:image') ? '📷' : n.tags.includes('asset:pdf') ? '📄' : '';
        if (glyph) {
          ctx.font = `${10 / symScale}px system-ui`; ctx.textAlign = 'center';
          ctx.fillText(glyph, p.x + r + 7 / symScale, p.y - r - 3 / symScale);
        }
      }
      if (inMatch && !dim) {            // search selection: accent ring on EVERY match
        ctx.strokeStyle = 'rgba(124,158,255,0.85)'; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 3.5, 0, 7); ctx.stroke();
      }
      if (pinned.has(n.id) && !dim) {   // pinned marker: thin white ring
        ctx.strokeStyle = 'rgba(230,230,230,0.65)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 2.5, 0, 7); ctx.stroke();
      }
      if (sim.focus === n.id) {         // the focused node: accent ring
        ctx.strokeStyle = '#e6e6e6'; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 4.5, 0, 7); ctx.stroke();
      }
      // issue #9: a STALE summary (a cited source changed since distill time) wears an
      // amber ring at every zoom — a badge you can only see at one zoom level is the
      // quiet-staleness failure all over again. Re-distill from the distills panel.
      if (n.stale && !dim) {
        ctx.strokeStyle = 'rgba(255,176,32,0.9)'; ctx.lineWidth = 1.8;
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 6, 0, 7); ctx.stroke();
      }
      // labels: hovered + small neighborhoods always; zoomed-in hubs pass the DECLUTTER grid
      const label = sim.hover === n.id || (hood?.has(n.id) && hood.size < 14) ||
                    (inMatch && matchSet.size <= 15) ||
                    (!hood && !matchSet && K >= 1.4 && deg >= 6 && labelFits(p.x, p.y));
      if (label && !dim) {
        ctx.fillStyle = '#e6e6e6'; ctx.font = `${11 / K}px system-ui`; ctx.textAlign = 'center';
        ctx.fillText(n.title.slice(0, 34), p.x, p.y - 9 / K);
      }
      ctx.globalAlpha = 1;
    }

    // ── the reveal layer: past REVEAL_K the long tail fades in around its repo hub ──
    staticsDrawn = 0;
    if (statics.length && sim.view.k >= REVEAL_K) {
      const t = Math.min(1, (sim.view.k - REVEAL_K) / 0.8);   // fade in over ~one zoom stop
      ctx.globalAlpha = 0.3 * t;
      for (const s of statics) {
        if (hiddenRepos.has(s.n.repo) || sim.p.has(s.n.id)) continue;
        const hub = sim.p.get(`repo:${s.n.repo}`); if (!hub) continue;
        const x = hub.x + s.dx, y = hub.y + s.dy;
        if (!inView(x, y, 0)) continue;
        const ds = 2.4 / sim.view.k;   // constant screen size, smaller than the living nodes
        ctx.fillStyle = `hsl(${hue(s.n.repo)} 45% 50%)`;
        ctx.fillRect(x - ds / 2, y - ds / 2, ds, ds);
        if (s.n.id === sim.hover) {
          ctx.globalAlpha = 1;
          ctx.fillStyle = '#e6e6e6'; ctx.font = `${11 / sim.view.k}px system-ui`; ctx.textAlign = 'center';
          ctx.fillText(s.n.title.slice(0, 34), x, y - 8 / sim.view.k);
          ctx.globalAlpha = 0.3 * t;
        }
        if (++staticsDrawn >= BUDGET.statics) break;   // layer budget — honesty over spectacle
      }
      ctx.globalAlpha = 1;
    }
    layerCounts = { edges: edgesDrawn, labels: labelsDrawn };
    if (infoEl) infoEl.textContent = big() ? 'large brain: simplified rendering (zoom in for detail)' :
      `${nodes.filter(n => n.kind === 'note' && visibleNode(n)).length} notes · ${edges.filter(visibleEdge).length} edges shown · hue = repo · brightness/size = connectedness`;
  }

  const toWorld = (ev) => {
    const r = canvas.getBoundingClientRect();
    return { x: ((ev.clientX - r.left) - sim.view.x) / sim.view.k, y: ((ev.clientY - r.top) - sim.view.y) / sim.view.k };
  };
  const nodeAt = (w) => {
    let best = null, bd = 11 / sim.view.k;
    for (const n of nodes) {
      if (!visibleNode(n)) continue;
      const p = sim.p.get(n.id); if (!p) continue;
      const r = n.kind === 'repo' ? 16 / sim.view.k : bd;   // hubs get a bigger grab radius
      const d = Math.hypot(p.x - w.x, p.y - w.y);
      if (d < (n.kind === 'repo' ? r : bd)) { bd = d; best = n; }
    }
    if (!best && sim.view.k >= REVEAL_K) {   // revealed long-tail dots are hoverable/clickable too
      for (const s of statics) {
        if (hiddenRepos.has(s.n.repo) || sim.p.has(s.n.id)) continue;
        const hub = sim.p.get(`repo:${s.n.repo}`); if (!hub) continue;
        const d = Math.hypot(hub.x + s.dx - w.x, hub.y + s.dy - w.y);
        if (d < bd) { bd = d; best = s.n; }
      }
    }
    return best;
  };

  canvas.addEventListener('mousemove', (ev) => {
    const w = toWorld(ev);
    if (sim.drag) {
      sim.moved = true;
      if (sim.drag.p) {
        const dx = w.x - sim.drag.p.x, dy = w.y - sim.drag.p.y;
        sim.drag.p.x = w.x; sim.drag.p.y = w.y;
        // dragging a repo hub carries its children along (rigid move, physics re-settles)
        if (sim.drag.id?.startsWith('repo:')) {
          const repo = sim.drag.id.slice(5);
          for (const n of nodes) {
            if (n.kind !== 'note' || n.repo !== repo) continue;
            const p = sim.p.get(n.id); if (p) { p.x += dx; p.y += dy; }
          }
        }
      } else { sim.view.x += ev.movementX; sim.view.y += ev.movementY; }
      kick(0.12); return;
    }
    const n = nodeAt(w);
    sim.hover = n?.id ?? null;
    canvas.style.cursor = n ? 'pointer' : 'grab';
    if (tooltipEl) {
      if (n) {
        tooltipEl.style.display = 'block';
        tooltipEl.style.left = ev.clientX + 12 + 'px'; tooltipEl.style.top = ev.clientY + 12 + 'px';
        tooltipEl.textContent = n.kind === 'ghost'
          ? `👻 ${n.title} · ${n.in_degree} referrer(s) · ${n.hint ?? 'future note'}`
          : `${n.title} · ${n.repo} · ${n.in_degree + n.out_degree} links`;
      } else tooltipEl.style.display = 'none';
    }
    draw();
  });
  canvas.addEventListener('mousedown', (ev) => {
    const n = nodeAt(toWorld(ev));
    sim.drag = { p: n ? sim.p.get(n.id) : null, id: n?.id }; sim.moved = false;
    if (n) kick(0.3);
  });
  addEventListener('mouseup', () => {
    if (!sim.drag) return;
    if (sim.moved && sim.drag.id) {
      pinned.add(sim.drag.id);           // you placed it — it STAYS (right-click to release)
    } else if (!sim.moved) {
      if (sim.drag.id?.startsWith('repo:')) sim.focus = sim.drag.id;         // hub → focus cluster
      else if (sim.drag.id) { sim.focus = sim.drag.id; onNodeClick?.(sim.drag.id); }
      else { sim.focus = null; if (pathIds) { pathIds = null; matchSet = null; } }   // empty canvas → defocus + clear path
      draw();
    }
    sim.drag = null;
  });
  canvas.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();                 // right-click a pinned node/hub = release it
    const n = nodeAt(toWorld(ev));
    if (n && pinned.has(n.id)) { pinned.delete(n.id); kick(0.4); }
  });
  function animView(tx, ty, targetK) {
    const s = { x: sim.view.x, y: sim.view.y, k: sim.view.k };
    let step = 0;
    const anim = () => {
      step++;
      const t = step / 8;
      sim.view = { x: s.x + (tx - s.x) * t, y: s.y + (ty - s.y) * t, k: s.k + (targetK - s.k) * t };
      draw();
      if (step < 8) requestAnimationFrame(anim);
    };
    requestAnimationFrame(anim);
  }
  function animTo(p, targetK = 1.7) {
    animView(W() / 2 - p.x * targetK, H() / 2 - p.y * targetK, targetK);
  }
  function fitTo(ids) {
    const pts = ids.map(id => sim.p.get(id)).filter(Boolean);
    if (!pts.length) return;
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const minX = Math.min(...xs) - 70, maxX = Math.max(...xs) + 70;
    const minY = Math.min(...ys) - 70, maxY = Math.max(...ys) + 70;
    const k = Math.min(1.6, Math.max(0.3, Math.min(W() / (maxX - minX), H() / (maxY - minY))));
    animView(W() / 2 - (minX + maxX) / 2 * k, H() / 2 - (minY + maxY) / 2 * k, k);
  }
  canvas.addEventListener('dblclick', (ev) => {
    const n = nodeAt(toWorld(ev));
    if (!n || n.kind !== 'note') return;
    const p = sim.p.get(n.id); if (p) animTo(p);
  });
  canvas.addEventListener('mouseleave', () => { sim.hover = null; if (tooltipEl) tooltipEl.style.display = 'none'; });
  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const k = Math.min(4, Math.max(0.35, sim.view.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
    sim.view.x = mx - (mx - sim.view.x) * (k / sim.view.k);
    sim.view.y = my - (my - sim.view.y) * (k / sim.view.k);
    sim.view.k = k;
    draw();
  }, { passive: false });
  addEventListener('resize', () => { if (nodes.length) draw(); });

  return {
    setData,
    setStatics,
    redraw: draw,
    reflow,
    reset() { pinned.clear(); sim.focus = null; setData(nodes, edges); },   // fresh layout, all pins released
    /** Highlight a node (persistent focus); zoom=true also flies the camera to it. */
    focusOn(id, { zoom = false } = {}) {
      if (!sim.p.has(id)) return;
      sim.focus = id;
      if (zoom) animTo(sim.p.get(id));
      else draw();
    },
    setMatch(set) { pathIds = null; matchSet = set; if (set?.size) fitTo([...set]); else draw(); },
    /** Glow an ordered route (Epic G path mode); null clears. Dims the rest like a match. */
    setPath(ids) {
      pathIds = ids?.length > 1 ? [...ids] : null;
      matchSet = pathIds ? new Set(pathIds) : null;
      if (pathIds) fitTo(pathIds); else draw();
    },
    toggleEdgeType(t) { hiddenEdges.has(t) ? hiddenEdges.delete(t) : hiddenEdges.add(t); draw(); return !hiddenEdges.has(t); },
    toggleRepo(r) { hiddenRepos.has(r) ? hiddenRepos.delete(r) : hiddenRepos.add(r); draw(); return !hiddenRepos.has(r); },
    repoColors: () => new Map([...repoHue].map(([r, h]) => [r, `hsl(${h} 65% 62%)`])),
    /** Install the visual vocabulary (GET /api/v1/node-classes). Recolours immediately —
     *  never needs an ingest or a rebuild, so `synapse classes set …` + reload is the loop. */
    setNodeClasses(list) { nodeClasses = Array.isArray(list) ? list : []; classCache.clear(); draw(); },
    /** What the glossary renders: every class + how many visible notes currently match it. */
    nodeClassLegend() {
      const counts = new Map();
      for (const n of nodes) {
        if (n.kind !== 'note') continue;
        const c = classOf(n);
        if (c) counts.set(c.id, (counts.get(c.id) ?? 0) + 1);
      }
      return nodeClasses.map(c => ({ ...c, count: counts.get(c.id) ?? 0 }));
    },
    state: () => ({ hiddenEdges: [...hiddenEdges], hiddenRepos: [...hiddenRepos], hasMatch: !!matchSet, pinned: [...pinned], focus: sim.focus, path: pathIds ? [...pathIds] : null, statics: statics.length, staticsDrawn, layerCounts: { ...layerCounts }, zoom: sim.view.k, view: { ...sim.view }, hubAt: (repo) => (nodes.some(n => n.id === `repo:${repo}`) ? { ...(sim.p.get(`repo:${repo}`) ?? {}) } : {}), posOf: (id) => (nodes.some(n => n.id === id) ? { ...(sim.p.get(id) ?? {}) } : {}) }),
  };
}
