"""Admin panel. Every route requires the admin;
destructive and money actions re-prompt for the admin's password. One module per area."""

from fastapi import APIRouter, Depends

from app.web.admin import events, integrations, players, props, system
from app.web.security import require_admin


def build_router() -> APIRouter:
    router = APIRouter(
        prefix="/admin", include_in_schema=False, dependencies=[Depends(require_admin)]
    )
    for area in (players, props, events, system, integrations):
        area.register(router)
    return router
