"""Setting one account up, end to end.

Everything below the login is the real code path: the limiter, the scheduler,
the session manager, the tier collectors, the gateway, the coordinators and the
entities. Exactly one seam is mocked -- ``session.build_client`` -- because a
suite that stubbed the gateway or the tiers would prove only that the mocks
agree with each other.

That matters most for the three defects that are invisible in a unit test and
expensive in production: a tier that forgets ``set_child`` and files one child's
data under the other, a login counted per ``(child, tier)`` rather than per
batch, and a reload that makes all ten tiers immediately due. Each has a test
here.

The setup failure paths get the same attention, because §7.2 draws a line the
integration must not blur: a wrong password asks the user to re-authenticate,
while a server that answers badly must **not** -- sending a parent to re-enter
credentials that are correct is worse than saying nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from pronotepy.exceptions import CryptoError, MFAError, PronoteAPIError
import pytest

from custom_components.pronote_ng.const import (
    DOMAIN,
    ISSUE_ACCOUNT_UNREADABLE,
    ISSUE_BOOTSTRAP_FAILED,
    OPT_TIER_ENABLED,
    SERVICE_GET_RATE_LIMIT_STATUS,
    SERVICE_REFRESH,
    Tier,
)
from custom_components.pronote_ng.hardened_client import BootstrapUnavailable
from custom_components.pronote_ng.ratelimit import (
    LOGIN_COST_KEY,
    REQUESTS_PER_LOGIN,
)

from .conftest import CHILDREN, REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pronote_ng.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)


async def _setup_failing(
    hass: HomeAssistant, entry: MockConfigEntry, error: BaseException
) -> None:
    """Attempt a set-up whose login raises ``error``."""
    with patch("custom_components.pronote_ng.session.build_client", side_effect=error):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> ir.IssueEntry:
    """The repair issue ``key`` opened for this entry, or fail the test."""
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")
    assert issue is not None, f"no {key} repair issue was opened"
    return issue


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_setup_loads_the_entry_and_every_platform(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """One login, both children, and the entry ends up loaded."""
    assert mock_entry.state is ConfigEntryState.LOADED
    assert mock_entry.runtime_data is account
    assert [student.id for student in account.students] == [STUDENT_ONE, STUDENT_TWO]
    assert hass.data[DOMAIN][mock_entry.entry_id] is account


async def test_a_device_per_child_hangs_off_the_account_device(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Which is what makes the device triggers of §2.3 readable.

    "When a lesson is cancelled" has to be asked about *somebody*, so each
    child is a device in its own right, and the establishment is the device they
    hang off -- not a prefix in their names.
    """
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, mock_entry.entry_id)
    by_identifier = {next(iter(device.identifiers))[1]: device for device in devices}

    account_device = by_identifier[mock_entry.entry_id]
    assert account_device.name == "Collège d'Essai"

    for student in account.students:
        child = by_identifier[f"{mock_entry.entry_id}_{student.id}"]
        assert child.via_device_id == account_device.id
        assert child.name == student.name
        assert child.model == student.class_name


async def test_every_entity_id_is_derived_from_the_pronote_identifier(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Never from a label, a period name or a rank (§2.4).

    An automation that breaks in September because an establishment relabelled
    "Trimestre 1" is a regression even though no code failed, so the unique id
    is built from the config entry, the identifier PRONOTE gave the child, and a
    functional key.

    The entry prefix matters as much as the rest. The entry's own ``unique_id``
    is the *account*, so two entries following the same child are legitimate --
    a mother's account and a father's, or a parent account beside the child's
    own -- and ``student.id`` is only unique inside one establishment's
    database. Without the prefix the registry does not reject the second entry:
    it re-points the existing entity at it, moving the entity to the other
    entry's device and silently breaking every automation that named it.
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_entry.entry_id)
    assert entries, "the platforms created no entities at all"

    prefixes = tuple(
        f"{mock_entry.entry_id}_{student}_" for student in (STUDENT_ONE, STUDENT_TWO)
    )
    per_child = [entry for entry in entries if entry.unique_id.startswith(prefixes[0])]
    assert per_child

    for entry in entries:
        # Either a child's entity, or one of the account's own diagnostics.
        assert entry.unique_id.startswith((*prefixes, f"{mock_entry.entry_id}_"))
        assert entry.unique_id.startswith(mock_entry.entry_id)


async def test_both_children_get_their_own_snapshot_of_every_tier(
    account: PronoteAccount,
) -> None:
    """Snapshots are keyed by student, because one entry is one *account*.

    A parent account holds several children who share a session, a budget and
    an IP address (§7.1) -- but not a timetable. Keying by tier alone would have
    the second child's collection overwrite the first's, and both devices would
    then show the same lessons.
    """
    for tier in Tier:
        for student_id in (STUDENT_ONE, STUDENT_TWO):
            snapshot = account.snapshot(tier, student_id)
            assert snapshot is not None, f"{tier} has no snapshot for {student_id}"
            assert snapshot.student_id == student_id


async def test_every_child_is_selected_before_its_data_is_read(
    account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The one defect a single-child test cannot see.

    ``set_child`` is what makes the next tab call return *this* child's data. A
    tier that omits it silently files the first child's timetable under the
    second, which no unit test on the gateway can detect: the gateway is handed
    a client and asks it for a tab, and both answers are well-formed.
    """
    assert set(parent_client.child_selections) == {STUDENT_ONE, STUDENT_TWO}


async def test_the_whole_first_batch_costs_one_login(
    account: PronoteAccount,
) -> None:
    """Per *batch*, not per ``(child, tier)``.

    Comparing only the strategy made ``_reconnect_required()`` true on every
    call, and ``run()`` is called once per unit of work: one tick of a two-child
    account with a closed period asked for dozens of full logins against a cap
    of 24, and the integration died before its first batch finished.
    """
    counters = account.limiter.snapshot_counters()
    assert counters["logins_today"] == 1
    assert counters["calls_today"] > 0


async def test_the_session_tier_costs_nothing(account: PronoteAccount) -> None:
    """Periods, class and establishment come free with the login (annexe A §2).

    Specification v1 filed them under a data tier and budgeted requests for
    them. They are in ``func_options`` and ``parametres_utilisateur``, already
    in memory once authenticated.
    """
    for student_id in (STUDENT_ONE, STUDENT_TWO):
        snapshot = account.snapshot(Tier.SESSION, student_id)
        assert snapshot is not None
        assert snapshot.calls == 0

    # And the login's own five requests are booked under their own key, not
    # under this tier's. They are real requests and they are counted -- but
    # attributing them to `session` made the one free tier read as the most
    # expensive thing the integration does, on the attribute the diagnostics
    # point a user at when tuning the budget.
    by_tier = account.limiter.snapshot_counters()["calls_by_tier"]
    assert by_tier[str(Tier.SESSION)] == 0
    assert by_tier[LOGIN_COST_KEY] == REQUESTS_PER_LOGIN


async def test_the_declared_cost_matches_what_was_actually_posted(
    account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The §11.1 contract, checked at the account level.

    ``GatewayResult.calls`` is reconciled against the limiter, so the sum of
    every tier's recorded calls has to equal the number of requests the client
    really placed. An accidental lazy-property access adds a request that the
    spacing, the bucket and the daily cap would all miss -- which is the exact
    regression the gateway's design exists to prevent.
    """
    counters = account.limiter.snapshot_counters()
    charged = sum(
        calls
        for tier, calls in counters["calls_by_tier"].items()
        # The login is charged to the budget but placed by the mocked seam, so
        # it never reaches `posts`; everything else did.
        if tier != LOGIN_COST_KEY
    )
    assert charged == len(parent_client.posts)


async def test_a_repeated_refresh_costs_one_batch(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The button and the service may not become a way round the limiter.

    Nothing is bypassed: the scheduler still decides what is due, so ten
    requests in a minute cost one batch at most -- and here, nothing at all,
    because the first batch just ran and no tier is due again yet (§5.1).
    """
    before = len(parent_client.posts)

    for _ in range(10):
        await account.async_request_tick()
    await hass.async_block_till_done()

    assert len(parent_client.posts) == before


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


async def test_an_options_change_reloads_without_recollecting(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§7.3: tuning the cadence must not cost a full round of requests.

    An options save reloads the entry, which builds a new scheduler whose every
    deadline is unset -- making all ten tiers immediately due. The schedule is
    handed across the reload through ``hass.data``, so the reload is free.
    """
    before = len(parent_client.posts)

    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_TIER_ENABLED.format(tier="menus"): False},
    )
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.LOADED
    assert len(parent_client.posts) == before
    assert mock_entry.runtime_data is not account


async def test_unloading_stops_the_heartbeat_and_releases_the_client(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """And takes the services with it, since they are registered once."""
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.NOT_LOADED
    assert parent_client.closed
    assert not hass.data.get(DOMAIN)
    assert not hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    assert not hass.services.has_service(DOMAIN, SERVICE_GET_RATE_LIMIT_STATUS)


# ---------------------------------------------------------------------------
# Failure paths -- §7.2's line between "your password" and "their server"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(CryptoError("challenge"), id="wrong-password"),
        pytest.param(MFAError("pin"), id="pin-demanded"),
    ],
)
async def test_a_credential_problem_opens_a_reauthentication_flow(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
    error: Exception,
) -> None:
    """Never a silently broken entry (§7.2).

    For a demanded PIN the flow has to *ask* for it: a secret we refuse to
    store is a secret we must know how to request again (§8.1).
    """
    await _setup_failing(hass, mock_entry, error)

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_a_login_with_no_session_key_is_a_credential_problem(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """``_login`` returning ``False`` is a wrong password by another route.

    pronotepy answers a refused authentication by leaving ``logged_in`` false
    rather than raising, so a client built without error is not yet a client
    that works.
    """
    parent_client.logged_in = False

    with patch(
        "custom_components.pronote_ng.session.build_client",
        return_value=parent_client,
    ):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    assert parent_client.closed


async def test_an_impossible_bootstrap_never_names_a_cause(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """§6.3, and the reason this repair reads the way it does.

    pronotepy decides an address is suspended with ``if "IP" in html`` -- two
    capitals anywhere in the page. Telling parents their home connection is
    banned because a school wrote "Espace IP" in a footer would be worse than
    saying nothing, so the issue enumerates the possible causes and picks none.
    """
    await _setup_failing(hass, mock_entry, BootstrapUnavailable("page unavailable"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, mock_entry, ISSUE_BOOTSTRAP_FAILED)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["url"] == mock_entry.data["pronote_url"]


async def test_an_undecodable_login_asks_for_a_report(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """A separate repair from ``bootstrap_failed``, because the remedy differs.

    The address is right and the credentials are right; something in this
    establishment's response is outside what the pinned ``pronotepy`` handles.
    The user cannot fix that, so the text asks for a report rather than a
    correction. ``ValueError`` is the shape it really arrives in: ``clients.py``
    parses ``PremierLundi`` with a bare ``strptime``.
    """
    await _setup_failing(hass, mock_entry, ValueError("PremierLundi"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    _issue(hass, mock_entry, ISSUE_ACCOUNT_UNREADABLE)


async def test_a_server_refusal_is_not_reported_as_a_credential_problem(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """A protocol refusal with no ``Erreur.G`` we recognise is the server's.

    So: retry, and no re-authentication flow. Sending a parent to re-enter
    credentials that are correct is the failure mode §7.2 is about.
    """
    await _setup_failing(hass, mock_entry, PronoteAPIError("refused"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]


async def test_a_school_that_is_simply_down_is_retried(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """``requests.Timeout`` inherits from ``OSError``, not ``TimeoutError``.

    ``except TimeoutError`` therefore caught nothing at all, and a school down
    for a weekend produced an uncounted bootstrap GET every five minutes with no
    backoff whatsoever while ``calls_today`` reported zero.
    """
    await _setup_failing(hass, mock_entry, OSError("connection refused"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
