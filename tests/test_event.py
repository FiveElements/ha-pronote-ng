"""The event entities, and the three checks that keep them apart.

``event.py`` exists because a state trigger cannot say "a *new* grade
arrived". The entity therefore listens on a single bus signal shared by every
event entity of every account on the instance, and decides for itself whether
a given signal is its own. The entry check was the one nothing exercised --
and it is the check that keeps two accounts on one instance from firing each
other's automations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import Event

from custom_components.carnet_scolaire.account import SIGNAL_DELTA
from custom_components.carnet_scolaire.const import EVENT_GRADE_ADDED, Tier
from custom_components.carnet_scolaire.event import EVENTS, PronoteEventEntity

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

pytestmark = REQUIRES_HASS


def _grade_event_entity(account: PronoteAccount) -> PronoteEventEntity:
    """The first child's new-grade event entity, as the platform builds it."""
    description = next(item for item in EVENTS if item.tier is Tier.MARKS)
    return PronoteEventEntity(
        account,
        account.coordinators[description.tier],
        account.students[0],
        description,
    )


def _signal(hass: HomeAssistant, **data: Any) -> Event:
    """One delta signal, as ``PronoteAccount`` fires it."""
    return Event(SIGNAL_DELTA, data)


async def test_a_signal_from_another_account_is_ignored(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """Two accounts on one instance share the bus and must not share events.

    A second family's entry, a second school, or the same school under two
    logins all publish on ``SIGNAL_DELTA``. Without the entry check every
    "new grade" automation would fire for every child on the instance, and the
    payload it inspected would name a child it has no entity for.
    """
    entity = _grade_event_entity(account)
    description = entity.entity_description

    entity._async_handle_delta(
        _signal(
            hass,
            entry_id="an-entry-that-is-not-ours",
            student_id=account.students[0].id,
            entity_key=description.key,
            event_type=EVENT_GRADE_ADDED,
        )
    )

    assert entity.state is None


async def test_a_signal_for_another_child_of_the_same_account_is_ignored(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """One parent account, two children, two sets of entities.

    The entry matches here, so only the student check can tell them apart --
    and getting it wrong would fire the elder's automation on the younger's
    grade, which reads as a correct integration doing the wrong thing.
    """
    entity = _grade_event_entity(account)
    description = entity.entity_description

    entity._async_handle_delta(
        _signal(
            hass,
            entry_id=mock_entry.entry_id,
            student_id=account.students[1].id,
            entity_key=description.key,
            event_type=EVENT_GRADE_ADDED,
        )
    )

    assert entity.state is None
