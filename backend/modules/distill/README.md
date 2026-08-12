# `modules/distill` — model #1: node/subtree → grounded summary

Select a vault note (or its subtree, BFS over wikilink+relative edges to a depth) and distill it
into an `S — <name>.md` **vault note**: faithful, every claim-cluster cited as `(vault: <id>)`,
sources wikilinked (so the next rebuild adds the summary to the graph, in its own
`✦ summaries` group).

- **Seam:** `Summarizer` interface; `AnthropicSummarizer` (SDK imported ONLY here; model
  `SUMMARIZER_MODEL`, default `claude-sonnet-5`) · `MockSummarizer` (deterministic,
  citation-complete) — what all tests and `SYNAPSE_MOCK_MODELS=1` use.
- **Honesty:** zero-citation output ⇒ `GroundingError` (422, run rejected). Size-cap cuts ⇒
  the summary SAYS it was truncated. Cost guard: est. tokens > `SUMMARIZE_CONFIRM_THRESHOLD`
  (default 20k) ⇒ `requires_confirmation` — the UI asks before spending.
- `POST /api/v1/distill {node_id, scope: node|subtree, depth, confirm}`.
- `POST /api/v1/redistill {note_id, confirm}` — one-click re-distill of a summary (issue #9):
  root/scope/depth ride the summary's own frontmatter (`synapse.distilled_from`,
  `synapse.distill_scope`, `synapse.distill_depth`); the fresh write clears `synapse.stale`.
- Live smoke: opt-in `RUN_LIVE_DISTILL_SMOKE=1` (never CI).

**Ripple maintenance (issue #9):** every summary records `synapse.source_hashes:
{"<note_id>": "<sha256>", …}` — a single-line JSON object with each cited source's content
hash at distill time (JSON because note ids embed raw filenames, and a legal filename can
contain any hand-rolled delimiter — pipes, `=<64 hex> | `, quotes, even a newline). Ingest
compares that map against the vault on every sync and flags drift with `synapse.stale:
true` (edited OR pruned source); a re-distill rewrites the map fresh and the flag clears.
The graph carries `stale: true` on the node (schema v5) so the UI can badge it.

Deviation from the epic card (recorded): source-note backlinks are NOT written into source
frontmatter — re-ingest would erase them; the graph provides reverse edges once the summary
note is rebuilt in.
