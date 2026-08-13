# `modules/render` — model #2: summary → image

Turns an `S — ` summary note into a generated image: prompt derives from the summary's **themes**
(scene-style, **no text in the image** — decision D-4), PNG lands in `<vault>/media/`, and the
note gets the image embedded (markdown at the top + `synapse.image` frontmatter) so rebuilds
keep it. Re-render swaps the image idempotently.

- **Seam:** `ImageRenderer` interface; `OpenAIImageRenderer` (SDK imported ONLY here; model
  `IMAGE_MODEL`, default `gpt-image-1` — requires a verified OpenAI org) ·
  `MockImageRenderer` — a deterministic stdlib-generated 256px PNG derived from the prompt
  hash, used by all tests and `SYNAPSE_MOCK_MODELS=1`.
- Only summary notes render (`NotASummary` 422 otherwise — distill first).
- `POST /api/v1/render {summary_note_id}` · images served at `GET /media/<file>`.
- Live smoke: opt-in, via `./start.sh smoke` (issue #3) — needs real keys, never CI.
