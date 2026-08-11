"""Graph data models (stdlib only). graph.json is DETERMINISTIC — no timestamps, sorted
nodes/edges — so the rebuild-invariance guarantee is testable by deep equality."""

from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 5   # v5 (issue #9): + node `stale` (optional, true-only) — a distilled
                     #     summary whose cited sources changed since distill time. Absent
                     #     means fresh-or-untracked, never false.
                     # v4 (sprint 06 S1/S2): + node `first_seen` / `file_mtime` (both optional —
                     #     a graph written before v4 loads with them ABSENT, never back-dated)
                     # v3 (Epic J): + edge `confidence` — EXTRACTED (parsed, 1.0) | INFERRED
#                      (AI-derived, discrete score 0.55–0.95) | AMBIGUOUS (flagged for review).
#                      Adopted BEFORE the first AI-derived edge ships (honest-status doctrine).
#                      v2 (D-5): + `pathref` edges — backticked `*.md` pointers.


@dataclass
class Node:
    id: str
    kind: str = "note"          # note | repo (repo = grouping hub)
    title: str = ""
    repo: str = ""
    source_path: str = ""
    tags: list[str] = field(default_factory=list)
    in_degree: int = 0
    out_degree: int = 0
    unresolved: list[str] = field(default_factory=list)
    # Sprint 06 S1/S2. Empty string = genuinely unknown, and the UI must SAY so rather than
    # render a blank that reads as "old". Notes indexed before v4 have no first_seen and are
    # never back-dated — an invented date is worse than an absent one.
    first_seen: str = ""
    file_mtime: str = ""
    # issue #9: a summary whose cited sources drifted from the distill-time hashes. Same
    # doctrine as the time fields — serialized ONLY when true; absent is the fresh case.
    stale: bool = False

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "title": self.title, "repo": self.repo,
            "source_path": self.source_path, "tags": self.tags,
            "in_degree": self.in_degree, "out_degree": self.out_degree,
            "unresolved": sorted(self.unresolved),
        }
        # Omitted entirely when unknown — a present-but-empty key invites `?? "old"` downstream.
        if self.first_seen:
            d["first_seen"] = self.first_seen
        if self.file_mtime:
            d["file_mtime"] = self.file_mtime
        if self.stale:
            d["stale"] = True
        return d


CONFIDENCE_TAGS = ("EXTRACTED", "INFERRED", "AMBIGUOUS")


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    type: str                   # wikilink | relative | pathref | asset | sibling | semantic (AI-derived)
    confidence: str = "EXTRACTED"          # EXTRACTED | INFERRED | AMBIGUOUS (schema v3)
    confidence_score: float | None = None  # INFERRED only — discrete rubric 0.55–0.95

    def __post_init__(self):
        # invariants enforced at construction (Codex P2): an invalid tag/score combination
        # must fail loudly at EMIT time, not surface as silent schema drift in graph.json
        if self.confidence not in CONFIDENCE_TAGS:
            raise ValueError(f"unknown edge confidence tag: {self.confidence!r}")
        if self.confidence == "INFERRED" and self.confidence_score is None:
            raise ValueError("INFERRED edges require a confidence_score (rubric 0.55-0.95)")
        if self.confidence != "INFERRED" and self.confidence_score is not None:
            raise ValueError(f"{self.confidence} edges carry no confidence_score")

    def to_dict(self) -> dict:
        d = {"src": self.src, "dst": self.dst, "type": self.type, "confidence": self.confidence}
        if self.confidence_score is not None:
            d["confidence_score"] = self.confidence_score
        return d


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": [self.nodes[k].to_dict() for k in sorted(self.nodes)],
            "edges": [e.to_dict() for e in sorted(
                self.edges,
                key=lambda e: (e.src, e.dst, e.type, e.confidence, e.confidence_score or -1.0),
            )],
        }

    def stats(self) -> dict:
        note_nodes = [n for n in self.nodes.values() if n.kind == "note"]
        by_type: dict[str, int] = {}
        for e in self.edges:
            by_type[e.type] = by_type.get(e.type, 0) + 1
        top = sorted(note_nodes, key=lambda n: n.in_degree + n.out_degree, reverse=True)[:5]
        return {
            "schema_version": SCHEMA_VERSION,
            "notes": len(note_nodes),
            "repos": sum(1 for n in self.nodes.values() if n.kind == "repo"),
            "edges_by_type": dict(sorted(by_type.items())),
            "edges_total": len(self.edges),
            "unresolved_links": sum(len(n.unresolved) for n in note_nodes),
            "top_connected": [
                {"id": n.id, "title": n.title, "degree": n.in_degree + n.out_degree} for n in top
            ],
        }
