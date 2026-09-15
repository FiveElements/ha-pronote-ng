"""Serving a homework document, and never publishing PRONOTE's address for it.

This file guards one rule and one cost.

**The rule.** A file's PRONOTE address is
``FichiersExternes/<hex>/<name>?Session=<h>`` where the hex segment is the
document's identifier encrypted with the session's own AES key and IV. It opens
the document with no credentials and it dies with the session, so it must not
reach a state, an attribute, the recorder or a diagnostics download -- the rule
§8.2 established for the iCal URL. What is published is a signed path to this
integration's own view, which relays the bytes.

**The cost.** The download is the one fetch in the whole integration that does
not go through ``ClientBase.post``, so the limiter cannot see it unless the
caller declares it. A missing declaration would spend budget invisibly, which
is the class of defect this project exists to prevent (§11.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event
import pytest

from custom_components.pronote_ng.attachment import (
    _SAFE_CONTENT_TYPES,
    SIGNATURE_LIFETIME_HOURS,
    _ByteCache,
    _content_type,
    _locate,
    fingerprint,
)
from custom_components.pronote_ng.const import DEFAULT_TIER_INTERVALS, Tier
from custom_components.pronote_ng.gateway import AttachmentUnavailable

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures import protocol

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.pronote_ng.account import PronoteAccount
    from custom_components.pronote_ng.gateway import PronoteGateway

    from .fixtures.client import FakeClient


# ---------------------------------------------------------------------------
# The download itself
# ---------------------------------------------------------------------------


def test_the_address_is_built_by_upstream_and_carries_the_session(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Not re-implemented here, and the test proves which code ran.

    The encrypted segment is produced by ``communication.encryption``, so an
    address that did not pass through it -- a hand-rolled path, a cached
    string, a constant -- would not contain the fake's keyed digest. Asserting
    on the *shape* rather than on a literal is deliberate: the point is that
    upstream's construction was used, not that it produces any particular
    bytes.
    """
    content, declared, cost = gateway.homework_attachment(
        client, attachment_id="ATTACHMENT-1", name="enonce.pdf"
    )

    assert cost == 1
    assert content == b"%PDF-1.4 not a real document"
    assert declared == "application/pdf"

    (address,) = client.communication.session.gets
    assert address.startswith("https://demo.example.invalid/pronote/FichiersExternes/")
    assert address.endswith("?Session=SESSION-NUMBER")
    assert "enonce.pdf" in address


def test_a_refused_download_is_an_error_and_not_an_error_page(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Attachment.data`` returns the body whatever the status.

    That is the trap. ``Attachment.save`` checks the status; the property does
    not, so an expired session yields PRONOTE's login page as ``bytes`` and a
    dashboard would render a few kilobytes of HTML as though it were the
    exercise. Checked here rather than delegated for exactly that reason.
    """
    client.communication.session.response.status_code = 403
    client.communication.session.response.content = b"<html>login</html>"

    with pytest.raises(AttachmentUnavailable) as refusal:
        gateway.homework_attachment(
            client, attachment_id="ATTACHMENT-1", name="enonce.pdf"
        )

    assert refusal.value.status == 403
    assert refusal.value.name == "enonce.pdf"


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def test_a_fingerprint_names_a_document_and_not_a_position() -> None:
    """Which is what makes a re-ordering between two collections harmless.

    An address built from an index would keep resolving after PRONOTE returned
    the same attachments in another order, and would then serve the wrong file
    -- silently, because both are documents of the same homework entry.
    """
    first = fingerprint("HOMEWORK-1", "ATTACHMENT-1")
    second = fingerprint("HOMEWORK-1", "ATTACHMENT-2")

    assert first != second
    assert first == fingerprint("HOMEWORK-1", "ATTACHMENT-1")


def test_the_same_document_id_under_two_homework_entries_differs() -> None:
    """The pair is hashed, not the attachment alone.

    Identifiers are unique per account in practice, but the lookup resolves the
    pair -- so a digest that ignored the homework entry would let one entry's
    row address another's document.
    """
    assert fingerprint("HOMEWORK-1", "SAME") != fingerprint("HOMEWORK-2", "SAME")


def test_a_fingerprint_is_safe_in_a_path() -> None:
    """A real PRONOTE identifier carries a ``#``.

    That is a fragment delimiter: put in a path it truncates the address, so
    everything after it is never sent to the server. Hex is the point, not a
    style choice.
    """
    print_ = fingerprint("29#abc", "79#def")

    assert print_.isalnum()
    assert "#" not in print_
    assert print_ == print_.lower()


# ---------------------------------------------------------------------------
# What the browser is told
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param("application/pdf", "application/pdf", id="a pdf"),
        pytest.param("image/png", "image/png", id="an image"),
        pytest.param(
            "text/plain; charset=utf-8", "text/plain", id="a type with parameters"
        ),
        pytest.param(
            "text/html", "application/octet-stream", id="markup, which must not render"
        ),
        pytest.param(
            "image/svg+xml", "application/octet-stream", id="svg, which carries script"
        ),
        pytest.param(None, "application/octet-stream", id="nothing declared"),
    ],
)
def test_only_a_safe_content_type_is_echoed_back(
    declared: str | None, expected: str
) -> None:
    """Because the bytes are served from Home Assistant's own origin.

    A teacher's upload echoed as ``text/html`` would run its own script with
    the viewer's Home Assistant session -- stored cross-site scripting, through
    a file nobody in this project ever reviewed. ``svg+xml`` is the same hazard
    wearing an image's name. Anything not on the whitelist downloads instead of
    rendering.
    """
    assert _content_type(declared) == expected


def test_markup_is_not_on_the_whitelist() -> None:
    """Stated as its own assertion, so widening the list trips a test.

    A future contributor adding ``text/html`` to make some establishment's
    attachment display would be reopening the injection route above, and a
    parametrised case is easy to delete by accident.
    """
    assert "text/html" not in _SAFE_CONTENT_TYPES
    assert "image/svg+xml" not in _SAFE_CONTENT_TYPES


# ---------------------------------------------------------------------------
# The byte cache
# ---------------------------------------------------------------------------


def test_the_cache_bounds_what_a_page_load_can_cost() -> None:
    """The reason the cost is per document and not per click.

    A browser prefetcher or a link scanner on the dashboard can request every
    address on the page. Cached, that is one request per document per Home
    Assistant run; uncached it would be one per fetch, against a school server
    whose one sanction applies to an IP address.
    """
    cache = _ByteCache()
    for index in range(40):
        cache.put(f"print-{index}", (b"x", "application/pdf"))

    assert cache.get("print-39") == (b"x", "application/pdf")
    assert cache.get("print-0") is None, "the cache grew without bound"


def test_the_signature_outlives_the_interval_that_refreshes_it() -> None:
    """Otherwise a page would show an address that had already expired.

    The attribute carrying the signature is rebuilt on each homework
    collection, so the signature has to last longer than that interval with
    room to spare -- or a parent meets a 401 on a link the dashboard is still
    displaying. Asserted against the interval table rather than a literal, so
    shortening the tier's cadence cannot silently invalidate the reasoning.
    """
    interval_hours = DEFAULT_TIER_INTERVALS[Tier.HOMEWORK] / 60

    assert interval_hours * 4 < SIGNATURE_LIFETIME_HOURS


# ---------------------------------------------------------------------------
# Resolution against the snapshot
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestWhatTheViewWillServe:
    """The lookup, exercised against a real account holding real snapshots."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Homework with one file and one link, which is the live shape."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [
                protocol.homework(
                    attachments=(
                        "enonce.pdf",
                        ("Le sujet en ligne", "https://exemple.invalid/sujet"),
                    )
                )
            ]
        )
        return client

    async def test_a_fingerprint_from_the_snapshot_resolves_to_its_document(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The snapshot is the authority on what this account may fetch.

        Which is why the address needs no student and no homework identifier:
        anything not in the snapshot is refused without ever reaching PRONOTE,
        so an address cannot be edited into a request for somebody else's
        document.
        """
        del hass
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        document = next(a for a in item.attachments if a.url is None)

        located = _locate(account, fingerprint(item.id, document.id))

        assert located is not None
        student_id, found = located
        assert student_id == CHILDREN[0][0]
        assert found.name == document.name

    async def test_an_unknown_fingerprint_resolves_to_nothing(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A guessed or stale address must not reach the school's server.

        Refused from the snapshot alone, so a request for a document that has
        left the display horizon costs zero PRONOTE requests -- the integration
        cannot vouch for what it no longer holds.
        """
        del hass
        assert _locate(account, "0" * 16) is None

    async def test_a_document_with_no_identifier_is_never_addressable(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """An attachment PRONOTE sent without an ``N`` cannot be fetched.

        Its fingerprint would be a digest of the empty string -- the same for
        every such attachment on the account -- so matching one would serve an
        arbitrary document. Skipped in the lookup rather than trusted.
        """
        del hass
        assert _locate(account, fingerprint("HOMEWORK-1", "")) is None

    async def test_the_download_is_charged_to_the_limiter(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """The one fetch that bypasses ``ClientBase.post``.

        ``Attachment.data`` goes straight to ``communication.session.get``, so
        nothing in the hardened client counts it. If the caller stopped
        declaring the cost, the budget would be spent invisibly -- exactly the
        regression §11.1's contract exists to catch.
        """
        del hass
        from custom_components.pronote_ng.attachment import _fetch

        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        document = next(a for a in item.attachments if a.url is None)

        before = account.limiter.calls_today
        content, content_type = await _fetch(account, document, CHILDREN[0][0])
        after = account.limiter.calls_today

        assert content == b"%PDF-1.4 not a real document"
        assert content_type == "application/pdf"
        assert after - before == 1
        assert len(parent_client.communication.session.gets) == 1


# ---------------------------------------------------------------------------
# What reaches an attribute
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestWhatTheDashboardReceives:
    """The published address, which is Home Assistant's and never PRONOTE's."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file and one link on the same homework entry."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [
                protocol.homework(
                    attachments=(
                        "enonce.pdf",
                        ("Le sujet en ligne", "https://exemple.invalid/sujet"),
                    )
                )
            ]
        )
        return client

    @staticmethod
    def _items(hass: HomeAssistant) -> list[dict[str, Any]]:
        state = hass.states.get("sensor.enfant_un_homework_to_do")
        assert state is not None
        items: list[dict[str, Any]] = state.attributes["items"]
        return items

    async def test_a_file_gets_a_local_address_and_a_link_keeps_its_own(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Both kinds are openable, and only one address is a third party's.

        A link is proxied through nothing: fetching an unrelated site with the
        school's session is not this integration's business. A file is served
        by Home Assistant, because PRONOTE's address for it cannot be
        published at all.
        """
        del account
        links = {
            entry["name"]: entry["url"]
            for entry in self._items(hass)[0]["attachment_links"]
        }

        assert links["Le sujet en ligne"] == "https://exemple.invalid/sujet"
        assert links["enonce.pdf"].startswith("/api/pronote_ng/attachment/")
        assert "authSig=" in links["enonce.pdf"]

    async def test_no_published_value_carries_pronotes_own_address(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The rule of this whole module, asserted on the published shape.

        `Session=` and `FichiersExternes` are the two halves of the address
        that must never leave the process. Searched across the entire attribute
        payload rather than field by field, so a future field cannot reintroduce
        it quietly.
        """
        del account
        payload = repr(self._items(hass))

        assert "FichiersExternes" not in payload
        assert "Session=" not in payload

    async def test_the_identifier_is_not_published_either(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A fingerprint, not an ``N``.

        Two reasons, and the second is the one a reader forgets: a real
        identifier carries a ``#`` that would truncate the path, *and* an
        attribute is written to the recorder, so publishing one puts a real
        PRONOTE identifier in a database that outlives the session.
        """
        del account
        item = self._items(hass)[0]
        payload = repr(item["attachment_links"])

        assert "ATTACHMENT-1" not in payload
        assert fingerprint(item["id"], "ATTACHMENT-1") in payload

    async def test_the_names_are_still_plain_strings(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """`attachments` predates the addresses and must keep its shape.

        A template doing ``| join(', ')`` on it is the normal way to write
        "Pièces jointes : a.pdf, b.pdf" into a notification, and that is why
        the addresses went into a second key instead of enriching this one.
        """
        del account
        names = self._items(hass)[0]["attachments"]

        assert names == ["enonce.pdf", "Le sujet en ligne"]
        assert all(isinstance(name, str) for name in names)


@REQUIRES_HASS
async def test_the_route_is_registered_once_for_the_whole_instance(
    hass: HomeAssistant,
) -> None:
    """Registered from ``async_setup``, like the actions, and for one reason.

    A route is per Home Assistant, not per account. Registering it from
    ``async_setup_entry`` would re-register the same path on every entry and on
    every reload -- at best redundantly, at worst replacing a handler while a
    request is in flight.
    """
    from custom_components.pronote_ng import async_setup

    with patch.object(hass, "http", create=True) as http:
        assert await async_setup(hass, {})

    assert http.register_view.call_count == 1


# ---------------------------------------------------------------------------
# What must not become durable
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestNothingRecordedCanOpenADocument:
    """The invariant, run through the recorder's own filter.

    Two earlier versions of this guard passed while the tokens were being
    written to a live database, and both failed the same way: they checked a
    belief about Home Assistant instead of asking Home Assistant.

    The first asserted that ``attachment_links`` appeared in
    ``_unrecorded_attributes``. True, and worthless -- the recorder filters
    **top-level** keys only, and the addresses live one level down, inside
    ``items``. The second named ``items`` in a declaration on `PronoteSensor`,
    which looked like the correction and was the cause: Home Assistant does not
    union those sets up a class hierarchy, so declaring the name on a subclass
    *replaced* the base set rather than extending it, and v0.0.22 shipped with
    every list attribute of every sensor handed back to the recorder.

    So this test calls ``StateAttributes.shared_attrs_bytes_from_event`` -- the
    function the recorder really uses -- on a real state-changed event, and
    searches the bytes it would have stored. Nothing is re-implemented and no
    key is named, so it survives a rename, a reshaping and a fourth theory
    about how the exclusion resolves.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file, so a signed address exists to be found."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(attachments=("enonce.pdf",))]
        )
        return client

    async def test_what_the_recorder_would_store_holds_no_signature(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A signed address is a bearer token and a database outlives it.

        Twelve hours is defensible for an address that leaks through a
        browser's history. It is not defensible for one written to the history
        database on every collection, because a database is copied into every
        backup and occasionally pasted into a bug report.

        Measured on a live instance rather than reasoned about: the history of
        `sensor.<child>_homework_to_do` held a row carrying every token.
        """
        del account
        from homeassistant.components.recorder.db_schema import StateAttributes

        state = hass.states.get("sensor.enfant_un_homework_to_do")
        assert state is not None
        assert "authSig=" in repr(state.attributes), (
            "no attribute carried a signature, so this proves nothing"
        )

        event = Event(
            EVENT_STATE_CHANGED,
            {"entity_id": state.entity_id, "old_state": None, "new_state": state},
        )
        stored = StateAttributes.shared_attrs_bytes_from_event(event, None)

        assert b"authSig=" not in stored, (
            f"the recorder would store a bearer token: {stored[:400]!r}"
        )

    async def test_the_signature_is_still_in_the_live_state(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Excluded from the recorder is not excluded from the dashboard.

        The pair matters: a card reads the live state, so the address has to be
        there. A guard that removed it from both would look like it worked and
        would have quietly deleted the feature.
        """
        del account
        state = hass.states.get("sensor.enfant_un_homework_to_do")
        assert state is not None

        links = state.attributes["items"][0]["attachment_links"]

        assert links[0]["url"].startswith("/api/pronote_ng/attachment/")
        assert "authSig=" in links[0]["url"]


def test_no_entity_class_shadows_the_shared_exclusion() -> None:
    """A subclass declaration replaces the base set; it does not extend it.

    `Entity.__init_subclass__` computes
    ``_entity_component_unrecorded_attributes | cls._unrecorded_attributes``,
    and the right-hand side resolves by ordinary attribute lookup. So any class
    in this integration that declares the name silently drops every entry
    `PronoteEntity` had declared -- which is exactly what v0.0.22 did, handing
    the recorder `items`, `lessons`, the menu fields, `messages`, `address` and
    `fetched_at` for every sensor, and writing signed addresses into a live
    database.

    This is the barrier for the whole class of mistake rather than for the one
    instance of it: a declaration is allowed, but only if it still covers the
    shared set. The same defect had already been paid for once, on the
    diagnostic entities that are a *sibling* of `PronoteEntity` rather than a
    subclass (see the comment in `entity.py`).
    """
    from importlib import import_module

    from homeassistant.helpers.entity import Entity

    from custom_components.pronote_ng import DOMAIN, PLATFORMS
    from custom_components.pronote_ng.const import UNRECORDED_LIST_ATTRIBUTES

    # Derived from `PLATFORMS`, not listed here, so a platform added later is
    # covered without anybody remembering to extend this test. `entity.py` is
    # added because the two bases live there and both are meant to declare.
    modules = [import_module(f"custom_components.{DOMAIN}.entity")] + [
        import_module(f"custom_components.{DOMAIN}.{platform}")
        for platform in PLATFORMS
    ]

    declaring = [
        (module.__name__, name, member._unrecorded_attributes)
        for module in modules
        for name, member in vars(module).items()
        if isinstance(member, type)
        and issubclass(member, Entity)
        and "_unrecorded_attributes" in member.__dict__
    ]
    assert declaring, "nothing declared an exclusion, so this proves nothing"

    offenders = {
        f"{module}.{name}": sorted(UNRECORDED_LIST_ATTRIBUTES - declared)
        for module, name, declared in declaring
        if not declared >= UNRECORDED_LIST_ATTRIBUTES
    }

    assert not offenders, (
        f"these classes shadow the shared exclusion and drop entries: {offenders}"
    )


@REQUIRES_HASS
class TestHowARefusalIsAnswered:
    """The two refusals a parent can actually meet, and what they say."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file, which is the case that needs fetching."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(attachments=("enonce.pdf",))]
        )
        return client

    @staticmethod
    def _document(account: PronoteAccount) -> tuple[str, Any]:
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        return fingerprint(item.id, item.attachments[0].id), item.attachments[0]

    async def test_a_click_is_run_at_the_priority_of_a_human_request(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Observed, not read off the source.

        `services.py` runs human-initiated calls at `HIGH`, and a click is one:
        somebody is waiting, so it should outrank a routine collection once the
        daily cap starts shedding. It is deliberately **not** `CRITICAL`, the
        only priority that crosses quiet hours, which is reserved for a tier
        that has never collected at all.
        """
        del hass
        from custom_components.pronote_ng.attachment import _fetch
        from custom_components.pronote_ng.const import Priority

        _print, document = self._document(account)
        seen: list[Priority] = []
        original = account.extras.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            seen.append(priority)
            return await original(name, priority, fn, **kwargs)

        with patch.object(account.extras.session, "run", spy):
            await _fetch(account, document, CHILDREN[0][0])

        assert seen == [Priority.HIGH]

    async def test_a_deferral_answers_with_the_limiters_own_delay(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A hard-coded minute would invite a client to retry all night.

        Quiet hours defer for hours, not for a minute, and this endpoint is
        reachable by a browser that honours `Retry-After`. Sending the
        limiter's own figure is what keeps an automatic retry from hammering a
        school server between 22:00 and 06:00.
        """
        from custom_components.pronote_ng.attachment import PronoteAttachmentView
        from custom_components.pronote_ng.ratelimit import DeferReason, TierDeferred

        print_, _document = self._document(account)
        request = _FakeRequest(hass)

        with patch(
            "custom_components.pronote_ng.attachment._fetch",
            side_effect=TierDeferred(DeferReason.QUIET_HOURS, 7200.0),
        ):
            response = await PronoteAttachmentView().get(
                request,  # type: ignore[arg-type]
                account.entry.entry_id,
                print_,
            )

        assert response.status == 503
        assert response.headers["Retry-After"] == "7200"

    async def test_an_unknown_account_is_refused_without_a_traceback(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The address embeds an entry id, so it can name an entry that is gone.

        A removed or unloaded account has to answer with a status rather than
        raising out of the handler -- aiohttp would turn that into a 500, which
        reads like a broken integration rather than "this account is not
        loaded".
        """
        del account
        from custom_components.pronote_ng.attachment import PronoteAttachmentView

        response = await PronoteAttachmentView().get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            "01ABSENTABSENTABSENTABSENT",
            "0" * 16,
        )

        assert response.status == 404

    async def test_a_link_is_not_relayed_through_the_school_session(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A link has its own address and the dashboard was given it directly.

        Relaying one would mean fetching an unrelated site with the
        establishment's authenticated session -- which is not this
        integration's business, and would make Home Assistant an open proxy for
        anything a teacher pastes.
        """
        from custom_components.pronote_ng.attachment import PronoteAttachmentView
        from custom_components.pronote_ng.models import HomeworkAttachment

        link = HomeworkAttachment(
            name="Le sujet", url="https://exemple.invalid/sujet", id="ATTACHMENT-9"
        )
        with patch(
            "custom_components.pronote_ng.attachment._locate",
            return_value=(CHILDREN[0][0], link),
        ):
            response = await PronoteAttachmentView().get(
                _FakeRequest(hass),  # type: ignore[arg-type]
                account.entry.entry_id,
                "0" * 16,
            )

        assert response.status == 404
        assert "link" in response.text


class _FakeRequest:
    """An aiohttp request, as far as the view reads one.

    The view takes ``hass`` out of ``request.app`` and nothing else, so the
    handler can be exercised without standing up an HTTP server -- which is
    what lets these tests assert on statuses and headers rather than on a
    rendered page.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        from homeassistant.helpers.http import KEY_HASS

        self.app = {KEY_HASS: hass}


# ---------------------------------------------------------------------------
# The view: what a browser gets when the document is not there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param('Devoir "maison".pdf', "Devoir maison.pdf", id="quotes-removed"),
        pytest.param(
            "Exercices" + chr(92) + "4.pdf",
            "Exercices4.pdf",
            id="backslash-removed",
        ),
        pytest.param("Énoncé été.pdf", "?nonc? ?t?.pdf", id="accents-reduced"),
        pytest.param("Éé", "??", id="a-name-that-survives-as-marks"),
        pytest.param('""', "document", id="nothing-left-is-still-a-filename"),
    ],
)
def test_the_download_filename_is_reduced_to_something_a_header_can_carry(
    name: str, expected: str
) -> None:
    """A ``Content-Disposition`` header is ASCII, and a quote closes it early.

    The name in the attribute stays the real one -- this is only what the
    browser is told to call the file it saves. The empty case is the one worth
    pinning: a name made entirely of characters that drop out would otherwise
    produce a header naming no file at all, which browsers answer by saving
    the page instead of the document.
    """
    from custom_components.pronote_ng.attachment import _ascii

    assert _ascii(name) == expected


def test_a_document_that_belongs_to_no_homework_is_not_located(
    account: PronoteAccount,
) -> None:
    """A fingerprint nobody can match must resolve to nothing, not to anything.

    The view answers 404 on this, and the alternative is worse than an error:
    a resolver that fell through to "the first attachment" would serve one
    child's document to a signature minted for another.
    """
    assert _locate(account, "a" * 64) is None


def test_an_attachment_with_no_identifier_is_skipped_rather_than_matched(
    account: PronoteAccount,
) -> None:
    """PRONOTE publishes documents that carry a name and no ``N``.

    Those cannot be downloaded -- there is nothing to ask for -- and their
    fingerprint would be built from an empty identifier, so every one of them
    would share it. Two homework items with such a document would then be
    indistinguishable, and the view would serve whichever came first.
    """
    from custom_components.pronote_ng.attachment import fingerprint

    # The fingerprint an empty identifier would produce, if one were minted.
    collision = fingerprint("HOMEWORK-1", "")

    assert _locate(account, collision) is None


@REQUIRES_HASS
class TestWhatTheBrowserGetsBack:
    """The view end to end: the bytes, the cache, and the two failures.

    Asserted through ``PronoteAttachmentView.get`` rather than on ``_fetch``,
    because what a dashboard tile meets is a status and a set of headers. A
    document that downloads correctly and is then served with the wrong
    ``Content-Disposition`` lands in the downloads folder instead of opening,
    and nothing below the view would notice.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One homework item with one file, which is what needs serving."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(attachments=("enonce.pdf",))]
        )
        return client

    @staticmethod
    def _print(account: PronoteAccount) -> str:
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        return fingerprint(item.id, item.attachments[0].id)

    async def test_an_address_naming_no_document_is_a_404_and_costs_nothing(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """A signed address outlives the document it names.

        The signature bounds *who* may ask, not *what* is still there: a tile
        rendered from yesterday's snapshot carries an address for a homework
        item that has since left the display horizon. That has to answer with a
        status and without a request -- the snapshot is the authority, so a
        fingerprint it cannot match is refused before the wire.
        """
        from custom_components.pronote_ng.attachment import PronoteAttachmentView

        before = len(parent_client.communication.session.gets)

        response = await PronoteAttachmentView().get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            "0" * 64,
        )

        assert response.status == 404
        assert "no such document" in response.text
        assert len(parent_client.communication.session.gets) == before

    async def test_the_bytes_are_served_inline_and_the_second_read_is_free(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """One request per document, however many times a card is rendered.

        A dashboard re-renders on every state change of every entity on it, and
        an exercise sheet is the same bytes for ever -- it is addressed by a
        digest of its identifiers, not by a position. Without the cache a page
        holding four documents would spend four requests each time somebody
        opened it, which is how a daily budget disappears into a screen nobody
        is even looking at.
        """
        from custom_components.pronote_ng.attachment import PronoteAttachmentView

        print_ = self._print(account)
        view = PronoteAttachmentView()

        first = await view.get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            print_,
        )

        assert first.status == 200
        assert first.body == b"%PDF-1.4 not a real document"
        assert first.content_type == "application/pdf"
        assert first.headers["Content-Disposition"] == 'inline; filename="enonce.pdf"'
        assert "private" in first.headers["Cache-Control"]
        fetched = len(parent_client.communication.session.gets)
        assert fetched == 1

        second = await view.get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            print_,
        )

        assert second.status == 200
        assert second.body == first.body
        assert len(parent_client.communication.session.gets) == fetched, (
            "the second read went to PRONOTE: the cache is not being reused"
        )

    async def test_a_download_that_fails_answers_502_and_caches_nothing(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A failed download must not become a cached empty document.

        502 and not 500: the school's server refused, this integration did not
        break, and the distinction is what tells a reader whether to open an
        issue here. Caching the failure would be worse than the failure -- the
        document would stay broken until a reload, long after the server
        recovered.
        """
        from custom_components.pronote_ng.attachment import (
            PronoteAttachmentView,
            cache_for,
        )

        print_ = self._print(account)

        with patch(
            "custom_components.pronote_ng.attachment._fetch",
            side_effect=AttachmentUnavailable("enonce.pdf", 403),
        ):
            response = await PronoteAttachmentView().get(
                _FakeRequest(hass),  # type: ignore[arg-type]
                account.entry.entry_id,
                print_,
            )

        assert response.status == 502
        assert cache_for(hass, account.entry.entry_id).get(print_) is None

    async def test_a_source_with_no_pronote_session_cannot_download_at_all(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Ecoledirecte has no session to fetch through, and must say so.

        The view is registered once for the whole instance, so it answers for
        every entry whatever its source. Reaching the gateway with no session
        would be an ``AttributeError`` inside the handler, which aiohttp turns
        into a 500 -- a broken integration, rather than a source that does not
        offer the feature.
        """
        del hass
        from custom_components.pronote_ng.attachment import _fetch

        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        document = snapshot.data.homework[0].attachments[0]

        with (
            patch(
                "custom_components.pronote_ng.account.has_pronote_extras",
                return_value=False,
            ),
            pytest.raises(AttachmentUnavailable) as raised,
        ):
            await _fetch(account, document, CHILDREN[0][0])

        assert raised.value.status == 501


@REQUIRES_HASS
class TestWhatTheScanSkipsOverRatherThanStopsAt:
    """``_locate`` walks every child, and a gap at one must not end the walk."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """A document PRONOTE sent with no ``N``, beside one that has one."""
        from .fixtures.client import FakeClient

        item = protocol.homework(attachments=("sans_n.pdf", "enonce.pdf"))
        # PRONOTE does publish documents carrying a name and no identifier;
        # the fixture builder always mints one, so it is removed here rather
        # than adding an option nothing else would use.
        item["ListePieceJointe"]["V"][0]["N"] = ""
        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response([item])
        return client

    async def test_a_document_with_no_identifier_is_stepped_over(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Skipped, and the document after it is still found.

        Every attachment with no ``N`` would share one fingerprint -- the
        digest of an empty identifier -- so matching on it would serve an
        arbitrary document. Returning ``None`` at the first such attachment
        would be the other defect: the usable document sitting behind it in the
        same homework item would become unreachable.
        """
        del hass
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        nameless, usable = item.attachments
        assert nameless.id == ""

        assert _locate(account, fingerprint(item.id, "")) is None

        located = _locate(account, fingerprint(item.id, usable.id))
        assert located is not None
        assert located[1].name == usable.name

    async def test_a_child_whose_homework_never_collected_does_not_end_the_scan(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """One child with no snapshot, and the sibling's document still serves.

        A second child's first collection can be deferred by the budget, or
        fail, or simply not have happened yet on a freshly added entry. If the
        scan answered ``None`` at that child instead of stepping over it, every
        document of every child listed after them would return 404 -- and the
        order children are announced in is PRONOTE's, so which siblings broke
        would look arbitrary.
        """
        del hass
        real = account.snapshot
        item = account.snapshot(Tier.HOMEWORK, CHILDREN[1][0])
        assert item is not None, "the fixture must give both children homework"

        def missing_for_the_first(tier: Tier, student_id: str) -> Any:
            if student_id == CHILDREN[0][0]:
                return None
            return real(tier, student_id)

        with patch.object(account, "snapshot", missing_for_the_first):
            located = _locate(
                account, fingerprint(item.data.homework[0].id, "ATTACHMENT-2")
            )

        assert located is not None
        assert located[0] == CHILDREN[1][0]
