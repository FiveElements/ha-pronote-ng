"""A to-do list for the homework, with a real checkbox.

Ticking an item posts ``SaisieTAFFaitEleve`` to PRONOTE, so the establishment
sees it too. That is the only write in the whole integration reachable without
a service call, which is exactly why it is gated: with write operations off --
the default (§8.3) -- the list is read-only and says so through its supported
features rather than by failing when tapped.

The tick is applied locally the moment the server accepts it, and the homework
tier is asked for a refresh rather than re-read immediately: the local truth is
already correct, and an instant re-fetch would double the cost of every
checkbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN, Priority, Tier
from .entity import PronoteEntity
from .ratelimit import TierDeferred

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .account import PronoteAccount
    from .coordinator import PronoteTierCoordinator
    from .models import Student

#: One at a time. Entities on this platform *act*: they place a real write
#: against the school's server. The limiter already serialises the wire under
#: its own lock, but declaring it at the platform level costs nothing and is
#: the layer Home Assistant itself honours -- zero here would let a script that
#: ticks off six homework items fire six writes at once.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one homework list per child."""
    account = entry.runtime_data
    coordinator = account.coordinators.get(Tier.HOMEWORK)
    if coordinator is None:
        return
    async_add_entities(
        PronoteHomeworkTodoList(account, coordinator, student)
        for student in account.students
    )


class PronoteHomeworkTodoList(PronoteEntity, TodoListEntity):
    """The child's homework, as a list that can be ticked."""

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
    ) -> None:
        super().__init__(account, coordinator, student, "homework")
        # Declared from the option rather than always: a checkbox that appears
        # tappable and then refuses is worse than one that is visibly
        # read-only. Changing the option reloads the entry, so this is
        # re-evaluated (§7.3).
        self._attr_supported_features = (
            TodoListEntityFeature.UPDATE_TODO_ITEM
            if account.write_enabled
            else TodoListEntityFeature(0)
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """The homework items, or ``None`` while nothing has been collected."""
        facts = self.facts
        if facts is None:
            return None
        return [
            TodoItem(
                uid=item.id,
                summary=item.subject or "?",
                description=item.description or None,
                due=item.due,
                status=(
                    TodoItemStatus.COMPLETED
                    if item.done
                    else TodoItemStatus.NEEDS_ACTION
                ),
            )
            for item in facts.homework
        ]

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Apply a tick or an untick to PRONOTE.

        Only the status is sent. PRONOTE owns the subject, the wording and the
        deadline of a homework item -- a client that also pushed those would be
        overwriting a teacher.
        """
        if not self.account.write_enabled:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="writes_disabled",
            )
        if item.uid is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="todo_item_unknown",
            )

        done = item.status == TodoItemStatus.COMPLETED
        homework_id = item.uid
        account = self.account

        def work(client: Any) -> int:
            return account.gateway.set_homework_done(client, homework_id, done=done)

        try:
            await account.session.run(
                str(Tier.HOMEWORK),
                # A human just tapped a checkbox, so this outranks a scheduled
                # collection -- but it still goes through the limiter, and it
                # is never CRITICAL: nothing done by hand may pre-empt the
                # session tier (annexe B §2.4).
                Priority.HIGH,
                work,
                student_id=self.student.id,
                cost=1,
            )
        except TierDeferred as deferred:
            # A tick that was silently postponed reads as a tick that did not
            # work, so this fails visibly instead.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="service_deferred",
                translation_placeholders={
                    "reason": str(deferred.reason),
                    "seconds": str(int(deferred.retry_after)),
                },
            ) from deferred

        account.scheduler.request([Tier.HOMEWORK])
        self.async_write_ha_state()
