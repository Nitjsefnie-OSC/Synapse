"""
Graph service: vault → graph.json + Index.md + stats.

Binding constraints (see project-management/sprints/sprint_01/todo/EPIC_B_graph_index.md):
- Input = THE VAULT ONLY (never the source repos) — the graph must be rebuildable from the
  vault alone; graph.json is a deterministic derived cache (delete + rebuild ⇒ identical).
- Unresolved links are recorded on the node, never silently dropped.
- Stdlib only.
"""

from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import Edge, Graph, Node

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FM_FIELD_RE = re.compile(
    r"^synapse\.(source_repo|source_path|kind|asset_type|inferred_links|asset_refs"
    r"|first_seen|file_mtime|stale):\s*(.+?)\s*$",
    re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
# backticked path pointers: `../roles/cpto.md`, `.claude/policies/commandments.md` — the
# thin-adapter convention (agents point at contracts as code paths, not hyperlinks)
_CODEPATH_RE = re.compile(r"`([^`\s]+?\.md)`")
# ASSET links (founder ask 2026-08-04: "media nodes MUST be linked to posts, articles").
# Assets already become NODES (sidecars, sprint 05) but nothing could ever LINK to one:
# both link regexes above hard-require `.md`, so `![hero](hero.png)` or `[deck](x.pdf)`
# resolved to nothing and every image/PDF sat at degree 1 — a node in name only. These
# match the same markdown/backtick forms against the extensions ingest actually sidecars,
# and resolve to the sidecar the same way (the exact map is keyed by the ORIGINAL
# source_path, `…/hero.png`, so no id munging is needed here).
_ASSET_EXT = r"(?:png|jpe?g|gif|webp|svg|pdf|mp4|webm|mov|m4v|mp3|m4a|wav|html?)"
_MDASSET_RE = re.compile(rf"\[[^\]]*\]\(([^)\s]+?\.{_ASSET_EXT})(?:#[^)]*)?\)", re.IGNORECASE)
_CODEASSET_RE = re.compile(rf"`([^`\s]+?\.{_ASSET_EXT})`", re.IGNORECASE)


class GraphService:
    def __init__(self, vault_path: Path):
        self.vault_path = Path(vault_path)
        self.notes_dir = self.vault_path / "notes"
        self.graph_file = self.vault_path / "graph.json"
        self.index_file = self.vault_path / "Index.md"

    # ── vault reading ─────────────────────────────────────────────────────
    def _load_notes(self) -> list[dict]:
        notes = []
        if not self.notes_dir.is_dir():
            return notes
        for path in sorted(self.notes_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue   # pruned/deleted between glob and read (racing tab) — not an error
            fm = _FM_RE.match(text)
            fields = dict(_FM_FIELD_RE.findall(fm.group(1))) if fm else {}
            body = text[fm.end():] if fm else text
            title_m = _TITLE_RE.search(body)
            notes.append({
                "id": path.name,
                "repo": fields.get("source_repo", ""),
                "source_path": fields.get("source_path", ""),
                "title": (title_m.group(1) if title_m else Path(fields.get("source_path", path.stem)).stem),
                "body": body,
                "asset_type": fields.get("asset_type", "") if fields.get("kind") == "asset" else "",
                # sprint 06 S2 — absent stays absent; never defaulted to now or to the epoch
                "first_seen": fields.get("first_seen", ""),
                "file_mtime": fields.get("file_mtime", ""),
                "inferred_links": fields.get("inferred_links", ""),
                "asset_refs": fields.get("asset_refs", ""),
                "stale": fields.get("stale", "") == "true",   # issue #9 — summaries only
            })
        return notes

    # ── link resolution ───────────────────────────────────────────────────
    @staticmethod
    def _namespace(notes: list[dict]) -> tuple[dict[str, str], dict[str, str | None]]:
        """(exact maps, stem map). Stem map values are None when ambiguous — an ambiguous
        wikilink is recorded as unresolved rather than guessed."""
        exact: dict[str, str] = {}
        stems: dict[str, str | None] = {}
        for n in notes:
            exact[n["id"].lower()] = n["id"]
            exact[f"{n['repo']}/{n['source_path']}".lower()] = n["id"]
            stem = Path(n["source_path"]).stem.lower()
            stems[stem] = None if stem in stems else n["id"]
            title = n["title"].strip().lower()
            if title:
                stems[title] = None if title in stems and stems[title] != n["id"] else n["id"]
        return exact, stems

    def _resolve_wikilink(self, target: str, exact, stems) -> str | None:
        t = target.strip().lower()
        return exact.get(t) or exact.get(f"{t}.md") or stems.get(t)

    @staticmethod
    def _resolve_relative(note: dict, target: str, exact) -> str | None:
        """A `[x](path.md)` link resolves against the note's source location in its own repo."""
        if target.startswith(("http://", "https://", "/")):
            return None
        base = posixpath.dirname(note["source_path"])
        joined = posixpath.normpath(posixpath.join(base, target))
        if joined.startswith(".."):
            return None   # points outside the repo — unresolvable by design
        return exact.get(f"{note['repo']}/{joined}".lower())

    # ── build ─────────────────────────────────────────────────────────────
    def build(self) -> Graph:
        notes = self._load_notes()
        exact, stems = self._namespace(notes)
        g = Graph()
        for n in notes:
            g.nodes[n["id"]] = Node(
                id=n["id"], kind="note", title=n["title"],
                repo=n["repo"], source_path=n["source_path"],
                # assets stay kind:note in the graph (windowing/search unchanged) and carry
                # their nature as a tag — the explorer draws the 📷/📄 glyph from it
                tags=[f"asset:{n['asset_type']}"] if n["asset_type"] else [],
                first_seen=n.get("first_seen", ""), file_mtime=n.get("file_mtime", ""),
                stale=n["stale"],
            )
        for repo in sorted({n["repo"] for n in notes if n["repo"]}):
            g.nodes[f"repo:{repo}"] = Node(id=f"repo:{repo}", kind="repo", title=repo, repo=repo)

        for n in notes:
            node = g.nodes[n["id"]]
            if n["repo"]:
                g.edges.add(Edge(src=n["id"], dst=f"repo:{n['repo']}", type="sibling"))
            for m in _WIKILINK_RE.finditer(n["body"]):
                dst = self._resolve_wikilink(m.group(1), exact, stems)
                if dst and dst != n["id"]:
                    g.edges.add(Edge(src=n["id"], dst=dst, type="wikilink"))
                elif not dst:
                    node.unresolved.append(f"[[{m.group(1).strip()}]]")
            for m in _MDLINK_RE.finditer(n["body"]):
                dst = self._resolve_relative(n, m.group(1), exact)
                if dst and dst != n["id"]:
                    g.edges.add(Edge(src=n["id"], dst=dst, type="relative"))
                elif dst is None and not m.group(1).startswith(("http://", "https://")):
                    node.unresolved.append(m.group(1))
            # pathref (D-5): backticked `*.md` pointers — resolved note-relative, then
            # repo-root-relative (both conventions exist in adapter files). Unresolvable code
            # mentions are NOT recorded as unresolved: code often quotes hypothetical paths.
            # sprint 05 (Epic L): AI-derived relations from `synapse.inferred_links` —
            # the FIRST real INFERRED edges. Ids are exact (validated at describe time);
            # a target pruned since then is recorded, never silently dropped.
            for raw_link in (t.strip() for t in n["inferred_links"].split(" | ") if t.strip()):
                if raw_link in g.nodes or raw_link.lower() in exact:
                    dst = raw_link if raw_link in g.nodes else exact[raw_link.lower()]
                    if dst != n["id"]:
                        g.edges.add(Edge(src=n["id"], dst=dst, type="semantic",
                                         confidence="INFERRED", confidence_score=0.75))
                else:
                    node.unresolved.append(f"[[{raw_link}]] (AI-inferred)")
            for m in _CODEPATH_RE.finditer(n["body"]):
                token = m.group(1)
                # strip only leading `./` sequences — a bare lstrip('./') would eat the dot
                # of dot-directories like `.claude/...`
                root_tok = re.sub(r"^(?:\./)+", "", token)
                dst = self._resolve_relative(n, token, exact) \
                    or exact.get(f"{n['repo']}/{root_tok}".lower())
                if dst and dst != n["id"]:
                    g.edges.add(Edge(src=n["id"], dst=dst, type="pathref"))
            # asset links — an article/post/manifest pointing at a real image or PDF. Same
            # resolution as above; unresolvable ones are NOT recorded as unresolved links
            # (a doc quoting an illustrative filename is not a broken reference).
            # ADAPTER resolutions (ingest turned `<Visual id=…>` / `<YouTube id=…>` into
            # real local paths without touching the verbatim body) — these are the edges
            # that make an id-referenced interactive/video a first-class graph citizen.
            for ref in (t.strip() for t in n["asset_refs"].split(" | ") if t.strip()):
                dst = exact.get(f"{n['repo']}/{ref}".lower())
                if dst and dst != n["id"]:
                    g.edges.add(Edge(src=n["id"], dst=dst, type="asset"))
            for rx in (_MDASSET_RE, _CODEASSET_RE):
                for m in rx.finditer(n["body"]):
                    token = m.group(1)
                    if token.startswith(("http://", "https://", "data:")):
                        continue
                    root_tok = re.sub(r"^(?:\./)+", "", token)
                    dst = self._resolve_relative(n, token, exact) \
                        or exact.get(f"{n['repo']}/{root_tok}".lower())
                    if dst and dst != n["id"]:
                        g.edges.add(Edge(src=n["id"], dst=dst, type="asset"))

        for e in g.edges:
            if e.type != "sibling":
                g.nodes[e.src].out_degree += 1
                g.nodes[e.dst].in_degree += 1
        return g

    # ── outputs ───────────────────────────────────────────────────────────
    def rebuild(self) -> Graph:
        """Vault → graph.json + Index.md. graph.json stays deterministic (no timestamps).
        Writes are ATOMIC (tmp + os.replace): git-hook auto-sync makes background rebuilds
        routine, and a reader (explorer refresh, MCP tool call) must never see a
        half-written file (GBU sprint-04 P2-4)."""
        import os as _os
        g = self.build()
        self.vault_path.mkdir(parents=True, exist_ok=True)
        for target, content in (
            (self.graph_file, json.dumps(g.to_dict(), ensure_ascii=False, indent=1)),
            (self.index_file, self._render_index(g)),
        ):
            tmp = target.with_name(f"{target.name}.{_os.getpid()}.tmp")   # unique per writer
            tmp.write_text(content, encoding="utf-8")
            _os.replace(tmp, target)
        return g

    def load(self) -> dict | None:
        if not self.graph_file.is_file():
            return None
        return json.loads(self.graph_file.read_text(encoding="utf-8"))

    def read_note(self, note_id: str) -> dict | None:
        """One vault note, split into frontmatter fields + body (for viewers)."""
        path = self.notes_dir / note_id
        if not path.is_file() or path.parent.resolve() != self.notes_dir.resolve():
            return None   # unknown id or a path-traversal attempt
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None   # deleted between check and read (racing tab)
        fm = _FM_RE.match(text)
        fields = dict(_FM_FIELD_RE.findall(fm.group(1))) if fm else {}
        return {
            "id": note_id,
            "repo": fields.get("source_repo", ""),
            "source_path": fields.get("source_path", ""),
            "kind": fields.get("kind", "note"),
            "asset_type": fields.get("asset_type", ""),
            "body": text[fm.end():] if fm else text,
        }

    def read_index(self) -> str | None:
        return self.index_file.read_text(encoding="utf-8") if self.index_file.is_file() else None

    def _render_index(self, g: Graph) -> str:
        s = g.stats()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "# Index — the front door of this brain",
            "",
            f"> Generated {now} by `synapse rebuild`. {s['notes']} notes · "
            f"{s['edges_total']} edges ({s['edges_by_type']}) · "
            f"{s['unresolved_links']} unresolved links (future notes).",
            "",
            "## Top connected",
            "",
        ]
        lines += [f"- [[{t['id']}]] — {t['title']} · {t['degree']} links" for t in s["top_connected"]]
        by_repo: dict[str, list[Node]] = {}
        for n in g.nodes.values():
            if n.kind == "note":
                by_repo.setdefault(n.repo or "(no repo)", []).append(n)
        for repo in sorted(by_repo):
            lines += ["", f"## {repo}", ""]
            for n in sorted(by_repo[repo], key=lambda x: x.id):
                deg = n.in_degree + n.out_degree
                lines.append(f"- [[{n.id}]] — {n.title} · {deg} links")
        return "\n".join(lines) + "\n"
