"""Adding the account, and the two things that must not happen while doing it.

**No secret is persisted that §8.1 promised not to keep.** The 2FA PIN, the QR
payload and the QR code's four-digit PIN are single-use: the flow needs them for
one login and the entry must not carry them afterwards. "We do not store your
PIN" is only true if the entry really does not have it, so that is asserted on
the stored data rather than trusted to the code that builds it.

**No login escapes the limiter.** This is the one login path a *person* can
repeat at will -- the re-authentication form re-displays itself on every
failure -- and it went straight to ``build_client``: no counter, no guard. The
three-attempt rail exists because PRONOTE's one sanction applies to an address
rather than to an account (annexe B §3.1), and a parent convinced their password
is right could walk straight past it. ``test_the_flow_stops_asking_after_three``
is the test that now holds that line.

Everything is driven through ``hass.config_entries.flow``, not by calling the
steps directly: the flow's contract is what Home Assistant does with it -- the
menu, the unique id, the abort, the reload -- and half of that lives in the
framework rather than in the handler.
"""

from __future__ import annotations

from collections.abc import Iterator  # noqa: TC003 -- a pytest fixture annotation
import json
import pathlib
from string import Formatter
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.pronote_ng.config_flow import (
    ProbeBootstrapFailed,
    ProbeInvalidCredentials,
    ProbeMfaRequired,
    ProbeQrInvalid,
)
from custom_components.pronote_ng.const import (
    CONF_ACCOUNT_PIN,
    CONF_CHILDREN,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    CONF_UUID,
    DOMAIN,
    LoginMode,
)
from custom_components.pronote_ng.login_guard import login_guard
from custom_components.pronote_ng.urls import public_url, url_host

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = REQUIRES_HASS

#: A deep link of the shape establishments really mail out. The query string is
#: the point: it is what must not survive into the config entry.
PASTED_URL = (
    "https://demo.example.invalid/pronote/parent.html"
    "?login=true&identifiant=NOT-A-REAL-TICKET"
)
TRIMMED_URL = "https://demo.example.invalid/pronote/parent.html"

QR_PAYLOAD = {
    "jeton": "not-a-real-token",
    "login": "not-a-real-login",
    "url": PASTED_URL,
}


@pytest.fixture(name="no_setup", autouse=True)
def no_setup_fixture() -> Iterator[None]:
    """Stop a created entry from setting itself up for real.

    A flow that reaches `async_create_entry` makes Home Assistant set the entry
    up, which builds a `PronoteAccount`, a worker thread and a real login
    attempt -- none of which is what these tests are about, and the thread is
    left behind because the flow never tears it down. Autouse, because the
    failure it prevents shows up as a lingering-thread error in the *next*
    test's teardown, which is a genuinely confusing place to debug from.
    """
    with patch("custom_components.pronote_ng.async_setup_entry", return_value=True):
        yield


def _outcome(*children: tuple[str, str]) -> dict[str, Any]:
    """What ``probe_account`` hands back on success."""
    return {
        "account_id": "demo.example.invalid|parent-under-test",
        "children": list(children),
        "title": "PRONOTE",
        "username": "parent-under-test",
        "password": "not-a-real-password",
    }


async def _start(hass: HomeAssistant) -> str:
    """Open the flow and step through the menu to the credentials form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"qr_code", "credentials", "ent"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    return str(result["flow_id"])


async def _submit_credentials(
    hass: HomeAssistant,
    flow_id: str,
    *,
    url: str = PASTED_URL,
    pin: str | None = None,
) -> dict[str, Any]:
    """Fill in the credentials form."""
    payload: dict[str, Any] = {
        CONF_PRONOTE_URL: url,
        "username": "parent-under-test",
        "password": "not-a-real-password",
    }
    if pin is not None:
        payload[CONF_ACCOUNT_PIN] = pin
    return dict(await hass.config_entries.flow.async_configure(flow_id, payload))


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


async def test_a_single_child_account_is_created_straight_away(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """One child means nothing to choose, so no extra step."""
    flow_id = await _start(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHILDREN] == ["STUDENT-1"]
    assert result["data"][CONF_LOGIN_MODE] == LoginMode.CREDENTIALS.value


async def test_a_parent_account_asks_which_children_to_follow(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """And the default is all of them, which is what a parent expects."""
    flow_id = await _start(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un"), ("STUDENT-2", "Enfant Deux")),
    ):
        result = await _submit_credentials(hass, flow_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "children"

        result = dict(
            await hass.config_entries.flow.async_configure(
                flow_id, {CONF_CHILDREN: ["STUDENT-2"]}
            )
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHILDREN] == ["STUDENT-2"]


async def test_the_same_account_cannot_be_added_twice(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """Otherwise every entity exists twice and every automation fires twice.

    The entry's ``unique_id`` is the account, which is why the fixture's entry
    and this flow collide: same server, same login.
    """
    flow_id = await _start(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_ent_account_records_its_provider(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The provider is resolved by name at login time, so it has to be stored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ent"}
    )
    assert result["type"] is FlowResultType.FORM

    schema_keys = {str(key.schema) for key in result["data_schema"].schema}
    assert CONF_ENT in schema_keys

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PRONOTE_URL: PASTED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
                CONF_ENT: "ac_reunion",
            },
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_ENT] == "ac_reunion"
    assert created["data"][CONF_LOGIN_MODE] == LoginMode.ENT.value


# ---------------------------------------------------------------------------
# The address
# ---------------------------------------------------------------------------


async def test_the_query_string_of_the_pasted_address_is_not_stored(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """§8.2, at the point where the string enters the integration.

    Establishments mail out deep links and ENT portals bounce back through
    single sign-on with a ticket in the URL, so what a parent pastes regularly
    carries a session parameter. Stored, it went on to appear in a repair
    issue's placeholders -- written to ``.storage`` and rendered in the Repairs
    panel -- and in the diagnostics download, the file users are asked to
    attach to a public issue.
    """
    flow_id = await _start(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        result = await _submit_credentials(hass, flow_id)

    assert result["data"][CONF_PRONOTE_URL] == TRIMMED_URL
    assert "identifiant" not in json.dumps(result["data"])
    # And the login itself is attempted against the trimmed address, so this is
    # not a display-only sanitisation that leaves the real value in flight.
    assert probe.call_args.args[0][CONF_PRONOTE_URL] == TRIMMED_URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(PASTED_URL, TRIMMED_URL, id="query-string"),
        pytest.param(f"{TRIMMED_URL}#fragment", TRIMMED_URL, id="fragment"),
        pytest.param(
            "https://user:secret@demo.example.invalid/pronote/parent.html",
            TRIMMED_URL,
            id="userinfo",
        ),
        pytest.param(
            "https://demo.example.invalid:8443/pronote/parent.html",
            "https://demo.example.invalid:8443/pronote/parent.html",
            id="keeps-a-port",
        ),
        pytest.param("not a url at all", "not a url at all", id="unparseable"),
        pytest.param("", "", id="empty"),
    ],
)
def test_the_address_is_trimmed_but_never_second_guessed(
    raw: str, expected: str
) -> None:
    """A port is kept, a credential is dropped, and nonsense is passed through.

    Refusing an address the user can see in their own browser would be worse
    than storing an odd one: the login will fail with the server's own message,
    which is a better diagnosis than anything this function could invent.
    """
    assert public_url(raw) == expected


def test_the_host_is_extracted_for_the_places_that_only_need_it() -> None:
    """Used by the diagnostics, where even the path is more than is needed."""
    assert url_host(PASTED_URL) == "demo.example.invalid"
    assert url_host("nonsense") == ""
    assert url_host(None) == ""


# ---------------------------------------------------------------------------
# Failures, and the guard rail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(ProbeInvalidCredentials(), "invalid_auth", id="wrong-password"),
        pytest.param(ProbeMfaRequired(), "mfa_required", id="pin-demanded"),
        pytest.param(ProbeBootstrapFailed(), "bootstrap_failed", id="no-session-page"),
        pytest.param(OSError("refused"), "cannot_connect", id="unreachable"),
        pytest.param(RuntimeError("surprise"), "unknown", id="unexpected"),
    ],
)
async def test_a_failed_login_re_displays_the_form_with_a_reason(
    hass: HomeAssistant, no_spacing: None, error: Exception, expected: str
) -> None:
    """Never an abort: the user is one corrected field away from succeeding."""
    flow_id = await _start(hass)

    with patch("custom_components.pronote_ng.config_flow._probe", side_effect=error):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": expected}


async def test_the_flow_stops_asking_after_three_refusals(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The guard rail that was not connected to this path at all.

    ``max_failed_logins_per_hour`` is three, and the fourth attempt must not be
    *sent*: PRONOTE's one sanction applies to the address, not to the account,
    and it is undocumented and costly (annexe B §3.1). The form still comes
    back -- with "too many attempts" rather than "wrong password", because
    nothing was tried.
    """
    for _attempt in range(3):
        flow_id = await _start(hass)
        with patch(
            "custom_components.pronote_ng.config_flow._probe",
            side_effect=ProbeInvalidCredentials(),
        ):
            result = await _submit_credentials(hass, flow_id)
        assert result["errors"] == {"base": "invalid_auth"}

    flow_id = await _start(hass)
    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ) as probe:
        result = await _submit_credentials(hass, flow_id)

    assert result["errors"] == {"base": "rate_limited"}
    assert probe.call_count == 0, "the fourth attempt reached the server"


async def test_the_guard_counts_the_requests_a_flow_login_really_spends(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Five for a login, and ten for QR enrolment, which does two.

    Counted because the daily cap is on HTTP requests: a flow that spent them
    invisibly made every subsequent budget figure wrong, and the diagnostics
    would have shown an integration well inside a cap it had already passed.
    """
    guard = login_guard(hass)
    before = guard.calls_today

    flow_id = await _start(hass)
    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        await _submit_credentials(hass, flow_id)

    assert guard.calls_today - before == 5
    assert guard.snapshot_counters()["logins_today"] >= 1


# ---------------------------------------------------------------------------
# QR enrolment
# ---------------------------------------------------------------------------


async def _start_qr(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "qr_code"}
    )
    assert result["type"] is FlowResultType.FORM
    return str(result["flow_id"])


async def test_qr_enrolment_keeps_neither_the_payload_nor_its_pin(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """§8.1, and both halves matter.

    The payload is an autonomous bearer until PRONOTE invalidates it, and the
    four-digit code decrypts it. Keeping either in the entry would put a working
    credential in ``.storage`` -- and in every backup -- for the sake of a value
    that is single-use by construction.
    """
    flow_id = await _start_qr(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = dict(
            await hass.config_entries.flow.async_configure(
                flow_id,
                {
                    CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD),
                    CONF_QR_PIN: "1234",
                },
            )
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_QR_PAYLOAD not in data
    assert CONF_QR_PIN not in data
    assert CONF_ACCOUNT_PIN not in data
    # The device UUID, on the other hand, has to persist: PRONOTE ties the
    # token to it, and a new one at the next login means a refused token.
    assert data[CONF_UUID]
    assert data[CONF_PRONOTE_URL] == TRIMMED_URL


async def test_a_qr_payload_that_is_not_one_is_refused_before_any_request(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Field-level, so the user sees which box is wrong."""
    flow_id = await _start_qr(hass)

    with patch("custom_components.pronote_ng.config_flow._probe") as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: "this is not json", CONF_QR_PIN: "1234"}
        )

    assert result["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0


async def test_a_consumed_qr_code_says_to_generate_a_new_one(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A QR code is single-use, and PRONOTE invalidates it on enrolment."""
    flow_id = await _start_qr(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        side_effect=ProbeQrInvalid(),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert result["errors"] == {"base": "invalid_qr"}


async def test_qr_enrolment_is_charged_for_the_two_logins_it_performs(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """``qrcode_login`` then ``token_login``: two handshakes, ten requests.

    Declared as two so the budget is honest, and exempted from the
    three-attempt guard rather than eating two thirds of it (annexe B §3.3).
    """
    guard = login_guard(hass)
    before = guard.calls_today

    flow_id = await _start_qr(hass)
    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert guard.calls_today - before == 10


# ---------------------------------------------------------------------------
# Re-authentication
# ---------------------------------------------------------------------------


async def test_reauthentication_asks_for_the_pin_and_forgets_it_again(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The whole reason this flow exists (§8.1).

    A secret we refuse to store is a secret we must know how to ask for again;
    and having asked, we must not keep it -- otherwise "we do not store your
    PIN" is true only until the first time PRONOTE demands one.
    """
    result = await mock_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"password": "corrected-not-real", CONF_ACCOUNT_PIN: "4321"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Supplied to the login...
    assert probe.call_args.args[0][CONF_ACCOUNT_PIN] == "4321"
    # ...and absent from what was persisted.
    assert CONF_ACCOUNT_PIN not in mock_entry.data
    assert mock_entry.data["password"] == "not-a-real-password"


async def test_reauthentication_lifts_the_hold_a_human_can_only_lift(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """``reset_after_reauth`` had no caller in the product at all.

    Which made the MFA hold inescapable: it exists precisely because a person
    has to supply a PIN, and the act of supplying it did not clear it. Here the
    guard is driven into the credentials hold first, then the re-authentication
    has to go through anyway.
    """
    guard = login_guard(hass)
    for _attempt in range(3):
        flow_id = await _start(hass)
        with patch(
            "custom_components.pronote_ng.config_flow._probe",
            side_effect=ProbeInvalidCredentials(),
        ):
            await _submit_credentials(hass, flow_id)
    assert not guard.may_login().allowed

    result = await mock_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "corrected-not-real"}
        )

    assert probe.call_count == 1, "the human's corrected password was not even tried"
    assert result["reason"] == "reauth_successful"
    assert guard.may_login().allowed


async def test_a_reauthentication_that_fails_says_so_and_stays_open(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The form comes back, because the next attempt may be the right one."""
    result = await mock_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong-not-real"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    # And the form names the establishment's address, trimmed, so the user can
    # see *which* account is being repaired.
    placeholders = result["description_placeholders"] or {}
    assert placeholders["url"] == public_url(mock_entry.data[CONF_PRONOTE_URL])


@pytest.mark.parametrize("payload", ["[1, 2]", '"just a string"', "42", "null", "true"])
async def test_a_qr_payload_that_is_json_but_not_an_object_is_refused(
    hass: HomeAssistant, no_spacing: None, payload: str
) -> None:
    """Valid JSON of the wrong shape, which is what a bad decode produces.

    A reader handed a cropped or blurred code regularly returns something that
    parses -- a bare number, a string, a list -- and this was rejected with a
    ``TypeError`` while the flow caught only ``ValueError``. The result was an
    unhandled exception and Home Assistant's generic "unknown error occurred",
    which tells a parent nothing about the box they should re-scan into.
    """
    flow_id = await _start_qr(hass)

    with patch("custom_components.pronote_ng.config_flow._probe") as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: payload, CONF_QR_PIN: "1234"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0, "nothing may reach the school for a bad payload"


async def test_the_first_screen_supplies_every_placeholder_its_text_uses(
    hass: HomeAssistant,
) -> None:
    """A placeholder nobody fills is rendered literally, braces and all.

    The login-mode screen carries the integration's logo as a markdown image,
    and the address is passed as a description placeholder rather than written
    into the translation string -- hassfest rejects a URL inside one and names
    this as the mechanism to use instead. The cost of that indirection is that
    the string and the code that feeds it can drift apart, and the failure is
    cosmetic-but-glaring: the very first screen of the flow greets a parent
    with `![Pronote Next Generation]({logo})`.

    So this reads the placeholders the catalogue actually asks for and checks
    the flow supplies each one, rather than asserting a hard-coded name.
    """
    strings = json.loads(
        (pathlib.Path("custom_components/pronote_ng/strings.json")).read_text(
            encoding="utf-8"
        )
    )
    description = strings["config"]["step"]["user"]["description"]
    wanted = {name for _, name, _, _ in Formatter().parse(description) if name}
    assert wanted, "the step's text uses no placeholder; this test is now moot"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.MENU
    supplied = result.get("description_placeholders") or {}
    missing = wanted - set(supplied)
    assert not missing, f"the flow does not supply {sorted(missing)}"
    assert supplied["logo"].startswith("https://")


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        pytest.param('{"login": "l", "jeton": "j"}', ["url"], id="no url"),
        pytest.param('{"url": "u"}', ["jeton", "login"], id="only the url"),
        pytest.param("{}", ["jeton", "login", "url"], id="an empty object"),
    ],
)
async def test_a_qr_payload_missing_a_key_is_refused_before_any_request(
    hass: HomeAssistant, no_spacing: None, payload: str, missing: list[str]
) -> None:
    """A JSON object of the right type and the wrong contents.

    This is not the cropped-code case above: it is what a QR reader produces
    from *another application's* code, or from a PRONOTE code generated by a
    version that names its fields differently. The three keys are the ones
    ``pronotepy`` indexes without checking, so letting such a payload through
    turns a re-scan into a `KeyError` inside the login -- reported as
    "unknown error" after having spent a login attempt against the address.
    """
    flow_id = await _start_qr(hass)

    with patch("custom_components.pronote_ng.config_flow._probe") as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: payload, CONF_QR_PIN: "1234"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0, "nothing may reach the school for a bad payload"


async def test_a_failed_ent_login_comes_back_to_the_ent_form(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Each mode re-displays *its own* form, and ENT is the one that differs.

    `_async_reshow` branches on the stored mode. Falling through to the
    credentials form -- which is what happens if the ENT arm is missed -- loses
    the provider the user chose, so the retry silently attempts a direct login
    against an establishment that only accepts federated ones, and fails for a
    second, unrelated reason.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ent"}
    )

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        side_effect=ProbeInvalidCredentials("the identity provider refused"),
    ):
        refused = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PRONOTE_URL: PASTED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
                CONF_ENT: "ac_reunion",
            },
        )

    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "ent"
    assert refused["errors"] == {"base": "invalid_auth"}
    assert CONF_ENT in {str(key.schema) for key in refused["data_schema"].schema}


async def test_reauthentication_accepts_a_pin_without_a_new_password(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The commonest reason this form is shown at all.

    PRONOTE asked for a security code; the password never changed and the user
    has no reason to retype it. Both fields are optional and each is applied
    only when filled, so an empty password box must leave the stored one alone
    rather than overwrite it with `""` -- which would turn one solvable
    interruption into a permanently broken entry.
    """
    stored = mock_entry.data["password"]
    result = await mock_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.pronote_ng.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT_PIN: "1234"}
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert mock_entry.data["password"] == stored
    assert CONF_ACCOUNT_PIN not in mock_entry.data


def test_the_flow_never_imports_pronotepy_to_be_added(
    hass: HomeAssistant,
) -> None:
    """`_probe` imports inside the function, and that is load-bearing twice.

    Not at module scope, because a broken or missing dependency would then turn
    "the integration cannot log in" into "the integration cannot be added at
    all" -- and the config flow is the only screen that could ever explain the
    problem.

    Not inside the coroutine either: Home Assistant instruments `importlib` and
    reports an import performed on the event loop as a blocking call. So it
    happens here, on the worker thread, immediately before the login it belongs
    to. Neither half of that is visible from reading `_probe`'s two lines,
    which is why it is asserted.
    """
    from custom_components.pronote_ng import config_flow

    assert not any(
        name == "pronotepy" or name.startswith("pronotepy.")
        for name in vars(config_flow)
    ), "pronotepy is bound in the flow's namespace, so it was imported at module scope"

    with patch(
        "custom_components.pronote_ng.flow_login.probe_account",
        return_value={"account_id": "a", "children": [], "title": "t"},
    ) as probe_account:
        assert config_flow._probe({"any": "data"})["account_id"] == "a"

    assert probe_account.call_args.args == ({"any": "data"},)
