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

import logging
from types import SimpleNamespace
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

from custom_components.pronote_ng import async_remove_config_entry_device
from custom_components.pronote_ng.account import (
    PronoteAccount,
    _establishment_timezone,
)
from custom_components.pronote_ng.connectors.ecoledirecte.connector import (
    EcoledirecteConnector,
)
from custom_components.pronote_ng.connectors.ecoledirecte.ed_limiter import (
    EdRateLimiter,
)
from custom_components.pronote_ng.connectors.protocol import Source
from custom_components.pronote_ng.const import (
    CONF_CHILDREN,
    CONF_SOURCE,
    DOMAIN,
    FUNC_MENUS,
    FUNC_TIMETABLE,
    ISSUE_ACCOUNT_UNREADABLE,
    ISSUE_BOOTSTRAP_FAILED,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_TIER_ENABLED,
    SERVICE_GET_RATE_LIMIT_STATUS,
    SERVICE_REFRESH,
    TIER_PRIORITY,
    Priority,
    Tier,
)
from custom_components.pronote_ng.hardened_client import BootstrapUnavailable
from custom_components.pronote_ng.ratelimit import (
    LOGIN_COST_KEY,
    REQUESTS_PER_LOGIN,
    RateLimitConfig,
    RateLimiter,
)
from custom_components.pronote_ng.sensor import LIST_SENSORS, PRIMITIVE_SENSORS
from custom_components.pronote_ng.session import SerialExecutor, SessionManager
from custom_components.pronote_ng.tiers import (
    _FIRST_COLLECTION_ATTEMPTS,
    _priority_for,
    collect_tier,
)

from .conftest import CHILDREN, REQUIRES_HASS, child_key

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

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


async def test_setup_uses_the_pronote_connector(account: PronoteAccount) -> None:
    """The account source must stay explicit when another school backend is added."""
    assert account.connector.capabilities.source is Source.PRONOTE


async def test_an_unselected_child_with_unreadable_facts_does_not_block_setup(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """A child excluded in the flow must be filtered before its facts are decoded."""
    from custom_components.pronote_ng.gateway import PronoteGateway

    del school_day, no_spacing
    hass.config_entries.async_update_entry(
        mock_entry,
        data={**mock_entry.data, CONF_CHILDREN: [STUDENT_ONE]},
    )
    original = PronoteGateway.session_facts

    def session_facts(gateway: PronoteGateway, client: FakeClient) -> Any:
        if client.selected_child_id == STUDENT_TWO:
            raise ValueError("unselected child is unreadable")
        return original(gateway, client)

    with (
        patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ),
        patch.object(PronoteGateway, "session_facts", session_facts),
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    account = mock_entry.runtime_data
    assert [student.id for student in account.students] == [STUDENT_ONE]

    await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()


async def test_account_now_is_timezone_aware(account: PronoteAccount) -> None:
    """Naive datetimes in entity state lose ordering and locale in one stroke."""
    assert account.now().tzinfo is not None
    assert account.today() == account.now().date()


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
        key = child_key(mock_entry, student.id)
        child = by_identifier[f"{mock_entry.entry_id}_{key}"]
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
        f"{mock_entry.entry_id}_{child_key(mock_entry, student)}_"
        for student in (STUDENT_ONE, STUDENT_TWO)
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
    """And leaves the actions in place, which is the opposite of tidiness.

    The actions belong to the integration, not to this entry: they are
    registered from `async_setup` and deliberately never removed. Home Assistant
    can only validate an automation against actions that *exist*, so removing
    them when the last entry unloaded made every automation referencing
    `pronote_ng.mark_homework_done` unvalidatable exactly when the user was
    already looking at a broken account -- and being driven by automations is
    this integration's stated purpose (§1).

    The handlers are built for it: each resolves its account from the target
    device at call time and raises `ServiceValidationError` for one it cannot
    place. So the action stays present and fails with a reason, rather than
    vanishing and failing with "action not found".
    """
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.NOT_LOADED
    assert parent_client.closed
    assert not hass.data.get(DOMAIN)
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_RATE_LIMIT_STATUS)


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


async def test_a_snapshot_collected_for_another_child_is_refused(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """The one runtime defence against publishing the wrong child's data.

    `pronotepy`'s parent client sets `_selected_child = self.children[0]` in
    its constructor, so a path that forgets `set_child` raises nothing: it
    returns the *first* child's data. With one child -- the likeliest test
    configuration -- the defect is undetectable. With two it publishes one
    child's timetable under the other child's entities, and the only detector
    left is a parent recognising the wrong lessons on their dashboard (§7.1).

    Every snapshot carries the child it was collected for, and this is the
    check that makes the stamp worth having. Asserted here rather than trusted
    to the atomic `(child, tier)` closure in `session.py`, because the point of
    a safety net is that it holds when the thing above it does not.
    """
    coordinator = account.coordinators[Tier.TIMETABLE]
    theirs = coordinator.snapshot_for(STUDENT_TWO)
    assert theirs is not None, "the fixture collected nothing for the second child"

    with pytest.raises(ValueError, match="another"):
        coordinator.publish(STUDENT_ONE, theirs)

    # And the first child's own snapshot is untouched by the refusal.
    assert coordinator.snapshot_for(STUDENT_ONE) is not None
    assert coordinator.snapshot_for(STUDENT_ONE).student_id == STUDENT_ONE


async def test_no_tab_call_happens_outside_the_selection_it_belongs_to(
    account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The ordered form of the invariant, which the set form cannot express.

    `test_every_child_is_selected_before_its_data_is_read` asserts the *set* of
    selections, and a set is blind to the defect that matters: both children
    could be selected, both tiers collected, and the pairing between them
    crossed. Upstream makes that failure silent rather than loud --
    `ParentClient.__init__` sets `_selected_child = self.children[0]`, so a
    call placed with no selection in force answers for the first child instead
    of raising (§7.1).

    So this reads one interleaved journal of selections and server calls, and
    checks the only property that rules the crossing out: every call is
    preceded by a selection, and the child in force when a call was made is the
    child whose snapshot the collection then published.

    Asserted on a two-child account because with one child the property holds
    vacuously -- which is precisely why the defect survives single-child
    testing.
    """
    parent_client.journal.clear()

    await account._async_collect(Tier.TIMETABLE)

    journal = list(parent_client.journal)
    assert journal, "collecting a tier placed no call at all"
    assert journal[0][0] == "select", (
        f"a call was placed before any child was selected: {journal[:3]}"
    )

    # Attribute every call to the selection that was in force when it happened.
    in_force: str | None = None
    calls_by_child: dict[str, list[str]] = {}
    for kind, value in journal:
        if kind == "select":
            in_force = value
            calls_by_child.setdefault(value, [])
            continue
        assert in_force is not None
        calls_by_child[in_force].append(value)

    expected = {student.id for student in account.students}
    assert set(calls_by_child) == expected, (
        "the collection selected children it does not follow, or skipped one"
    )
    for student_id, calls in calls_by_child.items():
        assert calls, f"{student_id} was selected and then never read"

    # And the snapshot each child ended up with was published under the same id
    # the client was pointed at while its data was being fetched.
    coordinator = account.coordinators[Tier.TIMETABLE]
    for student_id in expected:
        snapshot = coordinator.snapshot_for(student_id)
        assert snapshot is not None, f"no timetable published for {student_id}"
        assert snapshot.student_id == student_id


async def test_a_stale_child_selection_says_so_instead_of_recovering_quietly(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The silence that hid the worst defect this integration has had.

    A PRONOTE resource identifier is written ``46#<signature>``, and that
    signature is **not stable between sessions**. So the identifiers stored in
    ``entry.data["children"]`` at configuration time eventually match nothing
    the account announces, and following every child instead is the right
    recovery -- refusing to collect because a stored string went stale would
    take the whole integration down.

    Recovering *quietly* is what cost. An entity's ``unique_id`` embeds the
    identifier, so a changed one creates a new device and a full set of new
    entities while the previous generation is orphaned in the registry:
    every dashboard, automation and helper pointing at it dead, and nothing
    logged anywhere. On a live instance three generations accumulated before
    anybody noticed, and only because a dashboard read the dead set.

    ``config_flow._account_identity`` already learned this lesson for the
    account identifier -- it drops the signature before comparing, and its
    docstring explains why. Nobody carried it here.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        data={**mock_entry.data, CONF_CHILDREN: ["46#a-signature-from-last-session"]},
    )
    caplog.set_level(logging.WARNING)

    with patch(
        "custom_components.pronote_ng.session.build_client",
        return_value=parent_client,
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

        account = mock_entry.runtime_data
        # Recovered: every announced child is followed, so the account works.
        assert {student.id for student in account.students} == {
            child_id for child_id, _name in CHILDREN
        }
        # And said so, naming both counts so the reader can see it is a
        # mismatch and not an empty account.
        assert "selected children match" in caplog.text
        assert "not stable between sessions" in caplog.text

        await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()


async def test_a_finished_batch_is_counted_so_a_caller_can_wait_for_it(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """The lock could not tell "over" from "not begun", and that flaked CI.

    Set-up schedules the first collection as a task. A caller that checked
    ``_tick_lock`` before that task had taken it read the lock as free and
    concluded the batch was finished, having waited for nothing -- so the
    lowest-priority tier's entity was still ``unavailable`` when the test read
    it. It failed about one run in twenty, on the *gated* CI row, which is the
    worst place for it: a barrier that flickers either lets something through
    or blocks a release at random.

    ``completed_ticks`` is incremented at the end of a batch, so a non-zero
    value cannot mean "not started yet".
    """
    assert account.completed_ticks >= 1, (
        "set-up returned before its own first collection had finished"
    )
    assert not account._tick_lock.locked()


async def test_a_batch_with_nothing_due_is_still_counted(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """Counting only *productive* batches would reintroduce the same hang.

    Some tests deliberately leave no tier due -- that is how a broken tier or
    a spent budget is exercised -- and a wait that required a collection would
    never be satisfied by them. So an empty batch counts too, which is the one
    property the discarded lock check did have.
    """
    before = account.completed_ticks

    # Nothing has become due since the fixture drained the first batch.
    await account._async_tick()

    assert account.completed_ticks == before + 1


async def test_an_ed_batch_and_diagnostics_never_touch_session_manager() -> None:
    """ED owns an asyncio lock, so a SessionManager access crashes its first tick."""
    limiter = EdRateLimiter(
        RateLimitConfig(min_request_interval=0, quiet_hours_enabled=False)
    )
    connector = SimpleNamespace(
        capabilities=EcoledirecteConnector.CAPABILITIES,
        limiter=limiter,
        diagnostics=lambda: {
            "limiter": limiter.snapshot_counters(),
            "session": {"authenticated": True},
        },
    )
    account = object.__new__(PronoteAccount)
    account.connector = connector  # type: ignore[assignment]
    account.scheduler = SimpleNamespace(  # type: ignore[assignment]
        due=lambda: (Tier.TIMETABLE,),
        diagnostics=dict,
    )
    account._shutting_down = False
    account._completed_ticks = 0
    account.state = SimpleNamespace(students=(), periods=())
    account.stale_after = 3
    account.entry = SimpleNamespace(options={})  # type: ignore[assignment]

    collected: list[Tier] = []

    async def collect(tier: Tier) -> None:
        collected.append(tier)

    account._async_collect = collect  # type: ignore[method-assign]
    account._async_sync_issues = lambda: None  # type: ignore[method-assign]

    await account._async_run_batch()

    assert collected == [Tier.TIMETABLE]
    assert account.diagnostics()["session"] == {"authenticated": True}


async def test_an_ed_entry_instantiates_no_pronote_network_primitive(
    hass: HomeAssistant,
) -> None:
    """A second backend must not silently inherit PRONOTE's thread and login costs."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        data={
            CONF_SOURCE: Source.ECOLEDIRECTE.value,
            "username": "demo.example.invalid",
            "password": "not-a-real-password",
            CONF_CHILDREN: ["1"],
        },
    )
    entry.add_to_hass(hass)

    with (
        patch.object(PronoteAccount, "async_setup", return_value=None),
        patch.object(PronoteAccount, "async_start_first_collection"),
        patch(
            "custom_components.pronote_ng.session.SerialExecutor",
            wraps=SerialExecutor,
        ) as executor,
        patch(
            "custom_components.pronote_ng.session.SessionManager",
            wraps=SessionManager,
        ) as session_manager,
        patch(
            "custom_components.pronote_ng.ratelimit.RateLimiter",
            wraps=RateLimiter,
        ) as pronote_limiter,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    account = entry.runtime_data
    assert isinstance(account.connector, EcoledirecteConnector)
    executor.assert_not_called()
    session_manager.assert_not_called()
    pronote_limiter.assert_not_called()


async def test_a_stale_child_device_can_be_deleted(
    hass: HomeAssistant,
    account: PronoteAccount,
    mock_entry: MockConfigEntry,
) -> None:
    """The guide's only remedy for a superseded generation is the delete button.

    Without `async_remove_config_entry_device` Home Assistant hides that
    button and refuses the call with "Config entry does not support device
    removal". §10.5 and the v0.0.10 release notes both told the reader to
    delete the stale device, so the documentation promised something the code
    made impossible -- found by trying to follow my own instructions on a live
    instance and being refused.
    """
    del account
    devices = dr.async_get(hass)
    stale = devices.async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_entry.entry_id}_46#SUPERSEDED")},
        name="Enfant Un",
    )

    assert await async_remove_config_entry_device(hass, mock_entry, stale) is True


async def test_the_account_device_cannot_be_deleted(
    hass: HomeAssistant,
    account: PronoteAccount,
    mock_entry: MockConfigEntry,
) -> None:
    """Every child declares it as `via_device`.

    Removing it would leave the children pointing at a parent that no longer
    exists, which is the state that once flattened the whole device tree until
    the next restart. Allowing everything would have been the shorter hook and
    would have shipped that.
    """
    del account
    devices = dr.async_get(hass)
    parent = devices.async_get_device_by_identifier(
        (DOMAIN, mock_entry.entry_id), mock_entry.entry_id
    )
    assert parent is not None, "the account device is missing from the fixture"

    assert await async_remove_config_entry_device(hass, mock_entry, parent) is False


class TestHowLongAFirstCollectionOutranksQuietHours:
    """The dispensation, and the exit it was missing.

    A tier with no snapshot outranks quiet hours because its first collection
    is the one a user is watching for. That was described as self-limiting
    because a tier that collects stops qualifying -- true, and insufficient:
    **success was its only exit**. A tier that can never succeed stays
    data-less for ever and therefore held a standing exemption, and on a live
    instance the establishment's unusable teaching-staff tab became the only
    tier awake between 22:00 and 06:00. Being alone, it was the only tier able
    to accumulate failures, with no other tier's success available to reset the
    account's back-off: one broken tab produced an account-wide ``backoff`` and
    took every other tier's freshness down with it.

    Driven on an account that has been set up but has collected nothing, which
    is the state the dispensation is *about*. The shared ``account`` fixture
    has already collected every tier, so ``has_data`` is true there and this
    branch is unreachable -- a test written against that fixture would assert
    the declared priority in every case and pass whatever the bound was.
    """

    @staticmethod
    async def _fresh(
        hass: HomeAssistant, mock_entry: MockConfigEntry, client: FakeClient
    ) -> PronoteAccount:
        """An account that knows its shape and holds no snapshot."""
        account = PronoteAccount(hass, mock_entry)
        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=client,
        ):
            await account.async_setup()
            await hass.async_block_till_done()
        return account

    async def test_a_tier_with_no_data_yet_outranks_quiet_hours(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        no_spacing: None,
    ) -> None:
        """The dispensation itself, which must keep working.

        Asserted first and separately, because every bound below is only
        defensible if the thing being bounded still exists. An instance
        installed at 23:00 that shows nothing until 06:00 reads as broken, and
        that is what this branch is for.
        """
        del no_spacing
        account = await self._fresh(hass, mock_entry, parent_client)
        try:
            assert (
                _priority_for(account, Tier.STATIC, "STUDENT-1") is Priority.CRITICAL
            ), "a tier that has never collected must still outrank quiet hours"
        finally:
            await account.async_unload()
            await hass.async_block_till_done()

    async def test_a_tier_that_keeps_failing_loses_the_dispensation(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        no_spacing: None,
    ) -> None:
        """The exit that was missing, expressed as evidence rather than success.

        The argument for the exemption is about a first collection that
        *works*; it cannot survive the demonstration that this one does not.
        After the third failure the tier drops back to its declared priority,
        so quiet hours apply to it again and it waits for 06:00 like
        everything else -- while keeping its deadline and its floored retry.
        """
        del no_spacing
        account = await self._fresh(hass, mock_entry, parent_client)
        try:
            for _ in range(_FIRST_COLLECTION_ATTEMPTS):
                account.scheduler.mark_failed(Tier.STATIC, 0.0)

            assert (
                _priority_for(account, Tier.STATIC, "STUDENT-1")
                is (TIER_PRIORITY[Tier.STATIC])
            ), "a tab that does not work must stop being the only tier awake at 04:00"
        finally:
            await account.async_unload()
            await hass.async_block_till_done()

    async def test_one_failure_does_not_spend_the_dispensation(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        no_spacing: None,
    ) -> None:
        """The guard on the bound: a transient failure must not cost it.

        A school's server restarting, or one malformed response, is exactly
        the case the dispensation was granted for -- a fresh install that has
        nothing to show yet. Bounding it at the *first* failure would hand a
        new user an empty dashboard until morning for a fault that had already
        cleared.
        """
        del no_spacing
        account = await self._fresh(hass, mock_entry, parent_client)
        try:
            account.scheduler.mark_failed(Tier.STATIC, 0.0)

            assert (
                _priority_for(account, Tier.STATIC, "STUDENT-1") is Priority.CRITICAL
            ), "one bad response is not evidence that the collection cannot work"
        finally:
            await account.async_unload()
            await hass.async_block_till_done()


class TestWhenTheFirstBatchIsAllowedToRun:
    """The first collection must not race the entities it collects for.

    A defect measured on a live instance, and the second one this month whose
    symptom was "every entity is empty" with a different cause. Set-up learns
    the account's shape, then forwards the platforms -- but the first batch was
    scheduled from `async_setup`, which is *before* that. A coordinator
    publishing before its entities have subscribed pushes to nobody: each
    entity is added afterwards, renders once from `coordinator.data`, and a
    tier whose snapshot landed inside that window renders `unavailable` with
    its data already present.

    What makes it permanent rather than transient is that the tier is
    genuinely collected. The scheduler holds its deadline and `has_data` is
    true, so the first-collection dispensation of `collect_tier` no longer
    applies and a `refresh` is refused -- correctly, and to no effect. The
    entity then waits a full tier interval: three hours for `marks`, a day for
    `menus` and `history`, and with quiet hours on, until 06:00.

    Measured: ten tiers placed their requests and published; nineteen entities
    across seven tiers stayed `unavailable`. The only two that displayed were
    the two whose snapshots landed before the platforms were forwarded.
    """

    async def test_setting_the_account_up_does_not_collect_on_its_own(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        school_day: FrozenDateTimeFactory,
        no_spacing: None,
    ) -> None:
        """The ordering contract, asserted where it can be asserted at all.

        This is the one test that pins the *cause* rather than the symptom.
        `async_setup` must leave the account collected-nothing, so that the
        only thing which can start a batch is the explicit call made after
        `async_forward_entry_setups`. Asserting the symptom instead -- "no
        entity is empty" -- cannot catch a reintroduction, because the race is
        won by whichever of two tasks the loop happens to run first, and on an
        idle machine that was the harmless order roughly nineteen times in
        twenty.

        `completed_ticks` and the coordinators together, because either alone
        is satisfiable by accident: a batch that ran and found nothing due
        would leave the coordinators empty, and a batch that never ran leaves
        the counter at zero.
        """
        del school_day, no_spacing
        account = PronoteAccount(hass, mock_entry)

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            await account.async_setup()
            await hass.async_block_till_done()
            try:
                assert account.completed_ticks == 0
                assert all(
                    not coordinator.data
                    for tier, coordinator in account.coordinators.items()
                    if tier is not Tier.SESSION
                )
            finally:
                await account.async_unload()
                await hass.async_block_till_done()

    async def test_every_tier_that_collected_has_an_entity_that_shows_it(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The promise, stated the way a dashboard experiences it.

        Read through the entity registry by `unique_id` rather than by
        entity id: the identifier's suffix comes from the translated *name*,
        and guessing it from the translation key is the mistake that put
        eleven identifiers that do not exist into annexe A.

        Every tier holding a snapshot is required to have every one of its
        sensors available -- not a sampled one. The defect this replaces hit
        seven tiers out of ten and spared the two that happened to publish
        early, so a test naming one tier would have passed throughout.
        """
        registry = er.async_get(hass)
        student_id = account.state.students[0].id
        key = child_key(account.entry, student_id)

        empty: list[str] = []
        checked = 0
        for description in (*PRIMITIVE_SENSORS, *LIST_SENSORS):
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None or not coordinator.data:
                continue
            unique_id = f"{account.entry.entry_id}_{key}_{description.key}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is None:
                continue
            checked += 1
            state = hass.states.get(entity_id)
            if state is None or state.state == "unavailable":
                empty.append(entity_id)

        assert not empty, (
            f"tiers collected but these entities stayed empty: {empty}; "
            f"schedule={account.scheduler.diagnostics()}"
        )
        # A guard on the guard: if the descriptions or the registry lookup ever
        # stop matching, the loop above would assert nothing at all and pass.
        assert checked > 20


class TestWhatSurvivesAReload:
    """A reload must never leave an entity empty.

    This is a defect that reached a live instance. An options save reloads the
    config entry; the reload handed the *schedule* across but built fresh,
    empty coordinators. The new scheduler therefore believed every tier was
    freshly collected, `due()` returned nothing, and the batch that ran was a
    batch of nothing -- so every child entity read `unavailable` for a whole
    tier interval, which is twenty-four hours for `menus`, `static` and
    `history`. Inside quiet hours the limiter refused to refill them at all, so
    a reload at 22:10 emptied the dashboard until 06:00.
    """

    async def test_the_data_survives_so_no_entity_goes_empty(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The fix, stated as the user-visible promise rather than a mechanism.

        Asserted on the state machine and not on the coordinators, because
        "the entity has a value" is the thing that was broken; a test that
        checked `coordinator.data` could pass with the entity still
        unavailable.
        """
        # Read from `translations/en.json` rather than derived from the
        # translation key: the suffix comes from the *name*, and guessing
        # it is the mistake that put eleven identifiers that do not exist
        # into annexe A.
        entity_id = "sensor.enfant_un_timetable_this_week"
        before = hass.states.get(entity_id)
        assert before is not None
        assert before.state not in ("unavailable", "unknown")

        await hass.config_entries.async_reload(account.entry.entry_id)
        await hass.async_block_till_done()

        after = hass.states.get(entity_id)
        assert after is not None
        assert after.state == before.state

    async def test_a_tier_whose_data_did_not_survive_is_due_again(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        parent_client: FakeClient,
    ) -> None:
        """The invariant: no data means due, whatever the schedule claims.

        The schedule and the data are only sound *together*. This drops one
        tier's snapshot from the hand-off while leaving its deadline in place
        -- exactly the shape the defect had -- and requires the scheduler to
        ignore that deadline. Without the pairing the tier would sit idle,
        holding nothing, until its interval elapsed.
        """
        from custom_components.pronote_ng.account import (
            _saved_schedule,
            _saved_snapshots,
        )

        entry_id = account.entry.entry_id
        before = len(parent_client.posted_names)
        await hass.config_entries.async_unload(entry_id)
        await hass.async_block_till_done()

        # The schedule remembers every tier; the data forgets the timetable.
        inherited = _saved_schedule(hass)[entry_id]
        assert str(Tier.TIMETABLE) in inherited
        assert str(Tier.MENUS) in inherited
        _saved_snapshots(hass)[entry_id].pop(Tier.TIMETABLE)

        await hass.config_entries.async_setup(entry_id)
        await hass.async_block_till_done()

        account = hass.data[DOMAIN][entry_id]

        # Collected again, because it had nothing. The snapshot is the proof:
        # it was dropped from the hand-off above, so its only possible source
        # is a fresh request placed despite the inherited deadline.
        assert account.snapshot(Tier.TIMETABLE, CHILDREN[0][0]) is not None

        # The control, and the reason this is not simply "re-collect
        # everything on reload": `menus` kept its data, so it kept its
        # deadline and was never asked for. §7.3 -- tuning the cadence must
        # not cost a round of requests -- still holds.
        #
        # Asserted on the requests actually placed rather than on
        # `last_collected`, because the suite freezes the clock: a tier
        # re-collected under a frozen clock records the very instant it
        # inherited, so the two are indistinguishable by value. `posts` is
        # the ground truth for what went to the wire.
        placed = parent_client.posted_names[before:]
        assert FUNC_TIMETABLE[0] in placed
        assert FUNC_MENUS[0] not in placed

    async def test_an_empty_carry_restores_nothing_and_claims_nothing(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A tier carried as an empty mapping is not data.

        Reporting it as restored would hand its deadline back and strand its
        entities -- the defect, reintroduced through the door marked "we did
        carry something for that tier".
        """
        assert account._restore_snapshots(None) == frozenset()
        assert account._restore_snapshots({Tier.TIMETABLE: {}}) == frozenset()

    async def test_a_tier_with_no_data_collects_ahead_of_quiet_hours(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The half of the fix a reload cannot cover: a cold restart.

        After a real restart the hand-off store is empty, so every tier is due
        -- but during quiet hours the limiter refuses everything below
        `critical`, and an instance restarted at 23:00 published nothing at all
        until 06:00. §2.5 is explicit that an unavailable entity breaks
        automations rather than merely looking empty, and the first collection
        is precisely the one somebody is watching for.

        `critical` is not a new hole: it is the priority the login itself uses,
        and the quiet-hours branch of `RateLimiter.check` already exempts it.
        """
        del hass
        student_id = CHILDREN[0][0]
        recorded: list[Priority] = []
        original = account.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            recorded.append(priority)
            return await original(name, priority, fn, **kwargs)

        # A tier that has never produced anything, which is what a cold
        # restart leaves behind.
        account.coordinators[Tier.MENUS].data = {}
        with patch.object(account.session, "run", spy):
            await collect_tier(account, Tier.MENUS, student_id)

        assert recorded == [Priority.CRITICAL]

    async def test_a_tier_that_already_has_data_keeps_its_own_priority(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The dispensation is one batch per tier, not a standing exemption.

        This is the assertion that keeps the previous one honest. If the
        priority did not fall back once a snapshot exists, `menus` would
        outrank quiet hours every night for the rest of the year -- which is
        the opposite of what quiet hours are for.
        """
        del hass
        student_id = CHILDREN[0][0]
        assert account.snapshot(Tier.MENUS, student_id) is not None

        recorded: list[Priority] = []
        original = account.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            recorded.append(priority)
            return await original(name, priority, fn, **kwargs)

        with patch.object(account.session, "run", spy):
            await collect_tier(account, Tier.MENUS, student_id)

        assert recorded == [TIER_PRIORITY[Tier.MENUS]]
        assert TIER_PRIORITY[Tier.MENUS] is not Priority.CRITICAL


class TestWhichTimezonePronotesNaiveTimesAreReadIn:
    """The one option whose default is another system's setting, not a constant.

    §4.2: PRONOTE sends local times with no offset, the conversion happens once
    in the gateway, and the zone is `establishment_timezone` "whose default is
    Home Assistant's". Two consequences are asserted below, and both were
    wrong at some point.

    A *default* is not a stored value. The options page used to submit the
    instance's zone back as though the user had typed it, which pinned it: from
    then on the account never looked at `hass.config.time_zone` again, so
    correcting the instance's timezone -- the ordinary fix after a move, or
    after a first install left it on UTC -- silently changed nothing here.

    And a stored value is not necessarily a zone. The field is free text, and
    the string reaches `ZoneInfo` on the set-up path, where an unknown name
    fails the entry outright.
    """

    def test_nothing_stored_means_follow_the_instance(
        self, hass: HomeAssistant
    ) -> None:
        """Absence is the default, and the default is live rather than copied.

        Read at every set-up, so a user who fixes their instance's timezone
        gets the correction on the next reload without touching this
        integration.
        """
        assert _establishment_timezone(hass, {}) == str(hass.config.time_zone)

    def test_an_empty_string_is_not_a_timezone_either(
        self, hass: HomeAssistant
    ) -> None:
        """An emptied box must read as absence, not as a zone named "".

        Home Assistant's frontend may send a cleared optional text field as
        `""` rather than omitting it, and an entry stored before the field
        became clearable can carry one. `ZoneInfo("")` raises, so treating it
        as a value would fail the entry on a value nobody chose.
        """
        assert _establishment_timezone(hass, {OPT_ESTABLISHMENT_TIMEZONE: ""}) == str(
            hass.config.time_zone
        )

    def test_a_stored_zone_overrides_the_instance(self, hass: HomeAssistant) -> None:
        """The reason the option exists at all.

        A family in one timezone may follow a school in another, and PRONOTE
        never says which -- the protocol does not carry it. So a deliberate pin
        has to win over the instance, or the option would be decoration.
        """
        assert (
            _establishment_timezone(
                hass, {OPT_ESTABLISHMENT_TIMEZONE: "Pacific/Noumea"}
            )
            == "Pacific/Noumea"
        )

    def test_an_unusable_stored_zone_falls_back_and_says_which(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Falling back beats raising, for the reason `bounded_option` exists.

        The options page is the only place that checks this string, and it is
        not on the path a restored backup or a hand-edited `.storage` takes.
        Raising there costs every entity of the account with `unavailable` as
        the only explanation; falling back costs an hour of offset on a school
        abroad and writes down what it did.
        """
        caplog.set_level(logging.WARNING)

        resolved = _establishment_timezone(
            hass, {OPT_ESTABLISHMENT_TIMEZONE: "Europe/Pariss"}
        )

        assert resolved == str(hass.config.time_zone)
        assert "Europe/Pariss" in caplog.text
        assert str(hass.config.time_zone) in caplog.text

    async def test_an_entry_pinned_to_a_nonsense_zone_still_loads(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        school_day: Any,
        no_spacing: None,
    ) -> None:
        """The failure this is about, at the altitude a user sees it from.

        `PronoteGateway.__init__` calls `ZoneInfo(name)` and
        `PronoteAccount.__init__` calls that, so an unknown zone used to raise
        out of `async_setup_entry`: no session, no entities, and a log line
        about zone files rather than about a setting. The account is the
        application's data source, so a typo in one text box must not be able
        to take it off the air.
        """
        del school_day, no_spacing
        hass.config_entries.async_update_entry(
            mock_entry,
            options={**mock_entry.options, OPT_ESTABLISHMENT_TIMEZONE: "Paris"},
        )

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

        assert mock_entry.state is ConfigEntryState.LOADED
        assert str(mock_entry.runtime_data.gateway.timezone) == str(
            hass.config.time_zone
        )
