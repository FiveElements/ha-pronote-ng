"""Services: the on-demand actions, and the secrets that must not become states.

Two rules are load-bearing here, and both are tested by their failure mode
rather than by their happy path.

**A secret is returned, never stored (§8.2).** The iCal URL grants read access
to a child's whole timetable with no username and no password. As a state it
would land in the recorder database, in every backup, in screenshots and in
bug reports; as a service response it exists for the length of one script run.
``test_the_ical_url_never_reaches_a_state_or_a_diagnostic`` is what holds that
line -- it sweeps every state, every attribute and the diagnostics payload for
the URL the service just handed back.

**A write is off by default (§8.3).** Ticking a homework item, marking news read
and sending a message all change what the establishment sees, so each is
refused -- with an explanation, not silently -- until the user turns writes on.

The identity service gets a test of its own for a subtler reason: it must post
through ``communication.post`` with an explicit ``ressource``, because
``client.post`` stamps the *account holder* as ``membre`` and on a parent
account returns the **parent's** birth date, e-mail and INE number under the
child's device. That is a wrong-attribution disclosure of exactly the fields
§8.2 keeps out of the state machine, and it is invisible to any test that only
checks the response is well-formed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
import voluptuous as vol

from custom_components.pronote_ng.const import (
    DOMAIN,
    OPT_WRITE_OPERATIONS_ENABLED,
    SERVICE_GENERATE_TIMETABLE_PDF,
    SERVICE_GET_ICAL_URL,
    SERVICE_GET_IDENTITY,
    SERVICE_GET_RATE_LIMIT_STATUS,
    SERVICE_MARK_HOMEWORK_DONE,
    SERVICE_MARK_INFORMATION_READ,
    SERVICE_REFRESH,
    SERVICE_SEND_MESSAGE,
    Tier,
)

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures.client import FakeThread

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pronote_ng.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)


def _device_id(hass: HomeAssistant, entry_id: str, identifier: str) -> str:
    """The registry id of one of our devices, by its PRONOTE identifier.

    Looked up *through the owning entry*: from HA 2026.9 an identifier is only
    unique within a config entry, and the entry-less lookup this used to do is
    deprecated for being ambiguous. Passing the entry is not a workaround, it
    is the correct question -- two accounts on one instance can legitimately
    follow children whose identifiers collide.
    """
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, identifier), entry_id)
    assert device is not None, f"no device for {identifier}"
    assert entry_id in device.config_entries
    return device.id


def _child_device(hass: HomeAssistant, entry: MockConfigEntry, student: str) -> str:
    """One child's device id."""
    return _device_id(hass, entry.entry_id, f"{entry.entry_id}_{student}")


def _account_device(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The account device, which carries the diagnostics."""
    return _device_id(hass, entry.entry_id, entry.entry_id)


@pytest.fixture(name="writes_on")
async def writes_on_fixture(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> PronoteAccount:
    """Turn write operations on, which reloads the entry (§7.3).

    Depends on `account` so the entry is set up *before* the options change:
    an update on an unloaded entry reloads nothing, and the test then reads
    `runtime_data` off an entry that was never started.

    Returns the **new** account. The reload builds one, and holding the old
    reference is how a test ends up asserting against a torn-down object --
    which is also exactly what a user's automation would do if the integration
    handed out long-lived references, so the fixture models the right thing.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()
    reloaded: PronoteAccount = mock_entry.runtime_data
    assert reloaded.write_enabled
    return reloaded


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_ten_refreshes_cost_one_collection(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§5.1: the service raises a priority, it does not place a call.

    So it *does* lead to a collection -- a refresh that fetched nothing would
    be a refresh that does nothing -- but the scheduler still decides. Ten
    presses inside one interval therefore cost one batch, because the first
    collection moves the tier's deadline and the nine others find nothing due.

    Asserted on the requests actually placed rather than on the ``boosted``
    flag, which is cleared by the collection it triggers: a test watching the
    flag would pass just as happily if the collection never happened.
    """
    device_id = _child_device(hass, mock_entry, STUDENT_ONE)
    before = parent_client.posted_names.count("DernieresNotes")

    for _ in range(10):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {"device_id": device_id, "tiers": ["marks"]},
            blocking=True,
        )
    await hass.async_block_till_done()

    # One request per child, once -- not ten times, and not zero.
    after = parent_client.posted_names.count("DernieresNotes")
    assert after - before == len(account.students)


async def test_refresh_will_not_force_a_login(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """``session`` is absent from the refreshable tiers on purpose.

    Forcing a re-login on demand is the one gesture that can get an address
    suspended, and it is never what somebody pressing "refresh" wants
    (annexe B §3).
    """
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "tiers": ["session"],
            },
            blocking=True,
        )


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


async def test_an_unknown_device_is_refused_by_name(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """A validation error, so the automation editor shows it to the user."""
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, SERVICE_REFRESH, {"device_id": "not-a-device"}, blocking=True
        )

    assert raised.value.translation_key == "unknown_device"


async def test_a_device_from_another_integration_is_refused(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Targeting the wrong device must say so rather than act on a guess."""
    registry = dr.async_get(hass)
    stranger = registry.async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={("some_other_domain", "whatever")},
        name="Not ours",
    )

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, SERVICE_REFRESH, {"device_id": stranger.id}, blocking=True
        )

    assert raised.value.translation_key == "device_not_pronote"


async def test_a_two_child_account_must_say_which_child(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Guessing would send a message, or return an identity, from the wrong one.

    The account device is accepted as a shorthand only where it is unambiguous,
    which is an account with a single child.
    """
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _account_device(hass, mock_entry)},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "student_required"
    assert raised.value.translation_placeholders is not None
    assert "Enfant Un" in raised.value.translation_placeholders["children"]


# ---------------------------------------------------------------------------
# The secrets that are returned and not stored
# ---------------------------------------------------------------------------


async def test_the_ical_url_never_reaches_a_state_or_a_diagnostic(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§8.2, checked by sweeping everything that gets persisted.

    Anyone holding this URL reads the child's timetable with no credential, and
    states go to the recorder, the backups, the screenshots and the bug
    reports. The service response is the only place it may appear.
    """
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_ICAL_URL,
        {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    url = response["url"]
    assert url
    assert parent_client.ical_calls == 1

    for state in hass.states.async_all():
        assert url not in state.state
        assert url not in json.dumps(dict(state.attributes), default=str)

    assert url not in json.dumps(account.diagnostics(), default=str)


async def test_the_identity_is_asked_for_as_the_child_and_not_as_the_member(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """The wrong-attribution disclosure this service exists to avoid.

    ``client.post`` stamps the account holder as ``membre``; on a parent account
    that returns the **parent's** birth date, e-mail, telephone number and INE
    number, which the service would then hand back attributed to the child.
    ``ClientInfo._cache()`` bypasses ``ClientBase.post`` for exactly this
    reason, and the gateway does the same -- explicitly, with the child's own
    ``ressource``.
    """
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_IDENTITY,
        {"device_id": _child_device(hass, mock_entry, STUDENT_TWO)},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert response["name"]

    assert [name for name, _body in parent_client.communication_posts] == [
        "PageInfosPerso"
    ]
    _name, body = parent_client.communication_posts[0]
    assert body["Signature"]["ressource"] == {"N": STUDENT_TWO, "G": 4}
    assert "PageInfosPerso" not in parent_client.posted_names

    # None of it may become an attribute. Swept on the INE number, the birth
    # date and the address rather than on the name: the child's *name* is the
    # device name and appears in every `friendly_name` by design, so asserting
    # on it would only prove that entities are named after children.
    identifying = [
        response["ine_number"],
        response["birth_date"],
        response["email"],
        response["phone"],
        *response["address"],
    ]
    assert all(identifying), "the fixture must supply every identity field"
    for state in hass.states.async_all():
        attributes = json.dumps(dict(state.attributes), default=str)
        for value in identifying:
            assert str(value) not in attributes, (
                f"{value!r} leaked into {state.entity_id}"
            )


async def test_a_timetable_pdf_is_rendered_in_the_orientation_asked_for(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """And returned as a response, because the link carries its own authority."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GENERATE_TIMETABLE_PDF,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "orientation": "landscape",
        },
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert "portrait=False" in response["url"]
    assert parent_client.pdf_calls == 1


async def test_the_budget_can_be_read_without_spending_any_of_it(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§6.6: safe to poll from a dashboard while tuning the cadence.

    And its response is JSON-serialisable with no secret in it, which is the
    part worth pinning: this is the one service whose payload a user is
    encouraged to paste into an issue.
    """
    before = len(parent_client.posts)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_RATE_LIMIT_STATUS,
        {"device_id": _account_device(hass, mock_entry)},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert len(parent_client.posts) == before
    assert response["calls_today"] > 0
    assert "scheduler" in response
    assert "session" in response

    payload = json.dumps(response, default=str)
    for secret in (
        mock_entry.data["password"],
        mock_entry.data["username"],
        "icalsecurise",
    ):
        assert secret not in payload


# ---------------------------------------------------------------------------
# Writes -- off by default (§8.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        pytest.param(
            SERVICE_MARK_HOMEWORK_DONE,
            {"homework_id": "HOMEWORK-1"},
            id="mark-homework-done",
        ),
        pytest.param(
            SERVICE_MARK_INFORMATION_READ,
            {"information_id": "INFO-1"},
            id="mark-information-read",
        ),
        pytest.param(
            SERVICE_SEND_MESSAGE,
            {"message": "Bonjour", "subject": "Question", "recipients": ["Prof. Un"]},
            id="send-message",
        ),
    ],
)
async def test_a_write_is_refused_with_a_reason_by_default(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
    service: str,
    payload: dict[str, Any],
) -> None:
    """Refused, and refused *audibly*: silently ignoring it would be worse."""
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE), **payload},
            blocking=True,
        )

    assert raised.value.translation_key == "writes_disabled"
    assert len(parent_client.posts) == before


async def test_ticking_a_homework_item_posts_it_and_asks_for_a_re_read(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Re-read *soon*, not immediately.

    The tick is local truth already, and an instant re-fetch would double the
    cost of every checkbox.
    """

    await hass.services.async_call(
        DOMAIN,
        SERVICE_MARK_HOMEWORK_DONE,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "homework_id": "HOMEWORK-1",
            "done": True,
        },
        blocking=True,
    )

    assert parent_client.body_for("SaisieTAFFaitEleve") == {
        "listeTAF": [{"N": "HOMEWORK-1", "TAFFait": True}]
    }
    assert writes_on.scheduler.diagnostics()[str(Tier.HOMEWORK)]["boosted"] is True


async def test_marking_a_news_item_read_names_the_child_as_the_public(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """``public`` has to be the child, not the account holder.

    Marking it read as the parent leaves the child's own copy unread, so the
    news sensor keeps reporting it and the automation fires again tomorrow.
    """

    await hass.services.async_call(
        DOMAIN,
        SERVICE_MARK_INFORMATION_READ,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_TWO),
            "information_id": "INFO-1",
        },
        blocking=True,
    )

    body = parent_client.body_for("SaisieActualites")
    assert body["listeActualites"][0]["N"] == "INFO-1"
    assert body["listeActualites"][0]["lue"] is True
    assert body["listeActualites"][0]["public"] == {"N": STUDENT_TWO}


async def test_a_new_thread_needs_a_subject(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Checked before a request is spent, not after."""
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Bonjour",
                "recipients": ["Prof. Un"],
            },
            blocking=True,
        )

    assert raised.value.translation_key == "subject_required"
    assert len(parent_client.posts) == before


async def test_a_message_may_reply_or_open_a_thread_but_not_both(
    hass: HomeAssistant, mock_entry: MockConfigEntry, writes_on: PronoteAccount
) -> None:
    """Enforced in the schema, so a malformed script fails before it costs."""
    device_id = _child_device(hass, mock_entry, STUDENT_ONE)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": device_id,
                "message": "Bonjour",
                "discussion_id": "THREAD-1",
                "recipients": ["Prof. Un"],
            },
            blocking=True,
        )

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"device_id": device_id, "message": "Bonjour"},
            blocking=True,
        )


async def test_a_new_thread_reaches_the_named_recipients(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Matched on the published name, the only handle a user can read off."""

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "message": "Bonjour",
            "subject": "Sortie",
            "recipients": ["prof. un"],
        },
        blocking=True,
    )

    assert parent_client.new_discussions == [("Sortie", "Bonjour", ["Prof. Un"])]


async def test_an_unmatched_recipient_is_refused_with_the_available_names(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """A message that quietly went to nobody is worse than an error."""

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Bonjour",
                "subject": "Sortie",
                "recipients": ["Personne"],
            },
            blocking=True,
        )

    assert raised.value.translation_key == "recipient_not_found"
    assert raised.value.translation_placeholders is not None
    assert "Prof. Un" in raised.value.translation_placeholders["available"]
    assert parent_client.new_discussions == []


async def test_replying_to_a_thread_that_is_gone_is_refused(
    hass: HomeAssistant, mock_entry: MockConfigEntry, writes_on: PronoteAccount
) -> None:
    """The thread list is re-read on every reply, so it can be missing."""

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Merci",
                "discussion_id": "THREAD-GONE",
            },
            blocking=True,
        )

    assert raised.value.translation_key == "discussion_not_found"


async def test_replying_to_a_closed_thread_is_refused(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """PRONOTE closes threads; posting into one silently loses the reply."""
    parent_client.threads.append(FakeThread("THREAD-CLOSED", closed=True))

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Merci",
                "discussion_id": "THREAD-CLOSED",
            },
            blocking=True,
        )

    assert raised.value.translation_key == "discussion_closed"


async def test_a_reply_is_billed_at_what_it_actually_costs(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Three requests, not one -- and the limiter has to be told so.

    A reply re-lists the threads (``ListeMessagerie``), reads the message being
    answered (``ListeMessages``) and posts (``SaisieMessage``). A write that
    under-reports its cost corrupts the very budget that protects the account
    (annexe B §8).
    """
    parent_client.threads.append(FakeThread("THREAD-1"))
    before = writes_on.limiter.snapshot_counters()["calls_today"]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "message": "Merci",
            "discussion_id": "THREAD-1",
        },
        blocking=True,
    )

    after = writes_on.limiter.snapshot_counters()["calls_today"]
    assert after - before == 3
    assert parent_client.threads[-1].replies == ["Merci"]
