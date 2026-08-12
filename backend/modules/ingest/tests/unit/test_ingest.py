"""Epic A unit tests — ingest over the committed fixture repos (backend/tests/fixtures/).
Every case asserts real behavior; zero network access anywhere."""

from pathlib import Path

import pytest

from modules.ingest.src.services import (
    IngestService, encode_source_hashes, fm_quote, fm_unquote, parse_source_hashes)

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures"
REPO_A = FIXTURES / "repo_a"
REPO_B = FIXTURES / "repo_b"
IGNORE = frozenset({"node_modules", ".venv", ".git", "__pycache__"})


@pytest.fixture
def service(tmp_path: Path) -> IngestService:
    return IngestService(vault_path=tmp_path / "vault", ignore_dirs=IGNORE)


class TestScan:
    def test_finds_all_md_and_honors_ignore_list(self, service):
        files = service.scan_repo(REPO_A)
        rels = {f.rel_path for f in files}
        assert rels == {"README.md", "docs/alpha.md", "hebrew.md"}  # junk under node_modules excluded

    def test_note_ids_are_deterministic_and_readable(self, service):
        ids = {f.note_id for f in service.scan_repo(REPO_A)}
        assert "repo_a__docs__alpha.md" in ids
        assert "repo_a__README.md" in ids


class TestIngest:
    def test_counts_are_honest(self, service):
        report = service.ingest([REPO_A, REPO_B])
        assert report.files_found == 4
        assert report.notes_written == 4
        assert report.unchanged == 0 and report.skipped == 0
        per_repo = {r.repo: r for r in report.repos}
        assert per_repo["repo_a"].files_found == 3
        assert per_repo["repo_b"].files_found == 1

    def test_reingest_is_idempotent(self, service):
        service.ingest([REPO_A])
        second = service.ingest([REPO_A])
        assert second.notes_written == 0
        assert second.unchanged == 3

    def test_frontmatter_shape(self, service):
        service.ingest([REPO_A])
        note = (service.notes_dir / "repo_a__docs__alpha.md").read_text(encoding="utf-8")
        head = note.split("---")[1]
        for f in ("synapse.source_repo: repo_a", "synapse.source_path: docs/alpha.md",
                  "synapse.ingested_at: ", "synapse.content_hash: "):
            assert f in head, f"missing frontmatter field: {f}"

    def test_hebrew_content_survives_verbatim(self, service):
        service.ingest([REPO_A])
        original = (REPO_A / "hebrew.md").read_text(encoding="utf-8")
        note = (service.notes_dir / "repo_a__hebrew.md").read_text(encoding="utf-8")
        assert note.endswith(original)  # byte-faithful body below our frontmatter

    def test_missing_repo_reports_zero_not_crash(self, service):
        report = service.ingest([Path("/nowhere/ghost-repo")])
        assert report.files_found == 0 and report.notes_written == 0

    def test_never_ingests_its_own_vault(self, tmp_path):
        """A source repo that CONTAINS the vault must not self-ingest (notes-of-notes loop)."""
        repo = tmp_path / "repo"
        (repo / "sub").mkdir(parents=True)
        (repo / "real.md").write_text("# Real\n", encoding="utf-8")
        service = IngestService(vault_path=repo / "data" / "vault", ignore_dirs=IGNORE)
        first = service.ingest([repo])
        assert first.files_found == 1            # the vault dir itself is excluded from scanning
        second = service.ingest([repo])          # vault now has notes inside the repo
        assert second.files_found == 1 and second.unchanged == 1

    @staticmethod
    def _foreign_vault(repo: Path) -> Path:
        """A DIFFERENT synapse vault left inside a source repo (an old data/vault)."""
        foreign = repo / "data" / "vault"
        (foreign / "notes").mkdir(parents=True)
        (foreign / "graph.json").write_text('{"nodes": [], "edges": []}', encoding="utf-8")
        (foreign / "notes" / "old__note.md").write_text(
            "---\nsynapse.source_repo: old\nsynapse.content_hash: " + "0" * 64 + "\n---\n# Old\n",
            encoding="utf-8")
        return foreign

    def test_skips_foreign_vault_and_reports_it(self, service, tmp_path):
        """Issue #2: a repo holding a DIFFERENT synapse vault (graph.json + notes/ with
        synapse.* frontmatter) must SKIP it — recorded in the ledger, never silent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "real.md").write_text("# Real\n", encoding="utf-8")
        self._foreign_vault(repo)
        report = service.ingest([repo])
        assert report.files_found == 1 and report.notes_written == 1   # real.md only
        assert not list(service.notes_dir.glob("*old__note*"))         # no notes-of-notes
        assert any("skipped foreign synapse vault" in e for e in report.errors)
        second = service.ingest([repo])                                # stays skipped, idempotent
        assert second.files_found == 1 and second.unchanged == 1

    def test_configured_vault_excluded_even_when_vault_shaped(self, tmp_path):
        """The ACTIVE vault is excluded by IDENTITY, not by shape: even with both foreign
        markers present it is skipped silently (not misreported as a foreign vault)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "real.md").write_text("# Real\n", encoding="utf-8")
        service = IngestService(vault_path=repo / "data" / "vault", ignore_dirs=IGNORE)
        service.ingest([repo])                        # vault now holds synapse-frontmatter notes
        (repo / "data" / "vault" / "graph.json").write_text("{}", encoding="utf-8")
        report = service.ingest([repo])
        assert report.files_found == 1 and report.unchanged == 1
        assert not report.errors

    def test_graph_json_alone_is_not_a_vault(self, service, tmp_path):
        """ONE marker only — graph.json next to notes/ WITHOUT synapse frontmatter: real
        content, ingested normally."""
        repo = tmp_path / "repo"
        (repo / "data" / "notes").mkdir(parents=True)
        (repo / "data" / "graph.json").write_text("{}", encoding="utf-8")
        (repo / "data" / "notes" / "plain.md").write_text("# Plain\n", encoding="utf-8")
        report = service.ingest([repo])
        assert report.files_found == 1 and report.notes_written == 1 and not report.errors

    def test_synapse_frontmatter_alone_is_not_a_vault(self, service, tmp_path):
        """ONE marker only — notes/ WITH synapse.* frontmatter but no graph.json: ingested."""
        repo = tmp_path / "repo"
        (repo / "notes").mkdir(parents=True)
        (repo / "notes" / "quoted.md").write_text(
            "---\nsynapse.source_repo: elsewhere\n---\n# Quoted\n", encoding="utf-8")
        report = service.ingest([repo])
        assert report.files_found == 1 and report.notes_written == 1 and not report.errors

    def test_transient_read_failure_never_prunes_the_good_note(self, service, tmp_path):
        """GBU P2: a source file that fails to READ this pass ('skipped') still exists —
        the sync prune must keep the good note we already hold."""
        import os
        if os.geteuid() == 0:
            pytest.skip("permission bits don't bind as root")
        repo = tmp_path / "flaky_repo"; repo.mkdir()
        f = repo / "keep.md"; f.write_text("# Keep\n", encoding="utf-8")
        service.ingest([repo], managed_names={"flaky_repo"})
        note = service.notes_dir / "flaky_repo__keep.md"
        assert note.is_file()
        f.chmod(0)                                   # unreadable THIS pass only
        try:
            report = service.ingest([repo], managed_names={"flaky_repo"})
        finally:
            f.chmod(0o644)
        assert report.skipped == 1 and report.pruned == 0
        assert note.is_file()                        # survived the bad pass

    def test_filename_too_long_is_capped_never_aborts(self, service, tmp_path):
        """Founder repro: a deep Hebrew-named file flattens past ext4's 255-byte filename
        limit — the id must hash-cap deterministically and the whole ingest must survive."""
        repo = tmp_path / "deep"
        seg = "קורות חיים — דנה לוי מטפלת סיעודית למבוגרים מגורים בבית המטופל חיפה"
        nested = repo / seg / seg      # each segment is legal; the FLATTENED id is not
        nested.mkdir(parents=True)
        (nested / "כרטיס עובד ומועמד לתפקיד — גרסה סופית להדפסה.md").write_text("# עמוק\n", encoding="utf-8")
        (repo / "ok.md").write_text("# OK\n", encoding="utf-8")
        report = service.ingest([repo], managed_names={"deep"})
        assert report.notes_written == 2 and not report.errors     # BOTH written, nothing fatal
        long_note = [p for p in service.notes_dir.glob("*.md") if "…" in p.name]
        assert len(long_note) == 1 and len(long_note[0].name.encode()) <= 200
        second = service.ingest([repo], managed_names={"deep"})    # capped id is deterministic
        assert second.unchanged == 2 and second.pruned == 0

    def test_uppercase_md_is_markdown_too(self, service, tmp_path):
        repo = tmp_path / "shouty"; repo.mkdir()
        (repo / "README.MD").write_text("# Shout\n", encoding="utf-8")
        assert service.ingest([repo]).files_found == 1

    def test_changed_source_is_rewritten(self, service, tmp_path):
        repo = tmp_path / "live_repo"
        repo.mkdir()
        f = repo / "note.md"
        f.write_text("# v1\n", encoding="utf-8")
        assert service.ingest([repo]).notes_written == 1
        f.write_text("# v2 changed\n", encoding="utf-8")
        report = service.ingest([repo])
        assert report.notes_written == 1 and report.unchanged == 0
        assert "# v2 changed" in (service.notes_dir / "live_repo__note.md").read_text(encoding="utf-8")


class TestSourceHashesCodec:
    """Issue #9, second round — the staleness map is a SINGLE-LINE JSON OBJECT, so no note
    id can break its framing: a legal filename may contain the old ` | ` separator, the
    pair-anchor `=<64 hex> | `, both quote kinds, even a NEWLINE, and JSON string encoding
    escapes all of it by construction. Property-style: every adversarial id below must
    round-trip EXACTLY — the failure class is closed, not one instance at a time."""

    HEX = "0123456789abcdef" * 4
    ADVERSARIAL_IDS = [
        "plain.md",                            # control
        "a | b.md",                            # the old separator, verbatim
        f"a={HEX} | b.md",                     # the pair-anchor pattern, verbatim
        "x\ny.md",                             # a newline is a legal filename byte
        'quo"te\'s.md',                        # both quote kinds
        "key=value.md",                        # a bare '='
        " spaced .md",                         # leading/inner spaces
    ]

    def test_every_adversarial_id_roundtrips_exactly(self):
        for note_id in self.ADVERSARIAL_IDS:
            line = encode_source_hashes({note_id: self.HEX})
            assert "\n" not in line and "\r" not in line   # always ONE frontmatter line
            assert parse_source_hashes(line) == {note_id: self.HEX}

    def test_several_adversarial_ids_in_one_map(self):
        recorded = dict.fromkeys(self.ADVERSARIAL_IDS, self.HEX)
        assert parse_source_hashes(encode_source_hashes(recorded)) == recorded

    def test_unparsable_map_is_none_never_a_guess(self):
        # the pre-fix `id=hash | …` format and hand-edited garbage both decode to None —
        # the caller treats them as "no recorded hashes" and SURFACES the line, never
        # silently walks off it
        assert parse_source_hashes(f"plain.md={self.HEX}") is None
        assert parse_source_hashes("{not json") is None
        assert parse_source_hashes('["a", "list", "is", "not", "a", "map"]') is None

    def test_unparsable_map_on_disk_is_surfaced_never_silently_skipped(self, service):
        """The old stale-walk `break`ed on unparsable residue, abandoning the rest of the
        line without a trace. Now a summary whose map line cannot be decoded is reported
        in the ingest errors (VISIBLE) and its staleness is left UNTOUCHED — an unreadable
        map is not evidence of drift either way; a re-distill rewrites it."""
        service.notes_dir.mkdir(parents=True, exist_ok=True)
        summary = service.notes_dir / "S — hand-edited.md"
        summary.write_text("---\nsynapse.kind: summary\n"
                           f"synapse.source_hashes: plain.md={self.HEX}\n"   # pre-fix format
                           "---\nbody\n", encoding="utf-8")
        report = service.ingest([REPO_A])
        assert any("source_hashes" in e and summary.name in e for e in report.errors)
        assert "synapse.stale" not in summary.read_text(encoding="utf-8")


class TestFrontmatterEscaping:
    """Issue #9, third round — note ids, repo names and paths all embed RAW filenames, and
    a legal filename may contain a newline: interpolated bare into a frontmatter line it
    FORGES real frontmatter lines (`x\\nsynapse.stale: true\\ny.md` marks an unchanged
    source stale; a shadow `synapse.source_hashes` line wins the first-match regex). The
    whole class is closed at EMISSION: every hostile-capable scalar goes through fm_quote
    (bare when already a safe YAML plain scalar, else a JSON double-quoted string), and
    every reader decodes with fm_unquote."""

    # newline, `: `, ` #`, leading `-`, both quote kinds, backslash, a name that IS a
    # YAML key line, unicode, unicode line/paragraph separators, YAML-re-typed words —
    # plus the plain control
    ADVERSARIAL_SCALARS = [
        "plain.md",
        "x\nsynapse.stale: true\ny.md",
        'z\nsynapse.source_hashes: {"nonexistent.md": "' + "a" * 64 + '"}\nz.md',
        "colon: value.md",
        "hash # comment.md",
        "- leading dash.md",
        'quo"te\'s.md',
        "back\\slash.md",
        "synapse.stale: true.md",
        "קורות חיים.md",
        "line\u2028sep.md",
        "para\u2029sep.md",
        "trailing colon:.md",
        " leading space.md",
        "true", "null", "123", "2024-01-01", "",
    ]

    def test_fm_quote_roundtrips_any_scalar_through_real_yaml(self):
        """Property: for ANY string, fm_quote output is ONE physical line, fm_unquote
        inverts it exactly, AND a real YAML parser reads the line back as the exact
        original string — never a re-typed bool/date/int, never a forged mapping."""
        import yaml
        for v in self.ADVERSARIAL_SCALARS:
            enc = fm_quote(v)
            assert "\n" not in enc and "\r" not in enc, v
            assert fm_unquote(enc) == v, v
            assert yaml.safe_load(f"synapse.probe: {enc}")["synapse.probe"] == v, v

    def test_safe_scalars_stay_bare_and_readable(self):
        """The common case must not change shape: frontmatter is for humans. Repo names,
        paths, ids and the ✦ summaries literal all stay unquoted."""
        for v in ("repo_a", "docs/alpha.md", "✦ summaries", "S — probe.md",
                  "repo__Meeting | notes.md", "image", "2026-08-04T10:00:00+00:00"):
            assert fm_quote(v) == v, v

    def test_hostile_repo_name_decodes_for_the_prune_key(self, service, tmp_path):
        """A repo dir named with a newline forges frontmatter exactly like a hostile
        filename — and a repo name read back TRUNCATED (`ev` instead of the real name)
        prunes the note out from under an unchanged, managed repo. note_repo must decode
        the quoted value."""
        import yaml
        name = "ev\nsynapse.kind: summary\nil"
        repo = tmp_path / name
        repo.mkdir()
        (repo / "a.md").write_text("# A\n", encoding="utf-8")
        service.ingest([repo], managed_names={name})
        note = service.notes_dir / f"{name}__a.md"
        assert note.is_file()                       # NOT pruned — decoded name matched
        vals = yaml.safe_load(IngestService._frontmatter_text(note))
        assert set(vals) == {"synapse.source_repo", "synapse.source_path",
                             "synapse.ingested_at", "synapse.first_seen",
                             "synapse.file_mtime", "synapse.content_hash"}
        assert vals["synapse.source_repo"] == name  # exact, newline and all
        again = service.ingest([repo], managed_names={name})
        assert note.is_file() and again.pruned == 0  # idempotent, still managed

    def test_stale_scrub_clears_every_forged_flag_line(self, service):
        """The scrub removed only ONE `synapse.stale: true` line per sync (count=1) — a
        multi-line injection (delta N1 forged several) could never converge. Clearing
        must remove them ALL in one pass. (With fm_quote at emission no NEW forged line
        can be written; this is the legacy/hand-forged mop-up.)"""
        service.notes_dir.mkdir(parents=True, exist_ok=True)
        summary = service.notes_dir / "S — forged.md"
        summary.write_text("---\nsynapse.kind: summary\n"
                           + "synapse.stale: true\n" * 4
                           + "synapse.source_hashes: {}\n---\nbody\n", encoding="utf-8")
        service.refresh_summary_staleness(errors=[])
        assert "synapse.stale" not in IngestService._frontmatter_text(summary)
