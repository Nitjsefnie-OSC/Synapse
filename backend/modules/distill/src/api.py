"""FastAPI surface for distill — thin: route → provider factory → service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import load_settings

from .providers import AnthropicSummarizer, MockSummarizer
from .service import ConfirmationRequired, DistillService, GroundingError

router = APIRouter(prefix="/api/v1", tags=["distill"])


class DistillRequest(BaseModel):
    node_id: str
    scope: str = "node"          # node | subtree
    depth: int = Field(2, ge=0, le=10)   # bounded — an unbounded int would pin the worker
    confirm: bool = False


def _service() -> DistillService:
    s = load_settings()
    if s.mock_models:
        summarizer = MockSummarizer()
    elif s.anthropic_key:
        summarizer = AnthropicSummarizer(s.anthropic_key, s.summarizer_model, s.summarizer_max_tokens)
    else:
        raise HTTPException(status_code=400, detail=(
            "No ANTHROPIC_API_KEY configured (backend/.env) — set your key, or set "
            "SYNAPSE_MOCK_MODELS=1 to try the flow with the mock summarizer."))
    return DistillService(s.vault_path, summarizer, s.confirm_threshold_tokens)


@router.post("/distill")
def distill(req: DistillRequest) -> dict:
    try:
        return _service().distill(req.node_id, req.scope, req.depth, req.confirm)
    except ConfirmationRequired as c:
        return {"requires_confirmation": True, "tokens_est": c.tokens_est, "threshold": c.threshold}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    except GroundingError as e:
        raise HTTPException(status_code=422, detail=str(e))


class RedistillRequest(BaseModel):
    note_id: str             # the SUMMARY note — root/scope/depth ride its frontmatter
    confirm: bool = False


@router.post("/redistill")
def redistill(req: RedistillRequest) -> dict:
    """One-click re-distill of a (stale) summary — issue #9. The fresh write clears
    `synapse.stale` and re-records the source hashes; same spend gate as /distill."""
    try:
        return _service().redistill(req.note_id, req.confirm)
    except ConfirmationRequired as c:
        return {"requires_confirmation": True, "tokens_est": c.tokens_est, "threshold": c.threshold}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except GroundingError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── The seeing pass (sprint 05, Epic L) ─────────────────────────────────────────

class DescribeRequest(BaseModel):
    note_id: str
    confirm: bool = False


class DescribeAllRequest(BaseModel):
    confirm: bool = False


def _asset_source(note: dict):
    """Resolve the asset's original file from its root (same guard as GET /asset)."""
    from pathlib import Path

    from app.core.roots import load_roots
    root = next((e["path"] for e in load_roots(load_settings())
                 if Path(e["path"]).name == note["repo"]), None)
    if root is None:
        raise HTTPException(status_code=404, detail=f"Source root '{note['repo']}' is not configured.")
    root_p = Path(root).resolve()
    target = (root_p / note["source_path"]).resolve()
    if (root_p != target and root_p not in target.parents) or not target.is_file():
        raise HTTPException(status_code=404, detail=f"Asset file missing: {note['source_path']}")
    return target


def _describe_service():
    from modules.distill.src.providers import AnthropicVisionDescriber, MockVisionDescriber
    from modules.distill.src.service import DescribeService
    s = load_settings()
    if s.mock_models:
        describer = MockVisionDescriber()
    elif s.anthropic_key:
        describer = AnthropicVisionDescriber(
            s.anthropic_key, s.summarizer_model, s.summarizer_max_tokens)
    else:
        raise HTTPException(status_code=400, detail=(
            "No ANTHROPIC_API_KEY configured (backend/.env) — set your key, or set "
            "SYNAPSE_MOCK_MODELS=1 to try the flow with the mock describer."))
    return DescribeService(s.vault_path, describer, s.confirm_threshold_tokens)


@router.post("/describe")
def describe(req: DescribeRequest) -> dict:
    """Describe ONE asset sidecar (vision for images, text for PDFs) — cost-guarded."""
    from pathlib import Path

    from modules.graph.src.services import GraphService
    from app.core.vault_lock import vault_write_lock
    note = GraphService(load_settings().vault_path).read_note(req.note_id)
    if note is None or note.get("kind") != "asset":
        raise HTTPException(status_code=404, detail=f"No asset note '{req.note_id}' in the vault.")
    source = _asset_source(note)
    svc = _describe_service()
    try:
        with vault_write_lock(load_settings().vault_path):
            return svc.describe(req.note_id, source, confirm=req.confirm)
    except ConfirmationRequired as c:
        return {"requires_confirmation": True, "tokens_est": c.tokens_est, "threshold": c.threshold}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))


@router.post("/describe-all")
def describe_all(req: DescribeAllRequest) -> dict:
    """Describe EVERY not-yet-described asset. ALWAYS asks first (a 10k-photo library must
    never auto-spend): call without confirm to get the count + estimate."""
    from pathlib import Path

    from app.core.vault_lock import vault_write_lock
    from modules.distill.src.service import _AI_SECTION
    from modules.graph.src.services import GraphService
    svc = _describe_service()
    todo = svc.undescribed_assets()
    if not todo:
        return {"described": 0, "message": "every asset already has a description"}
    graph_svc = GraphService(load_settings().vault_path)
    if not req.confirm:
        # a REAL per-item estimate (GBU P2: 2600×N under-quoted PDFs 4-10×) + the
        # candidate block each call carries (~300 ids ≈ 4K tokens)
        est = 0
        for note_id in todo:
            note = graph_svc.read_note(note_id) or {}
            text = (note.get("body") or "").split(_AI_SECTION, 1)[0]
            est += svc.tokens_est(note.get("asset_type", "image"), text) + 4000
        return {"requires_confirmation": True, "count": len(todo), "tokens_est": est}
    done, failed = [], []
    for note_id in todo:
        try:
            note = graph_svc.read_note(note_id)
            source = _asset_source(note)
            with vault_write_lock(load_settings().vault_path):
                done.append(svc.describe(note_id, source, confirm=True))
        except (HTTPException, KeyError, OSError) as e:
            failed.append({"note_id": note_id, "error": str(getattr(e, "detail", e))})
    return {"described": len(done), "failed": failed, "results": done}
