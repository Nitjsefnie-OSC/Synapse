"""The frontmatter-emission guard — the choke point for the issue #9 injection class.

The class (four sites fixed so far): note ids / paths / repo names embed RAW filenames,
a legal filename may contain a newline or any YAML indicator, so any frontmatter line
that interpolates one bare forges real frontmatter. The class is closed at EMISSION
(every hostile-capable value goes through fm_quote / the one-line JSON encoder) and at
ANCHORS (insertion/strip points are line-anchored regexes, never substring searches —
a substring matches INSIDE an fm_quote'd value, which is one physical line that still
CONTAINS the anchor text: distill's `_write_back` spliced `synapse.inferred_links`
into the middle of a quoted `synapse.source_path` that way).

Those are CONVENTIONS, though — nothing in the language stops the next emitter being
written raw. This test is the tripwire: it statically scans every non-test Python
source under backend/ and FAILS on

1. VALUE RULE — any f-string line emitting `synapse.<key>:` with an interpolation that
   is not wrapped in fm_quote, unless (file, key) is in the audited FIXED-ALPHABET
   allowlist below, each entry naming WHY the value can never be hostile;
2. ANCHOR RULE — any use of a "synapse.*" string LITERAL as a substring probe
   (`in`, .replace/.find/.index/.split/.partition/.count), unless the exact line is in
   the audited read-only allowlist below.

A new raw emitter or substring anchor fails this test until its author either routes
the value through fm_quote / a line-anchored regex, or adds an allowlist entry with
the reason it is safe — which forces the audit the whack-a-mole series needed. Known
limits (a tripwire, not a proof): it reads single-line double-quoted f-strings and
literal probes; a multi-line f-string or an interpolated probe variable would evade
it. Test files are excluded — they build fixture notes by hand on purpose.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# an f-string line that emits a synapse key line and interpolates something into it
_EMIT_RE = re.compile(r'f"synapse\.([a-z_]+): [^"]*\{')
# a "synapse.*" LITERAL used as a substring probe (membership or search/replace)
_ANCHOR_RE = re.compile(
    r'["\']synapse\.[^"\']*["\']\s+(?:not\s+)?in\s'
    r'|\b(?:replace|find|index|split|partition|count)\(\s*["\']synapse\.')

# VALUE RULE allowlist — (file, key) sites whose interpolation is a self-generated,
# fixed-alphabet token that can never be filename-derived, each with its reason.
# Anything NOT here and not fm_quote'd on the line is a finding.
_FIXED_ALPHABET_OK = {
    # ingest write_asset / _frontmatter — tokens the backend itself generates:
    # ASSET_TYPES map values, `mtime_ns:size` from os.stat, sha256 hexdigests,
    # ISO-8601 timestamps from datetime.now — none can contain a newline or indicator
    ("modules/ingest/src/services.py", "asset_type"),
    ("modules/ingest/src/services.py", "asset_stat"),
    ("modules/ingest/src/services.py", "content_hash"),
    ("modules/ingest/src/services.py", "ingested_at"),
    ("modules/ingest/src/services.py", "file_mtime"),
    ("modules/ingest/src/services.py", "first_seen"),
    # write_asset carry-over: re-emits the inferred_links line ALREADY in the sidecar,
    # captured by the line-anchored _AI_LINKS_RE — single physical line by construction;
    # the value was fm_quote'd when _write_back wrote it
    ("modules/ingest/src/services.py", "inferred_links"),
    # distill _write_summary — SUMMARY_REPO is a compile-time literal, hash_map comes
    # from encode_source_hashes (one JSON line — the same escaping doctrine as fm_quote),
    # depth is an int, the timestamp is ours
    ("modules/distill/src/service.py", "source_repo"),
    ("modules/distill/src/service.py", "source_hashes"),
    ("modules/distill/src/service.py", "distill_depth"),
    ("modules/distill/src/service.py", "ingested_at"),
    # render — the image filename is re.sub-sanitized to [A-Za-z0-9_.-] plus our hex digest
    ("modules/render/src/service.py", "image"),
}

# ANCHOR RULE allowlist — (file, exact stripped line). All four are READ-ONLY kind
# checks on text already extracted (_frontmatter_text / a head slice / a split part);
# none is an insertion or strip point, so a match inside a quoted value is impossible
# or harmless. Any NEW substring probe is a finding until audited.
_SUBSTRING_READS_OK = {
    ("modules/distill/src/service.py", 'if "synapse.kind: summary" not in fm:'),
    ("modules/distill/src/service.py",
     'if "synapse.kind: asset" in head[:600] and _AI_SECTION not in head:'),
    ("modules/graph/src/api.py", 'if "synapse.kind: summary" not in fm:'),
    ("modules/render/src/service.py",
     'if not raw.startswith("---") or len(parts) < 3 or "synapse.kind: summary" not in parts[1]:'),
}


def _production_sources():
    for p in sorted(BACKEND.rglob("*.py")):
        rel = p.relative_to(BACKEND).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or "__pycache__" in rel:
            continue
        if p.name == "conftest.py":
            continue
        yield rel, p.read_text(encoding="utf-8").splitlines()


def test_every_frontmatter_value_is_quoted_or_fixed_alphabet():
    findings = []
    for rel, lines in _production_sources():
        for i, ln in enumerate(lines, 1):
            m = _EMIT_RE.search(ln)
            if m and "fm_quote(" not in ln and (rel, m.group(1)) not in _FIXED_ALPHABET_OK:
                findings.append(f"{rel}:{i}: raw synapse.{m.group(1)} emitter: {ln.strip()}")
    assert not findings, (
        "frontmatter emission NOT through fm_quote and not in the audited fixed-alphabet "
        "allowlist — quote it or justify it in test_frontmatter_guard.py:\n" + "\n".join(findings))


def test_no_substring_frontmatter_anchors():
    findings = []
    for rel, lines in _production_sources():
        for i, ln in enumerate(lines, 1):
            if _ANCHOR_RE.search(ln) and (rel, ln.strip()) not in _SUBSTRING_READS_OK:
                findings.append(f"{rel}:{i}: substring synapse.* probe: {ln.strip()}")
    assert not findings, (
        "a synapse.* literal used as a SUBSTRING probe — insertion/strip anchors must be "
        "line-anchored regexes (a substring matches INSIDE an fm_quote'd value); read-only "
        "checks must be justified in test_frontmatter_guard.py:\n" + "\n".join(findings))
