/** SYNAPSE explorer — sprint-02 Epic C, implements ui_kit/explorer KIT.md REV 2 1:1. */
import { api, health } from './api.js';
import { buildNamespace, createReader, esc, loadConventions } from './wiki.js';
import { createGraph } from './graph.js';

const $ = (id) => document.getElementById(id);
let nodes = [], edges = [], stats = null, ns = null;

// ── reader (docked wiki panel) ──────────────────────────────────────────────
const reader = createReader({
  crumbEl: $('reader-crumb'),
  bodyEl: $('reader-body'),
  backBtn: $('reader-back'),
  getNodes: () => nodes,
  getNs: () => ns,
  onShow: () => { if (innerWidth <= 700) document.body.dataset.reader = 'open'; else expandReader(); },
  // fires on EVERY open — including in-body wikilink clicks and Back, which never go through
  // reader.openNote — so the AI panel / delete button always act on the note actually shown
  onOpen: (id) => {
    currentOpenId = id;
    aiButtons(); updateDeleteBtn();
    // a note opened via search/wikilink while path mode is armed → disarm (the armed
    // prompt would otherwise go stale and the next canvas click surprise-picks)
    if (pathSel) { pathSel = null; $('pathBtn').dataset.on = ''; }
    if (!id) return;   // null = the Index view
    ac.notesOpened.add(id); acSave();
    pullIntoWindow(id);   // off-window note? bring it (+ neighbors) into the picture first
    graph.focusOn(id, { zoom: window.__zoomNext ?? false });   // show WHERE the note lives
    window.__zoomNext = false;
    renderExplain(id);    // Epic G: the note's connections, grouped, as a wiki footer
  },
  onError: (m) => setMsg(m, true),
});
window.readerBack = () => reader.back();
function updateDeleteBtn() {
  $('reader-del').style.display = currentOpenId?.startsWith('S — ') ? '' : 'none';
}
window.deleteSummary = async () => {
  if (!currentOpenId?.startsWith('S — ')) return;
  if (!(await appConfirm(`${currentOpenId}\n\nThis distilled summary (and its rendered image) will be deleted from the vault.`,
                          { title: 'Delete this summary?', ok: 'Delete', danger: true }))) return;
  try {
    await api(`/note/${encodeURIComponent(currentOpenId)}`, { method: 'DELETE' });
    await api('/rebuild', { method: 'POST' });
    currentOpenId = null; aiButtons(); updateDeleteBtn();
    $('reader-crumb').textContent = 'nothing open';
    $('reader-body').innerHTML = '<p class="reader-empty">Summary deleted.</p>';
    await refresh();
    setMsg('summary deleted + graph rebuilt');
  } catch (e) { setMsg(e.message, true); }
};
window.viewIndex = () => reader.loadIndex();

// ── graph ───────────────────────────────────────────────────────────────────
const graph = createGraph($('graph'), {
  tooltipEl: $('tooltip'),
  infoEl: null,
  onNodeClick: (id) => {
    if (id.startsWith('ghost:')) {   // a ghost has no note to open — explain it instead
      setMsg(`👻 ${id.slice(6)} — a future note (nothing to open yet); its referrers point at it`);
      return;
    }
    if (!pathPick(id)) reader.openNote(id);
  },
});
window.__synapse = { graph: () => graph.state(), counts: () => ({ nodes: nodes.length, edges: edges.length }) };
// camera framing seam (E2E + debugging): fit the view to ids exactly like a search
// multi-select does, then clear the match without moving the camera
window.__synapseFit = (ids) => graph.setMatch(ids?.length ? new Set(ids) : null);
window.resetLayout = () => { graph.reset(); setMsg('layout reset — all pins released'); };

// ── Path mode (Epic G): ⇢ button → click two notes → the shortest path glows ──
let pathSel = null;   // null = off · [] = picking first · [a] = picking second
window.togglePathMode = () => {
  pathSel = pathSel ? null : [];
  $('pathBtn').dataset.on = pathSel ? '1' : '';
  graph.setPath(null);
  setMsg(pathSel ? 'path: click the FIRST note' : 'path mode off');
};
function pathPick(id) {
  if (!pathSel || id.startsWith('repo:')) return false;
  pathSel.push(id);
  if (pathSel.length === 1) { setMsg(`path: ${id} ⇢ … click the SECOND note`); return true; }
  const [a, b] = pathSel;
  pathSel = null; $('pathBtn').dataset.on = '';
  api(`/path?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`).then((out) => {
    if (!out.found) { setMsg(`no path between ${a} and ${b} (repo-hub links don't count)`, true); return; }
    // D-7 windowed brain (GBU P2-5): intermediate hops are usually low-degree notes OUTSIDE
    // the importance window — pull every hop in first, or the glow renders with holes
    out.hops.forEach((h) => pullIntoWindow(h.id));
    graph.setPath(out.hops.map((h) => h.id));
    ac.pathFound = true; acSave();   // acceptance s41: the app just proved path mode
    setMsg(`path: ${out.length} hop${out.length === 1 ? '' : 's'} — ${a} ⇢ ${b} (click empty canvas to clear)`);
  }).catch((e) => setMsg(e.message, true));
  return true;
}

// ── Explain block (Epic G): the open note's connections as a wiki footer ─────
let explainSeq = 0;   // Codex P2: rapid A→B→A opens raced — only the LAST request may render
async function renderExplain(id) {
  const seq = ++explainSeq;
  document.getElementById('explain')?.remove();
  if (!id) return;
  try {
    const out = await api(`/explain?id=${encodeURIComponent(id)}`);
    if (seq !== explainSeq || currentOpenId !== id) return;   // a newer open superseded us
    document.getElementById('explain')?.remove();             // never two footers
    // sibling edges are repo plumbing, not knowledge — the footer shows real links only
    const groups = (out.connections ?? []).filter((g) => g.type !== 'sibling');
    if (!groups.length) {   // honest empty state — an isolated note SAYS so
      const empty = document.createElement('div');
      empty.id = 'explain';
      empty.innerHTML = '<h4>⛓ Connections · 0</h4><div class="exrow">no knowledge links yet — only repo grouping</div>';
      $('reader-body').appendChild(empty);
      ac.explainSeen = true; acSave();
      return;
    }
    const shown = groups.reduce((s, g) => s + g.nodes.length, 0);
    const rows = groups.map((g) => {
      const arrow = g.direction === 'out' ? '→' : '←';
      const links = g.nodes.slice(0, 12)
        .map((n) => `<a data-wl="${esc(n.id)}">${esc(n.title || n.id)}</a>`).join(' · ');
      const more = g.nodes.length > 12 ? ` <span class="muted">+${g.nodes.length - 12} more</span>` : '';
      return `<div class="exrow">${arrow} <b>${esc(g.type)}</b> · ${links}${more}</div>`;
    }).join('');
    const el = document.createElement('div');
    el.id = 'explain';
    el.innerHTML = `<h4>⛓ Connections · ${shown}</h4>${rows}`;
    $('reader-body').appendChild(el);
    ac.explainSeen = true; acSave();   // acceptance s42: the footer rendered with real links
  } catch { /* no graph yet — the empty state already guides the user */ }
}

/** In-app confirm (output doctrine: the APP's popup, never the browser's native dialog). */
function appConfirm(msg, { title = 'Confirm', ok = 'Confirm', danger = false } = {}) {
  return new Promise((resolve) => {
    $('am-title').textContent = title;
    $('am-msg').textContent = msg;
    const okBtn = $('am-ok');
    okBtn.textContent = ok;
    okBtn.classList.toggle('danger', danger);
    $('appmodal').classList.add('open');
    const done = (v) => { $('appmodal').classList.remove('open'); cleanup(); resolve(v); };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(false); } };
    const onOut = (e) => { if (e.target === $('appmodal')) done(false); };
    function cleanup() {
      okBtn.removeEventListener('click', onOk);
      $('am-cancel').removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onKey, true);
      $('appmodal').removeEventListener('click', onOut);
    }
    okBtn.addEventListener('click', onOk);
    $('am-cancel').addEventListener('click', onCancel);
    document.addEventListener('keydown', onKey, true);
    $('appmodal').addEventListener('click', onOut);
    okBtn.focus();
  });
}

// ── search results dropdown (founder: dimming alone reads as "search doesn't work") ──
let selIdx = -1;
function renderResults() {
  const q = $('filter').value.trim().toLowerCase();
  const box = $('sresults');
  if (!q) { box.classList.remove('open'); box.innerHTML = ''; selIdx = -1; return; }
  const colors = graph.repoColors();
  const grpOf = vRepoOf();
  // sprint 06 T1: the lens reorders BEFORE the cut to 12, or it would only sort the 12 that
  // relevance already picked — a sort of a pre-filtered slice is not the sort the user asked for.
  const hits = (typeof applyLens === 'function' ? applyLens(matchList(q)) : matchList(q)).slice(0, 12);
  box.innerHTML = hits.length
    ? hits.map((n, i) =>
        `<div class="r ${i === selIdx ? 'sel' : ''}" data-open="${esc(n.id)}">
           <span class="dot" style="background:${colors.get(grpOf.get(n.id) ?? n.repo)}"></span>
           ${typeof newMark === 'function' ? newMark(n) : ''}
           <span>${n.repo === '✦ summaries' ? '✦ ' : ''}${esc(n.title)}</span><small>${esc(grpOf.get(n.id) ?? n.repo)} · ${n.in_degree + n.out_degree} links${typeof lensMeta === 'function' ? lensMeta(n) : ''}</small></div>`).join('')
    : `<div class="r" style="color:var(--dim)">no notes match “${esc(q)}”</div>`;
  box.classList.add('open');
}
document.addEventListener('click', (ev) => {
  if (!ev.target.closest('.searchwrap')) { $('sresults').classList.remove('open'); selIdx = -1; }
});

// ── distills panel (founder: like Sources — a panel, not a popup): list every ✦ summary,
//    click to read, checkbox one-or-many + bulk delete ───────────────────────
window.toggleDistills = () => {
  const el = $('distills');
  el.classList.toggle('open');
  if (el.classList.contains('open')) { $('sources').classList.remove('open'); buildDistills(); }
};
function buildDistills() {
  const list = nodes.filter(n => n.kind === 'note' && n.repo === '✦ summaries')
    .sort((a, b) => a.title.localeCompare(b.title));
  const staleCount = list.filter(n => n.stale).length;
  const rows = list.map(n => `
    <div class="row">
      <input type="checkbox" data-dsel="${esc(n.id)}" title="select for bulk delete" />
      <span data-open-note="${esc(n.id)}" title="${esc(n.title)}"
            style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">✦ ${esc(n.title.replace(/^\s*S\s*—\s*/, ''))}</span>
      ${n.stale ? `<span style="color:var(--bad);white-space:nowrap"
            title="a cited source changed since this summary was distilled">⚠ stale</span>
      <button data-redistill="${esc(n.id)}" style="white-space:nowrap"
            title="re-distill from the current sources (same root, scope and depth)">↻ re-distill</button>` : ''}
      <span class="rootcount">${n.in_degree + n.out_degree} links</span>
    </div>`).join('');
  $('distills').innerHTML =
    `<h4 style="display:flex">✦ Distills — your summaries (${list.length})
       ${staleCount ? `<span style="color:var(--bad);font-weight:normal">&nbsp;· ${staleCount} stale</span>` : ''}
       <span style="margin-left:auto;cursor:pointer;color:var(--dim)" onclick="toggleDistills()">✕</span></h4>
     <div class="srcbtns">
       <button onclick="distillsSelectAll(true)">✓ Select all</button>
       <button onclick="distillsSelectAll(false)">◻ Clear</button>
       <button onclick="deleteSelectedDistills()" style="color:var(--bad)">🗑 Delete selected</button>
     </div>` +
    (rows || `<div class="row" style="color:var(--dim)">no distills yet — open a note and hit ✦ Distill</div>`) +
    `<p style="color:var(--dim);font-size:0.72rem;margin-top:0.5rem">
       Click a title to read it. Deleting removes the summary note AND its rendered image;
       source notes are never touched. ⚠ stale = a cited source changed since the distill —
       ↻ re-distill refreshes it from the current sources (stale summaries also wear an
       amber ring on the canvas).</p>`;
}
// issue #9 — one-click re-distill: the summary note carries its own root/scope/depth,
// so the click is JUST the note id. Same spend gate as a fresh distill.
window.redistillNote = async (id) => {
  $('ai-status').textContent = 're-distilling from the current sources…';
  try {
    let out = await api('/redistill', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note_id: id }) });
    if (out.requires_confirmation) {
      if (!(await appConfirm(`Re-distilling is ~${out.tokens_est.toLocaleString()} tokens (confirmation gate: ${out.threshold.toLocaleString()}).`,
                             { title: 'Cost guard', ok: 'Spend it' }))) {
        $('ai-status').textContent = 'cancelled — nothing spent'; return;
      }
      out = await api('/redistill', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note_id: id, confirm: true }) });
    }
    await api('/rebuild', { method: 'POST' });
    await refresh();
    buildDistills();
    $('ai-status').textContent = `re-distilled ✓ ${out.citations} citation(s)` + (out.truncated ? ' · truncated (disclosed)' : '');
    setMsg(`summary refreshed: ${out.summary_note_id} (${out.model})`);
  } catch (e) { $('ai-status').textContent = e.message; setMsg(e.message, true); }
};
window.distillsSelectAll = (v) => {
  $('distills').querySelectorAll('input[data-dsel]').forEach(cb => { cb.checked = v; });
};
window.deleteSelectedDistills = async () => {
  const ids = [...$('distills').querySelectorAll('input[data-dsel]:checked')].map(cb => cb.dataset.dsel);
  if (!ids.length) { setMsg('no distills selected', true); return; }
  if (!(await appConfirm(`${ids.length} distill(s) will be deleted from the vault (rendered images included).`,
                         { title: 'Delete distills?', ok: `Delete ${ids.length}`, danger: true }))) return;
  let done = 0;
  for (const id of ids) {
    try { await api(`/note/${encodeURIComponent(id)}`, { method: 'DELETE' }); done++; }
    catch (e) { setMsg(e.message, true); }
  }
  await api('/rebuild', { method: 'POST' });
  if (ids.includes(currentOpenId)) {
    currentOpenId = null; aiButtons(); updateDeleteBtn();
    $('reader-crumb').textContent = 'nothing open';
    $('reader-body').innerHTML = '<p class="reader-empty">Summary deleted.</p>';
  }
  await refresh();
  buildDistills();
  setMsg(`${done} distill(s) deleted + graph rebuilt`);
};
$('distills').addEventListener('click', (ev) => {
  const rd = ev.target.closest('[data-redistill]');
  if (rd) { redistillNote(rd.dataset.redistill); return; }
  const t = ev.target.closest('[data-open-note]');
  if (t) { window.__zoomNext = true; reader.openNote(t.dataset.openNote); }
});

// picking a search result = SINGLE selection: clear the multi-match, fly to the node
document.addEventListener('click', (ev) => {
  const t = ev.target.closest('#sresults .r[data-open]');
  if (!t) return;
  ev.preventDefault();
  graph.setMatch(null);
  $('filter').value = '';
  window.__zoomNext = true;
  reader.openNote(t.dataset.open);
  $('sresults').classList.remove('open');
});

// ── importance window ───────────────────────────────────────────────────────
// Above the render budget the graph shows the TOP-N most-connected notes (founder ruling:
// zoom/search-oriented, capped by importance — never all 20k at once). Search, the reader and
// the drawer always cover the FULL brain; opening an off-window note pulls it + its direct
// neighbors into the picture.
//
// The cap is overridable — "show me the ENTIRE graph" is a legitimate ask on a brain that
// fits: `?cap=all` (no window at all) · `?cap=4000` (a wider window) · `?cap=1500` (the
// default). A URL choice sticks (localStorage) so a reload keeps the view you asked for.
const CAP_KEY = 'synapse.windowCap';
const WINDOW_CAP = (() => {
  const q = new URLSearchParams(location.search).get('cap');
  if (q !== null) localStorage.setItem(CAP_KEY, q);
  const raw = localStorage.getItem(CAP_KEY) ?? '1500';
  if (raw === 'all') return Infinity;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : 1500;
})();
let winIds = null;   // Set of displayed ids · null = the whole brain fits the budget
let vNodes = [], vEdges = [];   // the DISPLAY view (grouped); search/reader keep the originals

// A huge single source (e.g. the whole-workspace root) is not one cluster — split it into
// its top-level folders as first-class groups: each gets its own hub, hue, hull and a
// hide/show toggle in the glossary (founder ask). Display-only: ids, search and wiki
// resolution stay on the original fields.
const GROUP_SPLIT = 1500;
// ── Type lens (founder ask): filter the view by node type + regroup assets ───
// Display-only, like everything in this pipeline: search/reader/ids never change.
const LENS_KEY = 'synapse.lens';
// Defensive: localStorage is user-editable and survives across versions, so a malformed or
// stale value must degrade to defaults, never throw at module load — an exception here happens
// BEFORE the app boots and leaves a blank page with no way to recover but clearing site data.
const _lens = (() => {
  try {
    const v = JSON.parse(localStorage.getItem(LENS_KEY) ?? '{}');
    return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
  } catch { return {}; }
})();
const hiddenTypes = new Set(_lens.hidden ?? []);
let assetsGrouped = _lens.assetsGrouped ?? true;   // 📷 assets as their own hull, default ON
function lensSave() {
  localStorage.setItem(LENS_KEY, JSON.stringify({ hidden: [...hiddenTypes], assetsGrouped }));
}
function nodeTypeOf(n) {
  if (n.repo === '✦ summaries' || n.repo?.startsWith('✦ summaries')) return 'distill';
  if (n.tags?.includes('asset:image')) return 'image';
  if (n.tags?.includes('asset:pdf')) return 'pdf';
  // video/audio (2026-08-04): without these a video fell through to 'note', so the type
  // lens had no pill for it and the count landed in 📝 — "I still see no videos" even
  // though six were ingested and linked. Every asset kind ingest can produce needs a
  // lens entry, or it is invisible as a CATEGORY however well it renders as a node.
  if (n.tags?.includes('asset:video')) return 'video';
  if (n.tags?.includes('asset:audio')) return 'audio';
  return 'note';
}

function groupView() {
  const pool = nodes.filter(n => n.kind !== 'note' || !hiddenTypes.has(nodeTypeOf(n)));
  const isAsset = (n) => assetsGrouped && (n.tags?.[0] ?? '').startsWith('asset:');
  const counts = {};
  for (const n of pool) if (n.kind === 'note' && !isAsset(n)) counts[n.repo] = (counts[n.repo] ?? 0) + 1;
  const split = new Set(Object.keys(counts).filter(r => counts[r] > GROUP_SPLIT));
  const anyAssets = pool.some(n => n.kind === 'note' && isAsset(n));
  if (!split.size && !anyAssets && !hiddenTypes.size) return { vnodes: nodes, vedges: edges };
  const groups = new Set();
  const vnotes = pool.filter(n => n.kind === 'note').map(n => {
    const g = isAsset(n) ? `${n.repo} / 📷 assets`
      : split.has(n.repo)
        ? `${n.repo} / ${n.source_path.includes('/') ? n.source_path.split('/')[0] : '·root'}`
        : n.repo;
    groups.add(g);
    return { ...n, repo: g };
  });
  const ids = new Set(vnotes.map(n => n.id));
  const vhubs = [...groups].sort().map(g => ({
    id: `repo:${g}`, kind: 'repo', title: g.includes(' / ') ? g.split(' / ')[1] : g,
    repo: g, source_path: '', in_degree: 0, out_degree: 0, unresolved: [],
  }));
  const vedges = edges.filter(e => e.type !== 'sibling' && ids.has(e.src) && ids.has(e.dst))
    .concat(vnotes.map(n => ({ src: n.id, dst: `repo:${n.repo}`, type: 'sibling' })));
  return { vnodes: [...vnotes, ...vhubs], vedges };
}

// rebuild the display pipeline in place (type/regroup toggles) — camera/pins preserved
function rebuildView() {
  ({ vnodes: vNodes, vedges: vEdges } = groupView());
  _vRepo = null;
  const view = windowed();
  const vg = withGhosts(view.n, view.e);
  graph.setData(vg.n, vg.e, { preserve: true });
  sendStatics();
  buildTypebar();
  const hiddenN = nodes.filter(n => n.kind === 'note' && hiddenTypes.has(nodeTypeOf(n))).length;
  if (stats) {
    const shown = view.n.filter(x => x.kind === 'note').length;
    $('st-notes').textContent = `${stats.notes} notes` +
      (hiddenN ? ` · ${hiddenN} filtered by type` : '') +
      (winIds ? ` · graph: top ${shown} by links · zoom reveals the rest` : '');
  }
}

function buildTypebar() {
  const el = $('typebar'); if (!el) return;
  const counts = { note: 0, image: 0, pdf: 0, video: 0, audio: 0, distill: 0 };
  for (const n of nodes) if (n.kind === 'note') counts[nodeTypeOf(n)]++;
  const PILLS = [['note', '📝'], ['image', '📷'], ['pdf', '📄'], ['video', '🎬'], ['audio', '🎧'], ['distill', '✦']];
  let html = PILLS.filter(([t]) => counts[t] > 0)
    .map(([t, icon]) => {
      const off = hiddenTypes.has(t);
      return `<button class="pill ${off ? 'off' : ''}" data-type="${t}" title="${off ? 'show' : 'hide'} ${t}s">${icon} ${counts[t]}</button>`;
    }).join('');
  if (counts.image + counts.pdf + counts.video + counts.audio > 0) {
    html += `<button class="pill ${assetsGrouped ? '' : 'off'}" data-regroup="1" title="assets as their own group (hull) vs mixed into folders">📦 ${assetsGrouped ? 'assets grouped' : 'assets mixed'}</button>`;
  }
  el.innerHTML = html;
}
window.toggleType = (t) => {
  hiddenTypes.has(t) ? hiddenTypes.delete(t) : hiddenTypes.add(t);
  lensSave(); rebuildView();
  setMsg(hiddenTypes.has(t) ? `${t}s hidden from the graph (search still finds them)` : `${t}s shown`);
};
window.toggleAssetsGroup = () => {
  assetsGrouped = !assetsGrouped;
  lensSave(); rebuildView();
  setMsg(assetsGrouped ? '📦 assets grouped into their own hull per source' : 'assets mixed into their folders');
};
let _vRepo = null;   // cached id → display-group map (rebuilt per refresh)
const vRepoOf = () => (_vRepo ??= new Map(vNodes.filter(n => n.kind === 'note').map(n => [n.id, n.repo])));

// ── Ghost nodes (Epic N): unresolved links become visible "future notes" ─────
// Display-only synthesis from the unresolved lists the graph already carries (same
// doctrine as D-9 groups — ids/search/reader never see ghosts). Default OFF; capped.
const GHOST_CAP = 300;
let ghostsOn = false;
let ghostClassify = null;   // target → {status, hint} from GET /unresolved (lazy)
function ghostView() {
  if (!ghostsOn) return { gn: [], ge: [] };
  const byTarget = new Map();   // normalized target → { refs: [noteIds], raw }
  for (const n of vNodes) {
    if (n.kind !== 'note') continue;
    // on a WINDOWED brain, only in-window referrers count — a ghost whose referrers are
    // all off-window would float edge-less at canvas center (GBU P1: orphan-ghost blob)
    if (winIds !== null && !winIds.has(n.id)) continue;
    for (const u of n.unresolved) {
      const raw = u.replace(/ \(AI-inferred\)$/, '');
      const key = raw.replace(/^\[\[|\]\]$/g, '').trim().toLowerCase();
      const g = byTarget.get(key) ?? { refs: [], raw };
      g.refs.push(n.id);
      byTarget.set(key, g);
    }
  }
  const ranked = [...byTarget.entries()]
    .sort((a, b) => b[1].refs.length - a[1].refs.length || (a[0] < b[0] ? -1 : 1));
  const gn = [], ge = [];
  for (const [key, g] of ranked.slice(0, GHOST_CAP)) {
    const id = `ghost:${key}`;
    const cls = ghostClassify?.[key];
    const label = g.raw.replace(/^\[\[|\]\]$/g, '');
    gn.push({ id, kind: 'ghost', title: label.split('/').pop(), repo: '', source_path: '',
              tags: [], in_degree: g.refs.length, out_degree: 0, unresolved: [],
              seedNear: g.refs[0],   // graph seeds the ghost beside a real referrer
              hint: cls?.hint ?? (g.raw.startsWith('[[') ? 'future note — nothing ingested has this name yet' : 'dead link — the file no longer exists') });
    for (const ref of g.refs) ge.push({ src: ref, dst: id, type: 'ghost' });
  }
  return { gn, ge, capped: Math.max(0, byTarget.size - GHOST_CAP), total: byTarget.size };
}
function withGhosts(n, e) {
  const { gn, ge } = ghostView();
  return gn.length ? { n: [...n, ...gn], e: [...e, ...ge] } : { n, e };
}
window.toggleGhosts = async () => {
  ghostsOn = !ghostsOn;
  if (ghostsOn && ghostClassify === null) {
    try { ghostClassify = (await api('/unresolved')).targets; }
    catch { ghostClassify = {}; }   // classification is garnish — ghosts render without it
  }
  // pure display toggle: rebuild from the CURRENT view, keep camera/positions/pins
  // (GBU P2: a refetch + fresh layout on every 👻 click violated the preserve doctrine)
  const view = windowed();
  const vg = withGhosts(view.n, view.e);
  graph.setData(vg.n, vg.e, { preserve: true });
  buildDrawer();
  const g = ghostsOn ? ghostView() : {};
  setMsg(ghostsOn
    ? `👻 ${Math.min(g.total ?? 0, GHOST_CAP)} future notes shown${g.capped ? ` (${g.capped} more capped — most-referenced win)` : ''}`
    : 'ghosts hidden');
};

function windowed() {
  const notes = vNodes.filter(n => n.kind === 'note');
  if (notes.length <= WINDOW_CAP) { winIds = null; return { n: vNodes, e: vEdges }; }
  const keep = new Set(vNodes.filter(n => n.kind === 'repo').map(n => n.id));
  for (const n of notes) if (n.repo === '✦ summaries') keep.add(n.id);   // user artifacts always shown
  const ranked = notes.filter(n => n.repo !== '✦ summaries')
    .sort((a, b) => (b.in_degree + b.out_degree) - (a.in_degree + a.out_degree));
  for (const n of ranked.slice(0, WINDOW_CAP)) keep.add(n.id);
  winIds = keep;
  return { n: vNodes.filter(x => keep.has(x.id)), e: vEdges.filter(x => keep.has(x.src) && keep.has(x.dst)) };
}

/** The long tail goes to the graph's semantic-zoom reveal layer (D-8): static dots around
 *  their repo hub, visible past zoom 1.5, clickable — a click promotes them into the window. */
function sendStatics() {
  graph.setStatics(winIds ? vNodes.filter(x => x.kind === 'note' && !winIds.has(x.id)) : []);
}

/** Opening a note outside the window pulls it (+ direct neighbors) into the graph. */
function pullIntoWindow(id) {
  if (!winIds || winIds.has(id) || !vNodes.some(x => x.id === id)) return;
  winIds.add(id);
  for (const e of vEdges) {
    if (e.type === 'sibling') continue;
    if (e.src === id) winIds.add(e.dst);
    if (e.dst === id) winIds.add(e.src);
  }
  const pw = withGhosts(vNodes.filter(x => winIds.has(x.id)),
                        vEdges.filter(x => winIds.has(x.src) && winIds.has(x.dst)));
  graph.setData(pw.n, pw.e, { preserve: true });   // incremental: keep camera, positions, pins
  sendStatics();
  // the statusbar must stay honest — the window just grew
  const shown = vNodes.filter(x => x.kind === 'note' && winIds.has(x.id)).length;
  if (stats) $('st-notes').textContent = `${stats.notes} notes · graph: ${shown} in view · zoom reveals the rest`;
}

// ── data ────────────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const g = await api('/graph');
    nodes = g.nodes; edges = g.edges;
    ns = buildNamespace(nodes);            // wiki resolution on the ORIGINAL fields
    ({ vnodes: vNodes, vedges: vEdges } = groupView());
    _vRepo = null;
    stats = await api('/stats');
    // The visual vocabulary (colour/shape/size per node class). Optional by design: an
    // older backend without the route just leaves the canvas on the original repo-hue
    // circles rather than failing the whole load.
    try { graph.setNodeClasses(await api('/node-classes')); } catch { /* defaults stand */ }
    $('empty').style.display = 'none';
    const view = windowed();
    const vg = withGhosts(view.n, view.e);   // Epic N: ghosts ride on top when toggled
    graph.setData(vg.n, vg.e);
    sendStatics();
    buildTypebar();
    $('st-notes').textContent = winIds
      ? `${stats.notes} notes · graph: top ${view.n.filter(x => x.kind === 'note').length} by links · zoom reveals the rest`
      : `${stats.notes} notes`;
    $('st-edges').textContent = `${stats.edges_total} edges`;
    $('st-unres').textContent = `${stats.unresolved_links} unresolved`;
    $('st-unres').style.color = stats.unresolved_links ? 'var(--bad)' : '';
    buildDrawer();
  } catch (e) {
    $('empty').style.display = 'grid';
    $('empty-err').textContent = e.message.includes('No graph') ? '' : e.message;
  }
}

function setMsg(m, bad = false) {
  const el = $('st-msg');
  el.textContent = m; el.style.color = bad ? 'var(--bad)' : 'var(--ok)';
  if (m) setTimeout(() => { if (el.textContent === m) el.textContent = ''; }, 6000);
}

window.runRebuild = async () => {
  setMsg('rebuilding graph + Index from the vault…');
  try {
    const st = await api('/rebuild', { method: 'POST' });
    setMsg(`rebuilt: ${st.notes} notes · ${st.edges_total} edges · ${st.unresolved_links} unresolved`);
    await refresh();
  } catch (e) { setMsg('rebuild failed: ' + e.message, true); }
};

window.runIngest = async () => {
  try {
    // safety: syncing with ZERO enabled roots empties every managed note — ask first
    const roots = await api('/roots');
    if (roots.length && roots.every(r => !r.enabled) &&
        !(await appConfirm('All sources are deselected — running ingest will prune EVERY note from these repos.\n✦ summaries are kept.',
                           { title: 'Empty the brain?', ok: 'Prune everything', danger: true }))) {
      setMsg('ingest cancelled — nothing changed'); return;
    }
  } catch { /* roots unavailable → let ingest itself report */ }
  setMsg('ingesting…');
  try {
    const rep = await api('/ingest', { method: 'POST' });
    await api('/rebuild', { method: 'POST' });
    const t = rep.totals;
    if (t.pruned > 0) { ac.prunedSeen = true; }   // acceptance s43: the sync pruned, honestly counted
    const assets = t.assets_found > 0 ? ` · ${t.assets_found} assets → ${t.assets_written} sidecars` : '';
    // issue #9: a flag that flips QUIETLY is the failure the issue exists to kill — say it
    const ripple = rep.stale_summaries?.length
      ? ` · ⚠ ${rep.stale_summaries.length} summar(y/ies) changed staleness (see ✦ Distills)` : '';
    setMsg(`ingest sync: ${t.files_found} found · ${t.notes_written} written · ${t.unchanged} unchanged · ${t.skipped} skipped · ${t.pruned} pruned${assets}${ripple}`);
    setDirty(false);
    await refresh();
    // an open Sources panel must recount — founder repro: root said "828 notes" while the
    // statusbar honestly said 1963 after an assets Apply
    if ($('sources').classList.contains('open')) await buildSources();
  } catch (e) { setMsg('ingest failed: ' + e.message, true); }
};

// ── filter ↔ graph sync ────────────────────────────────────────────────────
// Relevance scoring — an exact/prefix hit ("ARIA") must never lose to an accidental
// substring ("InvARIAnts", "adversARIAl"). Degree only breaks ties within a tier.
function scoreNote(n, q) {
  // a summary scores by its SUBJECT ("S — ARIA…" answers "aria"), and as a user artifact it
  // gets a boost so it never drowns under better-connected source mirrors (founder repro)
  const isSummary = n.repo === '✦ summaries';
  const t = (isSummary ? n.title.replace(/^\s*S\s*—\s*/, '') : n.title).toLowerCase();
  const id = n.id.toLowerCase();
  const stem = (n.source_path?.split('/').pop() ?? '').replace(/\.md$/, '').toLowerCase();
  let sc = 0;
  if (t === q || stem === q) sc = 100;
  else if (t.startsWith(q) || stem.startsWith(q)) sc = 80;
  else if (t.split(/[^a-z0-9]+/).some(w => w.startsWith(q))) sc = 60;
  else if (id.split(/[^a-z0-9]+/).some(w => w.startsWith(q))) sc = 40;
  else if (t.includes(q) || id.includes(q)) sc = 15;
  return sc && isSummary ? sc + 25 : sc;
}
const matchList = (q) => nodes
  .filter(n => n.kind === 'note')
  .map(n => [scoreNote(n, q), n])
  .filter(([sc]) => sc > 0)
  .sort((a, b) => b[0] - a[0] ||
    (b[1].in_degree + b[1].out_degree) - (a[1].in_degree + a[1].out_degree))
  .map(([, n]) => n);

$('filter').addEventListener('input', () => {
  const q = $('filter').value.trim().toLowerCase();
  graph.setMatch(q ? new Set(matchList(q).map(n => n.id)) : null);
  selIdx = -1;
  renderResults();
});
$('filter').addEventListener('keydown', (ev) => {
  const rows = [...$('sresults').querySelectorAll('.r[data-open]')];
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    selIdx = ev.key === 'ArrowDown' ? Math.min(selIdx + 1, rows.length - 1) : Math.max(selIdx - 1, 0);
    rows.forEach((r, i) => r.classList.toggle('sel', i === selIdx));
    rows[selIdx]?.scrollIntoView({ block: 'nearest' });
    return;
  }
  if (ev.key !== 'Enter') return;
  const target = rows[selIdx]?.dataset.open ?? rows[0]?.dataset.open;
  if (target) { window.__zoomNext = true; reader.openNote(target); $('sresults').classList.remove('open'); }
});

// ── sources (roots) CRUD drawer — D-6 ─────────────────────────────────────
async function buildSources() {
  try {
    const roots = await api('/roots');
    const counts = {};
    for (const n of nodes) if (n.kind === 'note') counts[n.repo] = (counts[n.repo] ?? 0) + 1;
    const rows = roots.map(r => {
      const name = r.path.split('/').pop();
      return `
      <div class="row" title="${esc(r.path)}" style="${r.enabled ? '' : 'opacity:0.5'}">
        <input type="checkbox" ${r.enabled ? 'checked' : ''} data-toggle-root="${esc(r.path)}" title="${r.enabled ? 'enabled — ingested on Apply' : 'disabled — pruned on Apply'}" />
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.path)}</span>
        <span class="rootcount">${counts[name] ?? 0} notes</span>
        <span class="tg" data-assets-root="${esc(r.path)}" style="cursor:pointer;${r.assets ? 'color:var(--accent)' : 'color:var(--dim);opacity:0.6'}" title="${r.assets ? 'assets ON — images/PDFs sync as sidecar notes' : 'assets OFF — click to also ingest images/PDFs from this root'}">📷</span>
        ${r.exists ? '' : '<span class="tag" title="directory not found">missing</span>'}
        <span class="tg" data-del-root="${esc(r.path)}" style="cursor:pointer;color:var(--bad)" title="remove root + prune its notes">✕</span>
      </div>`;
    }).join('');
    $('sources').innerHTML =
      `<h4 style="display:flex">Sources — the repos this brain ingests
         <span style="margin-left:auto;cursor:pointer;color:var(--dim)" onclick="toggleSources()">✕</span></h4>
       <div class="srcbtns">
         <button onclick="bulkRoots(true)">✓ Select all</button>
         <button onclick="bulkRoots(false)">◻ Deselect all</button>
         <button id="applyBtn" onclick="runIngest()">⟳ Apply (ingest)</button>
       </div>` + rows +
      `<h4>Add a root — the list below answers what you type</h4>
       <input type="text" id="newroot" placeholder="type a path to navigate, or a name to filter this folder" autocomplete="off" style="width:100%" />
       <div class="crumbs" id="fscrumbs"></div>
       <div class="fsls" id="fsls"></div>
       <p style="color:var(--dim);font-size:0.72rem;margin-top:0.5rem">
         Ingest is a <b>sync</b>: enabled roots are added/updated, disabled roots and deleted
         files are pruned (✦ summaries are never touched).</p>`;
    setDirty(srcDirty);
    await browseTo(fsPath);
  } catch (e) { $('sources').innerHTML = `<h4>Sources</h4><div class="row">${esc(e.message)}</div>`; }
}

// ── one surface: the folder list answers the input (file-manager model) ────
let fsPath = null, fsDirs = [], fsPrefix = '', srcDirty = false;
function setDirty(v) {
  srcDirty = v;
  $('applyBtn')?.classList.toggle('glow', v);
}
async function browseTo(path, prefix = '') {
  const d = await api('/fs' + (path ? `?path=${encodeURIComponent(path)}` : ''));
  fsPath = d.path; fsDirs = d.dirs; fsPrefix = prefix;
  renderCrumbs();
  renderFsRows();
}
function renderCrumbs() {
  const parts = fsPath.split('/').filter(Boolean);
  let acc = '';
  $('fscrumbs').innerHTML =
    `<span class="seg" data-crumb="/">/</span>` +
    parts.map(p => { acc += '/' + p; return `<span class="seg" data-crumb="${esc(acc)}">${esc(p)}</span><span>/</span>`; }).join('') +
    `<span class="addcur" data-use-root="${esc(fsPath)}">＋ Add this folder</span>`;
}
function renderFsRows() {
  const q = fsPrefix.toLowerCase();
  const list = fsDirs.filter(x => !q || x.name.toLowerCase().startsWith(q) || x.name.toLowerCase().includes(q));
  $('fsls').innerHTML = list.map(x => `
    <div class="row" data-fs="${esc(x.path)}">
      <span>📁 ${esc(x.name)}</span>
      ${x.is_repo ? '<span class="repo-badge">git repo</span>' : ''}
      <span class="use" data-use-root="${esc(x.path)}">+ Add</span>
    </div>`).join('') || `<div class="row" style="color:var(--dim)">no folders match “${esc(fsPrefix)}”</div>`;
}

// the location-and-filter bar: '/' = navigate (last segment prefix-filters), bare = filter here
let srcT = null;
$('sources').addEventListener('input', (ev) => {
  if (ev.target.id !== 'newroot') return;
  clearTimeout(srcT);
  srcT = setTimeout(async () => {
    const v = ev.target.value.trim();
    try {
      if (!v) { fsPrefix = ''; renderFsRows(); return; }
      if (v.includes('/')) {
        const cut = v.lastIndexOf('/');
        await browseTo(v.slice(0, cut + 1) || '/', v.slice(cut + 1));
      } else {
        fsPrefix = v; renderFsRows();
      }
    } catch { /* nonexistent path while typing — keep the current list */ }
  }, 150);
});
$('sources').addEventListener('keydown', (ev) => {
  if (ev.target.id !== 'newroot') return;
  if (ev.key === 'Enter') {
    const first = $('fsls').querySelector('.row[data-fs]');
    if (first) browseTo(first.dataset.fs).then(() => { $('newroot').value = fsPath + '/'; });
  } else if (ev.key === 'Escape') {
    ev.stopPropagation();
    $('newroot').value = ''; fsPrefix = ''; renderFsRows();
  }
});

window.bulkRoots = async (enabled) => {
  await api('/roots/bulk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
  await buildSources();
  setDirty(true);
  setMsg(enabled ? 'all roots enabled — Apply (ingest) to sync' : 'all roots disabled — Apply (ingest) will empty their notes');
};
window.toggleSources = () => { const el = $('sources'); el.classList.toggle('open'); if (el.classList.contains('open')) { $('distills').classList.remove('open'); buildSources(); } };
window.addRoot = async (path) => {
  if (!path) return;
  try {
    await api('/roots', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) });
    await buildSources();
    setDirty(true);
    setMsg('root added — Apply (ingest) to bring its notes in');
  } catch (e) { setMsg(e.message, true); }
};
$('sources').addEventListener('click', async (ev) => {
  const t = ev.target;
  try {
    if (t.dataset.useRoot) { await addRoot(t.dataset.useRoot); return; }
    if (t.dataset.crumb) { await browseTo(t.dataset.crumb); $('newroot').value = ''; return; }
    const fsrow = t.closest('.row[data-fs]');
    if (fsrow) { await browseTo(fsrow.dataset.fs); $('newroot').value = ''; return; }
    if (t.dataset.toggleRoot) {
      await api('/roots', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: t.dataset.toggleRoot }) });
      setDirty(true);
      setMsg('root toggled — Apply (ingest) to sync');
    } else if (t.dataset.assetsRoot) {
      const out = await api('/roots', { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: t.dataset.assetsRoot, toggle: 'assets' }) });
      await buildSources();
      const on = out.find((r) => r.path === t.dataset.assetsRoot)?.assets;
      setDirty(true);
      setMsg(on ? 'assets ON for this root — Apply (ingest) to sync its images/PDFs'
                : 'assets OFF — its sidecar notes prune on the next Apply');
    } else if (t.dataset.delRoot) {
      if (!(await appConfirm(`${t.dataset.delRoot}\n\nIts notes will be pruned from the vault (no ghost nodes).`,
                              { title: 'Remove this source?', ok: 'Remove + prune', danger: true }))) return;
      const out = await api('/roots', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: t.dataset.delRoot }) });
      await buildSources();
      await api('/rebuild', { method: 'POST' });
      await refresh();
      setMsg(`root removed · ${out.pruned_notes} note(s) pruned`);
    }
  } catch (e) { setMsg(e.message, true); }
});

// ── glossary drawer ────────────────────────────────────────────────────────
function buildDrawer() {
  const colors = graph.repoColors();
  const repoCounts = {};
  for (const n of vNodes) if (n.kind === 'note') repoCounts[n.repo] = (repoCounts[n.repo] ?? 0) + 1;
  const unresolved = [];
  for (const n of vNodes) if (n.kind === 'note') for (const u of n.unresolved) unresolved.push({ u, n });
  const EDGE = { wikilink: '#7c9eff', relative: '#8b93a6', pathref: '#4ec9b0', asset: '#b85fa8', sibling: '#2c3342' };
  // Node classes (founder ask): what each colour+shape on the canvas actually MEANS.
  // Rendered only when classes are configured, and each row shows its live match count —
  // a class matching 0 notes is visible as such instead of quietly implying coverage.
  const legend = (graph.nodeClassLegend?.() ?? []).filter(c => c.count > 0);
  const SHAPE_GLYPH = { circle: '●', square: '■', diamond: '◆', triangle: '▲', star: '★', hexagon: '⬢' };
  $('drawer').innerHTML =
    (legend.length
      ? `<h4>Node types — colour + shape</h4>` +
        legend.map(c =>
          // c.color is CONFIG, and config is not trust — an unescaped `"` here breaks out of the
          // style attribute (stored XSS from node-classes.json). Escaped here AND validated
          // at write time in node_classes.py; either alone is one typo away from a hole.
          `<div class="row"><span style="color:${esc(c.color)};font-size:15px;width:16px;display:inline-block">` +
          `${SHAPE_GLYPH[c.shape] ?? '●'}</span> ${esc(c.label)} <span class="tg">${c.count}</span></div>`).join('')
      : '') +
    `<h4>Repos (click toggles)</h4>` +
    Object.entries(repoCounts).map(([r, c]) =>
      `<div class="row" data-repo="${esc(r)}"><span class="dot" style="background:${esc(colors.get(r) ?? "#6f8fbf")}"></span> ${esc(r)} <span class="tg">${c} · on</span></div>`).join('') +
    `<h4>Edges (click toggles) — hue = repo · brightness = connectedness</h4>` +
    Object.entries(EDGE).map(([t, c]) => {
      const off = graph.state().hiddenEdges.includes(t);
      const name = t === 'sibling' ? 'repo grouping (shown as hulls)'
                 : t === 'pathref' ? 'path reference (`code` pointers)'
                 : t === 'asset' ? 'media link (image · PDF · video · audio)' : t;
      return `<div class="row ${off ? 'off' : ''}" data-edge="${t}"><span class="edot" style="background:${c}"></span> ${name} <span class="tg">${off ? 'off' : 'on'}</span></div>`;
    }).join('') +
    `<h4>👻 Future notes (ghosts)</h4>` +
    `<div class="row ${ghostsOn ? '' : 'off'}" data-ghosts="1"><span class="edot" style="background:#8b93a6;border-radius:99px;border:1px dashed #9aa3b2;background:transparent"></span> unresolved links as ghost nodes <span class="tg">${ghostsOn ? 'on' : 'off'}</span></div>` +
    `<h4>Unresolved (${unresolved.length}) — click opens the note</h4>` +
    (unresolved.slice(0, 40).map(({ u, n }) =>
      `<div class="row" data-open-note="${esc(n.id)}"><span class="unres">${esc(u)}<small>in ${esc(n.repo)} / ${esc(n.source_path)}</small></span></div>`).join('')
      || `<div class="row" style="color:var(--dim)">none — every link resolves 🎉</div>`);
}

$('drawer').addEventListener('click', (ev) => {
  const row = ev.target.closest('.row'); if (!row) return;
  if (row.dataset.repo) {
    const on = graph.toggleRepo(row.dataset.repo);
    row.classList.toggle('off', !on);
    row.querySelector('.tg').textContent = row.querySelector('.tg').textContent.replace(/on|off/, on ? 'on' : 'off');
  } else if (row.dataset.edge) {
    const on = graph.toggleEdgeType(row.dataset.edge);
    row.classList.toggle('off', !on);
    row.querySelector('.tg').textContent = on ? 'on' : 'off';
  } else if (row.dataset.ghosts) {
    toggleGhosts();
  } else if (row.dataset.openNote) {
    reader.openNote(row.dataset.openNote);
    toggleDrawer(false);
  }
});

window.toggleDrawer = (force) => {
  const open = force ?? !$('drawer').classList.contains('open');
  $('drawer').classList.toggle('open', open);
  $('glossaryBtn').classList.toggle('open', open);
};
document.addEventListener('click', (ev) => {
  if ($('drawer').classList.contains('open') &&
      !ev.target.closest('#drawer') && !ev.target.closest('#glossaryBtn')) toggleDrawer(false);
});

// ── accordion panels: collapse + stretch (persisted) ───────────────────────
if (innerWidth <= 700) { document.body.dataset.lhs = 'closed'; document.body.dataset.reader = 'closed'; }
const saved = JSON.parse(localStorage.getItem('synapse.panels') ?? '{}');
if (saved.lhsW) document.documentElement.style.setProperty('--lhs-w', saved.lhsW);
if (saved.readerW) document.documentElement.style.setProperty('--reader-w', saved.readerW);
if (saved.lhs === 'closed' && innerWidth > 700) document.body.dataset.lhs = 'closed';
if (saved.reader === 'closed' && innerWidth > 700) document.body.dataset.reader = 'closed';

function persist() {
  localStorage.setItem('synapse.panels', JSON.stringify({
    lhs: document.body.dataset.lhs, reader: document.body.dataset.reader,
    lhsW: document.documentElement.style.getPropertyValue('--lhs-w') || undefined,
    readerW: document.documentElement.style.getPropertyValue('--reader-w') || undefined,
  }));
  graph.reflow();   // panels changed → re-fit the layout into the new canvas size
}
window.toggleLhs = () => {
  document.body.dataset.lhs = document.body.dataset.lhs === 'closed' ? 'open' : 'closed';
  persist();
};
window.toggleReader = () => {
  document.body.dataset.reader = document.body.dataset.reader === 'closed' ? 'open' : 'closed';
  persist();
};
function expandReader() {
  if (document.body.dataset.reader === 'closed') { document.body.dataset.reader = 'open'; persist(); }
}

function makeResizer(el, varName, fromRight) {
  el.addEventListener('mousedown', (down) => {
    down.preventDefault();
    el.classList.add('active');
    const move = (ev) => {
      const w = fromRight ? innerWidth - ev.clientX : ev.clientX;
      const clamped = Math.max(180, Math.min(innerWidth * 0.45, w));
      document.documentElement.style.setProperty(varName, clamped + 'px');
      graph.reflow();   // live: the layout slides with the panel edge (RHS included)
    };
    const up = () => { el.classList.remove('active'); removeEventListener('mousemove', move); removeEventListener('mouseup', up); persist(); };
    addEventListener('mousemove', move); addEventListener('mouseup', up);
  });
  // double-click a handle = reset that panel to its default width
  el.addEventListener('dblclick', () => {
    document.documentElement.style.removeProperty(varName);
    persist();
  });
}
makeResizer($('rs-lhs'), '--lhs-w', false);
makeResizer($('rs-reader'), '--reader-w', true);

// window resize: panels never squeeze the canvas out — clamp stored widths to 45vw
addEventListener('resize', () => {
  for (const varName of ['--lhs-w', '--reader-w']) {
    const cur = parseInt(document.documentElement.style.getPropertyValue(varName));
    if (cur && cur > innerWidth * 0.45) {
      document.documentElement.style.setProperty(varName, Math.floor(innerWidth * 0.45) + 'px');
    }
  }
  graph.reflow();
});

// ── keyboard ────────────────────────────────────────────────────────────────
document.addEventListener('keydown', (ev) => {
  if (ev.key === '/' && document.activeElement !== $('filter')) { ev.preventDefault(); $('filter').focus(); return; }
  if (ev.key === 'Escape') {
    if ($('drawer').classList.contains('open')) return toggleDrawer(false);
    if ($('filter').value) { $('filter').value = ''; graph.setMatch(null); return; }
    if (document.body.dataset.reader !== 'closed') return window.toggleReader();
  }
  if (ev.key === 'ArrowLeft' && ev.altKey) reader.back();
});

// ── silent usage flags (no UI): the E2E suites assert real user flows through these ──
const AC_KEY = 'synapse.acceptance.s4';
const ac = JSON.parse(localStorage.getItem(AC_KEY) ?? '{}');
ac.notesOpened = new Set(ac.notesOpened ?? []);

function acSave() {
  localStorage.setItem(AC_KEY, JSON.stringify({ ...ac, notesOpened: [...ac.notesOpened] }));
  acRender();
}
// The acceptance PANEL is gone (founder call 2026-07-17: no in-app acceptance ceremony).
// The silent usage flags above stay — they cost nothing and the E2E suites assert real
// user flows through them (localStorage `synapse.acceptance.s4`).
function acRender() { /* no panel to render */ }

// hooks into the real interactions
let currentOpenId = null;
function aiButtons() {
  const isNote = !!currentOpenId, isSummary = !!currentOpenId?.startsWith('S — ');
  const isAsset = !!nodes.find((n) => n.id === currentOpenId)?.tags?.some((t) => t.startsWith('asset:'));
  $('ai-distill').disabled = !isNote || isSummary;
  $('ai-distill-tree').disabled = !isNote || isSummary;
  $('ai-render').disabled = !isSummary;
  $('ai-describe').style.display = isAsset ? '' : 'none';
  $('ai-describe').disabled = !isAsset;
  $('ai-status').textContent = isSummary ? 'a summary is open — render it as an image'
    : isAsset ? 'an asset is open — describe it (AI) or distill it'
    : isNote ? 'distill the open note (or its subtree)' : 'open a note to distill it';
}
// note-open side effects (AI buttons, graph focus, acceptance) live in the reader's onOpen
// callback (top of this file) — a monkey-patch here would miss in-body wikilink clicks + Back.

// ── Model keys — the app SAYS what's configured and takes keys in-app ─────────
// Status on load; unconfigured models get a one-click "Add keys" form. Keys are
// saved to the backend's env file AND applied live (no restart). Values never
// come back from the server — only presence + a masked tail.
async function refreshModelStatus() {
  const el = $('model-status'); if (!el) return;
  try {
    const s = await api('/models/status');
    if (s.mock) {
      el.innerHTML = '🧪 mock mode — model calls are simulated (unset SYNAPSE_MOCK_MODELS to go live)';
      $('keyform').style.display = 'none';
      return;
    }
    const providerName = { anthropic: 'Anthropic', openai: 'OpenAI' };
    const row = (icon, label, m) => m.configured
      ? `<div>✓ ${icon} ${label} ready — ${esc(m.model)} (key ${esc(m.key_hint || 'set')})</div>`
      : `<div>✗ ${icon} ${label} needs an ${esc(providerName[m.provider] || m.provider)} key</div>`;
    const allSet = s.distill.configured && s.render.configured;
    el.innerHTML = row('✦', 'Distill', s.distill) + row('▣', 'Render', s.render)
      + (allSet ? '' : `<div><a onclick="showKeyForm()">＋ Add keys here</a> · or edit <b>${esc(s.env_file)}</b></div>`);
    if (allSet) $('keyform').style.display = 'none';
  } catch { /* backend down — the health pill already says so */ }
}
window.showKeyForm = () => {
  const f = $('keyform');
  f.style.display = f.style.display === 'none' ? '' : 'none';
  if (f.style.display !== 'none') $('key-anthropic').focus();
};
window.saveModelKeys = async () => {
  const a = $('key-anthropic').value.trim(), o = $('key-openai').value.trim();
  const note = $('keyform-note');
  if (!a && !o) { note.textContent = 'paste at least one key'; return; }
  note.textContent = 'saving…';
  try {
    const s = await api('/models/keys', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ anthropic_key: a || null, openai_key: o || null }) });
    $('key-anthropic').value = ''; $('key-openai').value = '';
    note.textContent = `✓ saved to ${s.env_file} — live now, no restart needed`;
    await refreshModelStatus();
    setMsg('model keys saved — distill/render are live');
  } catch (e) { note.textContent = e.message; }
};

window.aiDistill = async (scope) => {
  if (!currentOpenId) return;
  const node = currentOpenId;
  // KISS (founder): "Distill" = the note + its close relationships (depth 1);
  // the ✧ button widens to depth 2. Pure single-note distill is API-only.
  const depth = scope === 'subtree' ? 2 : 1;
  $('ai-status').textContent = scope === 'subtree' ? 'distilling the subtree…' : 'distilling note + neighbors…';
  try {
    let out = await api('/distill', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_id: node, scope: 'subtree', depth }) });
    if (out.requires_confirmation) {
      if (!(await appConfirm(`This distillation is ~${out.tokens_est.toLocaleString()} tokens (confirmation gate: ${out.threshold.toLocaleString()}).`,
                             { title: 'Cost guard', ok: 'Spend it' }))) {
        $('ai-status').textContent = 'cancelled — nothing spent'; return;
      }
      out = await api('/distill', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node_id: node, scope: 'subtree', depth, confirm: true }) });
    }
    await api('/rebuild', { method: 'POST' });
    await refresh();
    await reader.openNote(out.summary_note_id);
    $('ai-status').textContent = `distilled ✓ ${out.citations} citation(s)` + (out.truncated ? ' · truncated (disclosed)' : '');
    setMsg(`S-note created: ${out.summary_note_id} (${out.model})`);
  } catch (e) { $('ai-status').textContent = e.message; setMsg(e.message, true); }
};

window.aiDescribe = async () => {
  if (!currentOpenId) return;
  const node = currentOpenId;
  $('ai-status').textContent = 'looking at it…';
  try {
    let out = await api('/describe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note_id: node }) });
    if (out.requires_confirmation) {
      if (!(await appConfirm(`Describing this asset is ~${out.tokens_est.toLocaleString()} tokens (gate: ${out.threshold.toLocaleString()}).`,
                             { title: 'Cost guard', ok: 'Spend it' }))) {
        $('ai-status').textContent = 'cancelled — nothing spent'; return;
      }
      out = await api('/describe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note_id: node, confirm: true }) });
    }
    await api('/rebuild', { method: 'POST' });
    await refresh();
    await reader.openNote(node, false);
    const dropped = out.links_dropped?.length ? ` · ${out.links_dropped.length} hallucinated link(s) dropped` : '';
    $('ai-status').textContent = `described ✓ ${out.links_added.length} related note(s) linked${dropped}`;
    setMsg(`description added (${out.model}) — related notes ride as dashed edges`);
  } catch (e) { $('ai-status').textContent = e.message; setMsg(e.message, true); }
};

window.aiRender = async () => {
  if (!currentOpenId?.startsWith('S — ')) return;
  $('ai-status').textContent = 'rendering the idea…';
  try {
    const out = await api('/render', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ summary_note_id: currentOpenId }) });
    await reader.openNote(currentOpenId, false);
    $('ai-status').textContent = `image ✓ (${out.model})`;
    setMsg(`rendered → ${out.image}`);
  } catch (e) { $('ai-status').textContent = e.message; setMsg(e.message, true); }
};
$('filter').addEventListener('input', () => { if ($('filter').value.trim()) { ac.filterUsed = true; acSave(); } });
$('filter').addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && $('filter').value.trim()) { ac.enterOpened = true; acSave(); } });
$('reader-body').addEventListener('click', (ev) => { if (ev.target.closest('a[data-wl]')) { ac.wikilinkNav = true; acSave(); } });
$('drawer').addEventListener('click', (ev) => {
  const row = ev.target.closest('.row');
  if (row?.dataset.repo || row?.dataset.edge) ac.glossaryToggled = true;
  if (row?.dataset.openNote) ac.unresolvedOpened = true;
  acSave();
});
{
  const _tr = window.toggleReader, _tl = window.toggleLhs;
  window.toggleReader = () => { _tr(); if (document.body.dataset.reader === 'closed') ac._rClosed = true; else if (ac._rClosed) ac.readerCycled = true; acSave(); };
  window.toggleLhs = () => { _tl(); if (document.body.dataset.lhs === 'closed') ac._lClosed = true; else if (ac._lClosed) ac.lhsCycled = true; acSave(); };
}
for (const rs of [$('rs-lhs'), $('rs-reader')]) {
  rs.addEventListener('mousedown', () => {
    const up = () => { ac.resized = true; acSave(); removeEventListener('mouseup', up); };
    addEventListener('mouseup', up);
  });
}
const acViewport = () => { if (innerWidth <= 700) { ac.mobileSeen = true; acSave(); } };
addEventListener('resize', acViewport);
acViewport();

const _refresh = refresh;
refresh = async function () {
  await _refresh();
  if (nodes.some(n => n.kind === 'note')) { ac.graphLoaded = true; acSave(); }
};

health($('health'));
// resolve the companion-media convention BEFORE the first render, so an <Visual/>
// never resolves against a stale default on a vault that configures its own layout
loadConventions().then(refresh);
refreshModelStatus();
acRender();
// app version in the statusbar — single source of truth: package.json (via vite define)
try { $('appver').textContent = `v${__APP_VERSION__} · `; } catch { /* dev without define */ }

// type-lens pill clicks (delegated — the bar rebuilds on every refresh)
$('typebar').addEventListener('click', (ev) => {
  const pill = ev.target.closest('.pill'); if (!pill) return;
  if (pill.dataset.type) toggleType(pill.dataset.type);
  else if (pill.dataset.regroup) toggleAssetsGroup();
});

// ── Project switcher (sprint 06 R5 · founder ask: "Switch should be in GUI also") ────────────
//
// Kit: project-management/ui_kit/explorer/project-switcher.html
//
// The active project lives SERVER-side (data/active-project), not in localStorage, so the UI,
// the CLI (`synapse --project`) and the MCP server all agree about which brain is open. A switch
// here is a switch everywhere. That also means the correct thing after a switch is a full reload:
// the whole page — graph, index, reader, search, glossary — is derived from one brain, and
// hand-patching each surface would leave one of them showing the previous one.
let _projects = [];

async function loadProjects() {
  try {
    _projects = await api('/projects');
    const { active } = await api('/projects/active');
    const cur = _projects.find((p) => p.slug === active);
    // A fresh install has ONE brain and no projects, and that is a perfectly good way to use
    // SYNAPSE. Showing a switcher that says "no projects" tells a new user something is missing
    // when nothing is — so the control simply is not there until there is something to switch
    // between. It appears the moment a second brain exists.
    const wrap = document.getElementById('projWrap');
    if (wrap) wrap.hidden = _projects.length < 2;
    const btn = document.getElementById('projName');
    if (btn) btn.textContent = cur ? cur.name : 'pick a project';
    renderProjects(active);
  } catch {
    // Backend down: say so rather than show a stale name — but only if the control is visible.
    const btn = document.getElementById('projName');
    if (btn) btn.textContent = 'unavailable';
  }
}

function renderProjects(active) {
  const menu = document.getElementById('projMenu');
  if (!menu) return;
  if (!_projects.length) {
    menu.innerHTML = '<div class="proj-opt"><span class="nm muted">No projects yet</span></div>';
    return;
  }
  menu.innerHTML = _projects.map((p) => {
    // An un-ingested brain says so. Rendering it as an empty graph is indistinguishable from a
    // working brain that happens to be empty — the one confusion this control could create.
    const ct = p.has_graph
      ? `${p.roots} root${p.roots === 1 ? '' : 's'}`
      : '<span class="warn">not ingested</span>';
    return `<div class="proj-opt" role="option" data-proj="${esc(p.slug)}"
                 aria-selected="${p.slug === active}">
              <span class="nm">${esc(p.name)}</span><span class="ct">${ct}</span>
            </div>`;
  }).join('');
}

window.toggleProjects = () => {
  const menu = document.getElementById('projMenu');
  const btn = document.getElementById('projBtn');
  if (!menu) return;
  const open = menu.hasAttribute('hidden');
  menu.toggleAttribute('hidden', !open);
  btn?.setAttribute('aria-expanded', String(open));
};

window.switchProject = async (slug) => {
  const cur = document.getElementById('projName')?.textContent;
  try {
    const p = await api(`/projects/${encodeURIComponent(slug)}/activate`, { method: 'POST' });
    setMsg(`switched to ${p.name} — reloading`);
    location.reload();          // one brain per page; see the note above
  } catch (e) {
    setMsg(`could not switch to ${slug}: ${e?.message || e}`);
    const n = document.getElementById('projName');
    if (n && cur) n.textContent = cur;
  }
};

document.addEventListener('click', (ev) => {
  const opt = ev.target.closest?.('.proj-opt[data-proj]');
  if (opt) { window.switchProject(opt.dataset.proj); return; }
  if (!ev.target.closest?.('.proj')) document.getElementById('projMenu')?.setAttribute('hidden', '');
});

loadProjects();

// ── Sort lens (sprint 06 T1/T2/T3) + new-note mark (S3) ──────────────────────
//
// Kit: project-management/ui_kit/explorer/lens-and-new-mark.html
//
// The lens changes ORDER ONLY. It never recolours, resizes or hides a node — the canvas already
// spends colour on repo, shape/size on node class and dashed/hollow on ghosts, and a sort control
// that also restyles is two features wearing one hat.
//
// There are TWO date lenses, not one, because the filesystem forces it: Linux has no creation
// time, so "when the file changed" (file_mtime) and "when it joined the brain" (first_seen) are
// different questions with different coverage. Each option states its own coverage rather than
// letting you assume it is complete.
const LENSES = [
  { id: 'match',   name: 'best match',        desc: 'search relevance — the default' },
  { id: 'links',   name: 'most connections',  desc: 'in + out links' },
  { id: 'changed', name: 'recently changed',  desc: 'when the file last changed', field: 'file_mtime' },
  { id: 'new',     name: 'new to the brain',  desc: 'first indexed',              field: 'first_seen' },
];
// NOT 'synapse.lens' — that key belongs to the TYPE lens above and holds JSON. Writing a bare
// string there made its JSON.parse throw at module load and took the whole app down on reload.
const SORT_LENS_KEY = 'synapse.sortLens';
let lens = localStorage.getItem(SORT_LENS_KEY) || 'match';

/** Coverage for a date lens, stated honestly in its own menu row — a lens that silently covers
 *  a third of the corpus is worse than one that says so. */
function lensCoverage(field) {
  const notes = nodes.filter((n) => n.kind === 'note');
  const have = notes.filter((n) => n[field]).length;
  return notes.length ? `${have.toLocaleString()} of ${notes.length.toLocaleString()} notes` : '';
}

/** How many nodes a lens highlights when there is no search query. Enough to be a useful
 *  "show me the top of this brain", few enough that the highlight still means something. */
const LENS_TOP_N = 12;

/** Apply the active lens. Dateless notes sort LAST under a date lens — never silently as "old". */
function applyLens(list) {
  const L = LENSES.find((x) => x.id === lens);
  if (!L || L.id === 'match') return list;
  if (L.id === 'links') {
    return [...list].sort((a, b) =>
      (b.in_degree + b.out_degree) - (a.in_degree + a.out_degree)
      || a.title.localeCompare(b.title));          // stable: equal degree must not reshuffle
  }
  return [...list].sort((a, b) => {
    const av = a[L.field] || '', bv = b[L.field] || '';
    if (!av && !bv) return a.title.localeCompare(b.title);
    if (!av) return 1;                              // no date → last, always
    if (!bv) return -1;
    return bv.localeCompare(av);                    // ISO-8601 sorts lexically
  });
}

/** The new-note mark + the date a row is being sorted by. List only — never the canvas. */
function lensMeta(n) {
  const L = LENSES.find((x) => x.id === lens);
  if (!L?.field) return '';
  const v = n[L.field];
  if (!v) return ' · <span class="nodate">no date</span>';
  const d = new Date(v);
  const days = (Date.now() - d.getTime()) / 86400000;
  const label = days < 1 ? `${Math.max(1, Math.round(days * 24))}h ago`
              : days < 7 ? `${Math.round(days)}d ago`
              : d.toISOString().slice(0, 10);
  return ` · ${label}`;
}
function isNew(n) {
  // "New" means new TO THE BRAIN — first_seen, not a recent file edit. A formatting sweep must
  // not mark 275 notes as new; that would make the mark meaningless on its first day.
  if (!n.first_seen) return false;
  return (Date.now() - new Date(n.first_seen).getTime()) < 86400000;
}
const newMark = (n) => isNew(n) ? '<span class="newmark" title="new to the brain">▸</span>' : '';

function renderLensMenu() {
  const menu = document.getElementById('lensMenu'); if (!menu) return;
  menu.innerHTML = LENSES.map((L) => {
    const cov = L.field ? ` · ${lensCoverage(L.field)}` : '';
    return `<div class="lens-opt" role="option" data-lens="${L.id}" aria-selected="${L.id === lens}">
              <span class="t">${esc(L.name)}</span><span class="d">${esc(L.desc)}${esc(cov)}</span>
            </div>`;
  }).join('');
  const cur = LENSES.find((x) => x.id === lens) || LENSES[0];
  const nm = document.getElementById('lensName');
  if (nm) nm.textContent = cur.name;
}

window.toggleLens = () => {
  const m = document.getElementById('lensMenu'), b = document.getElementById('lensBtn');
  if (!m) return;
  const open = m.hasAttribute('hidden');
  if (open) renderLensMenu();
  m.toggleAttribute('hidden', !open);
  b?.setAttribute('aria-expanded', String(open));
};

document.addEventListener('click', (ev) => {
  const opt = ev.target.closest?.('.lens-opt[data-lens]');
  if (opt) {
    lens = opt.dataset.lens;
    localStorage.setItem(SORT_LENS_KEY, lens);
    renderLensMenu();
    document.getElementById('lensMenu')?.setAttribute('hidden', '');
    showLensTop();
    return;
  }
  if (!ev.target.closest?.('.lens')) document.getElementById('lensMenu')?.setAttribute('hidden', '');
});

/** Show the lens's top N and HIGHLIGHT them on the canvas.
 *
 * The first version of this shipped as sort-only, and with an empty search box it did nothing
 * whatsoever — `renderResults()` returns early without a query, so there was no list to reorder
 * and the graph was never touched. A control that appears inert is a broken control.
 *
 * This reuses `graph.setMatch()`, the same transient highlight search already uses (it also fits
 * the view to the set). That is NOT the "no restyling" rule being broken: the rule is about
 * permanent node appearance — colour, shape, size — which still encodes repo and class only. A
 * selection highlight is transient and is exactly what "show me these" should look like. */
function showLensTop() {
  const box = $('sresults');
  if (lens === 'match') {                    // relevance has no meaning without a query
    graph.setMatch(null);
    if (!$('filter').value.trim()) { box.classList.remove('open'); box.innerHTML = ''; }
    return;
  }
  if ($('filter').value.trim()) { renderResults(); return; }   // a query wins; it is more specific

  const L = LENSES.find((x) => x.id === lens);
  const pool = applyLens(nodes.filter((n) => n.kind === 'note')).slice(0, LENS_TOP_N);
  if (!pool.length) { graph.setMatch(null); return; }

  const colors = graph.repoColors();
  const grpOf = vRepoOf();
  box.innerHTML =
    `<div class="r" style="color:var(--dim);cursor:default">top ${pool.length} by ${esc(L.name)}</div>`
    + pool.map((n) => `
      <div class="r" data-open="${esc(n.id)}">
        <span class="dot" style="background:${colors.get(grpOf.get(n.id) ?? n.repo)}"></span>
        ${newMark(n)}
        <span>${n.repo === '✦ summaries' ? '✦ ' : ''}${esc(n.title)}</span>
        <small>${esc(grpOf.get(n.id) ?? n.repo)} · ${n.in_degree + n.out_degree} links${lensMeta(n)}</small>
      </div>`).join('');
  box.classList.add('open');
  graph.setMatch(new Set(pool.map((n) => n.id)));    // highlight + fit — the visible answer
  setMsg(`${pool.length} highlighted — top by ${L.name}`);
}

window.__synapseLens = { get: () => lens, apply: applyLens, isNew, showTop: showLensTop };
renderLensMenu();
