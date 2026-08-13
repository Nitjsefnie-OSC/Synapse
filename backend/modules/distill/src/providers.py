"""
The Summarizer seam — model #1 (Distill). The Anthropic SDK may be imported HERE and nowhere
else (03_MODULE_CONTRACTS). Every test runs on MockSummarizer; AnthropicSummarizer is exercised
by the opt-in live smoke (`./start.sh smoke`, issue #3) and by founder acceptance with real keys.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SourceNote:
    note_id: str
    title: str
    body: str


@dataclass
class GroundedSummary:
    markdown: str          # the summary body — MUST carry `(vault: <note_id>)` citations
    model: str


class Summarizer:
    """Interface: summarize source notes into a grounded markdown summary."""

    def summarize(self, subject: str, notes: list[SourceNote], scope: str) -> GroundedSummary:  # pragma: no cover
        raise NotImplementedError


class MockSummarizer(Summarizer):
    """Deterministic, citation-complete summary — what tests and SYNAPSE_MOCK_MODELS=1 use."""

    def summarize(self, subject, notes, scope):
        lines = [
            f"This is a **mock distillation** of *{subject}* ({scope}, {len(notes)} source note(s)) — "
            "wire real keys (unset SYNAPSE_MOCK_MODELS) for a live summary.",
            "",
            "## Key threads",
        ]
        for n in notes:
            first = next((ln.strip() for ln in n.body.splitlines()
                          if ln.strip() and not ln.strip().startswith(("#", "---", ">"))), "(no prose)")
            lines.append(f"- **{n.title}** — {first[:140]} (vault: {n.note_id})")
        lines += ["", f"Image: a tidy constellation of {len(notes)} luminous index cards on a dark desk, soft focus"]
        return GroundedSummary(markdown="\n".join(lines), model="mock-summarizer")


class AnthropicSummarizer(Summarizer):
    def __init__(self, api_key: str, model: str, max_tokens: int):
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens

    def summarize(self, subject, notes, scope):
        import anthropic  # the ONLY allowed import site of the vendor SDK

        corpus = "\n\n".join(f"<note id=\"{n.note_id}\" title=\"{n.title}\">\n{n.body}\n</note>" for n in notes)
        prompt = (
            f"Write a wiki-style GIST of '{subject}' from the source notes below ({scope} scope).\n"
            "Voice: an encyclopedia entry about the SUBJECT ITSELF — lead with what it IS and "
            "what it does in plain language, then how it works and why it matters. Synthesize; "
            "never narrate file structure or walk the sources section by section.\n"
            "Rules: stay faithful — every claim-cluster cites its source as `(vault: <note id>)` "
            "using the ids given, exactly ONE id per parenthetical (repeat the parenthetical for "
            "multiple sources, never comma-join); if the sources cover only a narrow slice of the subject, say so "
            "in ONE closing line; if something is unclear, say so. Output clean markdown: a "
            "one-paragraph essence first, then '## Key threads' bullets.\n"
            "Finish with ONE line starting exactly `Image:` — a single-sentence visual brief "
            "(scene, mood, palette) that embodies THIS subject, drawn from its domain; the scene "
            "must contain no text.\n\n"
            f"{corpus}"
        )
        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # the model may lead with thinking blocks — the summary is the joined TEXT blocks
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return GroundedSummary(markdown=text, model=self.model)


# ── The seeing pass (sprint 05, Epic L) — model #1's vision lane ────────────────

@dataclass
class AssetDescription:
    markdown: str          # the description prose — NO wikilinks (links go in `links`)
    links: list[str]       # related note ids, chosen ONLY from the supplied candidates
    model: str


class VisionDescriber:
    """Interface: describe one asset (image bytes / extracted text) and pick related notes
    from a candidate list. AI-derived relations ship as INFERRED edges — never free-typed."""

    def describe(self, subject: str, image_bytes: bytes | None, text: str,
                 candidates: list[str], suffix: str = ".png") -> AssetDescription:  # pragma: no cover
        raise NotImplementedError


class MockVisionDescriber(VisionDescriber):
    """Deterministic description — tests and SYNAPSE_MOCK_MODELS=1."""

    def describe(self, subject, image_bytes, text, candidates, suffix=".png"):
        kind = "image" if image_bytes else "document"
        return AssetDescription(
            markdown=(f"This is a **mock description** of the {kind} *{subject}* — "
                      "wire real keys (unset SYNAPSE_MOCK_MODELS) for a live description."),
            links=candidates[:2],           # deterministic: first two candidates
            model="mock-vision")


class AnthropicVisionDescriber(VisionDescriber):
    _MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif"}

    def __init__(self, api_key: str, model: str, max_tokens: int):
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens

    def describe(self, subject, image_bytes, text, candidates, suffix=".png"):
        import base64

        import anthropic  # the ONLY allowed import site of the vendor SDK

        cand_block = "\n".join(f"- {c}" for c in candidates[:300])
        prompt = (
            f"Describe '{subject}' for a personal knowledge base.\n"
            "Write 2-5 sentences of plain prose: what it shows/says, notable details, likely "
            "context. No markdown headings, no links in the prose.\n"
            "Then, on the LAST line, output exactly `LINKS:` followed by the ids of the 0-3 "
            "most related notes chosen ONLY from this list (separated by ` | `; output "
            "`LINKS:` alone if none truly relate):\n"
            f"{cand_block}"
        )
        content: list = []
        if image_bytes is not None:
            content.append({"type": "image", "source": {
                "type": "base64",
                "media_type": self._MEDIA.get(suffix.lower(), "image/png"),
                "data": base64.standard_b64encode(image_bytes).decode(),
            }})
        else:
            prompt += f"\n\nDocument text:\n{text[:30000]}"
        content.append({"type": "text", "text": prompt})
        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        links: list[str] = []
        prose_lines = []
        for line in raw.splitlines():
            if line.strip().startswith("LINKS:"):
                links = [t.strip() for t in line.split("LINKS:", 1)[1].split(" | ") if t.strip()]
            else:
                prose_lines.append(line)
        return AssetDescription(markdown="\n".join(prose_lines).strip(), links=links,
                                model=self.model)
