"""The connector seam: costs are declared, protocol objects do not leak."""

import pytest

from custom_components.pronote_ng.connectors.errors import ConnectorUnsupportedError
from custom_components.pronote_ng.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.pronote_ng.const import Priority, Tier
from custom_components.pronote_ng.models import GatewayResult, HomeworkFacts
from tests.fixtures.fake_connector import FakeConnector


def test_a_capability_set_that_omits_menus_does_not_list_that_tier() -> None:
    """An Ecoledirecte-shaped connector must not be asked for a canteen page."""
    caps = ConnectorCapabilities(
        source=Source.ECOLEDIRECTE,
        tiers=frozenset(
            {Tier.TIMETABLE, Tier.HOMEWORK, Tier.MARKS, Tier.ATTENDANCE}
        ),
        writes=frozenset(),
        services=frozenset(),
    )
    assert Tier.MENUS not in caps.tiers
    assert caps.source is Source.ECOLEDIRECTE


def test_gateway_result_carries_the_declared_cost_next_to_the_facts() -> None:
    """A cost inferred after the fact is how a lazy property used to double a bill."""
    facts = HomeworkFacts(homework=())
    result = GatewayResult(facts, calls=1)
    assert result.calls == 1
    assert result.facts is facts


@pytest.mark.asyncio
async def test_collecting_a_supported_tier_returns_the_cost_the_fake_declared() -> None:
    """The seam's only number that matters is the one the connector claims."""
    connector = FakeConnector(
        facts_by_tier={Tier.HOMEWORK: HomeworkFacts(homework=())},
        cost_by_tier={Tier.HOMEWORK: 1},
    )
    result = await connector.async_collect(
        Tier.HOMEWORK, "STUDENT-1", priority=Priority.HIGH
    )
    assert result.calls == 1
    assert result.facts.homework == ()


@pytest.mark.asyncio
async def test_collecting_an_unsupported_tier_is_a_caller_bug() -> None:
    """Capabilities exist so account.py never asks; if it does, that must scream."""
    connector = FakeConnector()
    with pytest.raises(ConnectorUnsupportedError):
        await connector.async_collect(Tier.MENUS, "STUDENT-1", priority=Priority.LOW)


@pytest.mark.asyncio
async def test_collecting_session_is_a_caller_bug() -> None:
    """SESSION is not a collectable tier; collect_tier already raises ValueError."""
    connector = FakeConnector()
    with pytest.raises(ConnectorUnsupportedError):
        await connector.async_collect(Tier.SESSION, "STUDENT-1", priority=Priority.HIGH)
