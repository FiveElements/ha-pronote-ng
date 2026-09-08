"""Shared entity base: device, availability, staleness, translation.

Two behaviours are load-bearing here.

**Staleness rather than unavailability (§5.4, §2.5).** An entity becomes
``unavailable`` only if it never had data, or if the last snapshot has aged past
``stale_after`` times its tier's interval. In between it keeps its value and
flags its age. A ``numeric_state`` trigger on an entity that goes
``unavailable`` and comes back fires spuriously, so keeping the last known value
and marking it dated is safer than admitting ignorance every ten minutes.

**Clock-driven re-evaluation (annexe A §3.1).** Entities whose state depends on
the *time* and not only on the data -- "in class", "absent right now", "next
lesson", "wake-up" -- schedule a point-in-time callback at their next known
transition. Without it they change state at their tier's pace, up to fifteen
minutes late, which on a wake-up sensor removes the entire point of the sensor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNRECORDED_LIST_ATTRIBUTES, Tier

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE

    from .account import PronoteAccount
    from .coordinator import PronoteTierCoordinator, TierData
    from .models import Snapshot, Student


class PronoteEntity(CoordinatorEntity["PronoteTierCoordinator"]):
    """Base for every entity attached to a child's device."""

    _attr_has_entity_name = True
    # Every list attribute is unrecorded: PRONOTE lists blow past the
    # recorder's 16 KiB attribute limit, and the history worth keeping is the
    # count, not the payload (§9).
    _unrecorded_attributes = frozenset({*UNRECORDED_LIST_ATTRIBUTES, "fetched_at"})

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self.account = account
        self.student = student
        self._key = key

        # Config entry, then the PRONOTE identifier, then a stable functional
        # key -- never a label, a period name or a rank. An automation that
        # breaks in September because a sensor was renamed is a regression even
        # if no code failed (§2.4).
        #
        # The entry id is part of it because the entry's own `unique_id` is the
        # *account*, so two entries following the same child are legitimate: a
        # mother's account and a father's, or a parent account alongside the
        # child's own. And `student.id` is PRONOTE's resource number `N`, which
        # is only unique within one establishment's database. Without this
        # prefix the registry does not reject the second entry -- it *re-points*
        # the existing entity at it, moving the entity to the other entry's
        # device and silently breaking every automation that referenced it.
        self._attr_unique_id = f"{account.entry.entry_id}_{student.id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _student_device(account, student)

    @property
    def tier(self) -> Tier:
        """The tier feeding this entity."""
        return self.coordinator.tier

    @property
    def snapshot(self) -> Snapshot[Any] | None:
        """The latest successful snapshot for this student and tier."""
        return self.coordinator.snapshot_for(self.student.id)

    @property
    def facts(self) -> Any | None:
        """The payload of the latest snapshot, or ``None``."""
        snapshot = self.snapshot
        return snapshot.data if snapshot else None

    @property
    def available(self) -> bool:
        """Unavailable only when there is no data, or the data is too old.

        Note what is *not* consulted: ``coordinator.last_update_success``. A
        failed collection leaves the previous snapshot in place on purpose, and
        letting a transient failure flip availability is exactly the spurious
        trigger §2.5 exists to prevent.
        """
        if self.snapshot is None:
            return False
        return not self.account.is_stale(self.tier)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Age of the data, and whether it is considered stale."""
        snapshot = self.snapshot
        if snapshot is None:
            return {}
        return {
            "fetched_at": snapshot.fetched_at.isoformat(),
            "stale": self.account.is_stale(self.tier),
        }


class PronoteAccountEntity(CoordinatorEntity["PronoteTierCoordinator"]):
    """Base for diagnostic entities attached to the *account* device.

    The budget is shared between the children of one account, because the server
    sees one session and one IP address (§7.1), so these belong to the account
    and not to a child.
    """

    _attr_has_entity_name = True
    # A sibling of `PronoteEntity`, not a subclass, so this has to be repeated
    # rather than inherited -- and it was not. The diagnostic sensors carry
    # `by_tier` (a mapping) and `tiers_due` (a list), which the recorder was
    # storing on every state write despite both being named in
    # `UNRECORDED_LIST_ATTRIBUTES`.
    _unrecorded_attributes = frozenset({*UNRECORDED_LIST_ATTRIBUTES, "fetched_at"})

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self.account = account
        self._attr_unique_id = f"{account.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _account_device(account)

    @property
    def available(self) -> bool:
        """Diagnostics are always available: they describe the integration."""
        return True


class ClockDrivenMixin:
    """Re-evaluates an entity at a wall-clock instant rather than on a poll.

    ``async_track_point_in_time`` rather than a scan interval, because the
    transition is known in advance: a lesson ends at a specific minute, an
    absence covers a specific window. Polling would either be wasteful or late,
    and late is what makes a wake-up sensor useless.
    """

    hass: Any
    _clock_unsub: CALLBACK_TYPE | None = None

    def _schedule_next_transition(
        self,
        moment: datetime | None,
        callback_fn: Callable[[datetime], None],
    ) -> None:
        """Arm a single callback at ``moment``, replacing any pending one."""
        self._cancel_transition()
        if moment is None:
            return
        self._clock_unsub = async_track_point_in_time(self.hass, callback_fn, moment)

    def _cancel_transition(self) -> None:
        """Cancel a pending transition callback."""
        if self._clock_unsub is not None:
            self._clock_unsub()
            self._clock_unsub = None


def _student_device(account: PronoteAccount, student: Student) -> DeviceInfo:
    """The device carrying one child's entities.

    A device per child under a parent device carrying the establishment: that is
    what makes the device triggers of §2.3 readable, because "when a lesson is
    cancelled" has to be asked about *somebody*.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{account.entry.entry_id}_{student.id}")},
        name=student.name,
        manufacturer="PRONOTE",
        model=student.class_name or None,
        via_device=(DOMAIN, account.entry.entry_id),
    )


def _account_device(account: PronoteAccount) -> DeviceInfo:
    """The parent device carrying the establishment and the diagnostics."""
    return DeviceInfo(
        identifiers={(DOMAIN, account.entry.entry_id)},
        name=account.establishment_name,
        manufacturer="PRONOTE",
        model="Account",
        entry_type=None,
    )


def student_data(coordinator: PronoteTierCoordinator) -> TierData:
    """The coordinator's data, or an empty mapping."""
    return coordinator.data or {}
