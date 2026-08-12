"""
Ingest service: source repos → vault notes.

Binding constraints (see project-management/sprints/sprint_01/todo/EPIC_A_ingest_vault.md):
- The vault is the source of truth for everything downstream; this module is the only writer
  of `notes/` from external content.
- Notes are `<our frontmatter>\n<original content verbatim>` — UTF-8, byte-faithful body.
  (Known POC limitation: a source file's own frontmatter block remains visible in the body.)
- Idempotent: unchanged `content_hash` ⇒ skip and report `unchanged`.
- Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import IngestReport, RepoReport, SourceFile

_HASH_RE = re.compile(r"^synapse\.content_hash:\s*([0-9a-f]{64})\s*$", re.MULTILINE)
# Sprint 06 S1 — the two time fields, and why there are two.
# `first_seen` is when a note JOINED THE BRAIN; `file_mtime` is when the FILE last changed.
# They answer different questions and neither substitutes for the other: a note last edited in
# June but indexed today is a new addition wearing an old date, and a note touched by a
# formatting sweep today is an old note wearing a new one. Linux has no creation time
# (`st_birthtime` is absent on ext4; `st_ctime` is inode-change time), so mtime cannot be
# made to mean "added" no matter how it is squinted at.
_FIRST_SEEN_RE = re.compile(r"^synapse\.first_seen:\s*(\S+)\s*$", re.MULTILINE)
_FILE_MTIME_RE = re.compile(r"^synapse\.file_mtime:\s*(\S+)\s*$", re.MULTILINE)
_REFS_RE = re.compile(r"^synapse\.asset_refs:\s*(.*?)\s*$", re.MULTILINE)
_REPO_RE = re.compile(r"^synapse\.source_repo:\s*(.+?)\s*$", re.MULTILINE)
_FM_KEY_RE = re.compile(r"^synapse\.[a-z_]+:", re.MULTILINE)
# Issue #9 (ripple maintenance): a distilled summary records each cited source's content
# hash at distill time; ingest compares them against the notes NOW in the vault and flags
# drift with `synapse.stale: true`. The map is a SINGLE-LINE JSON OBJECT
# (`synapse.source_hashes: {"<note_id>": "<sha256>", …}`) — JSON string encoding escapes
# every byte a filesystem permits in a filename (pipes, `=`, quotes, even newlines), so NO
# note id can break the map's framing. This replaced a hand-rolled `id=hash | …` line whose
# delimiters could occur INSIDE an id ("Meeting | notes.md"; "a=<64 hex> | b.md"; a name
# with a newline) — patched twice at the separator, the failure class survived both times.
_SUMMARY_KIND_RE = re.compile(r"^synapse\.kind:\s*summary\s*$", re.MULTILINE)
_SOURCE_HASHES_RE = re.compile(r"^synapse\.source_hashes:\s*(.*?)\s*$", re.MULTILINE)
_STALE_LINE_RE = re.compile(r"^synapse\.stale: true\n", re.MULTILINE)
FRONTMATTER_END = "---"


def encode_source_hashes(hashes: dict[str, str]) -> str:
    """The `synapse.source_hashes` value: {note_id: sha256} as ONE JSON line. `ensure_ascii`
    off keeps Hebrew/emoji ids readable — JSON never emits raw control characters, so the
    line stays single-line (and YAML-frontmatter-safe) for ANY id. Key order is the distill
    citation order, so an unchanged source set re-encodes byte-identically."""
    return _json_line(hashes)


def parse_source_hashes(line: str) -> dict[str, str] | None:
    """Decode a `synapse.source_hashes` value back to {note_id: sha256}. Returns None when
    the value is NOT the JSON map — a pre-fix `id=hash | …` summary or a hand-edited line.
    Never guesses: an undecodable map carries no trustworthy pairs, so it is treated as
    "no recorded hashes" (and surfaced by the caller, not silently dropped)."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        return None
    return data


# Issue #9, third round — frontmatter injection is a CLASS, not two lines. Note ids, root
# ids, repo names and paths all embed RAW filenames, and a legal filename may contain a
# newline: `x\nsynapse.stale: true\ny.md` interpolated bare into ANY frontmatter line
# forges real frontmatter (a stale flag on an unchanged source; a shadow source_hashes map
# that wins the first-match regex). The class is closed at EMISSION: every hostile-capable
# scalar goes through fm_quote — bare when the value is already a safe YAML plain scalar
# (the common case; frontmatter is for humans, so readable stays readable), else a JSON
# double-quoted string, the same escaping doctrine as the hash map: JSON escapes every
# byte a filesystem permits, so the value can never break the line's framing. Readers
# decode with fm_unquote; bare legacy values pass through unchanged.
# The class has an ANCHOR side too: an insertion/strip point located by a synapse.*
# SUBSTRING matches inside a quoted value (one physical line that still CONTAINS the
# anchor text) — anchors must be line-anchored regexes (see _write_stale_flag below,
# DescribeService._write_back). backend/tests/test_frontmatter_guard.py statically
# fails any new emission not routed through fm_quote and any new substring anchor.
_YAML_BREAKS_RE = re.compile(
    # control chars, DEL, and the three UNICODE line breaks YAML 1.1 honours (NEL, LS, PS)
    # — json.dumps(ensure_ascii=False) emits the unicode ones RAW, so they must force
    # quoting here AND be escaped inside the quoted form (see _json_line)
    r"[\x00-\x1f\x7f\x85\u2028\u2029]")
# a plain scalar starting with one of these is a YAML indicator, never data
_LEADING_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
_YAML_KEYWORD_RE = re.compile(r"\A(?:~|null|true|false|yes|no|on|off)\Z", re.IGNORECASE)
_YAML_NUMBER_RE = re.compile(
    r"\A(?:[-+]?(?:\d[\d_]*(?:\.\d[\d_]*)?|\.\d+)(?:[eE][-+]?\d+)?"
    r"|[-+]?\.(?:inf|nan)|0[xX][0-9a-fA-F]+|0[oO][0-7]+)\Z", re.IGNORECASE)
_YAML_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")   # a bare DATE re-types as a timestamp


def _json_line(payload) -> str:
    """json.dumps on ONE line, plus escaping the three unicode line breaks YAML 1.1 treats
    as real breaks (NEL, LS, PS) — json emits those RAW with ensure_ascii=False, and a raw
    one would split the line for a YAML reader. Everything else stays readable."""
    return (json.dumps(payload, ensure_ascii=False)
            .replace("\x85", "\\u0085").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def fm_quote(value: str) -> str:
    """One frontmatter VALUE, safe for ANY string. Bare when the value is already a YAML
    plain scalar that re-reads as exactly itself (no control/unicode-line-break chars, no
    leading indicator, no `: ` / ` #` / trailing `:`, and not a word YAML would re-type —
    `true`, `123`, `2024-01-01`); else a JSON double-quoted string. Either way the result
    is ONE physical line that decodes back to the exact original with fm_unquote."""
    if (value
            and value == value.strip()
            and not _YAML_BREAKS_RE.search(value)
            and value[0] not in _LEADING_INDICATORS
            and ": " not in value and not value.endswith(":")
            and " #" not in value
            and not _YAML_KEYWORD_RE.fullmatch(value)
            and not _YAML_NUMBER_RE.fullmatch(value)
            and not _YAML_DATE_RE.fullmatch(value)):
        return value
    return _json_line(value)


def fm_unquote(text: str) -> str:
    """Inverse of fm_quote on a READ: a JSON-quoted value decodes back to the exact
    original string; anything else — every pre-fix note, every safe value — passes
    through unchanged. A quote-shaped value that does not parse as one JSON string is
    returned raw, never guessed."""
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            decoded = json.loads(text)
        except ValueError:
            return text
        if isinstance(decoded, str):
            return decoded
    return text


def is_vault_dir(path: Path) -> bool:
    """Vault-shaped directory: a `graph.json` next to a `notes/` dir whose notes carry
    synapse.* frontmatter — i.e. a synapse vault, not source content. BOTH markers are
    required: a stray graph.json (or an innocent `notes/` dir) must never hide real
    markdown from the scan. Catches FOREIGN vaults left inside a source repo (an old
    `data/vault`); ingesting one would index notes-of-notes as first-class content."""
    if not (path / "graph.json").is_file():
        return False
    notes = path / "notes"
    if not notes.is_dir():
        return False
    for note in sorted(notes.glob("*.md"))[:5]:   # a few heads is proof enough
        try:
            head = note.read_text(encoding="utf-8", errors="replace")[:600]
        except OSError:
            continue   # unreadable note — the walk records dir/file errors elsewhere
        if head.startswith(FRONTMATTER_END) and _FM_KEY_RE.search(head):
            return True
    return False


def note_repo(note_path: Path) -> str | None:
    """The `synapse.source_repo` a vault note belongs to (frontmatter head only) — THE prune
    key. Pruning must always key on this, never on filename shape: a `{name}__*` glob
    over-matches other roots whose name shares the prefix."""
    try:
        head = note_path.read_text(encoding="utf-8", errors="replace")[:600]
    except FileNotFoundError:
        return None   # deleted between glob and read (racing tab) — nothing to prune
    m = _REPO_RE.search(head)
    # fm_unquote: a repo named with a newline/indicator is written quoted (fm_quote) —
    # the prune key must be the DECODED real name or the note is pruned from under an
    # unchanged, managed repo
    return fm_unquote(m.group(1)) if m else None


class IngestService:
    def __init__(self, vault_path: Path, ignore_dirs: frozenset[str] | set[str],
                 companion_media_dir: str = "media", interactive_prefix: str = "interactive__"):
        self.vault_path = Path(vault_path)
        self.notes_dir = self.vault_path / "notes"
        self.ignore_dirs = set(ignore_dirs)
        # the companion-media convention, injected — see Settings.companion_media_dir. The
        # defaults are the previous hard-coded literals, so every existing caller is unchanged.
        self.companion_media_dir = companion_media_dir
        self.interactive_prefix = interactive_prefix

    # ── discovery ─────────────────────────────────────────────────────────
    def scan_repo(self, repo_root: Path, errors: list[str] | None = None) -> list[SourceFile]:
        """All .md files under `repo_root`. os.walk (not rglob): prunes ignore-dirs WITHOUT
        descending (fast on huge trees), never follows symlinks (no loops), and unreadable
        directories are RECORDED as errors instead of crashing the whole ingest. Vault-shaped
        directories (see is_vault_dir) are pruned the same way and recorded in `errors`:
        skipped loudly, never silently."""
        repo_root = Path(repo_root).resolve()
        vault = self.vault_path.resolve()
        found: list[SourceFile] = []

        def onerr(e: OSError) -> None:
            if errors is not None:
                errors.append(f"{getattr(e, 'filename', repo_root)}: {getattr(e, 'strerror', e)}")

        from .ignore import IgnoreMatcher
        matcher = IgnoreMatcher()

        for dirpath, dirnames, filenames in os.walk(repo_root, onerror=onerr, followlinks=False):
            dp = Path(dirpath)
            if dp == vault or vault in dp.parents:
                dirnames[:] = []
                continue   # never ingest the vault itself (a repo may contain it)
            if is_vault_dir(dp):
                dirnames[:] = []
                if errors is not None:
                    errors.append(f"{dp}: skipped foreign synapse vault "
                                  f"(graph.json + notes/ with synapse.* frontmatter)")
                continue   # a DIFFERENT vault left in the repo — never notes-of-notes (issue #2)
            rel_dir = "" if dp == repo_root else dp.relative_to(repo_root).as_posix()
            matcher.load_dir(dp, rel_dir)   # .gitignore/.synapseignore scoped to this subtree
            dirnames[:] = [
                d for d in dirnames
                if d not in self.ignore_dirs
                and not matcher.ignored(f"{rel_dir}/{d}" if rel_dir else d, is_dir=True)
            ]
            for fn in filenames:
                if not fn.lower().endswith(".md"):   # README.MD is markdown too
                    continue
                rel_f = f"{rel_dir}/{fn}" if rel_dir else fn
                if matcher.ignored(rel_f, is_dir=False):
                    continue
                found.append(SourceFile(repo_name=repo_root.name, repo_root=repo_root, path=dp / fn))
        found.sort(key=lambda f: f.path)
        return found

    def scan_assets(self, repo_root: Path, errors: list[str] | None = None) -> list:
        """Images/PDFs under an assets-ENABLED root (sprint 05, Epic K). Same walk
        discipline as scan_repo: ignore-dirs pruned, ignore files respected, vault
        excluded, never fatal."""
        from .ignore import IgnoreMatcher
        from .models import ASSET_TYPES, SourceAsset
        repo_root = Path(repo_root).resolve()
        vault = self.vault_path.resolve()
        found: list[SourceAsset] = []

        def onerr(e: OSError) -> None:
            if errors is not None:
                errors.append(f"{getattr(e, 'filename', repo_root)}: {getattr(e, 'strerror', e)}")

        matcher = IgnoreMatcher()
        for dirpath, dirnames, filenames in os.walk(repo_root, onerror=onerr, followlinks=False):
            dp = Path(dirpath)
            if dp == vault or vault in dp.parents:
                dirnames[:] = []
                continue
            if is_vault_dir(dp):
                # same guard as scan_repo (#17 by @Nitjsefnie) — a foreign vault's media/
                # must not become asset sidecars either; skipped loudly, never silently
                dirnames[:] = []
                if errors is not None:
                    errors.append(f"{dp}: skipped foreign synapse vault (assets scan)")
                continue
            rel_dir = "" if dp == repo_root else dp.relative_to(repo_root).as_posix()
            matcher.load_dir(dp, rel_dir)
            dirnames[:] = [
                d for d in dirnames
                if d not in self.ignore_dirs
                and not matcher.ignored(f"{rel_dir}/{d}" if rel_dir else d, is_dir=True)
            ]
            for fn in filenames:
                if Path(fn).suffix.lower() not in ASSET_TYPES:
                    continue
                rel_f = f"{rel_dir}/{fn}" if rel_dir else fn
                if matcher.ignored(rel_f, is_dir=False):
                    continue
                found.append(SourceAsset(repo_name=repo_root.name, repo_root=repo_root, path=dp / fn))
        found.sort(key=lambda a: a.path)
        return found

    _AI_SECTION = "## Description (AI)"
    _STAT_RE = re.compile(r"^synapse\.asset_stat: (\S+)$", re.MULTILINE)
    _AI_LINKS_RE = re.compile(r"^synapse\.inferred_links: (.*)$", re.MULTILINE)

    def write_asset(self, asset, errors: list[str] | None = None) -> str:
        """Write/refresh one asset SIDECAR note. Returns 'written'|'unchanged'|'skipped'.
        Fast path: (mtime_ns, size) recorded in frontmatter — an unchanged 4GB library is
        never re-read. A rewrite PRESERVES the AI description section + inferred links
        (they are user artifacts, like distills)."""
        note_path = self.notes_dir / asset.note_id
        try:
            st = asset.path.stat()
        except OSError as e:
            if errors is not None:
                errors.append(f"{asset.path}: {getattr(e, 'strerror', e)}")
            return "skipped"
        stat_token = f"{st.st_mtime_ns}:{st.st_size}"
        existing = ""
        head = ""
        if note_path.is_file():
            existing = note_path.read_text(encoding="utf-8", errors="replace")
            # the WHOLE frontmatter head — a fixed slice truncated long links lines and
            # silently corrupted paid AI artifacts (GBU sprint-05 P2)
            head = existing.split("\n---\n", 1)[0]
            m = self._STAT_RE.search(head)
            # sprint 06 S1: same rule as notes — a sidecar that predates the time fields is
            # not "unchanged"; it needs one rewrite to gain them, or media stays dateless
            # forever and the "latest" lens covers markdown only.
            if m and m.group(1) == stat_token and _FILE_MTIME_RE.search(head):
                # NOTE: an edit that restores mtime AND size (exiftool -P, rsync -t) is
                # invisible to this fast path by design — disclosed in the README
                return "unchanged"
        try:
            raw = asset.path.read_bytes()
        except OSError as e:
            if errors is not None:
                errors.append(f"{asset.path}: {getattr(e, 'strerror', e)}")
            return "skipped"
        digest = self.content_hash(raw)
        # carry over the AI artifacts from the previous sidecar, if any
        ai_section = ""
        idx = existing.find(self._AI_SECTION)
        if idx != -1:
            ai_section = "\n" + existing[idx:].rstrip() + "\n"
        links_m = self._AI_LINKS_RE.search(head)
        links_line = f"synapse.inferred_links: {links_m.group(1)}\n" if links_m else ""
        body = self._asset_body(asset, raw, errors)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            asset_mtime = datetime.fromtimestamp(
                asset.path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except OSError:
            asset_mtime = ""
        # same rule as notes: a sidecar that predates the field is not stamped as new
        _prior_fs = _FIRST_SEEN_RE.search(head)
        asset_first_seen = _prior_fs.group(1) if _prior_fs else ("" if existing else now)
        content = (
            "---\n"
            # fm_quote on the two filename-derived values (repo name, rel path): a legal
            # filename may contain a newline or a YAML indicator, and a bare interpolation
            # forges frontmatter lines. Safe values stay bare and readable; the other
            # fields are self-generated fixed-alphabet tokens (kind/asset_type literals,
            # mtime_ns:size, hex digest, ISO timestamps) that can never be hostile.
            f"synapse.source_repo: {fm_quote(asset.repo_name)}\n"
            f"synapse.source_path: {fm_quote(asset.rel_path)}\n"
            f"synapse.kind: asset\n"
            f"synapse.asset_type: {asset.asset_type}\n"
            f"synapse.asset_stat: {stat_token}\n"
            f"synapse.content_hash: {digest}\n"
            f"synapse.ingested_at: {now}\n"
            # sprint 06 S1 — assets are notes in the graph, so they carry the same two time
            # fields. Without this the "latest" lens would silently cover only markdown, which
            # in a media-heavy brain is a minority of the nodes (website: 148 of 352).
            + (f"synapse.file_mtime: {asset_mtime}\n" if asset_mtime else "")
            + (f"synapse.first_seen: {asset_first_seen}\n" if asset_first_seen != "" else "")
            + f"{links_line}"
            "---\n"
            f"{body}{ai_section}"
        )
        try:
            self.notes_dir.mkdir(parents=True, exist_ok=True)
            tmp = note_path.parent / f"{note_path.name}.{os.getpid()}.tmp"
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, note_path)
        except OSError as e:
            if errors is not None:
                errors.append(f"{note_path}: {getattr(e, 'strerror', e)}")
            return "skipped"
        return "written"

    _PDF_TEXT_CAP = 100_000

    def _asset_body(self, asset, raw: bytes, errors: list[str] | None) -> str:
        title = Path(asset.rel_path).name
        size_kb = len(raw) // 1024
        icon = "📷" if asset.asset_type == "image" else "📄"
        body = f"# {title}\n\n> {icon} {asset.asset_type} · `{asset.rel_path}` · {size_kb} KB\n"
        if asset.asset_type == "pdf":
            text, note = self._pdf_text(asset, errors)
            if text:
                body += f"\n## Extracted text\n\n{text}\n"
            elif note:
                body += f"\n> {note}\n"
        return body

    def _pdf_text(self, asset, errors: list[str] | None) -> tuple[str, str]:
        """(text, honesty-note). No pypdf → metadata-only sidecar with a note that says so;
        a corrupt PDF is recorded, never fatal."""
        try:
            import pypdf
        except ImportError:
            return "", "text not extracted — `pip install pypdf` and re-ingest to make this PDF searchable"
        try:
            reader = pypdf.PdfReader(str(asset.path))
            parts = []
            total = 0
            for page in reader.pages:
                t = page.extract_text() or ""
                parts.append(t)
                total += len(t)
                if total >= self._PDF_TEXT_CAP:
                    parts.append("\n\n> _Truncated — extracted text capped at 100K characters._")
                    break
            return "\n".join(parts).strip(), ""
        except Exception as e:   # pypdf raises a zoo of exceptions on malformed PDFs
            if errors is not None:
                errors.append(f"{asset.path}: PDF text extraction failed ({e})")
            return "", "text extraction failed — the PDF may be scanned or malformed"

    # ── note writing ──────────────────────────────────────────────────────
    @staticmethod
    def content_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _frontmatter(self, src: SourceFile, digest: str, asset_refs: str = "",
                     first_seen: str | None = None) -> str:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            mtime = datetime.fromtimestamp(src.path.stat().st_mtime, timezone.utc)
            file_mtime = mtime.isoformat(timespec="seconds")
        except OSError:
            file_mtime = ""          # unreadable stat is not fatal — the note still indexes
        return (
            "---\n"
            # fm_quote on the filename-derived values (repo name, rel path) and on the
            # asset_refs line (its refs embed media-dir FILENAMES): a legal filename may
            # contain a newline or a YAML indicator, and a bare interpolation forges
            # frontmatter lines. Safe values stay bare and readable; the timestamps and
            # the hex digest are self-generated fixed-alphabet tokens, never hostile.
            f"synapse.source_repo: {fm_quote(src.repo_name)}\n"
            f"synapse.source_path: {fm_quote(src.rel_path)}\n"
            f"synapse.ingested_at: {now}\n"
            + (f"synapse.first_seen: {first_seen if first_seen else now}\n"
               if first_seen != "" else "")
            + (f"synapse.file_mtime: {file_mtime}\n" if file_mtime else "")
            + f"synapse.content_hash: {digest}\n"
            + (f"synapse.asset_refs: {fm_quote(asset_refs)}\n" if asset_refs else "")
            + "---\n"
        )

    # ── the component ADAPTER (founder ruling 2026-08-04) ─────────────────────
    # A publishing platform's markdown references media by ID, not by path:
    #     <Visual id="aios-planning-process" height={660} />
    #     <YouTube id="O0bXo-4I8rY" />
    # The document is the SOURCE OF TRUTH and must stay byte-verbatim — rewriting those
    # markers into local links (as an earlier pass did) forks the source and is exactly
    # what must not happen. Instead this resolves each id to the real local file at INGEST
    # time, by convention, and records the resolution in `synapse.asset_refs`, so the
    # graph gets REAL edges (id → file) while the body is never touched.
    #
    # Convention (matches how the KB stores its media, next to the article):
    #     <Visual id="X"/>  → ../media/<article-stem>/interactive__X.html
    #     <YouTube id="Y"/> → any *.mp4 in that article's media dir (the local cut)
    # Unresolvable ids are simply not recorded — a reference to media this brain does not
    # hold is honest absence, never a fabricated edge.
    # ONE component grammar, shared with the reader and the sync adapter (Codex GBU P1):
    # tag case-insensitive, inline allowed, self-closing optional.
    _VISUAL_RE = re.compile(r'<Visual\s+id="([^"]+)"[^>]*?/?>', re.IGNORECASE)
    _YOUTUBE_RE = re.compile(r'<YouTube\s+id="([^"]+)"[^>]*?/?>', re.IGNORECASE)
    # An id becomes part of a FILENAME, so it must be a plain token — not a path. The old
    # guard only split on "/", which leaves `..\..\x` (a real separator on Windows), NUL,
    # and "|" (the asset_refs field separator, which would forge extra edges) all viable.
    # Allow-list instead of block-list: ids that are not tokens are simply not resolved.
    # (GBU 2026-08-04, P1.)
    # \A…\Z, not ^…$: Python's `$` also matches BEFORE a trailing newline, so "safe\n" passed
    # and a newline in a filename corrupts the one-line asset_refs field. (Codex GBU, P2.)
    _SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")

    def _resolve_asset_refs(self, src: SourceFile, body: str) -> str:
        vis = self._VISUAL_RE.findall(body)
        yt = self._YOUTUBE_RE.findall(body)
        if not vis and not yt:
            return ""
        stem = Path(src.rel_path).stem
        folder = self.companion_media_dir
        media_dir = src.path.parent.parent / folder / stem
        if not media_dir.is_dir():
            return ""
        base = Path(src.rel_path).parent.parent / folder / stem
        refs: list[str] = []
        for vid in vis:
            if not self._SAFE_ID_RE.fullmatch(vid):
                continue      # an id must never climb out of the article's media dir
            f = media_dir / f"{self.interactive_prefix}{vid}.html"
            if f.is_file():
                refs.append((base / f.name).as_posix())
        # A YouTube id is NOT evidence of a particular local file. Linking the first mp4
        # alphabetically claims a relationship that may be false — with two ids and two
        # cuts in one folder it is wrong by construction. Link a local video ONLY when the
        # filename actually carries the id; otherwise the id stays a remote reference and
        # no edge is invented. (Codex GBU P1.)
        for vid in yt:
            if not self._SAFE_ID_RE.fullmatch(vid):
                continue
            for f in sorted(media_dir.glob("*.mp4")):
                if vid.lower() in f.name.lower():
                    refs.append((base / f.name).as_posix())
                    break
        # de-dup, preserve order
        seen, out = set(), []
        for r in refs:
            if r not in seen:
                seen.add(r); out.append(r)
        return " | ".join(out)

    @staticmethod
    def _frontmatter_text(note_path: Path) -> str:
        """The note's frontmatter block, whole — never a fixed byte window.

        A window is a correctness bug, not just a limit: `synapse.asset_refs` is ONE line
        holding every resolved ref, so an article with enough media pushes it past any
        constant. The line then fails to match, the ingest concludes the refs changed, and
        it rewrites the note — on every single run, forever, while never converging.
        (GBU 2026-08-04, P1.) Bounded by the delimiter instead, so cost stays O(frontmatter)
        even when the body is a megabyte."""
        MAX_LINES, MAX_CHARS = 500, 256_000
        lines: list[str] = []
        size = 0
        try:
            with note_path.open(encoding="utf-8", errors="replace") as fh:
                # a UTF-8 BOM is invisible to an editor but would make the first line "\ufeff---"
                if fh.readline().lstrip("\ufeff").rstrip("\n") != "---":
                    return ""
                for line in fh:
                    if line.rstrip("\n") == "---":
                        return "".join(lines)          # only a CLOSED block is frontmatter
                    lines.append(line)
                    size += len(line)
                    # bound BOTH dimensions: 500 lines does not bound one 40MB line
                    if len(lines) > MAX_LINES or size > MAX_CHARS:
                        return ""
        except OSError:
            return ""
        return ""      # EOF with no closing delimiter — not a frontmatter block

    def _existing_refs(self, note_path: Path) -> str:
        if not note_path.is_file():
            return ""
        m = _REFS_RE.search(self._frontmatter_text(note_path))
        # fm_unquote undoes fm_quote at write time, so the freshness comparison below is
        # raw-vs-raw — a quoted (hostile) refs line still converges to "unchanged"
        return fm_unquote(m.group(1).strip()) if m else ""

    def existing_first_seen(self, note_path: Path) -> str | None:
        """The `first_seen` already on disk, so a rewrite never resets it.

        Every re-ingest rewrites the note; without this the field would silently become
        "last ingested" — the exact meaninglessness it exists to avoid."""
        if not note_path.is_file():
            return None
        m = _FIRST_SEEN_RE.search(self._frontmatter_text(note_path))
        return m.group(1) if m else None

    @staticmethod
    def existing_hash(note_path: Path) -> str | None:
        """The `synapse.content_hash` on disk (staticmethod: distill reads it too, to record
        each source's hash at distill time — issue #9)."""
        if not note_path.is_file():
            return None
        m = _HASH_RE.search(IngestService._frontmatter_text(note_path))
        return m.group(1) if m else None

    def write_note(self, src: SourceFile, errors: list[str] | None = None) -> str:
        """Write/refresh one note. Returns 'written' | 'unchanged' | 'skipped'.
        NO failure here may abort the ingest — one bad file (unreadable, un-writable,
        name-too-long) is recorded and the sync moves on."""
        try:
            raw = src.path.read_bytes()
        except OSError as e:
            if errors is not None:
                errors.append(f"{src.path}: {getattr(e, 'strerror', e)}")
            return "skipped"
        digest = self.content_hash(raw)
        note_path = self.notes_dir / src.note_id
        pre_existing = note_path.is_file()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "skipped"   # not honest UTF-8 markdown — report, don't mangle
        refs = self._resolve_asset_refs(src, body)
        # An unchanged BODY is not the whole story: the adapter resolves `<Visual id=…>` /
        # `<YouTube id=…>` against media that can arrive LATER. If a bundle shows up after
        # the article was last ingested, the body hash still matches and the note would
        # keep its stale (empty) asset_refs forever — the media would sit in the vault
        # unlinked. So the refs are part of the freshness check, not just the digest.
        # Sprint 06 S1: a note whose BODY is unchanged but which predates the time fields must
        # still be rewritten once to gain them — otherwise existing notes never acquire a date
        # and the "latest" lens silently covers only whatever happened to change since. Same
        # reasoning as the asset_refs check above: freshness is not only about the body.
        has_times = _FILE_MTIME_RE.search(self._frontmatter_text(note_path)) is not None
        if (self.existing_hash(note_path) == digest
                and self._existing_refs(note_path) == refs and has_times):
            return "unchanged"
        try:
            self.notes_dir.mkdir(parents=True, exist_ok=True)
            # atomic: a concurrent rebuild must never index a half-written note
            # unique temp name: concurrent writers must never share an intermediate
            # (the vault lock serializes entry points; this is belt-and-braces)
            tmp = note_path.parent / f"{note_path.name}.{os.getpid()}.tmp"
            # first_seen means "joined the brain". A note already on disk from before the
            # field existed did NOT join today, so it is left WITHOUT one rather than stamped
            # with a date that would mark the whole corpus as new. Only genuinely new notes
            # get one. `file_mtime` is real either way — it comes from the filesystem.
            prior = self.existing_first_seen(note_path)
            first_seen = prior if prior else ("" if pre_existing else None)
            tmp.write_text(self._frontmatter(src, digest, refs, first_seen) + body,
                           encoding="utf-8")
            os.replace(tmp, note_path)
        except OSError as e:
            if errors is not None:
                errors.append(f"{note_path.name}: {getattr(e, 'strerror', e)}")
            return "skipped"
        return "written"

    # ── ripple maintenance (issue #9) ─────────────────────────────────────
    def refresh_summary_staleness(self, errors: list[str] | None = None) -> list[str]:
        """Flag/unflag `synapse.stale: true` on every distilled summary in the vault, by
        comparing the source hashes recorded at distill time against the notes NOW on disk.
        An edited source stales the summary; so does a pruned one (a hash that can no
        longer be read IS a change). A reverted source clears the flag — this is a
        comparison, not a one-way latch. Summaries without `synapse.source_hashes`
        (distilled before this feature) are skipped: nothing to compare, never guessed.
        A summary whose map line is present but UNDECODABLE (pre-fix format, hand-edited)
        is likewise never guessed stale — but it is surfaced in `errors`, not ignored.
        Returns the note ids whose flag changed. Never fatal — a flag that can't be
        written is recorded, never aborts the sync."""
        if not self.notes_dir.is_dir():
            return []
        changed: list[str] = []
        for note in sorted(self.notes_dir.glob("*.md")):
            fm = self._frontmatter_text(note)
            if not fm or not _SUMMARY_KIND_RE.search(fm):
                continue   # only distill artifacts carry staleness
            m = _SOURCE_HASHES_RE.search(fm)
            if not m or not m.group(1):
                continue   # pre-#9 summary — honest absence
            recorded = parse_source_hashes(m.group(1))
            if recorded is None:
                # A map line that is present but undecodable (pre-fix `id=hash | …`
                # format, or a hand-edited line) is NOT evidence of drift either way —
                # but it must be VISIBLE, never silently abandoned (the old pair-walk
                # `break`ed on unparsable residue and dropped the rest of the line
                # without a trace). Surface it; a re-distill rewrites the map fresh.
                if errors is not None:
                    errors.append(f"{note.name}: unreadable synapse.source_hashes map — "
                                  "re-distill to rewrite it")
                continue
            stale = False
            for note_id, recorded_hash in recorded.items():
                if Path(note_id).name != note_id:
                    continue   # an id is a bare filename, never a path
                if self.existing_hash(self.notes_dir / note_id) != recorded_hash:
                    stale = True
                    break
            if stale != (_STALE_LINE_RE.search(fm) is not None):
                self._write_stale_flag(note, stale, errors)
                changed.append(note.name)
        return changed

    @staticmethod
    def _write_stale_flag(note_path: Path, stale: bool, errors: list[str] | None) -> None:
        """Insert/remove the `synapse.stale: true` frontmatter line (atomic write, like every
        other vault write). Only the flag line is ever touched — the summary body and the
        distill-time hash map are user artifacts and stay byte-identical."""
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
            # clear EVERY flag line first, not one-per-sync (the old count=1): with
            # fm_quote at emission no NEW forged `synapse.stale: true` line can be
            # written, but a legacy/hand-forged summary may carry several, and a scrub
            # that removes one line per pass never converges on them
            stripped = _STALE_LINE_RE.sub("", text)
            if stale:
                # group(0) stops BEFORE the line's "\n" (it backtracks off `\s*$`), so the
                # newline is re-added here — gluing the flag onto the anchor line would
                # corrupt BOTH frontmatter fields
                new = _SUMMARY_KIND_RE.sub(lambda m: m.group(0) + "\nsynapse.stale: true",
                                           stripped, count=1)
                if new == stripped:
                    return   # no anchor line — never invent frontmatter on a foreign note
            else:
                new = stripped
            tmp = note_path.parent / f"{note_path.name}.{os.getpid()}.tmp"
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, note_path)
        except OSError as e:
            if errors is not None:
                errors.append(f"{note_path.name}: could not write the stale flag "
                              f"({getattr(e, 'strerror', e)})")

    # ── the pipeline ──────────────────────────────────────────────────────
    def ingest(self, repos: Iterable[Path], managed_names: set[str] | None = None,
               asset_roots: set[str] | None = None) -> IngestReport:
        """Sync the vault to the enabled roots. With `managed_names` (ALL configured roots,
        enabled AND disabled), ingest also PRUNES: notes from disabled roots, and notes whose
        source file no longer exists in an enabled root. Notes from repos outside the roots
        list (e.g. `✦ summaries`) are never touched. Roots named in `asset_roots` (resolved
        path strings) additionally sync images/PDFs as sidecar notes (sprint 05, Epic K)."""
        report = IngestReport()
        expected: set[str] = set()
        enabled_names = set()
        for repo_root in repos:
            repo_root = Path(repo_root)
            enabled_names.add(repo_root.name)
            rr = RepoReport(repo=repo_root.name)
            report.repos.append(rr)
            if not repo_root.is_dir():
                report.errors.append(f"{repo_root}: not a directory on this machine")
                continue   # honest: 0 files found for a missing path
            if asset_roots and str(repo_root.resolve()) in asset_roots:
                for asset in self.scan_assets(repo_root, errors=report.errors):
                    rr.assets_found += 1
                    outcome = self.write_asset(asset, errors=report.errors)
                    if outcome == "written":
                        rr.assets_written += 1
                    elif outcome == "unchanged":
                        rr.assets_unchanged += 1
                    else:
                        rr.assets_skipped += 1
                    if outcome in ("written", "unchanged") or (
                        outcome == "skipped" and (self.notes_dir / asset.note_id).is_file()
                    ):
                        expected.add(asset.note_id)
            for src in self.scan_repo(repo_root, errors=report.errors):
                rr.files_found += 1
                outcome = self.write_note(src, errors=report.errors)
                if outcome == "written":
                    rr.notes_written += 1
                elif outcome == "unchanged":
                    rr.unchanged += 1
                else:
                    rr.skipped += 1
                if outcome in ("written", "unchanged") or (
                    outcome == "skipped" and (self.notes_dir / src.note_id).is_file()
                ):
                    # a transient read failure ('skipped') must never prune the good note we
                    # already hold — the source file still exists, it just didn't read this pass
                    expected.add(src.note_id)
        if managed_names is not None and self.notes_dir.is_dir():
            for note in self.notes_dir.glob("*.md"):
                repo = note_repo(note)
                if repo is None or repo not in managed_names:
                    continue   # not managed by the roots list (summaries etc.) — keep
                if repo not in enabled_names or note.name not in expected:
                    note.unlink(missing_ok=True)
                    report.pruned += 1
        # ripple maintenance (issue #9): AFTER the sync + prune, so the comparison sees
        # the vault as it now stands — a pruned source stales its summaries too.
        report.stale_summaries = self.refresh_summary_staleness(errors=report.errors)
        return report
