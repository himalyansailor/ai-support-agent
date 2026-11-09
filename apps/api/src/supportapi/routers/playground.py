"""Knowledge base playground: test retrieval and the answer the bot would give (kb.manage)."""

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, build_playground_service, get_container, require_permission
from supportapi.schemas.playground import PlaygroundOut, PlaygroundRequest
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["playground"])


@router.post("/orgs/{org_id}/kbs/{kb_id}/playground", response_model=PlaygroundOut)
async def run_playground(
    session: SessionDep,
    kb_id: UUID,
    body: PlaygroundRequest,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> PlaygroundOut:
    service = build_playground_service(session, ctx.organisation.id, container)
    result = await service.run(kb_id, body.question, body.top_k, body.with_answer)
    return PlaygroundOut.model_validate(asdict(result))
