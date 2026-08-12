"""The frontmatter-emission guard — the choke point for the issue #9 injection class.

The class (six sites fixed so far): note ids / paths / repo names embed RAW filenames,
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

1. VALUE RULE — any source line that emits a `synapse.<key>:` line with a value
   interpolated into it, in any of the mundane idioms:
   - f-strings with EITHER quote style, capital-F, and triple-quoted openers
     (`f"synapse.k: {v}"`, `f'…'`, `F"…"`, `f\"""…`), including the computed-key
     form `f"synapse.{key}: …"`;
   - `%` formatting (`"synapse.k: %s" % v`) and `.format()` (`"synapse.k: {}".format(v)`);
   - `+` concatenation with the value slot open (`"synapse.k: " + v`);
   - a bare `"synapse.k"` literal handed to a helper as first positional argument
     (`_kv("synapse.sources", raw)` — the helper builds the line out of sight),
   unless the line calls fm_quote( or (file, key) is in the audited FIXED-ALPHABET
   allowlist below, each entry naming WHY the value can never be hostile;
2. ANCHOR RULE — any "synapse.*" string LITERAL used as a substring probe:
   - membership (`"…synapse.…" in x` / `not in`),
   - str search/replace methods (.replace/.find/.rfind/.index/.rindex/.split/
     .partition/.rpartition/.count),
   - an re-family call (re.search/match/fullmatch/sub/subn/split/findall/finditer/
     compile — via ANY module alias or a compiled Pattern's methods) whose pattern
     literal does not line-anchor the key with `^` or `\\A` directly before it,
   unless the exact line is in the audited read-only allowlist below.

A new raw emitter or substring anchor fails this test until its author either routes
the value through fm_quote / a line-anchored regex, or adds an allowlist entry with
the reason it is safe — which forces the audit the whack-a-mole series needed. Known
limits (a tripwire, not a proof): the scan is per-line, so a call or f-string whose
pattern/interpolation continues on the NEXT line evades it; a probe or key held in a
VARIABLE (`_ANCHOR = "synapse.x:"; _ANCHOR in c`) evades it; a pattern built by
re.escape or a format template held in a variable evades it. Test files are excluded —
they build fixture notes by hand on purpose.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# ── VALUE RULE patterns ──────────────────────────────────────────────────────
# an f-string (f or F, any quote style incl. triple openers) that starts a synapse
# key line and interpolates; the lookbehind keeps `if "…"` / identifiers from
# supplying the "f"
_FSTRING_EMIT_RE = re.compile(r'(?<![A-Za-z])[fF](?:"""|\'\'\'|["\'])synapse\..*\{')
# a "synapse.<key>:" literal filled by % formatting or .format(
_FORMAT_EMIT_RE = re.compile(r'["\']synapse\.[a-z_]+:\s?[^"\']*["\']\s*(?:%|\.format\()')
# a "synapse.<key>: " literal (value slot open) glued to something with +
_CONCAT_EMIT_RE = re.compile(
    r'["\']synapse\.[a-z_]+:\s*["\']\s*\+|\+\s*["\']synapse\.[a-z_]+:\s*["\']')
# a bare "synapse.<key>" literal as first positional arg of a call — the helper
# builds the line out of sight (_kv("synapse.sources", raw))
_HELPER_EMIT_RE = re.compile(r'\(\s*["\']synapse\.[a-z_]+:?["\']\s*,')

# ── ANCHOR RULE patterns ─────────────────────────────────────────────────────
# a "synapse.*" LITERAL used as a membership probe
_IN_PROBE_RE = re.compile(r'["\'][^"\']*synapse\.[^"\']*["\']\s+(?:not\s+)?in\s')
# …or fed to a str search/replace method
_STR_METHOD_PROBE_RE = re.compile(
    r'\.(?:replace|find|rfind|index|rindex|split|partition|rpartition|count)'
    r'\(\s*["\'][^"\']*synapse\\?\.')
# …or used as the pattern of an re-family call. Any attribute receiver is matched
# (re.X, any alias like _re/rx, a compiled Pattern's .search/.sub/…, or an inline
# re.compile(…).sub(…) chain); the pattern may be raw or escaped (synapse./synapse\.)
_RE_CALL_RE = re.compile(
    r'\.(?:search|match|fullmatch|sub|subn|split|findall|finditer|compile)'
    r'\(\s*[a-z]{0,2}["\']([^"\']*synapse\\?\.)')
# the ONLY acceptable synapse pattern: the key line-anchored by ^ or \A
_RE_LINE_ANCHORED_RE = re.compile(r'(?:\^|\\A)synapse')

_KEY_RE = re.compile(r'synapse\.([a-z_]+):')

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
            hit = (_FSTRING_EMIT_RE.search(ln) or _FORMAT_EMIT_RE.search(ln)
                   or _CONCAT_EMIT_RE.search(ln) or _HELPER_EMIT_RE.search(ln))
            if not hit or "fm_quote(" in ln:
                continue
            m = _KEY_RE.search(ln)
            if m and (rel, m.group(1)) in _FIXED_ALPHABET_OK:
                continue
            key = m.group(1) if m else "<computed>"
            findings.append(f"{rel}:{i}: raw synapse.{key} emitter: {ln.strip()}")
    assert not findings, (
        "frontmatter emission NOT through fm_quote and not in the audited fixed-alphabet "
        "allowlist — quote it or justify it in test_frontmatter_guard.py:\n" + "\n".join(findings))


def test_no_substring_frontmatter_anchors():
    findings = []
    for rel, lines in _production_sources():
        for i, ln in enumerate(lines, 1):
            if (rel, ln.strip()) in _SUBSTRING_READS_OK:
                continue
            m = _RE_CALL_RE.search(ln)
            re_probe = m and not _RE_LINE_ANCHORED_RE.search(m.group(1))
            if _IN_PROBE_RE.search(ln) or _STR_METHOD_PROBE_RE.search(ln) or re_probe:
                findings.append(f"{rel}:{i}: substring synapse.* probe: {ln.strip()}")
    assert not findings, (
        "a synapse.* literal used as a SUBSTRING probe — insertion/strip anchors must be "
        "line-anchored regexes (a substring matches INSIDE an fm_quote'd value); read-only "
        "checks must be justified in test_frontmatter_guard.py:\n" + "\n".join(findings))
