"""The connector seam: costs are declared, protocol objects do not leak."""

from custom_components.pronote_ng.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.pronote_ng.const import Tier
from custom_components.pronote_ng.models import GatewayResult, HomeworkFacts


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
