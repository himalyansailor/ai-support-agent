"""Round-robin: the first candidate after the last assigned agent, wrapping around."""

from uuid import UUID

from supportcore.db.models import Membership


class RoundRobinStrategy:
    async def pick(self, candidates: list[Membership], last_assigned: UUID | None) -> Membership | None:
        """`candidates` are ordered by (created_at, id); `last_assigned` is the candidate to continue after
        (AssignmentService maps a last assignee who is no longer available onto the candidate before it).
        Unknown or None: the first candidate."""
        if not candidates:
            return None
        index = next((i for i, m in enumerate(candidates) if m.id == last_assigned), None)
        return candidates[0] if index is None else candidates[(index + 1) % len(candidates)]
