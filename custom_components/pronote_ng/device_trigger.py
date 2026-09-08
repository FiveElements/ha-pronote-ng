"""Device triggers: the automation editor's half of §2.3.

Without this file, "when a lesson is cancelled for Camille" is something a user
has to build out of an event entity and a state condition. With it, they pick
the child's device and choose the trigger from a list. That is the difference
between an integration that *can* be automated and one that is designed to be.

Every trigger here is the same underlying mechanism as the ``event`` entities:
the account fires one bus signal per detected change, after the snapshots are
published. This module simply exposes that signal through the device-automation
contract, filtered on the entry, the child and the event type -- so the trigger
fires once, for the right child, and its ``trigger.event.data.attributes``
carries the change (annexe A §4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .account import SIGNAL_DELTA
from .const import (
    DOMAIN,
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_EVALUATION_ADDED,
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_INFORMATION_ADDED,
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_MOVED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_STATUS_CHANGED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_PUNISHMENT_ADDED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
)

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
    from homeassistant.helpers.typing import ConfigType

#: One trigger per event type, not per entity. A user thinking "a room
#: changed" should not have to know that four different changes share one
#: ``event`` entity (annexe A §4).
TRIGGER_TYPES: Final = (
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_MOVED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
    EVENT_LESSON_STATUS_CHANGED,
    EVENT_INFORMATION_ADDED,
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_PUNISHMENT_ADDED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_EVALUATION_ADDED,
)

TRIGGER_SCHEMA: Final = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer every change type, for a child's device only.

    The account device is deliberately excluded: it carries the budget
    diagnostics, and "a grade arrived" is not something that happens to an
    account -- it happens to a child (§2.3).
    """
    if _student_id(hass, device_id) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach by delegating to the core event trigger.

    Delegated rather than reimplemented so the trigger inherits everything the
    core one already gets right -- variable rendering, tracing, referenced-entity
    reporting -- and so this module holds no listener bookkeeping of its own.
    """
    device_id = config[CONF_DEVICE_ID]
    student_id = _student_id(hass, device_id)
    entry_id = _entry_id(hass, device_id)

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: SIGNAL_DELTA,
            # All three keys matter: the entry separates two accounts on one
            # instance, the student separates two children of one parent
            # account, and the type separates the twelve changes.
            event_trigger.CONF_EVENT_DATA: {
                "entry_id": entry_id,
                "student_id": student_id,
                "event_type": config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )


async def async_get_trigger_capabilities(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the device-automation contract
    config: ConfigType,  # noqa: ARG001 -- every trigger here takes no extra field
) -> dict[str, vol.Schema]:
    """No extra fields.

    A threshold on a grade or a subject filter belongs in the automation's
    condition, not here: baking it into the trigger would make the trigger
    fire-or-not invisible in a trace.
    """
    return {"extra_fields": vol.Schema({})}


def _identifier(hass: HomeAssistant, device_id: str) -> str | None:
    """This integration's identifier for a device, or ``None``."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    return next(
        (identifier for domain, identifier in device.identifiers if domain == DOMAIN),
        None,
    )


def _entry_id(hass: HomeAssistant, device_id: str) -> str | None:
    """The config entry a device belongs to."""
    identifier = _identifier(hass, device_id)
    if identifier is None:
        return None
    entry_id, _, _student = identifier.partition("_")
    return entry_id


def _student_id(hass: HomeAssistant, device_id: str) -> str | None:
    """The child a device belongs to, or ``None`` for the account device.

    Device identifiers are ``<entry_id>_<student_id>`` for a child and a bare
    ``<entry_id>`` for the account, so the presence of the suffix is the test.
    """
    identifier = _identifier(hass, device_id)
    if identifier is None:
        return None
    _entry, separator, student_id = identifier.partition("_")
    return student_id if separator and student_id else None
