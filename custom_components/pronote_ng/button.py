"""Buttons: ask for a collection, do not perform one.

Pressing a button raises the priority of some tiers in the scheduler and wakes
the master tick. It does **not** call PRONOTE itself, and it does not get a
dispensation from the limiter. That distinction is the whole design: a button
that fetched directly would be a second path to the network, and a parent
pressing it ten times while waiting for a grade would place ten batches. Here
ten presses cost one batch, because the second press finds the tiers already
requested and the tick already running (§5.1, annexe B §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .const import Tier
from .entity import PronoteEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .account import PronoteAccount
    from .coordinator import PronoteTierCoordinator
    from .models import Student


@dataclass(frozen=True, kw_only=True)
class PronoteButtonDescription(ButtonEntityDescription):
    """A button and the tiers it asks for."""

    #: The tier whose coordinator this button subscribes to.
    tier: Tier
    #: What pressing it requests. ``None`` means "everything except the
    #: session": forcing a re-login by hand is the one gesture that can get an
    #: address suspended, so no button offers it (annexe B §3).
    requests: tuple[Tier, ...] | None = None


BUTTONS: Final[tuple[PronoteButtonDescription, ...]] = (
    PronoteButtonDescription(key="refresh", tier=Tier.TIMETABLE, requests=None),
    PronoteButtonDescription(
        key="refresh_marks", tier=Tier.MARKS, requests=(Tier.MARKS,)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the refresh buttons for every child."""
    account = entry.runtime_data
    entities: list[ButtonEntity] = []
    for student in account.students:
        for description in BUTTONS:
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None:
                continue
            entities.append(
                PronoteRefreshButton(account, coordinator, student, description)
            )
    async_add_entities(entities)


class PronoteRefreshButton(PronoteEntity, ButtonEntity):
    """Requests a priority pass from the scheduler."""

    entity_description: PronoteButtonDescription

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        description: PronoteButtonDescription,
    ) -> None:
        super().__init__(account, coordinator, student, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Always pressable.

        Deliberately not the base class's staleness rule: a stale integration
        is precisely when somebody wants to ask for a refresh, and a greyed-out
        button at that moment would be the opposite of helpful.
        """
        return True

    async def async_press(self) -> None:
        """Boost the requested tiers and wake the heartbeat."""
        requested = self.entity_description.requests
        tiers = (
            list(requested)
            if requested is not None
            else [tier for tier in Tier if tier is not Tier.SESSION]
        )
        self.account.scheduler.request(tiers)
        await self.account.async_request_tick()
