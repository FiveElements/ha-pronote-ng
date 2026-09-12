"""The connector seam: costs are declared, protocol objects do not leak."""

from types import SimpleNamespace

import pytest

from custom_components.pronote_ng.connectors.errors import ConnectorUnsupportedError
from custom_components.pronote_ng.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.pronote_ng.const import Priority, Tier
from custom_components.pronote_ng.models import GatewayResult, HomeworkFacts
from tests.fixtures.fake_connector import FakeConnector


def test_an_entry_without_a_source_key_is_pronote() -> None:
    """Existing installs must not wake up as Ecoledirecte."""
    from custom_components.pronote_ng.connectors.factory import source_from_entry_data
    from custom_components.pronote_ng.connectors.protocol import Source

    assert source_from_entry_data({}) is Source.PRONOTE
    assert source_from_entry_data({"source": "ecoledirecte"}) is Source.ECOLEDIRECTE


def test_building_an_ecoledirecte_connector_uses_the_ready_client() -> None:
    """The factory must select ED without constructing the Pronote transport stack."""
    from custom_components.pronote_ng.connectors.ecoledirecte.connector import (
        EcoledirecteConnector,
    )
    from custom_components.pronote_ng.connectors.ecoledirecte.ed_client import (
        EcoleDirecteClient,
    )
    from custom_components.pronote_ng.connectors.factory import build_connector
    from tests.test_ecoledirecte_client import RecordingTransport

    entry = SimpleNamespace(
        entry_id="entry-under-test",
        data={"source": "ecoledirecte"},
        options={},
    )
    client = EcoleDirecteClient(RecordingTransport.scripted([]))

    connector = build_connector(  # type: ignore[arg-type]
        None,
        entry,
        client=client,
        timezone="Europe/Paris",
    )

    assert isinstance(connector, EcoledirecteConnector)
    assert connector.client is client


def test_a_capability_set_that_omits_menus_does_not_list_that_tier() -> None:
    """An Ecoledirecte-shaped connector must not be asked for a canteen page."""
    caps = ConnectorCapabilities(
        source=Source.ECOLEDIRECTE,
        tiers=frozenset({Tier.TIMETABLE, Tier.HOMEWORK, Tier.MARKS, Tier.ATTENDANCE}),
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
