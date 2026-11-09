"""Test-only routes, mounted only when env == "test" and telephony == "fake" (`include_test_routes`): the e2e suite
reads back what the fake Twilio was asked to do. The voice app is public-facing, so nothing here exists elsewhere."""

from typing import Any

from fastapi import APIRouter, FastAPI, Request
from supportcore.config import Settings

router = APIRouter(prefix="/test", include_in_schema=False)


@router.get("/fake-telephony/calls")
async def fake_telephony_calls(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Each create_call (target, caller id, webhooks, sid) and update_call (TwiML or end) the fake recorded."""
    return request.app.state.container.adapters.telephony.recorded_calls()


def include_test_routes(app: FastAPI, settings: Settings) -> None:
    if settings.env == "test" and settings.telephony == "fake":
        app.include_router(router)
