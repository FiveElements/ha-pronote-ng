"""The configuration and options flows.

Three ways in, because PRONOTE has three: a QR code from the mobile app,
a direct username and password, and an ENT federated login. They are not
variations of one form -- QR enrolment produces a rotating token and needs a
device UUID, credentials mode does not, and ENT needs a provider chosen from a
list -- so each gets its own step rather than one form with fields that are
sometimes ignored.

Two things here are load-bearing.

**The 2FA PIN is never stored (§8.1).** It is asked for, used for the one login
that needs it, and dropped. Everything else -- the token, the UUID, the client
identifier -- is persisted, because losing those means losing access. The
re-authentication flow exists precisely so a secret we refuse to keep can be
requested again.

**Every login here goes through the executor and the limiter.** A config flow
that logged in on the event loop would block Home Assistant for the length of a
PRONOTE handshake, and one that retried a wrong password freely would spend the
three-attempt guard rail that protects the address from suspension (annexe B
§3).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final
import uuid as uuid_module

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
import voluptuous as vol

from .const import (
    BRAND_LOGO_URL,
    CONF_ACCOUNT_PIN,
    CONF_CHILDREN,
    CONF_DEVICE_NAME,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    CONF_UUID,
    DEFAULT_HOMEWORK_HORIZON,
    DEFAULT_MASTER_TICK,
    DEFAULT_SESSION_STRATEGY,
    DEFAULT_STALE_AFTER,
    DEFAULT_TIER_INTERVALS,
    DEFAULT_WAKE_MARGIN,
    DEFAULT_WRITE_OPERATIONS_ENABLED,
    DOMAIN,
    OPT_BACKOFF_BASE,
    OPT_BACKOFF_MAX,
    OPT_BOOTSTRAP_HOLD,
    OPT_BURST_SIZE,
    OPT_CONNECT_TIMEOUT,
    OPT_CREDENTIALS_HOLD,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_HOMEWORK_HORIZON,
    OPT_MASTER_TICK,
    OPT_MAX_FAILED_LOGINS_PER_HOUR,
    OPT_MAX_LOGINS_PER_DAY,
    OPT_MAX_REQUESTS_PER_DAY,
    OPT_MAX_REQUESTS_PER_HOUR,
    OPT_MAX_WAIT,
    OPT_MIN_REQUEST_INTERVAL,
    OPT_QUIET_END,
    OPT_QUIET_HOURS_ENABLED,
    OPT_QUIET_START,
    OPT_READ_TIMEOUT,
    OPT_SESSION_STRATEGY,
    OPT_STALE_AFTER,
    OPT_TIER_ENABLED,
    OPT_TIER_INTERVAL,
    OPT_WAKE_MARGIN,
    OPT_WRITE_OPERATIONS_ENABLED,
    OPTION_RANGES,
    TIER_INTERVAL_RANGE,
    LoginMode,
    SessionStrategy,
)
from .login_guard import clear_login_penalties, login_guard
from .options import estimate_daily_requests, tier_enabled, tier_intervals
from .ratelimit import REQUESTS_PER_LOGIN, LoginOutcome, LoginRefusedByLimiter
from .urls import public_url

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER: Final = logging.getLogger(__name__)

STEP_USER: Final = "user"
STEP_QR_CODE: Final = "qr_code"
STEP_CREDENTIALS: Final = "credentials"
STEP_ENT: Final = "ent"
STEP_CHILDREN: Final = "children"
STEP_REAUTH_CONFIRM: Final = "reauth_confirm"
STEP_REAUTH_QR: Final = "reauth_qr"


def _ent_options() -> list[SelectOptionDict]:
    """The ENT providers ``pronotepy`` actually ships.

    Read from the installed library rather than hard-coded: a list copied into
    this file would silently rot at the next upstream release, and the failure
    mode is a user picking a provider that no longer exists.
    """
    from pronotepy import ent as ent_module  # noqa: PLC0415 -- optional dependency

    names = sorted(
        name
        for name in dir(ent_module)
        if not name.startswith("_") and callable(getattr(ent_module, name))
    )
    return [SelectOptionDict(value=name, label=name) for name in names]


_URL_SELECTOR: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_TEXT: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_PASSWORD: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

QR_SCHEMA: Final = vol.Schema(
    {
        # The raw JSON from the app's QR code. Pasted rather than scanned
        # because Home Assistant has no camera in a config flow, and it is
        # single-use: PRONOTE invalidates it once enrolled.
        vol.Required(CONF_QR_PAYLOAD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        ),
        vol.Required(CONF_QR_PIN): _PASSWORD,
        vol.Optional(CONF_DEVICE_NAME, default="Home Assistant"): _TEXT,
        vol.Optional(CONF_ACCOUNT_PIN): _PASSWORD,
    }
)

CREDENTIALS_SCHEMA: Final = vol.Schema(
    {
        vol.Required(CONF_PRONOTE_URL): _URL_SELECTOR,
        vol.Required("username"): _TEXT,
        vol.Required("password"): _PASSWORD,
        vol.Optional(CONF_ACCOUNT_PIN): _PASSWORD,
    }
)


def _ent_schema() -> vol.Schema:
    """The ENT form, built at call time so the provider list is current."""
    return vol.Schema(
        {
            vol.Required(CONF_PRONOTE_URL): _URL_SELECTOR,
            vol.Required("username"): _TEXT,
            vol.Required("password"): _PASSWORD,
            vol.Required(CONF_ENT): SelectSelector(
                SelectSelectorConfig(
                    options=_ent_options(),
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                    custom_value=True,
                )
            ),
        }
    )


REAUTH_SCHEMA: Final = vol.Schema(
    {
        vol.Optional("password"): _PASSWORD,
        # The reason this flow exists. §8.1 refuses to store the PIN, so it has
        # to be askable again -- otherwise "we do not keep your PIN" would mean
        # "the integration breaks the first time PRONOTE asks for it".
        vol.Optional(CONF_ACCOUNT_PIN): _PASSWORD,
    }
)


#: Keys that must never reach a config entry, wherever the entry comes from.
#: The QR payload is single-use and PRONOTE invalidates it on enrolment; its
#: four-digit code and the account's two-factor PIN are never persisted (§8.1);
#: `account_id` exists only long enough to set the unique id.
#:
#: One set rather than a literal per call site, because there were two call
#: sites and only one list: the re-authentication path stripped the PIN and
#: nothing else, so a QR re-enrolment would have persisted the payload it had
#: just promised not to keep.
_NEVER_PERSISTED: Final = frozenset(
    {CONF_QR_PAYLOAD, CONF_QR_PIN, CONF_ACCOUNT_PIN, "account_id"}
)


def _log_refusal(error: BaseException) -> None:
    """Say *why* a login was refused, on this integration's own logger.

    A classified failure used to log nothing at all -- only the unexpected ones
    wrote a traceback -- so "PRONOTE refused these credentials" was the whole of
    what a bug report could contain, and the four causes behind it were
    indistinguishable. Diagnosing a real one meant guessing, twice in one day.

    At DEBUG, because DEBUG is what a user turns on before reporting a bug, and
    strictly more conservative than the ERROR-level traceback the unexpected arm
    already writes unconditionally. The category is ours and the cause is
    upstream's: `pronotepy` raises static messages here ("challenge decryption
    failed", "invalid confirmation code"), which is exactly the distinction that
    was missing.

    What this must never become is `logging.getLogger("pronotepy")` at DEBUG:
    `pronoteAPI.py` writes the hexadecimal of every request body there,
    credentials included (§8.2).
    """
    cause = error.__cause__
    _LOGGER.debug(
        "the login was refused: %s(%s), caused by %s(%s)",
        type(error).__name__,
        error,
        type(cause).__name__ if cause is not None else "nothing",
        cause if cause is not None else "",
    )


#: Errors whose wording assumes a password, and what a QR step says instead.
#:
#: `invalid_auth` is shared by every step of the flow and tells the reader to
#: check their password. On a QR step there is no password, so the message is
#: worse than unhelpful: it points at the one thing that cannot be the cause.
#: What *can* be, once PRONOTE has accepted the payload and its four-digit code
#: -- a wrong one of those raises `QRCodeDecryptError`, which surfaces as
#: `invalid_qr` -- is the account's two-factor PIN. That is a *different*
#: four-digit code, and it sits in the same form, two fields below.
_QR_ERROR_WORDING: Final = {"invalid_auth": "invalid_qr_auth"}


@callback
def _requalify_for_qr(errors: dict[str, str]) -> None:
    """Re-word a shared error for a step that has no password."""
    base = errors.get("base")
    if base is not None and base in _QR_ERROR_WORDING:
        errors["base"] = _QR_ERROR_WORDING[base]


def _account_identity(account_id: str | None) -> tuple[str, str]:
    """The parts of an account id that identify it *stably*.

    ``flow_login._account_id`` builds ``<establishment url>::<PRONOTE resource
    N>``, and the ``N`` of a parent resource is regularly written
    ``46#<signature>`` -- an establishment-local number followed by an opaque
    blob. That blob is undocumented and is not reliably stable across
    enrolments, so comparing the whole string would answer "is this the same
    *session*" where the question is "is this the same *account*" -- and a
    parent re-enrolling their own account from a fresh QR code would be told it
    belongs to somebody else, with no way out but deleting the entry.

    So the address and the number are compared and the signature is dropped.
    ``<url>::46`` is still a real identity -- resource 46 at that establishment
    -- and it cannot collide with a sibling, who carries a different number.

    The stored ``unique_id`` is deliberately left alone: every existing entry
    holds the full form, and narrowing it would orphan them.
    """
    url, _, resource = (account_id or "").rpartition("::")
    return url, resource.partition("#")[0]


def _same_account(existing_unique_id: str | None, account_id: Any) -> bool:
    """Whether a fresh login landed on the account an entry already follows."""
    return _account_identity(existing_unique_id) == _account_identity(str(account_id))


def _probe(data: dict[str, Any]) -> dict[str, Any]:
    """Import ``flow_login`` and run its probe, both on the worker thread.

    The import is deliberately here rather than at module scope, for the reason
    ``flow_login``'s own docstring gives: the flow must not import
    ``pronotepy`` at import time, or a broken dependency turns "cannot log in"
    into "cannot even be added". But it must not be inside a coroutine either
    -- Home Assistant instruments ``importlib`` and reports an import from the
    event loop as a blocking call -- so it happens here, in the executor,
    immediately before the login it belongs to.
    """
    from .flow_login import probe_account  # noqa: PLC0415 -- see the docstring

    return probe_account(data)


class PronoteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user through one PRONOTE account."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._children: list[tuple[str, str]] = []
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        entry: ConfigEntry,  # noqa: ARG004 -- required by the flow contract
    ) -> PronoteOptionsFlow:
        """Return the options flow."""
        return PronoteOptionsFlow()

    # -- entry point -------------------------------------------------------

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 -- a menu, not a form
    ) -> ConfigFlowResult:
        """Ask which of the three login methods to use."""
        return self.async_show_menu(
            step_id=STEP_USER,
            menu_options=[STEP_QR_CODE, STEP_CREDENTIALS, STEP_ENT],
            # See BRAND_LOGO_URL for why this is a URL and for the version gate
            # that keeps it: the mark reaches this screen as a markdown image
            # because hassfest refuses a URL written into the translation
            # string itself, and names a placeholder as the way to pass one.
            description_placeholders={"logo": BRAND_LOGO_URL},
        )

    # -- QR enrolment ------------------------------------------------------

    async def async_step_qr_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enrol from the mobile app's QR code.

        Costs **two** complete logins by construction: ``qrcode_login`` builds
        a client -- whose constructor logs in -- posts ``PageInfosPerso``, then
        the integration logs in again with the exported token. That deliberate
        doubling is exempted from the three-attempt guard rail rather than
        eating two thirds of it (annexe B §3.3).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                payload = _parse_qr_payload(user_input[CONF_QR_PAYLOAD])
            except (TypeError, ValueError):
                errors[CONF_QR_PAYLOAD] = "invalid_qr_payload"
            else:
                self._data = {
                    CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
                    CONF_PRONOTE_URL: public_url(payload["url"]),
                    CONF_QR_PAYLOAD: payload,
                    CONF_QR_PIN: user_input[CONF_QR_PIN],
                    # Must not change between logins, so it is generated once
                    # here and persisted with the entry.
                    CONF_UUID: uuid_module.uuid4().hex,
                    CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME),
                    CONF_ACCOUNT_PIN: user_input.get(CONF_ACCOUNT_PIN),
                }
                return await self._async_try_login(errors)

        return self.async_show_form(
            step_id=STEP_QR_CODE, data_schema=QR_SCHEMA, errors=errors
        )

    # -- credentials -------------------------------------------------------

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with a username and a password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_LOGIN_MODE: LoginMode.CREDENTIALS.value,
                # Trimmed here, once, and only the trimmed form is ever stored.
                # What parents paste is regularly a deep link from a school
                # mailing or an ENT bounce carrying a ticket, and the query
                # string went on to appear in a repair issue's placeholders and
                # in the diagnostics download (§8.2).
                CONF_PRONOTE_URL: public_url(user_input[CONF_PRONOTE_URL]),
                "username": user_input["username"],
                "password": user_input["password"],
                CONF_ACCOUNT_PIN: user_input.get(CONF_ACCOUNT_PIN),
            }
            return await self._async_try_login(errors)

        return self.async_show_form(
            step_id=STEP_CREDENTIALS,
            data_schema=CREDENTIALS_SCHEMA,
            errors=errors,
        )

    # -- ENT ---------------------------------------------------------------

    async def async_step_ent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in through a federated ENT provider."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_LOGIN_MODE: LoginMode.ENT.value,
                CONF_PRONOTE_URL: public_url(user_input[CONF_PRONOTE_URL]),
                "username": user_input["username"],
                "password": user_input["password"],
                CONF_ENT: user_input[CONF_ENT],
            }
            return await self._async_try_login(errors)

        return self.async_show_form(
            step_id=STEP_ENT, data_schema=_ent_schema(), errors=errors
        )

    # -- child selection ---------------------------------------------------

    async def async_step_children(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let a parent account choose which children to follow.

        Offered rather than assumed: every followed child multiplies the daily
        request count, and a parent with four children who only wants one
        should not pay for four (annexe B §5.3).
        """
        if user_input is not None:
            self._data[CONF_CHILDREN] = user_input[CONF_CHILDREN]
            return self._async_create()

        return self.async_show_form(
            step_id=STEP_CHILDREN,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CHILDREN,
                        default=[child_id for child_id, _ in self._children],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=child_id, label=name)
                                for child_id, name in self._children
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # -- re-authentication -------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a re-authentication, keeping the existing entry."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a corrected password and/or the 2FA PIN.

        A deliberate human gesture, so the limiter's failure counters are
        cleared before the attempt: a person retyping a password with the
        correction in hand is not an automatic retry and earns a clean slate
        (annexe B §3.2).

        A QR-enrolled account never reaches the form below. It has no password
        to correct -- see :meth:`async_step_reauth_qr`.
        """
        if self._data.get(CONF_LOGIN_MODE) == LoginMode.QR_CODE.value:
            return await self.async_step_reauth_qr()

        errors: dict[str, str] = {}
        entry = self._reauth_entry

        if user_input is not None and entry is not None:
            # Before the attempt, as the docstring says -- and it was not
            # happening at all: `reset_after_reauth` had no caller outside the
            # limiter's own tests, so the MFA hold, whose only exit is a human
            # supplying the PIN, could not be exited by a human supplying the
            # PIN.
            clear_login_penalties(self.hass, entry.entry_id)
            data = dict(entry.data)
            if user_input.get("password"):
                data["password"] = user_input["password"]
            if user_input.get(CONF_ACCOUNT_PIN):
                data[CONF_ACCOUNT_PIN] = user_input[CONF_ACCOUNT_PIN]
            self._data = data

            outcome = await self._async_probe(self._data, errors)
            if outcome is not None:
                return self._async_finish_reauth(entry, outcome)

        return self.async_show_form(
            step_id=STEP_REAUTH_CONFIRM,
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={
                "url": public_url(self._data.get(CONF_PRONOTE_URL))
            },
        )

    async def async_step_reauth_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enrol a QR account from a freshly generated QR code.

        A token account cannot be repaired with a password, and it has none:
        in token mode PRONOTE rotates ``jetonConnexionAppliMobile`` at every
        login, so a token that went stale -- an interrupted login, a session
        opened twice, a device revoked in the app -- is dead for good, and the
        only thing that mints a new one is a new QR code. Showing such an
        account the password form was offering a repair that could not work,
        and the account's own two-factor PIN is asked for here too, so nothing
        the old form could fix is lost by not showing it.

        The device UUID is deliberately **reused** rather than regenerated, and
        that is a requirement rather than a preference: upstream documents the
        parameter as "Unique ID for your application. Must not change between
        logins" (``pronotepy.ClientBase.qrcode_login``). A fresh one would also
        fill the account's enrolled-device list with copies of this Home
        Assistant, but the contract is the reason.
        """
        errors: dict[str, str] = {}
        entry = self._reauth_entry

        if user_input is not None and entry is not None:
            try:
                payload = _parse_qr_payload(user_input[CONF_QR_PAYLOAD])
            except (TypeError, ValueError):
                errors[CONF_QR_PAYLOAD] = "invalid_qr_payload"
            else:
                # Before the attempt, for the reason `async_step_reauth_confirm`
                # gives: a human pasting a QR code is not an automatic retry.
                clear_login_penalties(self.hass, entry.entry_id)
                self._data = {
                    **dict(entry.data),
                    CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
                    CONF_PRONOTE_URL: public_url(payload["url"]),
                    CONF_QR_PAYLOAD: payload,
                    CONF_QR_PIN: user_input[CONF_QR_PIN],
                    CONF_UUID: entry.data.get(CONF_UUID) or uuid_module.uuid4().hex,
                    CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME),
                    CONF_ACCOUNT_PIN: user_input.get(CONF_ACCOUNT_PIN),
                }

                outcome = await self._async_probe(self._data, errors)
                _requalify_for_qr(errors)
                if outcome is not None:
                    # A QR code is the one reconnection input that can name a
                    # *different* account: a parent with two children in two
                    # establishments has two apps to generate one from. Pasting
                    # the wrong one would silently repoint this entry -- keeping
                    # its devices, its entity ids and its history -- at somebody
                    # else's child, so the identity is checked rather than
                    # assumed.
                    if not _same_account(entry.unique_id, outcome["account_id"]):
                        return self.async_abort(reason="wrong_account")
                    return self._async_finish_reauth(entry, outcome)

        return self.async_show_form(
            step_id=STEP_REAUTH_QR,
            data_schema=QR_SCHEMA,
            errors=errors,
            description_placeholders={
                "url": public_url(self._data.get(CONF_PRONOTE_URL))
            },
        )

    def _async_finish_reauth(
        self, entry: ConfigEntry, outcome: dict[str, Any]
    ) -> ConfigFlowResult:
        """Persist a successful reconnection and reload the entry.

        ``children`` comes from the entry and never from the probe, for the
        reason ``_async_try_login`` gives at length: the probe returns
        ``(id, name)`` label pairs under the very key that holds the user's
        *selection*, so merging them in replaced "follow this one child" with
        "follow every child the account can see" -- silently, on every single
        reconnection, along with the second child's whole request budget.

        The PIN, and a QR payload if one was used, are dropped on the way out:
        they exist in ``self._data`` only long enough to log in once.
        """
        merged = dict(self._data)
        merged.update(
            {key: value for key, value in outcome.items() if key != CONF_CHILDREN}
        )
        persisted = {
            key: value
            for key, value in merged.items()
            if key not in _NEVER_PERSISTED and value is not None
        }
        return self.async_update_reload_and_abort(
            entry, data=persisted, reason="reauth_successful"
        )

    # -- shared login plumbing ---------------------------------------------

    async def _async_try_login(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Attempt the login, then branch on the account's shape."""
        outcome = await self._async_probe(self._data, errors)
        if outcome is None:
            return self._async_reshow(errors)

        # Everything except `children`, which is deliberately *not* merged.
        #
        # The probe returns `children` as `(id, name)` label pairs, and
        # `CONF_CHILDREN` -- the very same string -- is where the user's
        # *selection* is stored a few lines below. Merging the pairs in and then
        # dropping the key on the way out (because the labels have no business
        # being persisted) threw the selection away with them: a parent who
        # deliberately followed one of two children got both anyway, silently,
        # along with the second child's whole request budget. The labels live in
        # `self._children`, which is the only place that needs them.
        self._data.update(
            {key: value for key, value in outcome.items() if key != CONF_CHILDREN}
        )
        await self.async_set_unique_id(str(outcome["account_id"]))
        self._abort_if_unique_id_configured()

        self._children = list(outcome["children"])
        if len(self._children) > 1:
            return await self.async_step_children()

        self._data[CONF_CHILDREN] = [child_id for child_id, _ in self._children]
        return self._async_create()

    def _async_reshow(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Re-display the form the user came from, with its errors."""
        mode = self._data.get(CONF_LOGIN_MODE)
        if mode == LoginMode.QR_CODE.value:
            _requalify_for_qr(errors)
            return self.async_show_form(
                step_id=STEP_QR_CODE, data_schema=QR_SCHEMA, errors=errors
            )
        if mode == LoginMode.ENT.value:
            return self.async_show_form(
                step_id=STEP_ENT, data_schema=_ent_schema(), errors=errors
            )
        return self.async_show_form(
            step_id=STEP_CREDENTIALS,
            data_schema=CREDENTIALS_SCHEMA,
            errors=errors,
        )

    async def _async_probe(
        self, data: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """Log in once in a worker thread and learn the account's shape.

        Returns the credentials to persist plus the children, or ``None`` after
        filling ``errors``. Runs in the executor because constructing a
        ``pronotepy`` client *is* a network login (§3.2), and it runs **through
        the limiter**, which it did not.

        That omission mattered more than it looks. This is the one login path a
        human can repeat at will: the re-authentication form re-displays itself
        on every failure, so a parent convinced the password is right could
        submit it twenty times in a minute. None of those attempts reached
        `may_login`, none was counted by `note_login`, and the three-attempt
        guard rail -- the defence against the one sanction PRONOTE applies to an
        address rather than to an account -- never saw them (annexe B §3.1).
        """
        guard = login_guard(self.hass)
        # QR enrolment performs two complete logins by construction, so it is
        # declared as two. Under-declaring here would spend the budget without
        # recording it, which is the defect this whole file just stopped having.
        # The payload is part of the test, not just the mode: a QR-enrolled
        # entry keeps `qr_code` for ever, but once the payload is gone it logs
        # in through pronotepy's token mode, which is a single login. Charging
        # two for it would spend a budget nothing used.
        cost = (
            REQUESTS_PER_LOGIN * 2
            if data.get(CONF_LOGIN_MODE) == LoginMode.QR_CODE.value
            and data.get(CONF_QR_PAYLOAD)
            else REQUESTS_PER_LOGIN
        )

        try:
            outcome: dict[str, Any] = await guard.login(
                lambda: self.hass.async_add_executor_job(_probe, data),
                cost=cost,
            )
        except LoginRefusedByLimiter:
            # Nothing was sent, so this is not an attempt and must not be
            # counted as one.
            errors["base"] = "rate_limited"
        except ProbeInvalidCredentials as error:
            _log_refusal(error)
            guard.note_login(LoginOutcome.BAD_CREDENTIALS, requests_used=cost)
            errors["base"] = "invalid_auth"
        except ProbeMfaRequired as error:
            _log_refusal(error)
            guard.note_login(LoginOutcome.MFA_REQUIRED, requests_used=cost)
            errors["base"] = "mfa_required"
        except ProbeBootstrapFailed as error:
            _log_refusal(error)
            # Says the bootstrap failed and stops there. pronotepy infers an IP
            # suspension from `if "IP" in html` -- two capitals anywhere in the
            # page -- and telling somebody their home connection is banned
            # because a school wrote "Espace IP" in a footer would be worse
            # than saying nothing (§6.3).
            guard.note_login(LoginOutcome.BOOTSTRAP, requests_used=cost)
            errors["base"] = "bootstrap_failed"
        except ProbeQrInvalid as error:
            # A single-use payload that PRONOTE has already consumed. Charged
            # to the credentials guard, because from the server's point of view
            # it is a refused authentication like any other.
            _log_refusal(error)
            guard.note_login(LoginOutcome.BAD_CREDENTIALS, requests_used=cost)
            errors["base"] = "invalid_qr"
        except (TimeoutError, OSError) as error:
            _log_refusal(error)
            guard.note_login(LoginOutcome.TRANSPORT, requests_used=cost)
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("unexpected failure while validating the account")
            guard.note_login(LoginOutcome.TRANSPORT, requests_used=cost)
            errors["base"] = "unknown"
        else:
            guard.note_login(LoginOutcome.SUCCESS, requests_used=cost)
            return outcome
        return None

    def _async_create(self) -> ConfigFlowResult:
        """Create the entry, dropping the single-use and never-stored fields."""
        # `children` is *not* in `_NEVER_PERSISTED`, and must not be: it holds
        # the user's selection, which `PronoteAccount` reads to decide whose
        # data to collect. It used to be excluded here, which is how the
        # selection came to be discarded on every single install.
        data = {
            key: value
            for key, value in self._data.items()
            if key not in _NEVER_PERSISTED and value is not None
        }
        return self.async_create_entry(title=str(self._data["title"]), data=data)


class PronoteOptionsFlow(OptionsFlow):
    """Three sections, and a live budget estimate.

    The estimate is not decoration. Annexe B §7 requires the page to show what
    the entered values will cost, computed by the *same* function that produced
    the numbers in the annexe -- a setting whose consequence you cannot see gets
    set at random, and the consequence here is measured in requests against
    somebody's school account.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 -- a menu, not a form
    ) -> ConfigFlowResult:
        """Offer the three sections."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "tiers", "rate_limit"],
            description_placeholders=self._estimate_placeholders(
                self.config_entry.options
            ),
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cadence, horizons, timezone and the write switch."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_MASTER_TICK,
                    default=options.get(OPT_MASTER_TICK, DEFAULT_MASTER_TICK),
                ): _number(OPT_MASTER_TICK, "min"),
                vol.Required(
                    OPT_HOMEWORK_HORIZON,
                    default=options.get(OPT_HOMEWORK_HORIZON, DEFAULT_HOMEWORK_HORIZON),
                ): _number(OPT_HOMEWORK_HORIZON, "d"),
                vol.Required(
                    OPT_WAKE_MARGIN,
                    default=options.get(OPT_WAKE_MARGIN, DEFAULT_WAKE_MARGIN),
                ): _number(OPT_WAKE_MARGIN, "min"),
                vol.Required(
                    OPT_STALE_AFTER,
                    default=options.get(OPT_STALE_AFTER, DEFAULT_STALE_AFTER),
                ): _number(OPT_STALE_AFTER, "x"),
                vol.Required(
                    OPT_ESTABLISHMENT_TIMEZONE,
                    default=options.get(
                        OPT_ESTABLISHMENT_TIMEZONE,
                        str(self.hass.config.time_zone),
                    ),
                ): _TEXT,
                # Both strategies are offered because the server's inactivity
                # timeout is not published and cannot be assumed. `lazy` starts
                # pessimistic and degrades to `per_batch` on measured evidence,
                # so the default is never worse than the alternative (§6.5).
                vol.Required(
                    OPT_SESSION_STRATEGY,
                    default=options.get(
                        OPT_SESSION_STRATEGY, DEFAULT_SESSION_STRATEGY.value
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=strategy.value, label=strategy.value)
                            for strategy in SessionStrategy
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="session_strategy",
                    )
                ),
                vol.Required(
                    OPT_WRITE_OPERATIONS_ENABLED,
                    default=options.get(
                        OPT_WRITE_OPERATIONS_ENABLED,
                        DEFAULT_WRITE_OPERATIONS_ENABLED,
                    ),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="general",
            data_schema=schema,
            description_placeholders=self._estimate_placeholders(options),
        )

    async def async_step_tiers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Per-tier interval and on/off switch."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        intervals = tier_intervals(options)
        enabled = tier_enabled(options)

        fields: dict[Any, Any] = {}
        for tier, default in DEFAULT_TIER_INTERVALS.items():
            fields[
                vol.Required(
                    OPT_TIER_INTERVAL.format(tier=tier.value),
                    default=intervals.get(tier, default),
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    # From the same table `options.tier_intervals` clamps
                    # against, so the field and the reader cannot drift apart.
                    min=TIER_INTERVAL_RANGE[0],
                    max=TIER_INTERVAL_RANGE[1],
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            )
            fields[
                vol.Required(
                    OPT_TIER_ENABLED.format(tier=tier.value),
                    default=enabled.get(tier, True),
                )
            ] = BooleanSelector()

        return self.async_show_form(
            step_id="tiers",
            data_schema=vol.Schema(fields),
            description_placeholders=self._estimate_placeholders(options),
        )

    async def async_step_rate_limit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Every limiter tunable of annexe B §7."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_MIN_REQUEST_INTERVAL,
                    default=_default(options, OPT_MIN_REQUEST_INTERVAL),
                ): _number(OPT_MIN_REQUEST_INTERVAL, "s", step=0.1),
                vol.Required(
                    OPT_MAX_REQUESTS_PER_HOUR,
                    default=_default(options, OPT_MAX_REQUESTS_PER_HOUR),
                ): _number(OPT_MAX_REQUESTS_PER_HOUR, "req/h"),
                vol.Required(
                    OPT_BURST_SIZE, default=_default(options, OPT_BURST_SIZE)
                ): _number(OPT_BURST_SIZE, "req"),
                vol.Required(
                    OPT_MAX_REQUESTS_PER_DAY,
                    default=_default(options, OPT_MAX_REQUESTS_PER_DAY),
                ): _number(OPT_MAX_REQUESTS_PER_DAY, "req/d"),
                vol.Required(
                    OPT_MAX_WAIT, default=_default(options, OPT_MAX_WAIT)
                ): _number(OPT_MAX_WAIT, "s"),
                vol.Required(
                    OPT_MAX_LOGINS_PER_DAY,
                    default=_default(options, OPT_MAX_LOGINS_PER_DAY),
                ): _number(OPT_MAX_LOGINS_PER_DAY, "login/d"),
                # The one setting that guards against address suspension. Its
                # range starts at 1 and stops at 10 on purpose: annexe B §3.1
                # is clear that the sanction targets failed logins, and a large
                # value here is the mistake that costs an account.
                vol.Required(
                    OPT_MAX_FAILED_LOGINS_PER_HOUR,
                    default=_default(options, OPT_MAX_FAILED_LOGINS_PER_HOUR),
                ): _number(OPT_MAX_FAILED_LOGINS_PER_HOUR, "attempt/h"),
                vol.Required(
                    OPT_CREDENTIALS_HOLD,
                    default=_default(options, OPT_CREDENTIALS_HOLD),
                ): _number(OPT_CREDENTIALS_HOLD, "s"),
                vol.Required(
                    OPT_BOOTSTRAP_HOLD,
                    default=_default(options, OPT_BOOTSTRAP_HOLD),
                ): _number(OPT_BOOTSTRAP_HOLD, "s"),
                vol.Required(
                    OPT_BACKOFF_BASE, default=_default(options, OPT_BACKOFF_BASE)
                ): _number(OPT_BACKOFF_BASE, "s"),
                vol.Required(
                    OPT_BACKOFF_MAX, default=_default(options, OPT_BACKOFF_MAX)
                ): _number(OPT_BACKOFF_MAX, "s"),
                vol.Required(
                    OPT_CONNECT_TIMEOUT,
                    default=_default(options, OPT_CONNECT_TIMEOUT),
                ): _number(OPT_CONNECT_TIMEOUT, "s"),
                vol.Required(
                    OPT_READ_TIMEOUT, default=_default(options, OPT_READ_TIMEOUT)
                ): _number(OPT_READ_TIMEOUT, "s"),
                vol.Required(
                    OPT_QUIET_HOURS_ENABLED,
                    default=options.get(OPT_QUIET_HOURS_ENABLED, True),
                ): BooleanSelector(),
                vol.Required(
                    OPT_QUIET_START,
                    default=options.get(OPT_QUIET_START, "22:00:00"),
                ): TimeSelector(),
                vol.Required(
                    OPT_QUIET_END,
                    default=options.get(OPT_QUIET_END, "06:00:00"),
                ): TimeSelector(),
            }
        )
        return self.async_show_form(
            step_id="rate_limit",
            data_schema=schema,
            description_placeholders=self._estimate_placeholders(options),
        )

    def _save(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Merge one section's answers into the stored options.

        Merged rather than replaced: each section shows a subset, and replacing
        would silently reset everything the user did not have on screen.
        """
        merged = {**self.config_entry.options, **user_input}
        return self.async_create_entry(title="", data=merged)

    def _estimate_placeholders(self, options: Mapping[str, Any]) -> dict[str, str]:
        """The daily request estimate shown on every page.

        Uses the measured session lifetime when one exists, and the pessimistic
        assumption when it does not -- an estimate a user can be disappointed by
        is worse than no estimate (§6.5).
        """
        students = len(options.get(CONF_CHILDREN) or ()) or _entry_children(
            self.config_entry
        )
        measured = _measured_lifetime(self.config_entry)
        estimate = estimate_daily_requests(
            options,
            students=max(1, students),
            session_lifetime_minutes=measured,
        )
        return {
            "estimate": str(estimate),
            "students": str(max(1, students)),
            "measured": (f"{measured:.0f}" if measured is not None else "-"),
        }


def _entry_children(entry: ConfigEntry) -> int:
    """How many children the entry follows."""
    return len(entry.data.get(CONF_CHILDREN) or ()) or 1


def _measured_lifetime(entry: ConfigEntry) -> float | None:
    """The measured session lifetime, if the account is loaded."""
    account = getattr(entry, "runtime_data", None)
    if account is None:
        return None
    lifetime = account.session.lifetime.observed_minutes
    return float(lifetime) if lifetime is not None else None


def _default(options: Mapping[str, Any], key: str) -> Any:
    """The stored value for an option, else its documented default."""
    from . import const  # noqa: PLC0415 -- one lookup, keeps the table in const

    return options.get(key, getattr(const, f"DEFAULT_{key.upper()}"))


def _number(key: str, unit: str, *, step: float = 1) -> NumberSelector:
    """A bounded number field, using the range table of ``const.OPTION_RANGES``.

    Bounded in the UI and not only in the code: annexe B §7 gives every tunable
    a range, and a field that accepts 100000 requests an hour is a field that
    will eventually be set to 100000.
    """
    low, high = OPTION_RANGES[key]
    return NumberSelector(
        NumberSelectorConfig(
            min=float(low),
            max=float(high),
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def _parse_qr_payload(raw: str) -> dict[str, Any]:
    """Parse the QR code's JSON and check the three keys ``pronotepy`` needs."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("not JSON") from error
    if not isinstance(payload, dict):
        # A ValueError, deliberately, and not the TypeError this used to be.
        # Every failure of this function means the same thing to its one
        # caller -- "this payload is unusable" -- and that caller catches
        # ValueError. A TypeError therefore escaped the flow entirely, so a
        # payload of `[1, 2]` or `"nope"` -- valid JSON, wrong shape, and
        # exactly what a badly cropped QR code decodes to -- produced an
        # unhandled exception and Home Assistant's generic "unknown error"
        # instead of the field error beside the box to re-scan into.
        raise ValueError(  # noqa: TRY004 -- see the comment above
            "not a JSON object"
        )
    missing = {"login", "jeton", "url"} - set(payload)
    if missing:
        raise ValueError(f"missing keys: {', '.join(sorted(missing))}")
    return payload


class ProbeError(Exception):
    """Base class for the failures the flow knows how to explain."""


class ProbeInvalidCredentials(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The credentials were refused."""


class ProbeMfaRequired(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """PRONOTE asked for the 2FA PIN."""


class ProbeBootstrapFailed(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The bootstrap page carried no session block. Cause deliberately unnamed."""


class ProbeQrInvalid(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The QR payload or its PIN was rejected."""
