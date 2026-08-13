# SYNAPSE — architecture

> Owned by `cpto` + `dev`. Technical truth: boundaries, interfaces, NFRs, reversibility.
> Status: **design intent for the 3-sprint POC** — updated as modules land (each sprint's GBU
> checks this file against reality).

## The system in one picture

```
 SYNAPSE_SOURCE_REPOS (your repos, read-only)
        │  scan *.md
        ▼
 ┌─ ingest ─────────────┐      ┌──────────────── THE VAULT ────────────────┐
 │ scanner · frontmatter │ ───► │  data/vault/                              │
 │ writer · idempotency  │      │   notes/   one md note per source file    │
 └───────────────────────┘      │   media/   generated images (sprint 3)    │
                                │   Index.md  generated catalog              │
 ┌─ graph ──────────────┐      │   graph.json  DERIVED — always rebuildable │
 │ link extractor        │ ◄──► └────────────────────────────────────────────┘
 │ graph builder · index │              ▲ the ONLY source of truth
 └──────────┬────────────┘              │
            ▼                            │ writes S — notes + images back
 ┌─ FastAPI (:8000) ─────────────────────┴──────────────┐
 │ /api/v1/ingest · /graph · /stats · /distill · /render │
 └──────────┬────────────────────────────┬───────────────┘
            ▼                            ▼
 ┌─ frontend (:5173) ────┐   ┌─ model seams ────────────────────────────┐
 │ graph canvas · node   │   │ distill/  Summarizer  → Anthropic | Mock │
 │ panel · filter ·      │   │ render/   ImageRenderer → OpenAI  | Mock │
 │ distill/render buttons│   └──────────────────────────────────────────┘
 └───────────────────────┘
```

## Stack

- **Backend:** Python 3.12 · FastAPI/uvicorn (`backend/app`) · file-based storage (no DB in v0.1 —
  recorded decision D-3).
- **Frontend:** Vite (vanilla-first; graph-library choice is a recorded decision before Epic C
  builds).
- **Models:** Anthropic (`SUMMARIZER_MODEL=claude-sonnet-5`) · OpenAI (`IMAGE_MODEL=gpt-image-1`).
- **Tests:** pytest (mocked model seams — zero paid calls in CI) · Playwright real-Chromium E2E.

## Boundaries & modules (mirror in `03_MODULE_CONTRACTS.md`)

| Module | Sprint | Responsibility | May import |
|---|---|---|---|
| `backend/modules/ingest` | 1 | repos → vault notes (frontmatter, idempotent) | stdlib only |
| `backend/modules/graph` | 1 | vault → graph.json + Index.md + stats | stdlib only |
| `backend/modules/distill` | 3 | node/subtree → grounded summary → `S —` vault note | anthropic SDK (only here) |
| `backend/modules/render` | 3 | summary note → image in `media/`, embedded | openai SDK (only here) |
| `backend/app` | 1–3 | FastAPI wiring: routes → module services; `/health` | the modules above |
| `frontend/` | 2–3 | explorer UI over `/api/v1/*` — never reads the vault directly | — |

Modules don't import each other except via their public service interfaces; the vault directory
layout is the shared contract (documented here, versioned in `graph.json.schema_version`).

## Interfaces / contracts

- **Vault note frontmatter:** `synapse.source_repo`, `synapse.source_path`,
  `synapse.ingested_at` (absolute ISO), `synapse.content_hash`; summaries add
  `synapse.kind: summary`, `synapse.sources`, `synapse.model`, `synapse.image?`.
- **graph.json (v1):** `{schema_version, nodes:[{id,title,repo,source_path,tags,in_degree,out_degree,unresolved[]}], edges:[{src,dst,type: wikilink|relative|sibling}]}`.
- **API:** `POST /ingest` · `GET /graph` · `GET /stats` · `POST /distill {node_id, scope, depth}`
  (may return `requires_confirmation`) · `POST /render {summary_note_id}` · `GET /health`.
- **Provider seams:** `Summarizer.summarize(notes, scope) -> GroundedSummary` ·
  `ImageRenderer.render(summary) -> MediaRef`. Mocks are the test-time implementations.

## Non-functional requirements

- **Honesty:** every pipeline step reports real counts; truncation is always disclosed;
  zero-citation summaries are failures.
- **Cost safety:** token-estimate confirm gate on distill; live smokes opt-in
  (`./start.sh smoke`, issue #3), never CI.
- **Scale target (POC):** two repos ≈ hundreds of notes / ~2k nodes interactive in the explorer.
- **I18n:** UTF-8 end-to-end; Hebrew/RTL renders correctly in vault notes and the node panel.
- **Security:** keys only in `backend/.env` (git-ignored); sanitized markdown rendering in the UI.

## Reversibility

- The vault is plain markdown — exportable, diffable, Obsidian-openable; deleting SYNAPSE loses
  nothing but the derived graph (rebuildable) and generated media (regenerable).
- No DB and no cloud dependency in v0.1 ⇒ no migrations, no lock-in; adding a DB later is a
  flagged one-way-door decision.
- Model providers are swappable behind the seams (that's the point of D-2).
