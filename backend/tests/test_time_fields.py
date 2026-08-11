"""Sprint 06 S1/S2 — the two time fields.

The point of this file is the pair of properties that make the fields honest:
`first_seen` survives a re-ingest, and a note that never had one is left WITHOUT a date
rather than back-dated to now.
"""
from pathlib import Path

import pytest

from app.core.config import Settings
from modules.graph.src.services import GraphService
from modules.ingest.src.models import SourceFile
from modules.ingest.src.services import IngestService


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "src-repo"
    (r / "docs").mkdir(parents=True)
    (r / "docs" / "a.md").write_text("# Alpha\n\nbody\n", encoding="utf-8")
    return r


def _svc(tmp_path: Path) -> IngestService:
    s = Settings(vault_path=tmp_path / "vault")
    return IngestService(s.vault_path, s.ignore_dirs, s.companion_media_dir, s.interactive_prefix)


def _src(repo: Path) -> SourceFile:
    return SourceFile(repo_name=repo.name, repo_root=repo, path=repo / "docs" / "a.md")


def test_ingest_writes_both_fields(tmp_path, repo):
    svc = _svc(tmp_path)
    assert svc.write_note(_src(repo)) == "written"
    fm = (svc.notes_dir / _src(repo).note_id).read_text(encoding="utf-8")
    assert "synapse.first_seen:" in fm
    assert "synapse.file_mtime:" in fm


def test_first_seen_survives_a_reingest(tmp_path, repo):
    """The property the whole design rests on: rewrite must not reset it, or the field
    silently degrades into 'last ingested'."""
    svc = _svc(tmp_path)
    src = _src(repo)
    svc.write_note(src)
    note = svc.notes_dir / src.note_id
    first = [l for l in note.read_text(encoding="utf-8").splitlines()
             if l.startswith("synapse.first_seen:")][0]

    (repo / "docs" / "a.md").write_text("# Alpha\n\nCHANGED body\n", encoding="utf-8")
    assert svc.write_note(src) == "written"          # content changed → genuinely rewritten
    again = [l for l in note.read_text(encoding="utf-8").splitlines()
             if l.startswith("synapse.first_seen:")][0]
    assert again == first, "first_seen was reset by a re-ingest"


def test_file_mtime_tracks_the_file_not_the_ingest(tmp_path, repo):
    import os, time
    svc = _svc(tmp_path)
    src = _src(repo)
    old = time.time() - 90 * 24 * 3600            # 90 days ago
    os.utime(repo / "docs" / "a.md", (old, old))
    svc.write_note(src)
    fm = (svc.notes_dir / src.note_id).read_text(encoding="utf-8")
    mt = [l for l in fm.splitlines() if l.startswith("synapse.file_mtime:")][0]
    assert str(time.gmtime(old).tm_year) in mt, f"file_mtime followed the ingest, not the file: {mt}"


def test_graph_surfaces_the_fields(tmp_path, repo):
    svc = _svc(tmp_path)
    svc.write_note(_src(repo))
    g = GraphService(svc.notes_dir.parent).rebuild().to_dict()
    assert g["schema_version"] == 5
    note = next(n for n in g["nodes"] if n["kind"] == "note")
    assert note["first_seen"] and note["file_mtime"]


def test_a_pre_v4_note_is_left_dateless_never_backdated(tmp_path):
    """Notes indexed before the fields existed must load with them ABSENT. An invented
    'today' would mark the whole existing corpus as new; an invented old date is a lie."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "old__doc.md").write_text(
        "---\nsynapse.source_repo: old\nsynapse.source_path: doc.md\n"
        "synapse.content_hash: " + "0" * 64 + "\n---\n# Old\n", encoding="utf-8")
    g = GraphService(vault).rebuild().to_dict()
    note = next(n for n in g["nodes"] if n["kind"] == "note")
    assert "first_seen" not in note, "a pre-v4 note was given a fabricated first_seen"
    assert "file_mtime" not in note


def test_an_existing_note_gains_file_mtime_but_is_not_stamped_as_new(tmp_path, repo):
    """The defect this catches: notes that predate the fields were staying `unchanged`
    forever, so they never gained a date at all — and if they HAD been stamped, the whole
    corpus would have claimed to be first seen today."""
    svc = _svc(tmp_path)
    src = _src(repo)
    note = svc.notes_dir / src.note_id
    # simulate a pre-S1 note: correct body hash, no time fields
    svc.notes_dir.mkdir(parents=True, exist_ok=True)
    raw = src.path.read_bytes()
    note.write_text(
        "---\n"
        f"synapse.source_repo: {src.repo_name}\n"
        f"synapse.source_path: {src.rel_path}\n"
        f"synapse.content_hash: {svc.content_hash(raw)}\n---\n" + raw.decode(),
        encoding="utf-8")

    assert svc.write_note(src) == "written", "a note missing its dates was treated as unchanged"
    fm = note.read_text(encoding="utf-8")
    assert "synapse.file_mtime:" in fm, "existing note never gained a real file date"
    assert "synapse.first_seen:" not in fm, "an old note was stamped as first seen today"


def test_a_genuinely_new_note_does_get_first_seen(tmp_path, repo):
    svc = _svc(tmp_path)
    svc.write_note(_src(repo))
    fm = (svc.notes_dir / _src(repo).note_id).read_text(encoding="utf-8")
    assert "synapse.first_seen:" in fm
