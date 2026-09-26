"""The ENT portal registry: which names the form offers, and what they log in with."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from custom_components.carnet_scolaire import ent_providers
from custom_components.carnet_scolaire.ent_providers import (
    DISABLED_UPSTREAM,
    ent_provider_names,
    resolve_ent_provider,
)

if TYPE_CHECKING:
    import pytest


def test_the_hauts_de_seine_portal_is_offered_although_upstream_ships_it_disabled() -> (
    None
):
    """`pronotepy` writes this binding and comments it out.

    A parent whose establishment is reachable only through that portal --
    PRONOTE redirecting every request, `?login=true` included, to the portal's
    CAS endpoint -- had no login mode left: no QR code, no direct login, and
    no entry in the ENT list. The name must now be offered.
    """
    assert "enc_hauts_de_seine" in ent_provider_names()


def test_a_disabled_upstream_portal_logs_in_through_its_generic_function() -> None:
    """The binding is the one upstream wrote: the generic function and its URL."""
    from pronotepy.ent import generic_func

    provider = resolve_ent_provider("enc_hauts_de_seine")

    assert isinstance(provider, partial)
    assert provider.func is generic_func._oze_ent
    assert provider.keywords == {"url": "https://enc.hauts-de-seine.fr/"}


def test_every_upstream_portal_is_still_offered_and_resolved_to_its_own_function() -> (
    None
):
    """Adding portals must not lose one: the upstream list is a subset, unchanged."""
    from pronotepy import ent as ent_module

    upstream = [
        name
        for name in dir(ent_module)
        if not name.startswith("_") and callable(getattr(ent_module, name))
    ]

    assert upstream
    assert set(upstream) <= set(ent_provider_names())
    for name in upstream:
        assert resolve_ent_provider(name) is getattr(ent_module, name)


def test_an_upstream_release_that_enables_a_portal_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `pronotepy` ships the binding one day, its function wins over ours.

    Upstream is where a fix to that portal would land; a local copy that kept
    winning would pin the integration to whatever was broken at the time.
    """
    from pronotepy import ent as ent_module

    def upstream_binding(username: str, password: str) -> None:
        """Stand-in for a binding a future release would export."""

    monkeypatch.setattr(
        ent_module, "enc_hauts_de_seine", upstream_binding, raising=False
    )

    assert resolve_ent_provider("enc_hauts_de_seine") is upstream_binding
    assert ent_provider_names().count("enc_hauts_de_seine") == 1


def test_a_portal_whose_generic_function_disappeared_is_no_longer_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A portal that cannot log in would spend a login-guard slot to prove it."""
    from pronotepy.ent import generic_func

    for function, _url in DISABLED_UPSTREAM.values():
        monkeypatch.delattr(generic_func, function)

    assert "enc_hauts_de_seine" not in ent_provider_names()
    assert resolve_ent_provider("enc_hauts_de_seine") is None


def test_an_unknown_name_resolves_to_nothing() -> None:
    """Callers turn `None` into a refusal; they must never receive a guess."""
    assert resolve_ent_provider("a_portal_nobody_ships") is None
    assert "a_portal_nobody_ships" not in ent_providers.ent_provider_names()
