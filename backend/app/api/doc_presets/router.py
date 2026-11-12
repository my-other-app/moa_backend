"""The ops check for the render service (D9): /health stays independent of it."""
from fastapi import APIRouter

from app.api.doc_presets import engine
from app.core.auth.dependencies import AdminAuth

router = APIRouter(tags=["admin-ops"])


@router.get("/admin/ops/render", summary="Whether the render service draws documents now")
async def render_check(user: AdminAuth) -> dict:
    """Draws a tiny page through the render service (moa-render) and says how
    it went: {ok, error, ms}. Down, tickets and certificates are drawn by the
    classic renderer and ID cards cannot print; the admins are alerted
    (render_unavailable) the first time a document needs it."""
    return await engine.check()
