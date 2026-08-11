# `modules/ingest` — repos → vault

Turns the markdown scattered across configured repos (`SYNAPSE_SOURCE_REPOS`) into **vault
notes**: one note per source `.md`, our YAML frontmatter on top (provenance + `content_hash`),
the original content below **verbatim** (UTF-8/Hebrew byte-faithful).

- **Idempotent:** unchanged `content_hash` ⇒ `unchanged`, nothing rewritten.
- **Honest:** the `IngestReport` counts exactly what happened (found / written / unchanged /
  skipped); unreadable or non-UTF-8 files are *skipped and counted*, never mangled.
- **Ripple maintenance (issue #9):** after every sync, `refresh_summary_staleness()` compares
  each distilled summary's `synapse.source_hashes` map against the notes now in the vault and
  inserts/removes `synapse.stale: true` (an edited OR pruned source stales; a reverted source
  clears — a comparison, not a latch). Summaries without the map (pre-#9) are left alone.
  Changed flags are reported as `stale_summaries` on the `IngestReport`.
- **Boundaries:** stdlib only; writes only under `<vault>/notes/`; never touched by the graph
  module (which reads the vault, not the repos).
- **Known POC limitation:** a source file's own frontmatter block remains visible in the note
  body (below ours).

| Entry | What |
|---|---|
| `IngestService(vault_path, ignore_dirs).ingest(repos)` | the pipeline (used by CLI + API) |
| `POST /api/v1/ingest` | same, over HTTP |
| `python -m synapse ingest` / `./synapse ingest` | CLI (see `backend/synapse/`) |

Tests: `tests/unit/` against the committed fixture repos in `backend/tests/fixtures/` —
counts, frontmatter shape, idempotency, UTF-8/Hebrew round-trip, ignore-list.
