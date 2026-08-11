"""API surface tests — the FastAPI wiring over real module services (fixture repos, tmp vault).
Model-free, network-free, paid-call-free by construction (sprint 1 has no model code at all)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("SYNAPSE_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv(
        "SYNAPSE_SOURCE_REPOS", f"{FIXTURES / 'repo_a'},{FIXTURES / 'repo_b'}"
    )
    # NEVER read the developer's real backend/.env: tests must not depend on (or leak
    # into) dev-machine state — a real key there would flip "keyless" scenarios (GBU 2026-07-16)
    monkeypatch.setenv("SYNAPSE_ENV_FILE", str(tmp_path / "envdir" / ".env"))
    from app.main import app
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_graph_before_ingest_gives_actionable_404(client):
    r = client.get("/api/v1/graph")
    assert r.status_code == 404
    assert "ingest" in r.json()["detail"]        # tells the user what to run, not just "not found"


def test_ingest_then_graph_roundtrip(client):
    report = client.post("/api/v1/ingest").json()
    assert report["totals"]["files_found"] == 4
    assert report["totals"]["notes_written"] == 4

    rebuild = client.post("/api/v1/rebuild").json()
    assert rebuild["notes"] == 4

    graph = client.get("/api/v1/graph").json()
    assert graph["schema_version"] == 5
    assert len([n for n in graph["nodes"] if n["kind"] == "note"]) == 4

    stats = client.get("/api/v1/stats").json()
    assert stats["edges_by_type"]["wikilink"] == 3


def test_note_and_index_endpoints(client):
    client.post("/api/v1/ingest")
    client.post("/api/v1/rebuild")

    note = client.get("/api/v1/note/repo_a__hebrew.md").json()
    assert note["repo"] == "repo_a"
    assert "מסמך בעברית" in note["body"]          # UTF-8 through the API too

    assert client.get("/api/v1/note/no_such.md").status_code == 404
    assert client.get("/api/v1/note/..%2F..%2Fetc%2Fpasswd").status_code == 404

    index = client.get("/api/v1/index").json()
    assert "[[repo_b__beta.md]]" in index["markdown"]


def test_fresh_rebuild_is_invariant_via_api(client):
    client.post("/api/v1/ingest")
    first = client.post("/api/v1/rebuild").json()
    fresh = client.post("/api/v1/rebuild?fresh=true").json()
    assert first == fresh                          # the UI's invariance-proof contract


def test_distill_and_render_in_mock_mode(client, monkeypatch):
    monkeypatch.setenv("SYNAPSE_MOCK_MODELS", "1")
    client.post("/api/v1/ingest")
    client.post("/api/v1/rebuild")

    out = client.post("/api/v1/distill",
                      json={"node_id": "repo_a__docs__alpha.md", "scope": "subtree", "depth": 1}).json()
    assert out["citations"] >= 1 and out["summary_note_id"].startswith("S — ")

    img = client.post("/api/v1/render", json={"summary_note_id": out["summary_note_id"]}).json()
    assert img["image"].startswith("media/") and "no text" in img["prompt"].lower()

    note = client.get(f"/api/v1/note/{out['summary_note_id']}").json()
    assert "mock distillation" in note["body"]


def test_redistill_endpoint_roundtrip(client, tmp_path, monkeypatch):
    """Issue #9 over HTTP: edit a cited source → re-ingest → the summary is reported stale
    and its graph node carries the badge flag → one click on /redistill clears it.
    repo_a is copied into tmp — the shared fixture must never be edited in place."""
    import shutil
    repo = tmp_path / "repo_a"
    shutil.copytree(FIXTURES / "repo_a", repo, ignore=shutil.ignore_patterns("node_modules"))
    monkeypatch.setenv("SYNAPSE_SOURCE_REPOS", str(repo))
    monkeypatch.setenv("SYNAPSE_MOCK_MODELS", "1")
    client.post("/api/v1/ingest")
    client.post("/api/v1/rebuild")
    out = client.post("/api/v1/distill",
                      json={"node_id": "repo_a__docs__alpha.md", "scope": "subtree", "depth": 1}).json()
    sid = out["summary_note_id"]

    alpha = repo / "docs" / "alpha.md"
    alpha.write_text(alpha.read_text(encoding="utf-8") + "\nthe ripple edit\n", encoding="utf-8")
    rep = client.post("/api/v1/ingest").json()
    assert sid in rep["stale_summaries"]            # the ingest report SAYS it, not just flags it
    client.post("/api/v1/rebuild")
    node = next(n for n in client.get("/api/v1/graph").json()["nodes"] if n["id"] == sid)
    assert node.get("stale") is True                # the ✦ badge rides graph.json

    assert client.post("/api/v1/redistill", json={"note_id": "ghost.md"}).status_code == 404
    assert client.post("/api/v1/redistill",
                       json={"note_id": "repo_a__docs__alpha.md"}).status_code == 422   # not a summary
    out2 = client.post("/api/v1/redistill", json={"note_id": sid}).json()
    assert out2["summary_note_id"] == sid           # same subject → same note, refreshed
    client.post("/api/v1/rebuild")
    node = next(n for n in client.get("/api/v1/graph").json()["nodes"] if n["id"] == sid)
    assert "stale" not in node


def test_model_endpoints_fail_actionably_without_keys(client, monkeypatch):
    monkeypatch.delenv("SYNAPSE_MOCK_MODELS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REPLACE-ME")
    r = client.post("/api/v1/distill", json={"node_id": "x.md"})
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_roots_crud_with_prune(client, tmp_path, monkeypatch):
    # starts from the env-seeded list (source: env)
    roots = client.get("/api/v1/roots").json()
    assert len(roots) == 2 and all(r["source"] == "env" for r in roots)

    # add: validated; persists to roots.json (source flips to file)
    assert client.post("/api/v1/roots", json={"path": "/nowhere/ghost"}).status_code == 400
    extra = tmp_path / "repo_c"; extra.mkdir(); (extra / "solo.md").write_text("# Solo\n")
    roots = client.post("/api/v1/roots", json={"path": str(extra)}).json()
    assert len(roots) == 3 and all(r["source"] == "file" for r in roots)

    # the new root ingests
    rep = client.post("/api/v1/ingest").json()
    assert any(r["repo"] == "repo_c" for r in rep["repos"])

    # toggle: ingest is a SYNC — the disabled root's notes are pruned right away
    client.patch("/api/v1/roots", json={"path": str(extra)})
    rep = client.post("/api/v1/ingest").json()
    assert not any(r["repo"] == "repo_c" for r in rep["repos"])
    assert rep["totals"]["pruned"] == 1

    # deleted source file → next ingest prunes its note too (no ghost nodes)
    client.patch("/api/v1/roots", json={"path": str(extra)})   # re-enable
    client.post("/api/v1/ingest")
    (extra / "solo.md").unlink()
    rep = client.post("/api/v1/ingest").json()
    assert rep["totals"]["pruned"] == 1

    # delete root: list shrinks; nothing left to prune (sync already did)
    out = client.request("DELETE", "/api/v1/roots", json={"path": str(extra)}).json()
    assert out["pruned_notes"] == 0 and len(out["roots"]) == 2
    client.post("/api/v1/rebuild")
    graph = client.get("/api/v1/graph").json()
    assert not any(n["repo"] == "repo_c" for n in graph["nodes"])


def test_bulk_toggle_and_fs_browse(client, tmp_path):
    client.post("/api/v1/ingest")                                   # fill the vault first
    off = client.post("/api/v1/roots/bulk", json={"enabled": False}).json()
    assert all(not r["enabled"] for r in off)
    rep = client.post("/api/v1/ingest").json()
    assert rep["totals"]["files_found"] == 0 and rep["totals"]["pruned"] >= 4   # everything synced away
    on = client.post("/api/v1/roots/bulk", json={"enabled": True}).json()
    assert all(r["enabled"] for r in on)

    fs = client.get("/api/v1/fs", params={"path": str(FIXTURES)}).json()
    names = [d["name"] for d in fs["dirs"]]
    assert "repo_a" in names and "repo_b" in names
    assert client.get("/api/v1/fs", params={"path": "/nowhere"}).status_code == 404


def test_fs_complete_and_dot_folders(client, tmp_path):
    # dot-folders are browsable (that's where knowledge lives); noise dirs are not
    (tmp_path / ".claude").mkdir(); (tmp_path / ".git").mkdir(); (tmp_path / "docs").mkdir()
    fs = client.get("/api/v1/fs", params={"path": str(tmp_path)}).json()
    names = [d["name"] for d in fs["dirs"]]
    assert ".claude" in names and "docs" in names and ".git" not in names

    # path mode: prefix completion incl. dot-prefix
    comp = client.get("/api/v1/fs/complete", params={"q": str(tmp_path) + "/.cl"}).json()
    assert [c["name"] for c in comp["completions"]] == [".claude"]

    # search mode: bare substring within base
    comp = client.get("/api/v1/fs/complete", params={"q": "oc", "base": str(tmp_path)}).json()
    assert [c["name"] for c in comp["completions"]] == ["docs"]


def test_unhandled_500_carries_cors_for_the_frontend(client):
    """GBU P1: the catch-all handler runs OUTSIDE CORSMiddleware — it must attach the CORS
    grant itself, or a crashed server reads as 'backend unreachable' in the frontend."""
    from app.main import app
    if not any(getattr(r, "path", "") == "/__boom" for r in app.routes):
        @app.get("/__boom")
        def _boom():
            raise RuntimeError("kaboom")
    raw = TestClient(app, raise_server_exceptions=False)
    r = raw.get("/__boom", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 500 and "kaboom" in r.json()["detail"]
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    # an untrusted origin gets the JSON but NO cross-origin read grant
    evil = raw.get("/__boom", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_a_foreign_host_on_the_dev_port_is_not_trusted(client):
    """The allow-list used to be `https?://[^/]+:5173` — any host, as long as it used that
    port. CORS is precisely the control deciding whether a foreign page may READ this API's
    responses, so a page served from attacker.example:5173 could drive a local API that reads
    the filesystem and spends model tokens. (Security review 2026-08-04.)"""
    r = client.get("/health", headers={"Origin": "http://attacker.example:5173"})
    assert "access-control-allow-origin" not in r.headers
    # a LAN IP is no longer implied either — it is opt-in via SYNAPSE_ALLOWED_ORIGINS
    lan = client.get("/health", headers={"Origin": "http://192.168.1.7:5173"})
    assert "access-control-allow-origin" not in lan.headers
    ok = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_a_lan_origin_can_be_opted_in_explicitly(tmp_path, monkeypatch):
    """The documented WSL → Windows direct-IP fallback stays possible — explicitly."""
    monkeypatch.setenv("SYNAPSE_ALLOWED_ORIGINS", "http://172.19.5.5:5173")
    monkeypatch.setenv("SYNAPSE_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("SYNAPSE_ENV_FILE", str(tmp_path / "envdir" / ".env"))
    import importlib
    import app.main as m
    importlib.reload(m)
    try:
        c = TestClient(m.app)
        r = c.get("/health", headers={"Origin": "http://172.19.5.5:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://172.19.5.5:5173"
    finally:
        monkeypatch.delenv("SYNAPSE_ALLOWED_ORIGINS", raising=False)
        importlib.reload(m)   # never leave a widened policy behind for the next test


def test_duplicate_basename_root_is_rejected(client, tmp_path):
    """GBU P1: note ids are keyed by the root's folder name — same-name roots would silently
    clobber and cross-delete each other's notes."""
    a = tmp_path / "one" / "app"; a.mkdir(parents=True)
    b = tmp_path / "two" / "app"; b.mkdir(parents=True)
    assert client.post("/api/v1/roots", json={"path": str(a)}).status_code == 200
    r = client.post("/api/v1/roots", json={"path": str(b)})
    assert r.status_code == 409 and "same folder name" in r.json()["detail"]


def test_remove_root_prunes_by_frontmatter_not_prefix_glob(client, tmp_path):
    """GBU P1: removing root `foo` must NOT delete notes of a root named `foo__bar`
    (the old `foo__*.md` glob over-matched the prefix)."""
    foo = tmp_path / "foo"; foo.mkdir(); (foo / "a.md").write_text("# A\n", encoding="utf-8")
    foobar = tmp_path / "foo__bar"; foobar.mkdir(); (foobar / "b.md").write_text("# B\n", encoding="utf-8")
    client.post("/api/v1/roots", json={"path": str(foo)})
    client.post("/api/v1/roots", json={"path": str(foobar)})
    client.post("/api/v1/ingest")
    out = client.request("DELETE", "/api/v1/roots", json={"path": str(foo)}).json()
    assert out["pruned_notes"] == 1                  # ONLY foo's note
    client.post("/api/v1/rebuild")
    graph = client.get("/api/v1/graph").json()
    assert any(n["repo"] == "foo__bar" for n in graph["nodes"])      # neighbor survived
    assert not any(n["repo"] == "foo" for n in graph["nodes"])


def test_distill_depth_is_bounded(client, monkeypatch):
    """GBU P2: an unbounded depth would pin the worker scanning all edges forever."""
    monkeypatch.setenv("SYNAPSE_MOCK_MODELS", "1")
    client.post("/api/v1/ingest")
    client.post("/api/v1/rebuild")
    r = client.post("/api/v1/distill",
                    json={"node_id": "repo_a__docs__alpha.md", "scope": "subtree", "depth": 10**9})
    assert r.status_code == 422                      # pydantic bound, before any work


def test_delete_summary_only(client, monkeypatch):
    monkeypatch.setenv("SYNAPSE_MOCK_MODELS", "1")
    client.post("/api/v1/ingest")
    client.post("/api/v1/rebuild")
    out = client.post("/api/v1/distill", json={"node_id": "repo_a__docs__alpha.md"}).json()
    sid = out["summary_note_id"]
    img = client.post("/api/v1/render", json={"summary_note_id": sid}).json()

    # source-mirror notes refuse deletion (managed by the sync)
    assert client.delete("/api/v1/note/repo_a__docs__alpha.md").status_code == 422

    # summary deletes, media goes with it, graph forgets it after rebuild
    res = client.delete(f"/api/v1/note/{sid}").json()
    assert res["deleted"] == sid and img["image"] in res["media"]
    assert client.get(f"/api/v1/note/{sid}").status_code == 404
    client.post("/api/v1/rebuild")
    graph = client.get("/api/v1/graph").json()
    assert not any(n["id"] == sid for n in graph["nodes"])


# ── In-app model keys (models/status + models/keys) ─────────────────────────────
# Status must be honest, writes atomic and file-preserving, and key VALUES must
# never appear in any response. SYNAPSE_ENV_FILE isolates the real backend/.env.

class TestModelKeys:
    @pytest.fixture
    def keyless(self, client, tmp_path, monkeypatch) -> TestClient:
        monkeypatch.setenv("SYNAPSE_ENV_FILE", str(tmp_path / "envdir" / ".env"))
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SYNAPSE_MOCK_MODELS"):
            monkeypatch.delenv(var, raising=False)
        return client

    def test_status_reports_unconfigured_and_points_at_the_env_file(self, keyless):
        s = keyless.get("/api/v1/models/status").json()
        assert s["mock"] is False
        assert s["distill"]["configured"] is False and s["distill"]["key_hint"] is None
        assert s["render"]["configured"] is False and s["render"]["key_hint"] is None
        assert s["env_file"]                      # the manual path stays visible in the UI

    def test_mock_mode_is_reported(self, keyless, monkeypatch):
        monkeypatch.setenv("SYNAPSE_MOCK_MODELS", "1")
        assert keyless.get("/api/v1/models/status").json()["mock"] is True

    def test_save_key_goes_live_without_restart_and_never_echoes(self, keyless, tmp_path):
        import os
        r = keyless.post("/api/v1/models/keys", json={"anthropic_key": "sk-ant-test-1234abcd"})
        assert r.status_code == 200
        assert "sk-ant-test-1234abcd" not in r.text            # the value NEVER comes back
        s = r.json()
        assert s["distill"]["configured"] is True and s["distill"]["key_hint"] == "…abcd"
        env = (tmp_path / "envdir" / ".env").read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-ant-test-1234abcd" in env
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-1234abcd"   # live process env

    def test_upsert_replaces_placeholder_and_preserves_every_other_line(self, keyless, tmp_path):
        env_file = tmp_path / "envdir" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(
            "# my precious comment\nPORT=8000\nANTHROPIC_API_KEY=sk-ant-REPLACE-ME\n",
            encoding="utf-8")
        keyless.post("/api/v1/models/keys",
                     json={"anthropic_key": "sk-ant-new-1234wxyz", "openai_key": "sk-test-9999zzzz"})
        env = env_file.read_text(encoding="utf-8")
        assert "# my precious comment" in env and "PORT=8000" in env
        assert env.count("ANTHROPIC_API_KEY=") == 1             # replaced in place, not duplicated
        assert "ANTHROPIC_API_KEY=sk-ant-new-1234wxyz" in env
        assert "OPENAI_API_KEY=sk-test-9999zzzz" in env         # appended

    def test_bad_keys_are_rejected(self, keyless):
        assert keyless.post("/api/v1/models/keys", json={}).status_code == 422
        assert keyless.post("/api/v1/models/keys", json={"anthropic_key": "   "}).status_code == 422
        assert keyless.post("/api/v1/models/keys", json={"anthropic_key": "has spaces"}).status_code == 422
        assert keyless.post("/api/v1/models/keys", json={"openai_key": "short"}).status_code == 422

    # ── GBU 2026-07-16 fix wave (fresh-eyes + Codex cross-vendor findings) ──────

    def test_placeholder_paste_is_rejected_not_half_saved(self, keyless):
        """Both reviews P1: the .env.example placeholder returned 200 + 'live now' while
        status honestly said unconfigured — AND poisoned os.environ so a manual .env fix
        went inert until restart. Now: a clean 422 naming the mistake."""
        r = keyless.post("/api/v1/models/keys", json={"anthropic_key": "sk-ant-REPLACE-ME"})
        assert r.status_code == 422 and "placeholder" in r.json()["detail"]
        s = keyless.get("/api/v1/models/status").json()
        assert s["distill"]["configured"] is False

    def test_placeholder_in_process_env_never_blocks_a_real_key_from_the_file(
            self, keyless, tmp_path, monkeypatch):
        """Fresh-eyes P1: startup loads the .env.example placeholder into os.environ; the
        loader's 'env wins' rule then blocked a real key manually written to .env until
        restart. A placeholder must never win over a real value."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REPLACE-ME")
        env_file = tmp_path / "envdir" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-manual-1234real\n", encoding="utf-8")
        s = keyless.get("/api/v1/models/status").json()
        assert s["distill"]["configured"] is True and s["distill"]["key_hint"] == "…real"

    def test_concurrent_saves_lose_no_key_and_never_500(self, keyless, tmp_path):
        """Both reviews: two saves racing on a shared temp filename could 500 or drop one
        provider's key from the file. Serialized now — all succeed, both keys land."""
        from concurrent.futures import ThreadPoolExecutor
        payloads = [{"anthropic_key": "sk-ant-th-1234aaaa"} if i % 2 == 0
                    else {"openai_key": "sk-th-9999bbbb"} for i in range(16)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            codes = list(ex.map(
                lambda p: keyless.post("/api/v1/models/keys", json=p).status_code, payloads))
        assert codes == [200] * 16
        env = (tmp_path / "envdir" / ".env").read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-ant-th-1234aaaa" in env
        assert "OPENAI_API_KEY=sk-th-9999bbbb" in env
        leftovers = list((tmp_path / "envdir").glob(".env.*.tmp"))
        assert leftovers == []                      # no key-bearing temp files left behind


# ── Sprint 05: assets + the seeing pass ─────────────────────────────────────────

class TestAssets:
    PHOTOS = Path(__file__).resolve().parent / "fixtures" / "repo_photos"
    SUNSET = "repo_photos__album__sunset.png.asset.md"

    @pytest.fixture
    def aclient(self, client, tmp_path, monkeypatch) -> TestClient:
        monkeypatch.setenv("SYNAPSE_MOCK_MODELS", "1")
        import json as j
        (tmp_path / "roots.json").write_text(j.dumps([
            {"path": str(self.PHOTOS), "enabled": True, "assets": True},
            {"path": str(FIXTURES / "repo_a"), "enabled": True},
        ]), encoding="utf-8")
        client.post("/api/v1/ingest")
        client.post("/api/v1/rebuild")
        return client

    def test_assets_toggle_via_patch(self, client, tmp_path):
        import json as j
        (tmp_path / "roots.json").write_text(j.dumps(
            [{"path": str(self.PHOTOS), "enabled": True}]), encoding="utf-8")
        out = client.patch("/api/v1/roots",
                           json={"path": str(self.PHOTOS), "toggle": "assets"}).json()
        assert out[0]["assets"] is True
        assert client.patch("/api/v1/roots",
                            json={"path": str(self.PHOTOS), "toggle": "nope"}).status_code == 422

    def test_ingest_reports_assets_and_serves_them(self, aclient):
        rep = aclient.post("/api/v1/ingest").json()
        assert rep["totals"]["assets_found"] == 2
        r = aclient.get(f"/api/v1/asset/{self.SUNSET}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")
        assert aclient.get("/api/v1/asset/repo_a__docs__alpha.md").status_code == 404  # not an asset
        assert aclient.get("/api/v1/asset/ghost.png.md").status_code == 404

    def test_describe_mock_flow_end_to_end(self, aclient):
        out = aclient.post("/api/v1/describe", json={"note_id": self.SUNSET}).json()
        assert out["links_added"] and out["model"] == "mock-vision"
        note = aclient.get(f"/api/v1/note/{self.SUNSET}").json()
        assert "## Description (AI)" in note["body"]
        aclient.post("/api/v1/rebuild")
        graph = aclient.get("/api/v1/graph").json()
        sem = [e for e in graph["edges"] if e["type"] == "semantic"]
        assert sem and all(e["confidence"] == "INFERRED" for e in sem)

    def test_describe_all_always_asks_first(self, aclient):
        out = aclient.post("/api/v1/describe-all", json={}).json()
        assert out["requires_confirmation"] is True and out["count"] == 2
        out = aclient.post("/api/v1/describe-all", json={"confirm": True}).json()
        assert out["described"] == 2 and out["failed"] == []
        again = aclient.post("/api/v1/describe-all", json={}).json()
        assert again == {"described": 0, "message": "every asset already has a description"}


def test_unresolved_report_classifies_targets(client, tmp_path):
    """Epic N: ghosts get honest classification, mirroring the RESOLVER's rules exactly —
    ../-escapes and absolute paths are never probed and never promise resolution."""
    client.post("/api/v1/ingest")
    client.post("/api/v1/rebuild")
    out = client.get("/api/v1/unresolved").json()
    assert out["total"] >= 1
    ghost = out["targets"].get("ghost concept")
    assert ghost and ghost["status"] == "future" and ghost["referrers"] >= 1
    assert all(t["hint"] for t in out["targets"].values())   # every ghost explains itself
    # GBU P2 guards: escape/absolute targets classify WITHOUT filesystem probing
    for key, t in out["targets"].items():
        if key.startswith(("/", "http")):
            assert t["status"] == "external"
        assert t["status"] in ("future", "dead", "external", "outside-repo", "future-file")
        if t["status"] == "outside-repo":
            assert "never" in t["hint"]     # the hint must not promise add-a-Source fixes it
