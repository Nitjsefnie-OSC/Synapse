"""Epic D unit tests — ALL on MockSummarizer (zero network, zero paid calls)."""

import re
from pathlib import Path

import pytest

from modules.distill.src.providers import GroundedSummary, MockSummarizer, Summarizer
from modules.distill.src.service import ConfirmationRequired, DistillService, GroundingError
from modules.ingest.src.services import IngestService, _STALE_LINE_RE

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures"
IGNORE = frozenset({"node_modules", ".venv", ".git", "__pycache__"})
ALPHA = "repo_a__docs__alpha.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    IngestService(v, IGNORE).ingest([FIXTURES / "repo_a", FIXTURES / "repo_b"])
    return v


@pytest.fixture
def service(vault: Path) -> DistillService:
    return DistillService(vault, MockSummarizer(), confirm_threshold=20000)


class TestCollect:
    def test_node_scope_is_just_the_node(self, service):
        notes, truncated = service.collect(ALPHA, "node", 2)
        assert [n.note_id for n in notes] == [ALPHA] and not truncated

    def test_subtree_bfs_follows_real_edges_both_directions(self, service):
        notes, _ = service.collect(ALPHA, "subtree", 1)
        ids = {n.note_id for n in notes}
        assert ALPHA in ids
        assert "repo_b__beta.md" in ids            # [[Beta]] out-edge, cross-repo
        assert "repo_a__README.md" in ids          # relative back-edge (README → alpha)

    def test_unknown_node_raises(self, service):
        with pytest.raises(KeyError):
            service.collect("ghost.md", "node", 1)


class TestDistill:
    def test_summary_note_written_with_frontmatter_and_wikilinks(self, service, vault):
        out = service.distill(ALPHA, scope="subtree", depth=1)
        note = (vault / "notes" / out["summary_note_id"]).read_text(encoding="utf-8")
        assert "synapse.kind: summary" in note
        assert "synapse.source_repo: ✦ summaries" in note
        assert f"[[{ALPHA}]]" in note               # sources wikilinked → graph edges on rebuild
        assert out["citations"] >= 1 and out["sources"]

    def test_summary_joins_the_graph_on_rebuild(self, service, vault):
        out = service.distill(ALPHA, scope="node")
        from modules.graph.src.services import GraphService
        g = GraphService(vault).build()
        assert out["summary_note_id"] in g.nodes
        assert any(e.src == out["summary_note_id"] and e.dst == ALPHA and e.type == "wikilink"
                   for e in g.edges)

    def test_cost_guard_requires_confirmation(self, vault):
        svc = DistillService(vault, MockSummarizer(), confirm_threshold=1)   # everything is "expensive"
        with pytest.raises(ConfirmationRequired) as e:
            svc.distill(ALPHA, scope="node")
        assert e.value.tokens_est > 1
        assert svc.distill(ALPHA, scope="node", confirm=True)["summary_note_id"]

    def test_zero_citation_summary_is_rejected(self, vault):
        class Ungrounded(Summarizer):
            def summarize(self, subject, notes, scope):
                return GroundedSummary(markdown="A confident summary with no receipts.", model="bad")
        with pytest.raises(GroundingError):
            DistillService(vault, Ungrounded()).distill(ALPHA)

    def test_hallucinated_citation_is_rejected(self, vault):
        """GBU P2: grounded means grounded — a citation of a note that is NOT in the source
        set is a hallucination, not a receipt."""
        class Hallucinator(Summarizer):
            def summarize(self, subject, notes, scope):
                return GroundedSummary(markdown="Confident claim (vault: made-up-note.md).", model="bad")
        with pytest.raises(GroundingError, match="NOT in the source set"):
            DistillService(vault, Hallucinator()).distill(ALPHA)

    def test_truncation_starves_references_not_definitions(self, tmp_path):
        """When the cap cuts, OUT-links (what the root points to) must survive over in-links
        (what points at it) — founder repro: ARIA's role contract was truncated out while
        alphabetically-earlier echo-adapters shipped."""
        repo = tmp_path / "ringrepo"; repo.mkdir()
        (repo / "root.md").write_text("# Root\n\npoints to [[zdef]]\n", encoding="utf-8")
        (repo / "zdef.md").write_text("# zdef\n\nthe definition body\n", encoding="utf-8")
        (repo / "a_ref.md").write_text("# a_ref\n\nreferences [[Root]]\n", encoding="utf-8")
        v = tmp_path / "ringvault"
        IngestService(v, IGNORE).ingest([repo])
        svc = DistillService(v, MockSummarizer())
        full, _ = svc.collect("ringrepo__root.md", "subtree", 1)
        assert {n.note_id for n in full} == {"ringrepo__root.md", "ringrepo__zdef.md", "ringrepo__a_ref.md"}
        zdef = next(n for n in full if n.note_id == "ringrepo__zdef.md")
        svc.hard_cap_chars = len(full[0].body) + len(zdef.body) + 1   # room for root + ONE
        cut, truncated = svc.collect("ringrepo__root.md", "subtree", 1)
        assert truncated
        # a_ref is alphabetically first (the old order shipped it) — the OUT-link must survive
        assert [n.note_id for n in cut] == ["ringrepo__root.md", "ringrepo__zdef.md"]

    def test_dual_linked_neighbor_is_a_definition_deterministically(self, tmp_path):
        """A neighbor linked BOTH ways (root→it and it→root) must classify as an OUT-link
        (definition) regardless of edge-set iteration order — the single-pass classifier
        made its truncation survival depend on the process hash seed."""
        repo = tmp_path / "dualrepo"; repo.mkdir()
        (repo / "root.md").write_text("# Root\n\npoints to [[zdual]]\n", encoding="utf-8")
        (repo / "zdual.md").write_text("# zdual\n\nthe definition, links back to [[Root]]\n", encoding="utf-8")
        (repo / "a_ref.md").write_text("# a_ref\n\nreferences [[Root]]\n", encoding="utf-8")
        v = tmp_path / "dualvault"
        IngestService(v, IGNORE).ingest([repo])
        svc = DistillService(vault_path=v, summarizer=MockSummarizer())
        full, _ = svc.collect("dualrepo__root.md", "subtree", 1)
        root = next(n for n in full if n.note_id == "dualrepo__root.md")
        zdual = next(n for n in full if n.note_id == "dualrepo__zdual.md")
        svc.hard_cap_chars = len(root.body) + len(zdual.body) + 1   # room for root + ONE more
        for _ in range(3):   # stable across repeated builds within (and thanks to the two-pass
            cut, truncated = svc.collect("dualrepo__root.md", "subtree", 1)   # fix, across) runs
            assert truncated
            assert [n.note_id for n in cut] == ["dualrepo__root.md", "dualrepo__zdual.md"]

    def test_citation_audit_tolerates_real_model_formats(self):
        """Live sonnet-5 comma-joins ids in one parenthetical, and note ids may contain
        parentheses (which truncate the regex match) — neither is a hallucination."""
        from modules.distill.src.service import citation_audit
        known = {"repo_a__docs__alpha.md", "s — aria (ui·ux) — client thin adapter.md"}
        md = ("Claim one (vault: repo_a__docs__alpha.md, vault: repo_a__docs__alpha.md). "
              "Claim two (vault: S — ARIA (UI·UX). "          # id truncated by its own paren
              "Claim three (vault: repo_a__docs__alpha).")     # id without the .md suffix
        count, unknown = citation_audit(md, known)
        assert count == 4 and unknown == []
        _, bad = citation_audit("Sure thing (vault: made-up-note.md).", known)
        assert bad == ["made-up-note.md"]

    def test_citation_of_comma_containing_id_is_not_split(self):
        """Second-opinion P1: a note id WITH a comma, cited verbatim (as the prompt instructs),
        must never be split into fragments and falsely rejected — that burns a paid call
        deterministically on every retry."""
        from modules.distill.src.service import citation_audit
        known = {"repo__plan, v2.md", "repo__קורות חיים — דנה, מטפלת.md"}
        count, unknown = citation_audit(
            "Claim (vault: repo__plan, v2.md). Also (vault: repo__קורות חיים — דנה, מטפלת.md).",
            known)
        assert count == 2 and unknown == []

    def test_summary_filename_is_byte_capped(self, tmp_path):
        """Second-opinion P2: an emoji/CJK-heavy subject must not blow ext4's 255-byte
        filename limit AFTER the model call — same cap doctrine as ingest note ids."""
        repo = tmp_path / "emojirepo"; repo.mkdir()
        (repo / "wow.md").write_text("# " + "🧠" * 80 + "\n\nbody\n", encoding="utf-8")
        v = tmp_path / "capvault"
        IngestService(v, IGNORE).ingest([repo])
        out = DistillService(v, MockSummarizer()).distill("emojirepo__wow.md", scope="node")
        sid = out["summary_note_id"]
        assert len(sid.encode("utf-8")) <= 200
        assert (v / "notes" / sid).is_file()   # written, not OSError'd after the spend

    def test_truncation_is_disclosed(self, vault):
        svc = DistillService(vault, MockSummarizer())
        svc.hard_cap_chars = 150                     # tiny safety cap → the subtree must be cut
        notes, truncated = svc.collect(ALPHA, "subtree", 1)
        assert truncated
        assert len(notes) >= 1                       # kept what fits…
        out = svc.distill(ALPHA, scope="subtree", depth=1, confirm=True)
        assert out["truncated"] is True              # …and the result SAYS it was cut
        note = (vault / "notes" / out["summary_note_id"]).read_text(encoding="utf-8")
        assert "Truncated" in note


class TestStaleness:
    """Issue #9 — a distilled `S —` summary goes quietly stale when its cited sources
    change. Distill records each source's content hash; ingest compares and flags."""

    @staticmethod
    def _fm(note_path: Path) -> str:
        text = note_path.read_text(encoding="utf-8")
        return text.split("---\n", 2)[1] if text.startswith("---\n") else ""

    @pytest.fixture
    def editable_vault(self, tmp_path):
        """repo_a copied into tmp — the shared fixture must never be edited in place.
        The copy keeps the name `repo_a` so note ids match the shared constants."""
        import shutil
        repo = tmp_path / "repo_a"
        shutil.copytree(FIXTURES / "repo_a", repo, ignore=shutil.ignore_patterns("node_modules"))
        v = tmp_path / "vault"
        ing = IngestService(v, IGNORE)
        ing.ingest([repo])
        return v, repo, ing

    def test_distill_records_source_hashes_and_is_not_born_stale(self, editable_vault):
        v, _, _ = editable_vault
        out = DistillService(v, MockSummarizer()).distill(ALPHA, scope="subtree", depth=1)
        fm = self._fm(v / "notes" / out["summary_note_id"])
        assert "synapse.source_hashes:" in fm          # the distill-time hash map
        assert "synapse.stale" not in fm               # a fresh distill is not born stale

    def test_cited_source_edit_marks_stale_and_redistill_clears(self, editable_vault):
        """The issue's acceptance, end to end on the mock: edit a cited source →
        re-ingest → the summary carries the stale flag; re-distill → the flag clears."""
        v, repo, ing = editable_vault
        svc = DistillService(v, MockSummarizer())
        out = svc.distill(ALPHA, scope="subtree", depth=1)
        summary = v / "notes" / out["summary_note_id"]
        # a summary of an UNRELATED note — the invariant is "a summary whose CITED sources
        # changed is stale", never "any edit stales every summary"
        other = svc.distill("repo_a__hebrew.md", scope="node")
        other_note = v / "notes" / other["summary_note_id"]

        alpha = repo / "docs" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "\nthe ripple edit\n",
                         encoding="utf-8")
        ing.ingest([repo])
        assert "synapse.stale: true" in self._fm(summary)     # ← the acceptance assertion
        assert "synapse.stale" not in self._fm(other_note)    # untouched summaries stay fresh

        svc.distill(ALPHA, scope="subtree", depth=1)          # re-distill overwrites…
        assert "synapse.stale" not in self._fm(summary)       # …and the flag clears

    def test_reverted_source_clears_the_flag_on_ingest(self, editable_vault):
        """The flag is a honest COMPARISON, not a one-way latch: a source reverted to its
        distilled content (same hash) makes the summary fresh again on the next ingest."""
        v, repo, ing = editable_vault
        svc = DistillService(v, MockSummarizer())
        out = svc.distill(ALPHA, scope="subtree", depth=1)
        summary = v / "notes" / out["summary_note_id"]
        alpha = repo / "docs" / "alpha.md"
        original = alpha.read_bytes()
        alpha.write_bytes(original + b"\nthe ripple edit\n")
        ing.ingest([repo])
        assert "synapse.stale: true" in self._fm(summary)
        alpha.write_bytes(original)
        ing.ingest([repo])
        assert "synapse.stale" not in self._fm(summary)

    def test_summary_without_recorded_hashes_is_left_alone(self, editable_vault):
        """Summaries distilled before this feature carry no `synapse.source_hashes` —
        nothing to compare against, so they are honestly UNMARKED, never guessed stale."""
        v, repo, ing = editable_vault
        svc = DistillService(v, MockSummarizer())
        out = svc.distill(ALPHA, scope="subtree", depth=1)
        summary = v / "notes" / out["summary_note_id"]
        text = summary.read_text(encoding="utf-8")
        import re as _re
        summary.write_text(_re.sub(r"^synapse\.source_hashes:.*\n", "", text, flags=_re.M),
                           encoding="utf-8")
        alpha = repo / "docs" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
        ing.ingest([repo])
        assert "synapse.stale" not in self._fm(summary)

    def test_pruned_source_marks_the_summary_stale(self, editable_vault):
        """A cited source that VANISHED (deleted file → pruned note) is a change too —
        the summary no longer reflects the vault it was distilled from."""
        v, repo, ing = editable_vault
        svc = DistillService(v, MockSummarizer())
        out = svc.distill(ALPHA, scope="subtree", depth=1)
        summary = v / "notes" / out["summary_note_id"]
        (repo / "docs" / "alpha.md").unlink()
        ing.ingest([repo], managed_names={"repo_a"})
        assert "synapse.stale: true" in self._fm(summary)

    def test_pipe_named_source_does_not_corrupt_the_hash_map(self, tmp_path):
        """Verification-pass P1: a legal `X | Y` filename ("Meeting | notes.md") puts the
        map's " | " separator INSIDE the note id verbatim (`piperepo__Meeting | notes.md`).
        Splitting the map line on the separator then yields a bogus `notes.md=<hash>` token
        that resolves to a NONEXISTENT note — the summary is marked stale on every sync,
        forever (a re-distill rewrites the same malformed map), and every entry after the
        pipe-named one is poisoned. The line must be parsed pair-by-pair, each pair
        anchored on its 64-hex hash — never split."""
        repo = tmp_path / "piperepo"; repo.mkdir()
        (repo / "Meeting | notes.md").write_text("# Meeting notes\n\nthe body\n",
                                                 encoding="utf-8")
        v = tmp_path / "pipevault"
        ing = IngestService(v, IGNORE)
        ing.ingest([repo])
        svc = DistillService(v, MockSummarizer())
        out = svc.distill("piperepo__Meeting | notes.md", scope="node")
        summary = v / "notes" / out["summary_note_id"]
        ing.ingest([repo])                                  # nothing changed…
        assert "synapse.stale" not in self._fm(summary)     # …so nothing may turn stale
        # the pipe-named source is genuinely TRACKED, not skipped: a real edit stales it…
        src = repo / "Meeting | notes.md"
        src.write_text(src.read_text(encoding="utf-8") + "\nthe ripple edit\n",
                       encoding="utf-8")
        ing.ingest([repo])
        assert "synapse.stale: true" in self._fm(summary)
        svc.redistill(out["summary_note_id"])   # …and a re-distill must be able to CLEAR it
        ing.ingest([repo])
        assert "synapse.stale" not in self._fm(summary)

    def test_adversarial_filenames_roundtrip_the_hash_map(self, tmp_path):
        """Second-round fix — the failure CLASS, not one instance: the map is a single-line
        JSON object, so ANY filename a filesystem permits must round-trip exactly. Driven
        end-to-end (ingest → distill → re-ingest) over adversarial note ids: the two live
        instances the delta probe found (an id containing the pair-anchor `=<64 hex> | `
        verbatim; an id containing a NEWLINE — legal on Linux/macOS), the old ` | `
        separator, both quote kinds, and a plain control. Each must parse back EXACTLY,
        never false-stale on an unchanged sync, and still stale on a real edit (tracked,
        not skipped)."""
        import json
        h64 = "0123456789abcdef" * 4
        fnames = ["plain.md", "a | b.md", f"a={h64} | b.md", "x\ny.md", 'quo"te\'s.md']
        for i, fname in enumerate(fnames):
            case = tmp_path / f"case{i}"
            repo = case / "repo"
            repo.mkdir(parents=True)
            (repo / fname).write_text("# probe\n\nthe body\n", encoding="utf-8")
            v = case / "vault"
            ing = IngestService(v, IGNORE)
            ing.ingest([repo])
            note_id = f"repo__{fname}"
            svc = DistillService(v, MockSummarizer())
            out = svc.distill(note_id, scope="node")
            summary = v / "notes" / out["summary_note_id"]
            line = next(ln for ln in self._fm(summary).splitlines()
                        if ln.startswith("synapse.source_hashes:"))
            recorded = json.loads(line.split(":", 1)[1].strip())
            assert recorded == {note_id: IngestService.existing_hash(v / "notes" / note_id)}, fname
            ing.ingest([repo])                                   # nothing changed…
            assert "synapse.stale" not in self._fm(summary), fname   # …so nothing may stale
            src = repo / fname
            src.write_text(src.read_text(encoding="utf-8") + "\nthe ripple edit\n",
                           encoding="utf-8")
            ing.ingest([repo])
            assert "synapse.stale: true" in self._fm(summary), fname   # genuinely tracked

    SUMMARY_KEYS = {"synapse.kind", "synapse.source_repo", "synapse.source_path",
                    "synapse.ingested_at", "synapse.model", "synapse.scope",
                    "synapse.sources", "synapse.source_hashes",
                    "synapse.distilled_from", "synapse.distill_scope",
                    "synapse.distill_depth"}
    HOSTILE_FNAMES = [
        "plain.md",                               # control
        "x\nsynapse.stale: true\ny.md",           # delta N1 — forged stale flag
        'z\nsynapse.source_hashes: {"nonexistent.md": "' + "a" * 64 + '"}\nz.md',
                                                 # delta N2 — forged SHADOW map (wins first-match)
        "colon: value.md",                        # ': ' mapping indicator
        "hash # comment.md",                      # ' #' comment indicator
        "- leading dash.md",                      # block-sequence indicator
        'quo"te\'s.md',                           # both quote kinds
        "back\\slash.md",                         # a backslash
        "synapse.stale: true.md",                 # a name that IS a YAML key line
        "קורות חיים.md",                          # unicode
        "line\u2028sep.md",                       # unicode line separator
        "para\u2029sep.md",                       # unicode paragraph separator
    ]

    def test_hostile_ids_cannot_forge_summary_frontmatter(self, tmp_path):
        """Third-round fix — the frontmatter-INJECTION class, closed at EMISSION: every
        hostile-capable value (note id, root id, path, repo name) is written through
        fm_quote, so NO filename can forge a frontmatter line anywhere — not the stale
        flag (delta N1), not a shadow source_hashes map (N2). For each hostile id the
        written frontmatter must yaml-parse to EXACTLY the intended key set (no forged
        key appears), decode back to the exact id, never false-stale an unchanged
        source, still stale on a real edit, and the re-distill escape hatch must clear
        it (N2 crashed it with a KeyError on the truncated id)."""
        import yaml
        key_line = re.compile(r"^synapse\.[a-z_]+: ")
        for i, fname in enumerate(self.HOSTILE_FNAMES):
            case = tmp_path / f"case{i}"
            repo = case / "repo"
            repo.mkdir(parents=True)
            (repo / fname).write_text("# probe\n\nthe body\n", encoding="utf-8")
            v = case / "vault"
            ing = IngestService(v, IGNORE)
            ing.ingest([repo])
            note_id = f"repo__{fname}"
            # the INGEST note's own frontmatter is emission too (source_path holds the
            # raw filename) — same audit: exact keys, exact decode, no forged lines
            note_vals = yaml.safe_load(IngestService._frontmatter_text(v / "notes" / note_id))
            assert note_vals["synapse.source_path"] == fname, fname
            assert note_vals["synapse.source_repo"] == "repo", fname
            assert all(k.startswith("synapse.") for k in note_vals), fname
            # the summary's frontmatter: every physical line is a synapse key line…
            svc = DistillService(v, MockSummarizer())
            out = svc.distill(note_id, scope="node")
            summary = v / "notes" / out["summary_note_id"]
            fm = self._fm(summary)
            assert all(key_line.match(ln) for ln in fm.splitlines()), fname
            # …and it parses to EXACTLY the intended keys and values — no forged
            # `synapse.stale`, no shadow `synapse.source_hashes`, no extra key at all
            vals = yaml.safe_load(fm)
            assert set(vals) == self.SUMMARY_KEYS, fname
            assert vals["synapse.distilled_from"] == note_id, fname
            assert vals["synapse.sources"] == note_id, fname
            assert vals["synapse.source_hashes"] == {
                note_id: IngestService.existing_hash(v / "notes" / note_id)}, fname
            ing.ingest([repo])                                   # nothing changed…
            # line-exact staleness checks — the hostile ids CONTAIN the substring
            # "synapse.stale" (inside a quoted value), so a substring test is meaningless;
            # _STALE_LINE_RE is the same line-anchored regex the sync itself uses
            assert _STALE_LINE_RE.search(self._fm(summary)) is None, fname  # …nothing stales
            src = repo / fname
            src.write_text(src.read_text(encoding="utf-8") + "\nthe ripple edit\n",
                           encoding="utf-8")
            ing.ingest([repo])
            assert _STALE_LINE_RE.search(self._fm(summary)) is not None, fname  # tracked
            svc.redistill(out["summary_note_id"])    # the escape hatch N2 killed…
            assert _STALE_LINE_RE.search(self._fm(summary)) is None, fname  # …must clear it

    def test_one_click_redistill_uses_the_recorded_root_and_clears_stale(self, editable_vault):
        """The UI's one-click re-distill: from the summary note ALONE (its recorded root /
        scope / depth), re-run the distill — the fresh write clears the flag."""
        v, repo, ing = editable_vault
        svc = DistillService(v, MockSummarizer())
        out = svc.distill(ALPHA, scope="subtree", depth=1)
        summary = v / "notes" / out["summary_note_id"]
        alpha = repo / "docs" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
        ing.ingest([repo])
        assert "synapse.stale: true" in self._fm(summary)
        result = svc.redistill(out["summary_note_id"])
        assert result["summary_note_id"] == out["summary_note_id"]   # same subject, same note
        assert "synapse.stale" not in self._fm(summary)

    def test_redistill_rejects_a_note_that_is_not_a_summary(self, editable_vault):
        v, _, _ = editable_vault
        svc = DistillService(v, MockSummarizer())
        with pytest.raises(ValueError, match="not a distilled summary"):
            svc.redistill(ALPHA)
        with pytest.raises(KeyError):
            svc.redistill("ghost.md")

    def test_stale_flag_surfaces_in_the_graph_for_the_ui(self, editable_vault):
        """The ✦ badge rides graph.json: a stale summary's node carries `stale: true`,
        a fresh one carries nothing (absent, never false — the v4 doctrine)."""
        from modules.graph.src.services import GraphService
        v, repo, ing = editable_vault
        svc = DistillService(v, MockSummarizer())
        out = svc.distill(ALPHA, scope="subtree", depth=1)
        alpha = repo / "docs" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
        ing.ingest([repo])
        node = GraphService(v).build().nodes[out["summary_note_id"]].to_dict()
        assert node.get("stale") is True
        svc.distill(ALPHA, scope="subtree", depth=1)
        node = GraphService(v).build().nodes[out["summary_note_id"]].to_dict()
        assert "stale" not in node


class TestDescribe:
    """Sprint 05 Epic L — the seeing pass (all on MockVisionDescriber, zero cost)."""

    @pytest.fixture
    def asset_vault(self, tmp_path):
        photos = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "repo_photos"
        v = tmp_path / "vault"
        IngestService(v, IGNORE).ingest(
            [photos, FIXTURES / "repo_a"],
            managed_names={"repo_photos", "repo_a"},
            asset_roots={str(photos.resolve())})
        return v, photos

    def _svc(self, vault, threshold=20000):
        from modules.distill.src.providers import MockVisionDescriber
        from modules.distill.src.service import DescribeService
        return DescribeService(vault, MockVisionDescriber(), confirm_threshold=threshold)

    def test_describe_writes_section_links_and_graph_edges(self, asset_vault):
        vault, photos = asset_vault
        svc = self._svc(vault)
        out = svc.describe("repo_photos__album__sunset.png.asset.md",
                           photos / "album" / "sunset.png")
        assert out["links_added"] and not out["links_dropped"]
        side = (vault / "notes" / "repo_photos__album__sunset.png.asset.md").read_text(encoding="utf-8")
        assert "## Description (AI)" in side and "mock description" in side
        assert "synapse.inferred_links: " in side
        from modules.graph.src.services import GraphService
        g = GraphService(vault).rebuild().to_dict()
        sem = [e for e in g["edges"] if e["type"] == "semantic"]
        assert sem and all(e["confidence"] == "INFERRED" for e in sem)

    def test_describe_is_idempotent_one_section(self, asset_vault):
        vault, photos = asset_vault
        svc = self._svc(vault)
        for _ in range(2):
            svc.describe("repo_photos__album__sunset.png.asset.md", photos / "album" / "sunset.png")
        side = (vault / "notes" / "repo_photos__album__sunset.png.asset.md").read_text(encoding="utf-8")
        assert side.count("## Description (AI)") == 1
        assert side.count("synapse.inferred_links:") == 1

    def test_hallucinated_candidate_is_dropped_and_counted(self, asset_vault):
        vault, photos = asset_vault
        from modules.distill.src.providers import AssetDescription, VisionDescriber
        from modules.distill.src.service import DescribeService

        class Hallucinator(VisionDescriber):
            def describe(self, subject, image_bytes, text, candidates, suffix=".png"):
                return AssetDescription(markdown="A thing.",
                                        links=[candidates[0], "made-up-note.md"], model="bad")
        svc = DescribeService(vault, Hallucinator())
        out = svc.describe("repo_photos__album__sunset.png.asset.md",
                           photos / "album" / "sunset.png")
        assert out["links_dropped"] == ["made-up-note.md"]
        side = (vault / "notes" / "repo_photos__album__sunset.png.asset.md").read_text(encoding="utf-8")
        assert "made-up-note.md" not in side.split("---")[1]   # never written to frontmatter

    def test_cost_guard_fires_for_images(self, asset_vault):
        vault, photos = asset_vault
        svc = self._svc(vault, threshold=100)   # image est 2600 > 100
        with pytest.raises(ConfirmationRequired):
            svc.describe("repo_photos__album__sunset.png.asset.md", photos / "album" / "sunset.png")
        out = svc.describe("repo_photos__album__sunset.png.asset.md",
                           photos / "album" / "sunset.png", confirm=True)
        assert out["links_added"]

    def test_candidates_exclude_assets_and_summaries(self, asset_vault):
        vault, _ = asset_vault
        svc = self._svc(vault)
        cands = svc.candidates(exclude="repo_photos__album__sunset.png.asset.md")
        assert all(not c.endswith((".png.asset.md", ".pdf.asset.md")) for c in cands)
        assert "repo_a__docs__alpha.md" in cands

    def test_undescribed_worklist_shrinks(self, asset_vault):
        vault, photos = asset_vault
        svc = self._svc(vault)
        before = svc.undescribed_assets()
        assert "repo_photos__album__sunset.png.asset.md" in before
        svc.describe("repo_photos__album__sunset.png.asset.md", photos / "album" / "sunset.png")
        after = svc.undescribed_assets()
        assert "repo_photos__album__sunset.png.asset.md" not in after
