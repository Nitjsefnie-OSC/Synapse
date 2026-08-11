"""Epic B unit tests — vault → graph + Index over the committed fixtures.
Proves the binding invariants: typed edges, unresolved-recorded, rebuild-invariance."""

import json
from pathlib import Path

import pytest

from modules.graph.src.services import GraphService
from modules.ingest.src.services import IngestService

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures"
IGNORE = frozenset({"node_modules", ".venv", ".git", "__pycache__"})

ALPHA = "repo_a__docs__alpha.md"
README = "repo_a__README.md"
HEBREW = "repo_a__hebrew.md"
BETA = "repo_b__beta.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A real vault: fixtures ingested via the actual ingest service (no hand-made notes)."""
    vault_path = tmp_path / "vault"
    IngestService(vault_path, IGNORE).ingest([FIXTURES / "repo_a", FIXTURES / "repo_b"])
    return vault_path


@pytest.fixture
def graph(vault: Path):
    return GraphService(vault).build()


class TestGraphBuild:
    def test_nodes_are_notes_plus_repo_hubs(self, graph):
        kinds = {}
        for n in graph.nodes.values():
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        assert kinds == {"note": 4, "repo": 2}

    def test_wikilink_resolution_incl_cross_repo(self, graph):
        edges = {(e.src, e.dst) for e in graph.edges if e.type == "wikilink"}
        assert (README, ALPHA) in edges          # [[Alpha]] by unique stem
        assert (ALPHA, BETA) in edges            # [[Beta]] resolves ACROSS repos
        assert (HEBREW, ALPHA) in edges          # wikilink from Hebrew note

    def test_relative_links_resolve_within_repo(self, graph):
        edges = {(e.src, e.dst) for e in graph.edges if e.type == "relative"}
        assert (README, ALPHA) in edges          # docs/alpha.md
        assert (ALPHA, README) in edges          # ../README.md

    def test_sibling_edges_group_by_repo_hub(self, graph):
        siblings = {(e.src, e.dst) for e in graph.edges if e.type == "sibling"}
        assert (ALPHA, "repo:repo_a") in siblings
        assert (BETA, "repo:repo_b") in siblings
        assert len(siblings) == 4                # linear, one per note — never a clique

    def test_pathref_backticked_pointers_become_edges(self, graph):
        edges = {(e.src, e.dst) for e in graph.edges if e.type == "pathref"}
        assert (ALPHA, HEBREW) in edges            # `../hebrew.md` in a code span
        assert not any("http" in d for _, d in edges)

    def test_unresolved_links_recorded_not_dropped(self, graph):
        assert "[[Ghost Concept]]" in graph.nodes[ALPHA].unresolved
        assert "missing/nothing.md" in graph.nodes[BETA].unresolved

    def test_external_urls_never_become_edges_or_unresolved(self, graph):
        beta = graph.nodes[BETA]
        assert not any("agents.md" in u for u in beta.unresolved)
        assert not any("http" in e.dst for e in graph.edges)


class TestRebuildInvariance:
    def test_graph_json_is_deterministic_and_vault_derived(self, vault):
        service = GraphService(vault)
        service.rebuild()
        first = json.loads(service.graph_file.read_text(encoding="utf-8"))
        service.graph_file.unlink()              # delete the derived cache
        service.rebuild()
        second = json.loads(service.graph_file.read_text(encoding="utf-8"))
        assert first == second                   # deep-equal: the vault alone reproduces it

    def test_schema_versioned(self, vault):
        service = GraphService(vault)
        service.rebuild()
        assert json.loads(service.graph_file.read_text(encoding="utf-8"))["schema_version"] == 5


class TestIndexAndStats:
    def test_index_lists_every_note_grouped_by_repo(self, vault):
        service = GraphService(vault)
        service.rebuild()
        index = service.index_file.read_text(encoding="utf-8")
        for note_id in (ALPHA, README, HEBREW, BETA):
            assert f"[[{note_id}]]" in index
        assert "## repo_a" in index and "## repo_b" in index
        assert "4 notes" in index                # honest header counts

    def test_stats_match_reality(self, graph):
        s = graph.stats()
        assert s["notes"] == 4 and s["repos"] == 2
        assert s["edges_by_type"] == {"pathref": 1, "relative": 2, "sibling": 4, "wikilink": 3}
        assert s["unresolved_links"] == 2
        assert s["top_connected"][0]["id"] == ALPHA   # the hub of the fixture brain
