"""Sprint 04 Epic G/J unit tests — the query trio + schema v3. Zero network, zero models."""

from pathlib import Path

import pytest

from modules.graph.src.query import explain, query, resolve, shortest_path
from modules.graph.src.services import GraphService
from modules.ingest.src.services import IngestService

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures"
IGNORE = frozenset({"node_modules", ".venv", ".git", "__pycache__"})


@pytest.fixture(scope="module")
def graph(tmp_path_factory) -> dict:
    v = tmp_path_factory.mktemp("qvault")
    IngestService(v, IGNORE).ingest([FIXTURES / "repo_a", FIXTURES / "repo_b"])
    return GraphService(v).rebuild().to_dict()


class TestSchemaV3:
    def test_edges_carry_confidence_extracted_by_default(self, graph):
        assert graph["schema_version"] == 5
        assert graph["edges"], "fixture graph must have edges"
        assert all(e["confidence"] == "EXTRACTED" for e in graph["edges"])
        assert all("confidence_score" not in e for e in graph["edges"])   # EXTRACTED = 1.0 implicit


class TestResolve:
    def test_exact_id_wins(self, graph):
        assert resolve(graph, "repo_a__docs__alpha.md") == "repo_a__docs__alpha.md"

    def test_fuzzy_title(self, graph):
        assert resolve(graph, "alpha") == "repo_a__docs__alpha.md"

    def test_garbage_is_none(self, graph):
        assert resolve(graph, "zzznope-nothing") is None


class TestExplain:
    def test_groups_by_direction_and_type(self, graph):
        out = explain(graph, "repo_a__docs__alpha.md")
        assert out["node"]["id"] == "repo_a__docs__alpha.md"
        assert out["degree"] >= 2
        directions = {g["direction"] for g in out["connections"]}
        assert "out" in directions and "in" in directions

    def test_unknown_is_none(self, graph):
        assert explain(graph, "ghost.md") is None


class TestPath:
    def test_cross_repo_path_found_and_deterministic(self, graph):
        out1 = shortest_path(graph, "repo_a__docs__alpha.md", "repo_b__beta.md")
        out2 = shortest_path(graph, "repo_a__docs__alpha.md", "repo_b__beta.md")
        assert out1["found"] and out1 == out2
        assert out1["hops"][0]["id"] == "repo_a__docs__alpha.md"
        assert out1["hops"][-1]["id"] == "repo_b__beta.md"
        assert out1["length"] == len(out1["hops"]) - 1

    def test_sibling_edges_never_carry_a_path(self, graph):
        """Two notes whose ONLY connection is sharing a repo hub must be unreachable —
        pathing through 'sibling' would make every same-repo pair trivially connected."""
        out = shortest_path(graph, "repo_a__docs__alpha.md", "repo_b__beta.md")
        assert all(h["via"] is None or h["via"]["type"] != "sibling" for h in out["hops"])

    def test_same_node_is_zero_hops(self, graph):
        out = shortest_path(graph, "repo_a__docs__alpha.md", "repo_a__docs__alpha.md")
        assert out["found"] and out["length"] == 0


class TestQuery:
    def test_scoped_subgraph_with_seeds_and_neighbors(self, graph):
        out = query(graph, "what connects alpha to beta?")
        assert "alpha" in out["terms"] and "beta" in out["terms"]
        ids = {n["id"] for n in out["nodes"]}
        assert "repo_a__docs__alpha.md" in ids
        assert out["seeds"][0] in ids                    # seeds always survive the budget
        assert all(e["src"] in ids and e["dst"] in ids for e in out["edges"])

    def test_budget_is_honest(self, graph):
        out = query(graph, "alpha beta readme", budget=5)
        assert len(out["nodes"]) <= 5
        # if anything was cut, the result SAYS so
        full = query(graph, "alpha beta readme", budget=200)
        if len(full["nodes"]) > 5:
            assert out["truncated"] is True

    def test_stopword_only_question_is_empty_not_crashy(self, graph):
        out = query(graph, "what is the of and")
        assert out["nodes"] == [] and out["seeds"] == []

    def test_deterministic(self, graph):
        assert query(graph, "alpha readme") == query(graph, "alpha readme")


class TestCodexWave:
    """Codex cross-vendor GBU (2026-07-17) regression pins."""

    def test_hebrew_retrieval_works(self, graph):
        """Codex P1: the ASCII tokenizer made Hebrew queries silently return nothing —
        fatal for a Hebrew-heavy vault. The committed fixture note must be findable."""
        out = query(graph, "מסמך בעברית")
        assert any(n["id"] == "repo_a__hebrew.md" for n in out["nodes"])
        assert resolve(graph, "מסמך") == "repo_a__hebrew.md"

    def test_budget_is_a_hard_cap_even_below_seed_count(self, graph):
        """Codex P1: budget=1 used to return 5 seeds; and absurd budgets must clamp."""
        assert len(query(graph, "alpha beta readme", budget=1)["nodes"]) == 1
        assert len(query(graph, "alpha beta readme", budget=-7)["nodes"]) == 1
        big = query(graph, "alpha beta readme", budget=10**9)
        assert len(big["nodes"]) <= 200

    def test_repo_hub_is_a_legal_path_endpoint(self, graph):
        """Codex P2: hubs resolved as endpoints but were unreachable (all-sibling edges)."""
        out = shortest_path(graph, "repo:repo_a", "repo_a__docs__alpha.md")
        assert out["found"] and out["length"] == 1

    def test_same_repo_pair_cannot_shortcut_through_its_hub(self, tmp_path):
        """The hub-endpoint allowance must NOT reopen the trivial a→hub→b 2-hop route."""
        repo = tmp_path / "loner"; repo.mkdir()
        (repo / "one.md").write_text("# One\n\nno links\n", encoding="utf-8")
        (repo / "two.md").write_text("# Two\n\nno links\n", encoding="utf-8")
        v = tmp_path / "lonervault"
        IngestService(v, IGNORE).ingest([repo])
        g = GraphService(v).rebuild().to_dict()
        out = shortest_path(g, "loner__one.md", "loner__two.md")
        assert out["found"] is False   # only plumbing connects them — no knowledge path

    def test_edge_confidence_invariants_fail_loudly(self):
        """Codex P2: invalid tag/score combinations must be constructor errors."""
        from modules.graph.src.models import Edge
        with pytest.raises(ValueError):
            Edge(src="a", dst="b", type="wikilink", confidence="GUESSED")
        with pytest.raises(ValueError):
            Edge(src="a", dst="b", type="wikilink", confidence="INFERRED")   # no score
        with pytest.raises(ValueError):
            Edge(src="a", dst="b", type="wikilink", confidence="EXTRACTED", confidence_score=0.9)
        Edge(src="a", dst="b", type="wikilink", confidence="INFERRED", confidence_score=0.85)


class TestDesktopGBUWave:
    """Recall fixes from the Claude Desktop field GBU (2026-07-17)."""

    def test_filename_tokens_rank_canonical_docs(self, tmp_path):
        """P0 false-negative: 'bible' must surface `30_HS_BIBLE.md` even when its TITLE
        doesn't contain the word — filename tokens are words now."""
        repo = tmp_path / "hs"; (repo / "pm").mkdir(parents=True)
        (repo / "pm" / "30_HS_BIBLE.md").write_text("# The Source Book\n\nbinding rules\n", encoding="utf-8")
        (repo / "pm" / "notes.md").write_text("# Notes\n\nthe bible is elsewhere\n", encoding="utf-8")
        v = tmp_path / "v"
        IngestService(v, IGNORE).ingest([repo])
        g = GraphService(v).rebuild().to_dict()
        out = query(g, "bible source of truth")
        assert out["seeds"][0] == "hs__pm__30_HS_BIBLE.md"

    def test_repo_name_no_longer_flattens_scores(self, graph):
        """Querying the brain's own repo name must not give every note the same big
        prefix bonus — distinctive terms must dominate."""
        out = query(graph, "repo_a alpha")
        assert out["seeds"][0] == "repo_a__docs__alpha.md"   # 'alpha' outranks the repo prefix

    def test_rare_term_beats_generic_title_words(self, tmp_path):
        """IDF follow-through: 'bible source of truth' must surface the Bible even when
        six other notes are literally TITLED 'Source of Truth'."""
        repo = tmp_path / "hs2"; repo.mkdir()
        for i in range(6):
            (repo / f"spec{i}.md").write_text(f"# Source of Truth {i}\n\nspec\n", encoding="utf-8")
        (repo / "30_HS_BIBLE.md").write_text("# The Book\n\nbinding\n", encoding="utf-8")
        v = tmp_path / "v2"
        IngestService(v, IGNORE).ingest([repo])
        g = GraphService(v).rebuild().to_dict()
        out = query(g, "bible source of truth")
        assert "hs2__30_HS_BIBLE.md" in out["seeds"]
