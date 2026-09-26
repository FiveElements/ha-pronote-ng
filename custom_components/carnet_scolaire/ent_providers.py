"""The ENT portals the login form offers, and how a stored name becomes a login.

A portal is persisted by **name**, because a config entry holds JSON and a
portal is a function. That name used to be resolved in three places -- the
form's option list, the flow's probe and the session's reconnection -- each
with its own ``getattr`` on ``pronotepy.ent``. They agreed only because
nothing had yet been added to one of them. This module is now the single
answer to both questions: which names exist, and what a name logs in with.

It also offers portals that ``pronotepy`` **writes but leaves disabled**. Its
``ent.py`` carries ``enc_hauts_de_seine`` as a commented-out line: the login
function it would bind, ``_oze_ent``, ships and is importable, only the
binding is missing. A parent whose establishment is reachable solely through
that portal -- PRONOTE answering every request with a redirect to the
portal's CAS endpoint, so neither a direct login nor ``?login=true`` exists --
had no way in at all. Binding it here costs one ``partial``.

What is *not* known about such a portal, and so must not be claimed: why
upstream disabled it. ``_oze_ent`` finds the PRONOTE entry point through the
portal's application list first; only its fallback builds the address by
hand, and that fallback asks for the pupil's function (``fonction=ELV``). A
parent account that reaches the fallback may therefore be refused. The
portal is offered because the alternative is no route at all, and a refusal
surfaces as an ordinary ENT login failure, charged to the login guard like
any other.

Precedence goes to upstream: if a ``pronotepy`` release enables one of these
names, its own function is used and the local binding is ignored.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

#: Portals ``pronotepy`` writes but ships disabled: name -> (generic login
#: function in ``pronotepy.ent.generic_func``, portal URL). Copied from the
#: commented-out line in the pinned release, not invented.
DISABLED_UPSTREAM: Final[dict[str, tuple[str, str]]] = {
    "enc_hauts_de_seine": ("_oze_ent", "https://enc.hauts-de-seine.fr/"),
}


def _upstream() -> dict[str, Callable[..., Any]]:
    """The portals ``pronotepy.ent`` exports, by name."""
    from pronotepy import ent as ent_module  # noqa: PLC0415 -- optional dependency

    return {
        name: getattr(ent_module, name)
        for name in dir(ent_module)
        if not name.startswith("_") and callable(getattr(ent_module, name))
    }


def _enabled_here() -> dict[str, Callable[..., Any]]:
    """The disabled-upstream portals whose generic function still ships.

    A name whose function a later release removes is dropped rather than
    offered: listing a portal that cannot log in would spend a slot on the
    login guard to find that out.
    """
    from pronotepy.ent import generic_func  # noqa: PLC0415 -- optional dependency

    bound: dict[str, Callable[..., Any]] = {}
    for name, (function, url) in DISABLED_UPSTREAM.items():
        base = getattr(generic_func, function, None)
        if callable(base):
            bound[name] = partial(base, url=url)
    return bound


def ent_provider_names() -> list[str]:
    """Every portal name the login form may offer, sorted."""
    return sorted(_enabled_here().keys() | _upstream().keys())


def resolve_ent_provider(name: str) -> Callable[..., Any] | None:
    """The login function for ``name``, or ``None`` when no portal has it."""
    upstream = _upstream()
    if name in upstream:
        return upstream[name]
    return _enabled_here().get(name)
