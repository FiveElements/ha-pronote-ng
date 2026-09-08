"""ha-pronote -- a Home Assistant PRONOTE integration designed for automations.

The directing objective is in ``docs/SPECIFICATION.md`` §1: make automations on
PRONOTE data simple to write. Every structural choice here follows from it, and
the success criterion is precise -- **any ordinary school automation must be
writable without a Jinja template.**

Set-up order matters. The account logs in once, learns its own shape (children,
periods, current period), and only then are the platforms forwarded: an entity
that does not know which child it belongs to has no stable ``unique_id``, and
§2.4 makes identifier stability a guarantee rather than an aspiration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from pronotepy.exceptions import PronoteAPIError

from .account import PronoteAccount
from .const import DOMAIN
from .services import async_setup_services, async_unload_services
from .session import (
    AccountUnreadable,
    BootstrapFailed,
    IntegrationFault,
    InvalidCredentials,
    LoginRefused,
    MfaRequired,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER: Final = logging.getLogger(__name__)

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
    Platform.TODO,
]

type PronoteConfigEntry = ConfigEntry[PronoteAccount]


async def async_setup_entry(hass: HomeAssistant, entry: PronoteConfigEntry) -> bool:
    """Set up one PRONOTE account."""
    account = PronoteAccount(hass, entry)

    try:
        await account.async_setup()
    except (InvalidCredentials, MfaRequired) as error:
        # A re-authentication flow, never a silently broken entry (§7.2). For
        # MFA specifically the flow has to *ask for the PIN*: a secret we refuse
        # to store is a secret we must know how to request again (§8.1).
        await account.async_unload()
        raise ConfigEntryAuthFailed(str(error)) from error
    except BootstrapFailed as error:
        # Deliberately does not claim a cause. pronotepy decides an address is
        # suspended with `if "IP" in html` -- two capitals anywhere in the page
        # -- and telling parents their home connection is banned because of a
        # school's footer would be worse than saying nothing (§6.3).
        account.async_open_bootstrap_issue()
        await account.async_unload()
        raise ConfigEntryNotReady(str(error)) from error
    except AccountUnreadable as error:
        # The server answered and the credentials were fine; what came back is
        # outside what the pinned pronotepy can parse. Retrying is right --
        # establishments do publish transient nonsense -- but the limiter holds
        # it back, so this does not become a loop.
        account.async_open_unreadable_issue()
        await account.async_unload()
        raise ConfigEntryNotReady(str(error)) from error
    except (LoginRefused, IntegrationFault) as error:
        await account.async_unload()
        raise ConfigEntryNotReady(str(error)) from error
    except PronoteAPIError as error:
        # A protocol-level refusal with no `Erreur.G` we recognise. It is the
        # server's answer, so "not ready" and retry -- never "auth failed",
        # which would send the user to re-enter credentials that are correct.
        await account.async_unload()
        raise ConfigEntryNotReady(str(error)) from error
    except (TimeoutError, OSError) as error:
        await account.async_unload()
        raise ConfigEntryNotReady(str(error)) from error

    entry.runtime_data = account
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = account

    # The account device, created here rather than left to whichever platform
    # happens to register a child first. Every child device declares
    # `via_device` on it, and the seven platforms are forwarded in parallel: on
    # a first start the child devices were created before their parent existed,
    # which Home Assistant reports as "references a non existing via_device"
    # and answers by flattening the device tree. It repaired itself on the next
    # restart, which is exactly why it survived review.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="PRONOTE",
        model="Account",
        name=account.establishment_name,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PronoteConfigEntry) -> bool:
    """Tear one account down without ever joining its worker thread."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    account: PronoteAccount | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if account is not None:
        await account.async_unload()

    if not hass.data.get(DOMAIN):
        async_unload_services(hass)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: PronoteConfigEntry) -> None:
    """Apply an options change by reloading, without a restart (§7.3).

    The schedule survives: intervals are re-read but each tier's deadline is
    recomputed from its last collection, so shortening an interval does not fire
    an immediate batch. Otherwise tuning the cadence would cost a full round of
    requests every time the options page is saved.
    """
    await hass.config_entries.async_reload(entry.entry_id)
