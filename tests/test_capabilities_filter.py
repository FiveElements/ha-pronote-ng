"""Capabilities decide what the integration is allowed to expose."""

from custom_components.pronote_ng.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.pronote_ng.const import Tier
from tests.fixtures.fake_connector import FakeConnector


def test_a_connector_without_menus_never_collects_that_tier() -> None:
    """Unavailable-forever menu entities were how a missing ED page looked."""
    connector = FakeConnector(
        capabilities=ConnectorCapabilities(
            source=Source.ECOLEDIRECTE,
            tiers=frozenset({Tier.TIMETABLE}),
            writes=frozenset(),
            services=frozenset(),
        )
    )
    from custom_components.pronote_ng.account import scheduled_tiers

    assert Tier.MENUS not in scheduled_tiers(connector.capabilities, enabled=None)
    assert Tier.TIMETABLE in scheduled_tiers(connector.capabilities, enabled=None)
