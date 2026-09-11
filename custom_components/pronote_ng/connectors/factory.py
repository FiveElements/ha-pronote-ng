from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.pronote_ng.const import CONF_SOURCE

from .pronote import PronoteConnector
from .protocol import SchoolConnector, Source

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.pronote_ng import PronoteConfigEntry


def source_from_entry_data(data: dict[str, Any]) -> Source:
    """Return the configured school backend, defaulting old entries to PRONOTE."""
    return Source(data.get(CONF_SOURCE, Source.PRONOTE))


def build_connector(
    hass: HomeAssistant,
    entry: PronoteConfigEntry,
    **deps: Any,
) -> SchoolConnector:
    """Build the connector selected by the config entry source."""
    del hass
    source = source_from_entry_data(dict(entry.data))
    if source is Source.PRONOTE:
        return PronoteConnector(**deps)
    if source is Source.ECOLEDIRECTE:
        raise ValueError("ecoledirecte connector is not wired yet")
    raise ValueError(f"unsupported source: {source}")
