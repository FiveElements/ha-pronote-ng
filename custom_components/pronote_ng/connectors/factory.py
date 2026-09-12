from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ..const import CONF_SOURCE  # noqa: TID252
from .ecoledirecte.connector import EcoledirecteConnector
from .pronote import PronoteConnector
from .protocol import SchoolConnector, Source

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .. import PronoteConfigEntry  # noqa: TID252


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
        return cast("SchoolConnector", PronoteConnector(**deps))
    if source is Source.ECOLEDIRECTE:
        return cast("SchoolConnector", EcoledirecteConnector(**deps))
    raise ValueError(f"unsupported source: {source}")
