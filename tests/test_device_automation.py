"""Device triggers, conditions and actions -- the surface §2.3 is about.

This is what the project is *for*: "when a lesson is cancelled, for this child"
has to be one card in the automation editor, not a Jinja template over an event
bus. Which makes the failure mode of this layer particularly nasty -- a trigger
that never attaches, or a condition that is never offered, produces no error
anywhere. The automation simply stops firing, and the user concludes the
integration is broken without a single line in the log.

Two of those had actually happened, and each has a test here:

* every condition whose key contains an underscore was silently absent from
  the editor, because the entity key was extracted with ``rpartition("_")`` --
  which answers ``day`` for ``school_day``. Eight of the ten conditions were
  invisible, and the two that survived were the two single-word keys;
* the write actions are gated on the option, but a hand-written automation must
  still be refused by the service rather than by the editor's filtering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components import automation
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest

from custom_components.pronote_ng.const import (
    DOMAIN,
    EVENT_GRADE_ADDED,
    EVENT_LESSON_CANCELED,
    OPT_WRITE_OPERATIONS_ENABLED,
)
from custom_components.pronote_ng.device_action import (
    ACTION_TYPES,
    WRITE_ACTIONS,
    async_get_actions,
)
from custom_components.pronote_ng.device_condition import (
    CONDITION_MAP,
    CONDITION_SCHEMA,
    async_condition_from_config,
    async_get_conditions,
)
from custom_components.pronote_ng.device_trigger import (
    TRIGGER_TYPES,
    async_get_triggers,
)

from .conftest import CHILDREN, REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pronote_ng.account import PronoteAccount

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)


def _device_id(hass: HomeAssistant, entry_id: str, identifier: str) -> str:
    """A registry device id, by our own identifier, within its entry.

    The entry is required rather than incidental: from HA 2026.9 identifiers
    are unique only within a config entry, and the entry-less lookup is
    deprecated for being ambiguous.
    """
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry_id
    )
    assert device is not None, f"no device for {identifier}"
    return device.id


def _child(hass: HomeAssistant, entry: MockConfigEntry, student: str) -> str:
    return _device_id(hass, entry.entry_id, f"{entry.entry_id}_{student}")


def _account_device(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    # The account device's identifier *is* the bare entry id, so the entry is
    # passed twice on purpose: once as the owner, once as the identifier.
    return _device_id(hass, entry.entry_id, entry.entry_id)


async def _fire(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    student_id: str,
    event_type: str,
    entity_key: str = "lesson_changed",
) -> None:
    """Fire one delta on the bus, the way the account does."""
    from custom_components.pronote_ng.account import SIGNAL_DELTA

    hass.bus.async_fire(
        SIGNAL_DELTA,
        {
            "entry_id": entry.entry_id,
            "student_id": student_id,
            "entity_key": entity_key,
            "event_type": event_type,
            "attributes": {"subject": "Mathématiques"},
        },
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


async def test_every_change_type_is_offered_on_a_child(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """One trigger per event type, not per entity.

    A user thinking "a room changed" should not have to know that four
    different changes share one ``event`` entity (annexe A §4).
    """
    triggers = await async_get_triggers(hass, _child(hass, mock_entry, STUDENT_ONE))

    assert {trigger["type"] for trigger in triggers} == set(TRIGGER_TYPES)
    assert all(trigger["domain"] == DOMAIN for trigger in triggers)


async def test_the_account_device_offers_no_change_triggers(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """ "A grade arrived" does not happen to an account; it happens to a child.

    The account device carries the budget diagnostics, and offering it a
    "new grade" trigger would produce a card that can never fire.
    """
    assert await async_get_triggers(hass, _account_device(hass, mock_entry)) == []


async def test_a_trigger_fires_for_its_own_child_only(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The defect a one-child test cannot see, on the automation side.

    Both children share an entry, a session and an event bus, so the trigger's
    event data has to match on the student as well as on the entry and the
    type. Matching on the type alone made every automation fire for every
    child: "wake me when Enfant Un's first lesson is cancelled" also fired for
    Enfant Deux.
    """
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "alias": "child one",
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": _child(hass, mock_entry, STUDENT_ONE),
                        "type": EVENT_LESSON_CANCELED,
                    },
                    "action": {
                        "event": "test_fired",
                        "event_data": {"who": "one"},
                    },
                }
            ]
        },
    )
    fired: list[Any] = []
    hass.bus.async_listen("test_fired", lambda event: fired.append(event.data))

    await _fire(
        hass, mock_entry, student_id=STUDENT_TWO, event_type=EVENT_LESSON_CANCELED
    )
    assert fired == []

    await _fire(
        hass, mock_entry, student_id=STUDENT_ONE, event_type=EVENT_LESSON_CANCELED
    )
    assert fired == [{"who": "one"}]

    # And the type has to match too: a grade is not a cancellation.
    await _fire(hass, mock_entry, student_id=STUDENT_ONE, event_type=EVENT_GRADE_ADDED)
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


async def test_every_condition_is_offered(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The regression that hid eight of the ten conditions.

    The entity key was recovered from the unique id with ``rpartition("_")``,
    which keeps only the last segment: ``day`` for ``school_day``, ``class``
    for ``in_class``, ``progress`` for ``absence_in_progress``. Only the two
    single-word keys matched, so the automation editor offered a child exactly
    one condition -- "Holidays" -- and the module's own documented example,
    "only if it is a school day", could not be written at all.
    """
    conditions = await async_get_conditions(hass, _child(hass, mock_entry, STUDENT_ONE))

    assert {condition["type"] for condition in conditions} == set(CONDITION_MAP)
    for condition in conditions:
        assert condition["entity_id"]
        assert condition["domain"] == DOMAIN


async def test_a_condition_reads_the_binary_sensor_it_names(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Both polarities, and both actually evaluated.

    Offering ``is_not_school_day`` as well as ``is_school_day`` is one fewer
    place for a mistake than negating a condition in the editor -- but only if
    the two really do read the same entity and answer opposite questions.
    """
    conditions = {
        item["type"]: item
        for item in await async_get_conditions(
            hass, _child(hass, mock_entry, STUDENT_ONE)
        )
    }
    entity_id = conditions["is_school_day"]["entity_id"]

    # Through the module's own entry point, with the schema applied first --
    # which is how `device_automation` reaches it. Going via
    # `homeassistant.helpers.condition.async_from_config` would test Home
    # Assistant's dispatch rather than this integration's mapping.
    positive = async_condition_from_config(
        hass, CONDITION_SCHEMA(conditions["is_school_day"])
    )
    negative = async_condition_from_config(
        hass, CONDITION_SCHEMA(conditions["is_not_school_day"])
    )

    hass.states.async_set(entity_id, STATE_ON)
    assert positive(hass, {}) is True
    assert negative(hass, {}) is False

    hass.states.async_set(entity_id, STATE_OFF)
    assert positive(hass, {}) is False
    assert negative(hass, {}) is True


async def test_a_condition_is_not_offered_without_its_entity(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """An establishment that publishes nothing has no entity to read.

    Offering a condition that can never be true would be worse than offering
    nothing (§2.5), so the account device -- which has no child binary sensors
    at all -- gets an empty list rather than ten dead cards.
    """
    assert await async_get_conditions(hass, _account_device(hass, mock_entry)) == []


async def test_a_hand_written_condition_resolves_its_own_entity(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """A blueprint carries the device and the type, and nothing else.

    ``entity_id`` is optional in the schema and the editor always fills it in,
    so every path anybody exercised had it -- while a condition written by hand
    or delivered in a blueprint raised ``KeyError`` inside the automation. What
    the user sees is "this automation is broken", with no indication of which
    line, and the condition can never be satisfied.
    """
    config = CONDITION_SCHEMA(
        {
            "condition": "device",
            "domain": DOMAIN,
            "device_id": _child(hass, mock_entry, STUDENT_ONE),
            "type": "is_school_day",
        }
    )
    checker = async_condition_from_config(hass, config)

    resolved = {
        item["type"]: item["entity_id"]
        for item in await async_get_conditions(
            hass, _child(hass, mock_entry, STUDENT_ONE)
        )
    }
    hass.states.async_set(resolved["is_school_day"], STATE_ON)

    assert checker(hass, {}) is True


async def test_a_condition_whose_entity_does_not_exist_says_so(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Rather than answering "false" for ever.

    A condition that quietly evaluates to false disarms every automation
    containing it, silently. An error at least appears in the trace.
    """
    config = CONDITION_SCHEMA(
        {
            "condition": "device",
            "domain": DOMAIN,
            "device_id": _account_device(hass, mock_entry),
            "type": "is_school_day",
        }
    )

    with pytest.raises(HomeAssistantError, match="school_day"):
        async_condition_from_config(hass, config)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def test_the_write_actions_are_hidden_until_writes_are_enabled(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Hidden in the editor, and -- separately -- refused by the service.

    Filtering the list is a courtesy, not the guard: an automation written by
    hand, or imported from a blueprint, still reaches
    ``async_call_action_from_config``. The refusal lives in the service
    (§8.3), which is what ``test_services.py`` pins.
    """
    device_id = _child(hass, mock_entry, STUDENT_ONE)

    offered = {action["type"] for action in await async_get_actions(hass, device_id)}
    assert offered == set(ACTION_TYPES) - set(WRITE_ACTIONS)

    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()

    offered = {action["type"] for action in await async_get_actions(hass, device_id)}
    assert offered == set(ACTION_TYPES)


async def test_an_unknown_device_offers_nothing_at_all(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """Rather than raising inside the automation editor."""
    assert await async_get_actions(hass, "not-a-device") == []
    assert await async_get_conditions(hass, "not-a-device") == []
    assert await async_get_triggers(hass, "not-a-device") == []


@pytest.mark.parametrize(
    ("action_type", "expected_service", "extra"),
    [
        pytest.param("refresh", "refresh", {}, id="refresh"),
        pytest.param("refresh_marks", "refresh", {}, id="refresh-marks"),
    ],
)
async def test_a_refresh_action_calls_the_matching_service(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    action_type: str,
    expected_service: str,
    extra: dict[str, Any],
) -> None:
    """Delegated to the service, so the limiter and the scheduler still decide.

    A device action that reached the gateway directly would be a second path to
    the network, and annexe B §6 gives the limiter a monopoly on that.
    """
    from custom_components.pronote_ng.device_action import (
        async_call_action_from_config,
    )

    calls: list[Any] = []
    hass.bus.async_listen(
        "call_service",
        lambda event: calls.append(event.data)
        if event.data.get("domain") == DOMAIN
        else None,
    )

    await async_call_action_from_config(
        hass,
        {
            "domain": DOMAIN,
            "device_id": _child(hass, mock_entry, STUDENT_ONE),
            "type": action_type,
            **extra,
        },
        {},
        None,
    )
    await hass.async_block_till_done()

    assert [call["service"] for call in calls] == [expected_service]
