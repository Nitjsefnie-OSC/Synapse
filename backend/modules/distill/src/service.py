"""
Distill service: node/subtree → grounded summary → `S — ` vault note.

Binding constraints (EPIC_D): subtree = BFS over wikilink+relative edges to a depth; size guard
with HONEST truncation reporting; a summary with zero `(vault: ...)` citations is a FAILED run;
summaries join the vault (own repo group `✦ summaries`) so the next rebuild adds them to the
graph, linked to their sources by wikilinks.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from modules.graph.src.services import GraphService
from modules.ingest.src.models import cap_note_id
from modules.ingest.src.services import IngestService, encode_source_hashes, fm_quote, fm_unquote

from .providers import SourceNote, Summarizer

_CITE_RE = re.compile(r"\(vault:\s*([^)]+?)\s*\)")
SUMMARY_REPO = "✦ summaries"
# issue #9 — the one-click re-distill reads the summary's own provenance lines
_DISTILLED_FROM_RE = re.compile(r"^synapse\.distilled_from:\s*(.+?)\s*$", re.MULTILINE)
_DISTILL_SCOPE_RE = re.compile(r"^synapse\.distill_scope:\s*(.+?)\s*$", re.MULTILINE)
_DISTILL_DEPTH_RE = re.compile(r"^synapse\.distill_depth:\s*(\d+)\s*$", re.MULTILINE)


def citation_audit(markdown: str, known: set[str]) -> tuple[int, list[str]]:
    """(citation_count, unknown_ids). Tolerant of real-model formatting: several ids
    comma-joined inside one parenthetical, repeated `vault:` prefixes, and ids truncated
    by an inner ')' (note ids may themselves contain parentheses)."""
    def strip_prefix(s: str) -> str:
        s = s.strip()
        return s[6:].strip() if s.lower().startswith("vault:") else s

    def matches(c: str) -> bool:
        cl = c.lower()
        if not cl:
            return False
        if cl in known or f"{cl}.md" in known:
            return True
        return len(cl) >= 4 and any(k.startswith(cl) for k in known)

    count, unknown = 0, []
    for m in _CITE_RE.finditer(markdown):
        whole = strip_prefix(m.group(1))
        if matches(whole):
            # a comma-CONTAINING note id cited verbatim — never split it (a false REJECT
            # burns an already-paid model call, deterministically, on every retry)
            count += 1
            continue
        for part in m.group(1).split(","):
            part = strip_prefix(part)
            if not part:
                continue
            count += 1
            if not matches(part):
                unknown.append(part)
    return count, sorted(set(unknown))


class ConfirmationRequired(Exception):
    def __init__(self, tokens_est: int, threshold: int):
        self.tokens_est, self.threshold = tokens_est, threshold


class GroundingError(Exception):
    pass


class DistillService:
    def __init__(self, vault_path: Path, summarizer: Summarizer, confirm_threshold: int = 20000):
        self.vault_path = Path(vault_path)
        self.graph = GraphService(vault_path)
        self.summarizer = summarizer
        self.confirm_threshold = confirm_threshold   # spend gate (asks first)
        self.hard_cap_chars = 480_000                # absolute safety cap (~120k tokens), independent of the gate

    # ── subtree collection ────────────────────────────────────────────────
    def collect(self, node_id: str, scope: str, depth: int) -> tuple[list[SourceNote], bool]:
        """BFS over non-sibling edges. Returns (notes, truncated) — truncated=True when the
        size guard cut sources; the summary must SAY so."""
        g = self.graph.build()
        if node_id not in g.nodes:
            raise KeyError(f"No note '{node_id}' in the vault.")
        wanted = [node_id]
        if scope == "subtree":
            seen, frontier = {node_id}, [node_id]
            for _ in range(depth):
                if not frontier:
                    break   # exhausted — never keep scanning all edges for empty rings
                out_ring, in_ring, fset = [], [], set(frontier)
                # TWO passes (edges live in a SET, whose iteration order changes across
                # interpreter runs): out-links are classified first for the whole ring, so a
                # note reachable BOTH ways always counts as a definition — single-pass
                # classification depended on which edge the set yielded first.
                for e in g.edges:
                    if e.type == "sibling":
                        continue
                    if e.src in fset and e.dst not in seen and e.dst in g.nodes and g.nodes[e.dst].kind == "note":
                        seen.add(e.dst); out_ring.append(e.dst)
                for e in g.edges:
                    if e.type == "sibling":
                        continue
                    if e.dst in fset and e.src not in seen and e.src in g.nodes and g.nodes[e.src].kind == "note":
                        seen.add(e.src); in_ring.append(e.src)
                # ring order = OUT-links first (what the subject POINTS TO — its definitions),
                # then in-links (what points at it — echoes/references); each sorted for
                # determinism. When the size cap cuts, it must starve the echo-adapters,
                # never the subject's own contracts (founder repro: ARIA rendered as a
                # doorway because her ux-design role note was truncated out).
                nxt = sorted(out_ring) + sorted(in_ring)
                frontier = nxt
                wanted.extend(nxt)
        notes, budget, truncated = [], self.hard_cap_chars, False
        for nid in wanted:
            data = self.graph.read_note(nid)
            if data is None:
                continue
            if notes and budget - len(data["body"]) < 0:   # the ROOT always ships; the cap cuts the rest
                truncated = True
                break
            budget -= len(data["body"])
            notes.append(SourceNote(note_id=nid, title=g.nodes[nid].title, body=data["body"]))
        return notes, truncated

    @staticmethod
    def tokens_est(notes: list[SourceNote]) -> int:
        return sum(len(n.body) for n in notes) // 4

    # ── the pipeline ──────────────────────────────────────────────────────
    def distill(self, node_id: str, scope: str = "node", depth: int = 2, confirm: bool = False) -> dict:
        notes, truncated = self.collect(node_id, scope, depth)
        est = self.tokens_est(notes)
        if est > self.confirm_threshold and not confirm:
            raise ConfirmationRequired(est, self.confirm_threshold)

        subject = notes[0].title if notes else node_id
        result = self.summarizer.summarize(subject, notes, scope)
        # grounded means grounded: every cited id must BE one of the source notes — a
        # hallucinated `(vault: made-up.md)` must not pass the gate
        known = {n.note_id.lower() for n in notes} | {n.title.strip().lower() for n in notes}
        citations, unknown = citation_audit(result.markdown, known)
        if citations == 0:
            raise GroundingError("The summary contains zero (vault: ...) citations — rejected as ungrounded.")
        if unknown:
            raise GroundingError(
                "The summary cites notes that are NOT in the source set (hallucinated citations): "
                + ", ".join(unknown[:5]) + (" …" if len(unknown) > 5 else "") + " — rejected.")

        note_id = self._write_summary(subject, node_id, scope, depth, notes, truncated, result)
        return {"summary_note_id": note_id, "citations": citations, "truncated": truncated,
                "tokens_est": est, "model": result.model, "sources": [n.note_id for n in notes]}

    def redistill(self, summary_note_id: str, confirm: bool = False) -> dict:
        """One-click re-distill (issue #9): re-run the distill that produced a summary,
        from the summary note ALONE — root/scope/depth ride its frontmatter
        (`synapse.distilled_from` / `distill_scope` / `distill_depth`). The fresh write
        clears the stale flag and re-records the source hashes. The same spend gate and
        grounding audit apply — a re-distill is a distill."""
        path = self.vault_path / "notes" / summary_note_id
        if (not path.is_file()
                or path.parent.resolve() != (self.vault_path / "notes").resolve()):
            raise KeyError(f"No note '{summary_note_id}' in the vault.")
        # the WHOLE frontmatter, delimiter-bounded — never a fixed window: a long
        # `synapse.source_hashes` line (many sources) would push `distilled_from` past
        # any constant slice (same doctrine as ingest's _frontmatter_text, GBU P1)
        fm = IngestService._frontmatter_text(path)
        if "synapse.kind: summary" not in fm:
            raise ValueError(f"'{summary_note_id}' is not a distilled summary.")
        m = _DISTILLED_FROM_RE.search(fm)
        if not m:
            raise ValueError(
                f"'{summary_note_id}' predates distill provenance (no synapse.distilled_from) "
                "— re-distill it from its root note instead.")
        scope_m, depth_m = _DISTILL_SCOPE_RE.search(fm), _DISTILL_DEPTH_RE.search(fm)
        # fm_unquote undoes the fm_quote at write time: a root id or scope that needed
        # quoting (a newline filename rides every note id) decodes back to the EXACT
        # vault id — a truncated read is the KeyError the delta probe killed redistill with
        return self.distill(fm_unquote(m.group(1)),
                            scope=fm_unquote(scope_m.group(1)) if scope_m else "node",
                            depth=int(depth_m.group(1)) if depth_m else 2,
                            confirm=confirm)

    def _write_summary(self, subject, root_id, scope, depth, notes, truncated, result) -> str:
        safe = re.sub(r"[/\\:*?\"<>|]", "·", subject)[:80].strip() or "note"
        # byte-cap, not char-cap: an emoji/CJK-heavy 80-char subject can exceed ext4's
        # 255-byte filename limit — same doctrine (and helper) as ingest note ids
        note_id = cap_note_id(f"S — {safe}.md")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # issue #9: the distill-time hash map — each cited source's content hash as it was
        # when this summary was written. Ingest compares it against the vault on every sync
        # and flags drift (`synapse.stale: true`); a re-distill rewrites this map fresh.
        # The map is ONE JSON line: note ids embed raw filenames, and a legal filename can
        # contain any delimiter a hand-rolled format would pick — JSON escapes by
        # construction, so no id can break the framing.
        hash_map = encode_source_hashes({
            n.note_id: h for n in notes
            if (h := IngestService.existing_hash(self.vault_path / "notes" / n.note_id))})
        # issue #9, third round — EVERY hostile-capable scalar goes through fm_quote
        # (note ids / root id embed raw filenames; a legal filename may contain a
        # newline, and a bare interpolation forges real frontmatter lines: the delta
        # probe marked an unchanged source stale via `synapse.sources` and crashed
        # redistill via a shadow `synapse.source_hashes`). Safe values stay bare and
        # human-readable; hostile ones become one JSON-quoted line. `source_repo` is a
        # compile-time literal, ingested_at an ISO timestamp, distill_depth an int —
        # fixed alphabets, never hostile.
        scope_line = f"{scope} (depth {depth if scope == 'subtree' else '-'})"
        fm = (
            "---\n"
            "synapse.kind: summary\n"
            f"synapse.source_repo: {SUMMARY_REPO}\n"
            f"synapse.source_path: {fm_quote(note_id)}\n"
            f"synapse.ingested_at: {now}\n"
            f"synapse.model: {fm_quote(result.model)}\n"
            f"synapse.scope: {fm_quote(scope_line)}\n"
            f"synapse.sources: {fm_quote(', '.join(n.note_id for n in notes))}\n"
            f"synapse.source_hashes: {hash_map}\n"
            # machine-readable provenance for the one-click re-distill (`synapse.scope`
            # above stays the human line — it predates this feature and is display-shaped)
            f"synapse.distilled_from: {fm_quote(root_id)}\n"
            f"synapse.distill_scope: {fm_quote(scope)}\n"
            f"synapse.distill_depth: {depth}\n"
            "---\n"
        )
        trunc_note = ("\n> ⚠️ **Truncated:** the source set exceeded the size cap — this summary "
                      "covers only the notes listed below.\n") if truncated else ""
        links = "\n".join(f"- [[{n.note_id}]]" for n in notes)
        body = (f"# S — {subject}\n{trunc_note}\n{result.markdown}\n\n"
                f"## Distilled from ({len(notes)} note(s), root [[{root_id}]])\n\n{links}\n")
        path = self.vault_path / "notes" / note_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fm + body, encoding="utf-8")
        return note_id


# ── The seeing pass (sprint 05, Epic L) ─────────────────────────────────────────

_AI_SECTION = "## Description (AI)"
_LINKS_LINE_RE = re.compile(r"^synapse\.inferred_links: .*\n", re.MULTILINE)


class DescribeService:
    """Describe one asset sidecar: vision (images) / text (PDFs) → a `## Description (AI)`
    section + `synapse.inferred_links` frontmatter. Links are chosen ONLY from a supplied
    candidate list; anything else the model invents is DROPPED and counted honestly."""

    # a vision call is roughly flat-cost per image; text rides the usual /4 heuristic
    _IMAGE_TOKENS_EST = 2600

    def __init__(self, vault_path: Path, describer, confirm_threshold: int = 20000):
        self.vault_path = Path(vault_path)
        self.graph = GraphService(vault_path)
        self.describer = describer
        self.confirm_threshold = confirm_threshold
        self._pool: list[str] | None = None   # candidate pool, built ONCE per service life

    def candidates(self, exclude: str) -> list[str]:
        """Top-degree KNOWLEDGE notes (no other assets, no summaries) — the model picks
        related ids from this list, never free-types them. Cached per service instance:
        a describe-all over 500 photos must not rebuild the graph 500 times (GBU P2).
        Ids containing ' | ' are excluded — that's the links-line separator."""
        if self._pool is None:
            g = self.graph.build()
            pool = [
                n for n in g.nodes.values()
                if n.kind == "note" and not n.tags and n.repo != SUMMARY_REPO
                and " | " not in n.id
            ]
            pool.sort(key=lambda n: (-(n.in_degree + n.out_degree), n.id))
            self._pool = [n.id for n in pool[:301]]
        return [c for c in self._pool if c != exclude][:300]

    def tokens_est(self, asset_type: str, text: str) -> int:
        return self._IMAGE_TOKENS_EST if asset_type == "image" else 800 + len(text) // 4

    def describe(self, note_id: str, source_file: Path, confirm: bool = False) -> dict:
        note = self.graph.read_note(note_id)
        if note is None or note.get("kind") != "asset":
            raise KeyError(f"No asset note '{note_id}' in the vault.")
        asset_type = note.get("asset_type", "image")
        image_bytes = None
        text = ""
        if asset_type == "image":
            image_bytes = source_file.read_bytes()
        else:
            # the sidecar carries the extracted PDF text — WITHOUT any previous AI section
            # (a re-describe must never feed the model its own prior output as "document text")
            text = note["body"].split(_AI_SECTION, 1)[0]
        est = self.tokens_est(asset_type, text)
        if est > self.confirm_threshold and not confirm:
            raise ConfirmationRequired(est, self.confirm_threshold)
        cands = self.candidates(exclude=note_id)
        result = self.describer.describe(
            subject=Path(note["source_path"]).name,
            image_bytes=image_bytes, text=text, candidates=cands,
            suffix=Path(note["source_path"]).suffix)
        known = set(cands)
        kept = [l for l in result.links if l in known]
        dropped = [l for l in result.links if l not in known]
        self._write_back(note_id, result.markdown, kept, result.model)
        return {"note_id": note_id, "model": result.model,
                "links_added": kept, "links_dropped": dropped,
                "description_chars": len(result.markdown)}

    def _write_back(self, note_id: str, description: str, links: list[str], model: str) -> None:
        """Idempotent: replaces any existing AI section + inferred_links line."""
        path = self.graph.notes_dir / note_id
        content = path.read_text(encoding="utf-8")
        content = _LINKS_LINE_RE.sub("", content)
        if links:
            # fm_quote the joined line: link ids are graph note ids, which embed raw
            # filenames — a newline in one would otherwise forge frontmatter here too
            line = f"synapse.inferred_links: {fm_quote(' | '.join(links))}\n"
            if "synapse.ingested_at:" in content:
                content = content.replace("synapse.ingested_at:",
                                          line + "synapse.ingested_at:", 1)
            elif content.startswith("---\n"):   # hand-authored sidecar (Obsidian edits)
                content = "---\n" + line + content[4:]
            else:
                content = f"---\n{line}---\n" + content
        idx = content.find(_AI_SECTION)
        if idx != -1:
            content = content[:idx].rstrip() + "\n"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        content = (content.rstrip() + f"\n\n{_AI_SECTION}\n\n{description}\n\n"
                   f"> _Described by {model}, {now}. Related notes ride as dashed INFERRED "
                   "edges in the graph._\n")
        import os
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def undescribed_assets(self) -> list[str]:
        """Asset sidecars with no AI section yet — the bulk-describe work list."""
        out = []
        for p in sorted(self.graph.notes_dir.glob("*.md")):
            head = p.read_text(encoding="utf-8", errors="replace")
            if "synapse.kind: asset" in head[:600] and _AI_SECTION not in head:
                out.append(p.name)
        return out
