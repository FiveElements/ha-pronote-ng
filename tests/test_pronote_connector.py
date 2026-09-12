"""Contract tests for the PRONOTE implementation of ``SchoolConnector``."""


def test_pronote_connector_capabilities_list_every_collectable_tier() -> None:
    """Silently dropping history is a missing bulletin, not a setting."""
    from custom_components.pronote_ng.connectors.pronote import PronoteConnector
    from custom_components.pronote_ng.const import Tier

    collectable = frozenset(tier for tier in Tier if tier is not Tier.SESSION)
    assert collectable <= PronoteConnector.CAPABILITIES.tiers
    assert Tier.SESSION not in PronoteConnector.CAPABILITIES.tiers
