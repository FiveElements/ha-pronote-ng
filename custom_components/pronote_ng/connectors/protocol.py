from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

from ..const import Priority, Tier
from ..models import GatewayResult, SessionFacts


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


class SchoolConnector(Protocol):
    capabilities: ConnectorCapabilities

    def now(self) -> datetime: ...

    def today(self) -> date: ...

    async def async_open(self) -> None: ...

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
