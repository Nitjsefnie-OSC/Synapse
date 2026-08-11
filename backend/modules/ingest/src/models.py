"""Data models for the ingest module (stdlib dataclasses — no framework deps)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# ext4 caps a FILENAME at 255 bytes; UTF-8 multiplies (Hebrew = 2 bytes/char). Cap well below
# it (room for the atomic-write `.tmp` suffix too): over-long ids keep a readable head and get
# a deterministic content-free hash tail — same path always maps to the same id.
_MAX_ID_BYTES = 200


def cap_note_id(flat: str) -> str:
    if len(flat.encode("utf-8")) <= _MAX_ID_BYTES:
        return flat
    stem = flat[:-3] if flat.endswith(".md") else flat
    tail = "…" + hashlib.sha256(flat.encode("utf-8")).hexdigest()[:10] + ".md"
    budget = _MAX_ID_BYTES - len(tail.encode("utf-8"))
    while len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return stem + tail


@dataclass(frozen=True)
class SourceFile:
    """One markdown file discovered in a source repo."""

    repo_name: str
    repo_root: Path
    path: Path            # absolute

    @property
    def rel_path(self) -> str:
        return self.path.relative_to(self.repo_root).as_posix()

    @property
    def note_id(self) -> str:
        """Deterministic, collision-free, readable: `<repo>__<rel/path>` with `/` → `__`.
        Keeps the `.md` suffix so the note is a valid markdown filename as-is. Over-long
        paths (deep trees, long Hebrew titles) are hash-capped — one un-writable filename
        must never exist, let alone abort a whole-workspace ingest."""
        return cap_note_id(f"{self.repo_name}__{self.rel_path.replace('/', '__')}")


# Asset extensions the sidecar pass understands (sprint 05, Epic K — issue #13 stage 1)
ASSET_TYPES = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    # .svg: heroes ship as SVG on several articles. Omitting it meant the file was synced
    # into the KB, never became a node, and so no article could resolve its own hero.
    ".svg": "image",
    ".pdf": "pdf",
    # video/audio (2026-08-04): a marketing brain whose whole point is "where are the
    # videos?" cannot answer that if the only ingestible media is stills and PDFs. A
    # sidecar is METADATA — the bytes never enter the vault — so a 2 GB footage folder
    # costs the same as a screenshot. Text extraction obviously doesn't apply; these
    # sidecars carry provenance + type, and the reader renders a real player.
    ".mp4": "video", ".webm": "video", ".mov": "video", ".m4v": "video",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio",
    # interactives (2026-08-04): a self-contained HTML visual pack IS media — the KB is
    # the source of truth for it, and must be able to hold and PLAY it whether or not the
    # publishing platform can render it. Sandboxed at display time (see wiki.js).
    ".html": "interactive", ".htm": "interactive",
}


@dataclass(frozen=True)
class SourceAsset:
    """One binary asset (image/PDF) discovered in an assets-enabled source root.
    The vault gets a SIDECAR markdown note (metadata + extracted text); the bytes stay
    in the source root — a 50GB photo library must never duplicate into the vault."""

    repo_name: str
    repo_root: Path
    path: Path            # absolute

    @property
    def rel_path(self) -> str:
        return self.path.relative_to(self.repo_root).as_posix()

    @property
    def asset_type(self) -> str:
        return ASSET_TYPES[self.path.suffix.lower()]

    @property
    def note_id(self) -> str:
        """Sidecar id = asset filename + `.asset.md` (`…__img.jpg.asset.md`). The extra
        `.asset` disambiguates from a REAL markdown file named `img.jpg.md` sitting next to
        the photo — the Obsidian annotation convention — which would otherwise share the
        id and silently merge/thrash (GBU sprint-05 P1)."""
        return cap_note_id(f"{self.repo_name}__{self.rel_path.replace('/', '__')}.asset.md")


@dataclass
class RepoReport:
    repo: str
    files_found: int = 0
    notes_written: int = 0
    unchanged: int = 0
    skipped: int = 0
    assets_found: int = 0
    assets_written: int = 0
    assets_unchanged: int = 0
    assets_skipped: int = 0


@dataclass
class IngestReport:
    """The honest ingest outcome — printed by the CLI and returned by the API verbatim."""

    repos: list[RepoReport] = field(default_factory=list)
    pruned: int = 0            # notes removed by the sync (disabled roots / deleted sources)
    errors: list[str] = field(default_factory=list)   # unreadable paths etc. — reported, never fatal
    # issue #9: summaries whose stale flag CHANGED this sync (either direction) — the UI
    # toasts it, the CLI prints it; a quiet flag flip is the exact failure the issue kills
    stale_summaries: list[str] = field(default_factory=list)

    @property
    def files_found(self) -> int:
        return sum(r.files_found for r in self.repos)

    @property
    def notes_written(self) -> int:
        return sum(r.notes_written for r in self.repos)

    @property
    def unchanged(self) -> int:
        return sum(r.unchanged for r in self.repos)

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.repos)

    @property
    def assets_found(self) -> int:
        return sum(r.assets_found for r in self.repos)

    @property
    def assets_written(self) -> int:
        return sum(r.assets_written for r in self.repos)

    def to_dict(self) -> dict:
        return {
            "repos": [vars(r) for r in self.repos],
            "totals": {
                "files_found": self.files_found,
                "notes_written": self.notes_written,
                "unchanged": self.unchanged,
                "skipped": self.skipped,
                "pruned": self.pruned,
                "assets_found": self.assets_found,
                "assets_written": self.assets_written,
            },
            "errors": self.errors[:50],
            "stale_summaries": self.stale_summaries[:50],
        }

    def render(self) -> str:
        lines = ["Ingest report", "-------------"]
        for r in self.repos:
            assets = (f", {r.assets_found} assets → {r.assets_written} sidecars"
                      if r.assets_found else "")
            lines.append(
                f"  {r.repo}: {r.files_found} md found → {r.notes_written} written, "
                f"{r.unchanged} unchanged, {r.skipped} skipped{assets}"
            )
        t = self.to_dict()["totals"]
        lines.append(
            f"  TOTAL: {t['files_found']} found → {t['notes_written']} written, "
            f"{t['unchanged']} unchanged, {t['skipped']} skipped, {t['pruned']} pruned"
        )
        if self.errors:
            lines.append(f"  ERRORS ({len(self.errors)}): " + "; ".join(self.errors[:5]) +
                         (" …" if len(self.errors) > 5 else ""))
        if self.stale_summaries:
            lines.append(f"  RIPPLE: {len(self.stale_summaries)} summar(y/ies) changed staleness: "
                         + ", ".join(self.stale_summaries[:5]) +
                         (" …" if len(self.stale_summaries) > 5 else ""))
        return "\n".join(lines)
