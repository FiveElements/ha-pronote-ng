"""Serving a homework document without ever publishing PRONOTE's address.

A homework attachment comes in two kinds and only one of them has an address
that means anything outside the session that fetched it (see
:class:`.models.HomeworkAttachment`). A **link** is published as-is. A **file**
is the reason this module exists.

PRONOTE's address for a file is
``FichiersExternes/<hex>/<name>?Session=<h>``, where the hex segment is the
document's identifier encrypted with the session's own AES key and IV. Two
consequences decide the whole design: it stops working when the session does,
and while it works it opens the document with no credentials at all. So it must
never reach a state, an attribute, the recorder or a diagnostics download --
the rule §8.2 established for the iCal URL.

What is published instead is an address **Home Assistant** serves: a signed,
expiring path to the view below, which fetches the bytes through the ordinary
chokepoint -- one session, one lock, charged to the limiter -- and relays them.

Relayed and not redirected, deliberately. A 302 would put PRONOTE's address in
the browser's address bar, its history, the network tab and the log of any
proxy on the way, which is precisely the credential this module exists not to
publish. Nothing in the response body or headers carries it.

Three properties of the path are load-bearing:

**It is rooted, not absolute.** That is what :func:`async_sign_path` produces,
and it is also the only correct form: an absolute URL would make the
integration guess which host the browser arrived through, and ``get_url`` hands
the internal address to somebody connected from outside.

**It names a fingerprint, not an identifier.** A real PRONOTE identifier
carries a ``#`` -- a fragment delimiter, which would truncate the path -- and
writing one into an attribute puts it in the recorder. The fingerprint is the
same idiom ``diagnostics.py`` uses for child identifiers. It also makes the
address name a *document* rather than a position, so re-ordering between two
collections cannot serve the wrong file.

**It expires.** An address copied out of a dashboard stops working, which is
what keeps an old recorder row from being a lasting key to a child's homework.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.helpers.http import KEY_HASS

from .const import DOMAIN, Priority, Tier
from .gateway import AttachmentUnavailable
from .ratelimit import TierDeferred

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .account import PronoteAccount
    from .models import HomeworkAttachment, HomeworkFacts

#: How long a published address stays usable.
#:
#: Twelve hours, and the bound is derived rather than picked. It has to be
#: comfortably longer than the homework tier's interval -- thirty minutes -- or
#: an attribute would hold a signature that expired before the next collection
#: replaced it, and a parent would meet a 401 on a link the page was still
#: showing. It has to be short enough that a row left in the recorder, or a
#: screenshot, is not a lasting key: half a day means an address leaked at
#: breakfast is inert by bedtime.
SIGNATURE_LIFETIME_HOURS: Final = 12

#: Characters of the digest kept in the path.
#:
#: Sixteen hex characters, 64 bits. This is not a secret -- the signature is
#: what authorises the request -- so its only job is to name one document
#: without colliding with another on the same account. At a few dozen
#: attachments a collision is not a rounding error away from impossible, it is
#: impossible in practice, and a longer digest would only make the address
#: harder to read in a log.
_FINGERPRINT_LENGTH: Final = 16

#: How many documents' bytes are kept in memory, per account.
#:
#: The cache is what makes the cost of this feature bounded rather than
#: per-click: a browser prefetcher, a link scanner on the page, or a parent
#: opening the same exercise twice costs one request per document per Home
#: Assistant run, not one per fetch. Sixteen is above the number of documents
#: a fortnight of homework carries -- twelve, measured -- so in practice
#: nothing is evicted before a reload clears it anyway.
_CACHE_ENTRIES: Final = 16

#: Refused rather than guessed. The browser is told what PRONOTE declared, and
#: PRONOTE declares nothing useful for some documents, so a fallback is needed
#: -- but it must not be a type the browser will *execute* or render as a
#: document in our own origin. ``application/octet-stream`` makes an unknown
#: document download instead of running.
_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"

#: Content types the browser is allowed to be told, when PRONOTE declares one.
#:
#: A whitelist, because the response is served from Home Assistant's **own
#: origin**: a document echoed back as ``text/html`` would run its own script
#: with the user's session, which is a stored cross-site scripting route
#: through a teacher's file upload. PDFs and images are what exercise sheets
#: actually are, and everything else downloads.
_SAFE_CONTENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
    }
)

#: Where the per-entry byte caches live.
_CACHE_KEY: Final = f"{DOMAIN}_attachment_cache"


def fingerprint(homework_id: str, attachment_id: str) -> str:
    """Name one document in a URL, without naming it in the recorder.

    A digest of the pair rather than of the attachment alone: PRONOTE's
    identifiers are unique per account in practice, but the pair is what the
    lookup actually resolves, and a digest that matched two homework entries
    would serve one child's document from the other's row.
    """
    digest = hashlib.sha256(f"{homework_id}\x00{attachment_id}".encode())
    return digest.hexdigest()[:_FINGERPRINT_LENGTH]


def signed_path(hass: HomeAssistant, entry_id: str, print_: str) -> str:
    """The address a dashboard receives for one document."""
    return async_sign_path(
        hass,
        f"/api/{DOMAIN}/attachment/{entry_id}/{print_}",
        timedelta(hours=SIGNATURE_LIFETIME_HOURS),
    )


def async_register_view(hass: HomeAssistant) -> None:
    """Register the route, once, independently of any account.

    Called from ``async_setup`` and not from ``async_setup_entry``, for the same
    reason the actions are registered there: a route is per Home Assistant, not
    per account, and registering it once per entry would re-register the same
    path -- at best redundantly, at worst replacing a live handler mid-request.
    The view resolves its account from the entry id in the path at call time, so
    it needs nothing from set-up.
    """
    hass.http.register_view(PronoteAttachmentView())


class _ByteCache:
    """The fetched documents, newest last, oldest evicted.

    Deliberately not an ``lru_cache``: the entries are megabytes and their
    lifetime is a config entry's, so eviction has to be observable and the
    whole thing has to be droppable on unload.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[bytes, str]] = {}

    def get(self, key: str) -> tuple[bytes, str] | None:
        return self._entries.get(key)

    def put(self, key: str, value: tuple[bytes, str]) -> None:
        self._entries[key] = value
        while len(self._entries) > _CACHE_ENTRIES:
            self._entries.pop(next(iter(self._entries)))


def cache_for(hass: HomeAssistant, entry_id: str) -> _ByteCache:
    """The byte cache of one account, created on first use.

    Held in ``hass.data`` under the entry rather than on the account object,
    for the reason `login_guard` holds the login counters there: an account is
    rebuilt on every reload and a retried set-up would otherwise drop a cache
    that is still perfectly valid -- the documents have not changed because
    Home Assistant reloaded.
    """
    caches: dict[str, _ByteCache] = hass.data.setdefault(_CACHE_KEY, {})
    if entry_id not in caches:
        caches[entry_id] = _ByteCache()
    return caches[entry_id]


def _locate(
    account: PronoteAccount, print_: str
) -> tuple[str, HomeworkAttachment] | None:
    """Find the document one fingerprint names, and whose child it belongs to.

    Scanned rather than addressed, and that is why the path needs no student
    and no homework identifier: the snapshot is the authority on what this
    account may fetch, so a fingerprint that matches nothing in it is refused
    without ever reaching PRONOTE. An address for a document that has since
    left the horizon therefore stops working, which is the correct answer --
    the integration cannot vouch for what it no longer holds.
    """
    for student in account.students:
        snapshot = account.snapshot(Tier.HOMEWORK, student.id)
        if snapshot is None:
            continue
        facts: HomeworkFacts = snapshot.data
        for item in facts.homework:
            for attachment in item.attachments:
                if not attachment.id:
                    continue
                if hmac.compare_digest(fingerprint(item.id, attachment.id), print_):
                    return student.id, attachment
    return None


class PronoteAttachmentView(HomeAssistantView):
    """Relay one homework document.

    ``requires_auth`` stays at its default -- ``True`` -- and that is what makes
    a plain ``<a href>`` work from a dashboard: a request carrying a valid
    ``authSig`` satisfies Home Assistant's auth middleware without an
    ``Authorization`` header, which is the mechanism ``entity_picture`` uses for
    camera and image entities. An unsigned guess at this URL gets 401 before
    any of the code below runs, so nothing here can be made to spend a request
    by somebody who cannot already see the dashboard.
    """

    url = f"/api/{DOMAIN}/attachment/{{entry_id}}/{{print_}}"
    name = f"api:{DOMAIN}:attachment"

    async def get(
        self, request: web.Request, entry_id: str, print_: str
    ) -> web.StreamResponse:
        """Answer with the document's bytes, or with a status that says why not."""
        hass = request.app[KEY_HASS]
        account = _account(hass, entry_id)
        if account is None:
            return web.Response(status=404, text="unknown or unloaded account")

        located = _locate(account, print_)
        if located is None:
            return web.Response(status=404, text="no such document on this account")
        student_id, attachment = located

        if attachment.url is not None:
            # A link, not a file. It has its own address and the dashboard was
            # given it directly, so serving it here would mean fetching a third
            # party's page with the school's session -- which this integration
            # has no business doing.
            return web.Response(status=404, text="that attachment is a link")

        cache = cache_for(hass, entry_id)
        if (cached := cache.get(print_)) is None:
            try:
                cached = await _fetch(account, attachment, student_id)
            except TierDeferred:
                # The limiter said no. 503 with `Retry-After` rather than an
                # error page, because this is a "later", not a "never", and a
                # browser understands the difference.
                return web.Response(
                    status=503, headers={"Retry-After": "60"}, text="rate limited"
                )
            except AttachmentUnavailable as error:
                return web.Response(status=502, text=str(error))
            cache.put(print_, cached)

        content, content_type = cached
        return web.Response(
            body=content,
            content_type=content_type,
            headers={
                # `inline` so an exercise sheet opens in the browser's own
                # viewer rather than landing in the downloads folder, but with
                # the real filename so saving it keeps a usable name.
                "Content-Disposition": f'inline; filename="{_ascii(attachment.name)}"',
                # The bytes never change for a given fingerprint -- it names a
                # document, not a position -- and the signature is what bounds
                # access, so letting the browser keep them saves re-serving the
                # same megabytes on every render.
                "Cache-Control": "private, max-age=3600",
            },
        )


async def _fetch(
    account: PronoteAccount, attachment: HomeworkAttachment, student_id: str
) -> tuple[bytes, str]:
    """Download one document through the one path to the network."""

    def work(client: Any) -> tuple[bytes, str | None, int]:
        return account.gateway.homework_attachment(
            client, attachment_id=attachment.id, name=attachment.name
        )

    content, declared, _cost = await account.session.run(
        str(Tier.HOMEWORK),
        # Never CRITICAL: a click is not more important than the collection
        # that keeps every other entity current, and quiet hours have no reason
        # to be crossed for a document a parent can open in the morning.
        Priority.LOW,
        work,
        student_id=student_id,
        cost=1,
    )
    return content, _content_type(declared)


def _content_type(declared: str | None) -> str:
    """What the browser is told, which is not always what PRONOTE said."""
    if declared is None:
        return _FALLBACK_CONTENT_TYPE
    # Parameters dropped before the comparison: PRONOTE sends
    # `text/plain; charset=...`, and a whitelist that matched the whole header
    # would fall through to the fallback for a document it should have allowed.
    base = declared.split(";", 1)[0].strip().lower()
    if base in _SAFE_CONTENT_TYPES:
        return base
    return _FALLBACK_CONTENT_TYPE


def _ascii(name: str) -> str:
    """A filename safe to put in a header.

    Header values are latin-1 on the wire and a quote would end the parameter
    early, so an accented or quoted document name is reduced rather than
    trusted. The name in the attribute stays the real one -- this is only what
    the browser is told to call the file it saves.
    """
    cleaned = name.replace('"', "").replace("\\", "")
    return cleaned.encode("ascii", "replace").decode("ascii") or "document"


def _account(hass: HomeAssistant, entry_id: str) -> PronoteAccount | None:
    """The loaded account for one entry, or ``None``."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    return getattr(entry, "runtime_data", None)
