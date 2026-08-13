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
- Live smoke: opt-in, via `./start.sh smoke` (issue #3) — needs real keys, never CI.

Deviation from the epic card (recorded): source-note backlinks are NOT written into source
frontmatter — re-ingest would erase them; the graph provides reverse edges once the summary
note is rebuilt in.
