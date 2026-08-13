# Changelog

All notable changes to **SYNAPSE** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`./start.sh smoke`** (issue #3) — wraps the two opt-in live-model smokes (real Anthropic
  distill, real gpt-image-1 render) that were previously runbook steps. Refuses unconditionally
  in CI; refuses keyless with an actionable message naming the missing key(s), exit 1 (never the
  unknown-command exit 2); refuses against a `SYNAPSE_MOCK_MODELS=1` backend (never mislabels a
  zero-spend run as live); shows a genuinely non-spending token estimate (`POST /api/v1/distill
  {dry_run: true}` — new, zero-cost path, `backend/modules/distill/src/service.py`) and asks
  before making the ONE paid distill call (never two); a failed provider call is diagnosed, the
  transcript records what succeeded and failed (never silently truncated), and the command exits
  a documented code (3), not a bare curl status. Transcripts get a collision-proof filename
  (`mktemp`) under the active sprint's `reports/` dir; `SYNAPSE_SMOKE_YES=1` is the documented
  non-interactive confirmation bypass. `backend/tests/test_start_smoke.py` pins the refusal
  contract, the confirmation-before-spend ordering, the single-paid-call invariant, the `.env`
  loader's tolerance of comments/odd keys, and the provider-failure diagnostics.

## [0.2.0] — 2026-08-06 · "the open, secured, movable brain"

The graph learned to say what a node **is**, the reader learned to show media inline, a security
review closed the gap where an ingested repo was trusted input, the vault became portable, and a
guided tour now shows all of it end to end.

### Added
- **Node classes** — colour · shape · size assigned by matching a node's own path/name/tag, so a
  one-repo brain is still legible (articles as gold stars, each social channel its own colour,
  media split by kind). First-match-wins over an ordered list; edit via `synapse classes
  list|add|remove`, and a page reload is enough — matching happens client-side, never an ingest.
- **`synapse roots`** — add/remove/enable/disable source roots from the CLI, the same store the
  Sources panel writes.
- **Media as real graph edges** — `<Visual id="…"/>` and `<YouTube id="…"/>` are resolved at ingest
  against the article's companion media folder and recorded in `synapse.asset_refs`, so what the
  reader shows and what the graph links are the same file. Interactive bundles render inline,
  sized by the bundle's own height seam.
- Asset sidecars now cover `.svg`, video (`.mp4/.webm/.mov/.m4v`), audio (`.mp3/.m4a/.wav`) and
  interactive `.html`.
- `GET /api/v1/conventions` — the companion-media convention, published so the reader resolves it
  the same way ingest did instead of keeping a second, drifting copy.
- `SYNAPSE_COMPANION_MEDIA_DIR` / `SYNAPSE_INTERACTIVE_PREFIX` — that convention is configuration
  now, not a literal baked into the public core for one private KB's layout.
- Regression suite for all of the above (`backend/tests/test_security_and_classes.py`) and a
  real-Chromium spec asserting both halves of the security fix — the boundary holds *and* the
  content still renders (`tests/e2e/security_active_content.spec.mjs`).
- **Vault export/import** (`synapse export` / `synapse import`, issue #1) — one verifiable zip:
  every file's sha256 in a `MANIFEST.json`, import verifies EVERY hash before writing anything
  (all-or-nothing), refuses zip-slip entries (absolute/`..`/backslash/drive paths), and refuses
  an existing target vault without `--force`. Stdlib only, 12 tests, zero network. External
  contribution ([@Nitjsefnie](https://github.com/Nitjsefnie), PR #19), reviewed and merged.
- **Guided tour generator** (`docs/tour/`) — drives the real explorer in Chromium end to end
  (search → open → inspect → follow → trace → expose gaps) and packages a narrated master,
  a 60-second social cut, WebVTT captions, hero/thumbnail/end-cards, and a real-Chromium capture
  of the live public GitHub repo as its opening (`build-narrated.mjs`, `capture-github-intro.mjs`).
  The README now leads with the animated result instead of a static screenshot.

### Security
See [`SECURITY.md`](SECURITY.md) for the threat model. A repo you ingest is untrusted input:
- The reader no longer accepts `<iframe>`/`<object>`/`<embed>` from vault markdown.
- Interactive bundles are fetched and mounted via `srcdoc` into a sandboxed, **opaque-origin**
  frame — no `allow-same-origin`, so a bundle can reach neither the app nor the API.
- The API serves repo-authored `.html` as an inert attachment, and every asset response carries
  `nosniff` plus a sandboxing CSP, so active content cannot execute at the API's origin — the
  same origin as its unauthenticated ingest/delete/distill endpoints.
- CORS narrowed from *any host on port 5173* to loopback, plus an explicit `SYNAPSE_ALLOWED_ORIGINS`
  opt-in for the documented WSL → Windows direct-IP path.
- Node-class colours are validated at write time and escaped at render time (they were
  interpolated into a `style` attribute unescaped); sizes are clamped.
- Component ids are allow-listed as plain tokens — the previous guard split on `/` only, letting
  `..\..\x`, NUL and the `|` field separator through.

A second review round, on the fixes themselves, closed:
- **Stored XSS in the dashboard** — note titles/ids/repo names were interpolated into `innerHTML`
  unescaped, so a repo heading could execute at the app origin (the origin CORS trusts).
- **Cross-origin writes** — a sandbox does not block the network, and a simple `POST` needs no
  preflight, so a bundle could still trigger `/ingest` or `/rebuild`. The frame now carries an
  app-authored CSP (`connect-src 'none'`) and the server refuses state-changing requests from an
  untrusted `Origin` (including the `null` a sandboxed frame sends). No-`Origin` callers — the
  CLI, the MCP server — are untouched.
- **CORS lookalikes** — the 500 handler matched an unanchored alternation, accepting
  `http://localhost.attacker.example` whenever `SYNAPSE_ALLOWED_ORIGINS` was set.
- **Malformed attachment headers** — `Content-Disposition` was hand-built from an untrusted
  filename (a non-Latin-1 name raised `UnicodeEncodeError` in the ASGI layer).
- **Non-portable root identity** — basenames were compared case-sensitively and unresolved, so
  `KB`/`kb` and symlinked aliases both slipped past the collision guard.
- Stricter grammars and bounds: `\A…\Z` id termination (Python's `$` matches before a trailing
  newline), hex colours limited to 3/4/6/8 digits, and a frontmatter reader that requires a closed
  block and bounds characters as well as lines.

### Fixed
- The ingest freshness check read a fixed byte window of a note, so an article with enough media
  pushed its (single-line) `asset_refs` past it: the check never matched, concluded the note had
  changed, and rewrote it on **every** run without converging. It now reads the frontmatter block
  by its delimiter.
- Images in source notes were pointed at an asset id derived by path arithmetic without checking
  the asset exists, turning "this brain doesn't hold it" into a broken-image 404 and shadowing the
  fallback that would often have resolved it.
- `synapse roots add` bypassed the duplicate-basename guard the HTTP API enforced — two roots with
  the same folder name collide in the vault and cross-delete each other's notes. The invariant now
  lives in one place and both callers use it.

### Also in this release — sprints 04 "The Open Brain" + 05 "Everything In" (both closed, founder PASS)

The brain became infrastructure — queryable, always fresh, reachable from your AI assistant:

- **The query trio** (deterministic retrieval — no embeddings, no model calls, <100ms warm on
  a 21k-note brain): `synapse query "…"` (scoped subgraph), `synapse path A B` (shortest chain,
  hop by hop), `synapse explain ID` (connections grouped) — also as `GET /api/v1/{query,path,explain}`.
- **Explorer:** ⇢ path mode (click two notes → the shortest path glows gold, statusbar reports
  hops) and a **⛓ Connections footer** on every open note (grouped by link type, clickable).
- **MCP server** (`backend/synapse/serve.py`, stdlib stdio): register once —
  `claude mcp add synapse -- <venv-python> <repo>/backend/synapse/serve.py` — and your coding
  agent answers from YOUR vault (`query_graph`, `get_note`, `get_neighbors`, `shortest_path`).
- **Ignore files:** repos' `.gitignore` respected automatically + `.synapseignore` overrides
  (gitignore-style documented subset; `Archive/` = one line); newly-ignored notes prune on the
  next sync with honest counts.
- **Auto-sync:** `synapse hook install` (post-commit/post-checkout, no daemon, output logged,
  `hook status` honestly reports a broken interpreter) and `synapse watch` for non-git roots.
- **graph.json schema v3:** every edge carries a `confidence` tag (`EXTRACTED` now;
  `INFERRED`/`AMBIGUOUS` reserved) — adopted before the first AI-derived edge ships.
- Atomic graph.json/Index.md writes (background hook rebuilds can never expose a half-written file).

Reviewed: internal fresh-eyes GBU (REVISE → all P1/P2 fixed same session, APPROVE 4.5);
Codex cross-vendor pass quota-deferred. Evidence: 112/112 backend · 6/6 real-Chromium E2E.

Further v0.2 candidates: [`project-management/0m_BACKLOG.md`](project-management/0m_BACKLOG.md).

## [0.1.1] — 2026-07-16 · **the clean-machine release**

First-run hardening driven by a real clean-laptop field report, plus in-app model keys —
reviewed by an internal fresh-eyes pass **and** a Codex cross-vendor pass (all findings fixed).

### Added
- **In-app model keys**: the AI panel now shows each model's status on load (mock badge ·
  ready with a masked key tail · "needs an … key"), and missing keys can be pasted straight
  into the panel — saved to `backend/.env` and applied live, no restart
  (`GET/POST /api/v1/models/{status,keys}`; values are never echoed back). E2E:
  `tests/e2e/keys_panel.spec.mjs`.
- **Layman-proof first run** (from a clean-laptop field report): both launchers now run a
  preflight that names any missing prerequisite (Python 3.11–3.13, Node 20.19+/22+, venv
  module), shows the exact fix, and offers to install it **only after an explicit yes**
  (winget on Windows · apt/NodeSource on Linux & WSL). `./start.sh preflight` /
  `.\start.cmd -Preflight` runs the check standalone. Windows preflight also detects the
  Microsoft Store's fake `python.exe`.
- Dev mode now verifies itself: an explicit `✔ Backend is UP` / `✔ Explorer is UP — open
  http://localhost:5173` line once each server actually answers (or a clear failure after 90s).
- `.gitattributes` pinning `*.sh` to LF — a Windows-git clone (autocrlf) used to corrupt
  `start.sh` into `env: 'bash\r'` when run from WSL.

### Fixed
- `pip install` / `npm install` failures inside the launchers now stop with an actionable
  message instead of continuing half-set-up.
- **In-app keys GBU wave** (internal + Codex cross-vendor review, all findings fixed):
  placeholder pastes now 422 instead of a false "live now"; a placeholder loaded at startup
  no longer blocks a real key from a manual `.env` edit; concurrent saves are serialized
  (unique 0600 temp file, no crash-window leftovers, `.gitignore`d); the keys E2E assertions
  can actually fail and its write flow is double-gated (`E2E_KEYS_WRITE_OK=1` + keyless);
  API tests no longer read the developer's real `backend/.env`.

## [0.1.0] — 2026-07-15 · **the POC** (sprints 01–03, each closed on founder acceptance PASS)

The full loop the PRD promised — **ingest → graph → distill → render** — live end-to-end:
proven on a 21k-note whole-workspace brain and on a fresh keyless clone (mock mode).

### Sprint 03 — The Twist (closed 2026-07-15, founder acceptance PASS · POC close)
- `modules/distill`: `Summarizer` seam (Anthropic `claude-sonnet-5` + deterministic mock),
  BFS subtree collection (definitions-first truncation, honest disclosure), grounding gate
  (every `(vault: …)` citation must BE a source note — hallucinations are rejected),
  cost-confirm gate above a token threshold; summaries saved as `S —` vault notes
  (`✦ summaries` group), gist voice, each authoring its own `Image:` visual brief.
- `modules/render`: `ImageRenderer` seam (OpenAI `gpt-image-1` + stdlib-PNG mock); PNG into
  `vault/media/` (content-hashed), idempotently embedded in the summary, no text in images
  (D-4), deleted together with its summary.
- Explorer AI panel: Distill (note + neighbors) / Distill wider / Render as image, in-app
  cost-guard modal, ✦ My distills panel (read, bulk delete), summary-aware search boost.
- Scale arc (founder-driven, D-7/8/9): complete-ingest fix (hash-capped note ids — one bad
  filename can never abort a sync; 21,103 files, 0 errors), importance-windowed graph
  (top-1,500 by connectivity; API 19s → 0.2s), semantic zoom (long-tail reveal layer),
  source groups + hard per-frame layer budgets + viewport culling.
- Never-500 hardening: global JSON exception handler that keeps CORS headers, defensive
  filesystem walk, XSS-escaping across the explorer (fresh-eyes GBU wave: 4 P1 + 7 P2 fixed).
- Live smokes (opt-in, once each, recorded): real Anthropic distill (8 grounded citations) +
  real gpt-image-1 render (1024×1024 embedded PNG). Suite: 65 tests, zero paid calls.

### Sprint 02 — The Explorer (closed 2026-07-15, founder acceptance PASS, grade 4.6)
- Explorer as the root page: accordion panels (grip-pill resizers, dblclick reset, persisted,
  mobile overlays), LHS menu + AI panel (sprint-3 slots), glossary drawer (repo/edge toggles +
  actionable unresolved list), filter↔graph live sync, docked wiki reading panel.
- Immersive graph: hue=repo · brightness/size=connectedness · per-repo hull territories ·
  curved edges · LOD labels · dblclick zoom · reflow-not-clip on panel changes.
- Placeable brain: drag=place&pin (right-click releases, ⟲ reset), separable clusters
  (hub-only gravity), persistent click-focus (node/cluster; empty-click defocuses).
- In-UI acceptance checklist (auto-PASS + manual ticks); vault-agnostic multi-viewport E2E;
  opt-in CI e2e job with screenshot artifact.

### Sprint 01 — The Brain (closed 2026-07-15, founder acceptance PASS)
- `modules/ingest`: repos → vault notes (provenance frontmatter, byte-verbatim bodies,
  idempotent, ignore-list, vault-self-exclusion).
- `modules/graph`: vault → deterministic `graph.json` + generated `Index.md` + stats; typed
  edges (wikilink/relative/sibling); unresolved links recorded as forward-links.
- CLI (`./synapse ingest·rebuild·stats`) + FastAPI (`/api/v1/{ingest,graph,stats,rebuild,note,index}`).
- Acceptance dashboard: live checklist, honest ingest report, one-click rebuild-invariance
  proof, **wiki-article popup** (KB-wiki visuals, clickable [[wikilinks]], infobox, RTL),
  **Obsidian-style graph** (force layout, repo colors, hover neighborhood, click-to-article).
- 31 unit/API tests (zero network) + committed Chromium E2E (`tests/e2e/`).

### Added
- Project instantiated from [synaptix-scaffold](https://github.com/SynaptixLabs/scaffold)
  (agent layer: JANUS/ARIA/CORE, drift guard, cross-platform start scripts).
- SYNAPSE identity: PRD (`project-management/0k_PRD.md`), sprint 1 scope
  (ingest → graph → distill → render), env template with the two model seams
  (Anthropic summarizer · OpenAI gpt-image-1).
