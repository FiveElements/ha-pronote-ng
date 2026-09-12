from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from ..const import Tier  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    from ..const import Priority  # noqa: TID252
    from ..models import GatewayResult, SessionFacts  # noqa: TID252


class Source(StrEnum):
    PRONOTE = "pronote"
    ECOLEDIRECTE = "ecoledirecte"


class ChallengeKind(StrEnum):
    PIN = "pin"
    QCM = "qcm"


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    source: Source
    tiers: frozenset[Tier]
    writes: frozenset[str]
    services: frozenset[str]


def has_pronote_extras(connector: object) -> bool:
    """Whether the connector owns the PRONOTE session/gateway façade."""
    return hasattr(connector, "session") and hasattr(connector, "gateway")


class SchoolConnector(Protocol):
    capabilities: ConnectorCapabilities

    def now(self) -> datetime: ...

    def today(self) -> date: ...

    async def async_open(self) -> None: ...

    async def async_load_session_facts(self, student_ids: Sequence[str]) -> None: ...

    def session_facts(self, student_id: str) -> SessionFacts: ...

    def student_ids(self) -> tuple[str, ...]: ...

    async def async_collect(
        self,
        tier: Tier,
        student_id: str,
        *,
        priority: Priority,
    ) -> GatewayResult[Any]: ...

    async def async_close(self) -> None: ...

    def diagnostics(self) -> dict[str, Any]: ...
