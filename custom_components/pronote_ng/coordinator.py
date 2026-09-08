"""One coordinator per tier, driven by the scheduler rather than by a timer.

``update_interval`` is ``None`` on purpose: the scheduler decides what is due
and calls ``async_set_updated_data()``. An entity subscribes only to its own
tier's coordinator, so collecting the canteen menus does not rewrite the
timetable's state (§5.3).

Data is keyed by student because one config entry is one *account*, and a parent
account holds several children who share a session, a budget and an IP address
(§7.1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, Tier

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import Snapshot

_LOGGER: Final = logging.getLogger(__name__)

#: A tier's payload for every student on the account.
TierData = dict[str, "Snapshot[Any]"]


class PronoteTierCoordinator(DataUpdateCoordinator[TierData]):
    """Holds one tier's latest successful snapshot, per student.

    There is no ``_async_update_data``: this coordinator is never allowed to
    fetch on its own. A coordinator that could would be a second path to the
    network, and annexe B §6 gives the limiter a monopoly on that.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tier: Tier,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id[:8]}:{tier}",
            update_interval=None,
            config_entry=entry,
            always_update=False,
        )
        self.tier = tier
        self.data = {}

    def snapshot_for(self, student_id: str) -> Snapshot[Any] | None:
        """The latest snapshot for one student, or ``None``."""
        if not self.data:
            return None
        return self.data.get(student_id)

    def publish(self, student_id: str, snapshot: Snapshot[Any]) -> None:
        """Publish one student's snapshot, leaving the others untouched.

        A failed collection must never overwrite a good snapshot: entities keep
        their last known value and report its age instead (§4.4, §5.4). That is
        enforced simply by this being the only writer and only ever being called
        after a success.
        """
        merged: TierData = dict(self.data or {})
        merged[student_id] = snapshot
        self.async_set_updated_data(merged)

    def note_failure(self, error: Exception) -> None:
        """Record a failure without discarding the data.

        ``last_update_success`` flips, so Home Assistant logs a single line at
        the success-to-failure transition rather than a traceback per attempt
        (§5.3), while ``self.data`` stays exactly as it was.
        """
        self.async_set_update_error(error)
