"""The adapter: one function per protocol call, frozen DTOs out.

This is the only module that imports ``pronotepy``, so it is the only file to
re-read on a version bump and the only point to double in tests (§3.4).

Two rules govern the decoding, and they are not the same rule.

**Deduplicate without reimplementing.** Specification v1 concluded the raw
responses had to be decoded by hand, which contradicted its own §3.4: it
refused to take on upstream's decoding debt and then took it on. pronotepy's
data classes accept the bare dictionary -- ``Average(json)``, ``Absence(json)``,
``Delay(json)``, ``Evaluation(json)``, ``Report(data)``; ``Lesson(client,
json)`` and ``Punishment(client, json)`` take a client and nothing else. So the
shape is one raw ``post()`` per tab, the sub-lists handed to upstream's
classes, then mapping to frozen DTOs. Deduplication *and* upstream's fixes.

**One exception: `Grade`.** ``Grade.__init__`` resolves ``self.period`` through
``Util.get(Period.instances, id=p)[0]``, and ``Period.instances`` is a
never-cleared class attribute -- the only reader of that registry in all of
``dataClasses.py``. Using it would tie us to a global we can neither keep (it
leaks a dead client per period) nor clear (``[0]`` on an empty list raises
``IndexError``, wrapped as ``ParsingError``, failing the whole marks batch).
And independently: ``Util.grade_parse`` has already replaced ``|1``…``|8`` with
``"Absent"``, ``"Dispense"``… so ``Grade.grade`` is *already* lossy, and
decoding ``note.V`` ourselves is the only route to the raw sentinel that
:class:`~.const.GradeStatus` needs. Both reasons are recorded here on purpose;
this is a bounded exception, not a policy.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from pronotepy import dataClasses
from pronotepy.exceptions import DataError, ParsingError

from .const import (
    FUNC_ATTENDANCE,
    FUNC_EVALUATIONS,
    FUNC_HOMEWORK,
    FUNC_MARKS,
    FUNC_NEWS,
    FUNC_NEWS_WRITE,
    FUNC_PERSONAL_INFO,
    FUNC_REPORT,
    FUNC_TIMETABLE,
    GRADE_SENTINELS,
    PRESENCE_KIND_ABSENCE,
    PRESENCE_KIND_DELAY,
    PRESENCE_KIND_PUNISHMENT,
    GradeStatus,
)
from .models import (
    Absence,
    Acquisition,
    AttendanceFacts,
    Average,
    Delay,
    Discussion,
    DiscussionsFacts,
    Evaluation,
    EvaluationsFacts,
    Grade,
    Guardian,
    Homework,
    HomeworkFacts,
    Identity,
    Information,
    Lesson,
    MarksFacts,
    Menu,
    MenusFacts,
    Message,
    NewsFacts,
    Period,
    Punishment,
    PunishmentSlot,
    Report,
    ReportSubject,
    SessionFacts,
    StaticFacts,
    Student,
    TeachingStaffMember,
    TimetableFacts,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .hardened_client import HardenedClient

_LOGGER: Final = logging.getLogger(__name__)

#: What decoding one entry can raise, and why each member is in the list.
#:
#: * ``ParsingError`` -- upstream's own wrapper, raised by ``Object._resolver``
#:   when a *strict* field is missing (dataClasses.py:220).
#: * ``DataError`` -- its parent, and **not** a ``PronoteAPIError``, so it never
#:   reaches the protocol-error handling in :mod:`.session`.
#: * ``ValueError`` -- ``strptime`` on a date the establishment wrote its own
#:   way, and ``float()`` on a number that is not one.
#: * ``KeyError`` -- a converter lambda indexing a key that moved; upstream's
#:   resolver only guards the *path*, never the converter's own body.
#: * ``IndexError`` -- ``Util.grade_parse`` does ``grade_translate[int(s[1]) -
#:   1]`` against a table of eight (dataClasses.py:118), so a future ``|9``
#:   sentinel indexes past the end. This one is why the tuple is shared: it was
#:   present on two decoders and missing from five.
#: * ``TypeError`` -- a path segment that is ``null`` rather than absent, which
#:   upstream's ``KeyError`` guard does not cover.
#: * ``ZeroDivisionError`` -- ``Lesson.__init__`` computes ``place %
#:   (len(end_times) - 1)`` (dataClasses.py:905), so an establishment
#:   publishing a single ``ListeHeuresFin`` entry divides by zero.
_ENTRY_ERRORS: Final = (
    ParsingError,
    DataError,
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    ZeroDivisionError,
)

#: Cap on how many discussion threads may be expanded in one cycle. Reading
#: ``Discussion.messages`` costs a request each; without a cap, a parent coming
#: back from holiday to twenty new threads would spend twenty requests in one
#: batch and trip the token bucket.
MAX_DISCUSSION_EXPANSIONS: Final = 3


class ProtocolChanged(Exception):  # noqa: N818 -- fails a tier, never a service
    """A collection the protocol should provide was absent from the response.

    Raised rather than returning an empty list, so the tier *fails* -- keeping
    its previous snapshot and going stale in due course -- instead of
    publishing a successful, empty one. See :func:`_required_list`.
    """

    def __init__(self, what: str, path: str) -> None:
        super().__init__(f"PRONOTE returned no {what} (no {path} in the response)")
        self.what = what
        self.path = path


class DiscussionNotFound(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """No thread with that identifier is visible to this account."""

    def __init__(self, discussion_id: str) -> None:
        super().__init__(f"no discussion with id {discussion_id}")
        self.discussion_id = discussion_id


class DiscussionIsClosed(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """PRONOTE closed the thread; ``pronotepy`` would refuse the reply."""

    def __init__(self, discussion_id: str) -> None:
        super().__init__(f"discussion {discussion_id} is closed")
        self.discussion_id = discussion_id


class RecipientNotFound(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """One or more named recipients are not reachable from this account."""

    def __init__(self, missing: list[str], available: list[str]) -> None:
        super().__init__(f"unknown recipients: {', '.join(missing)}")
        self.missing = missing
        self.available = available


class GatewayResult[T]:
    """A tier's facts plus what they actually cost on the wire.

    Returning the cost rather than inferring it is what makes the per-tier
    contract test of §11.1 possible: an accidental property access doubles the
    cost without anything breaking, and that is the regression this whole
    project exists to prevent.
    """

    __slots__ = ("calls", "facts")

    def __init__(self, facts: T, calls: int) -> None:
        self.facts = facts
        self.calls = calls


# ---------------------------------------------------------------------------
# Non-strict primitives (§3.3.3)
# ---------------------------------------------------------------------------


def _get(source: Any, *path: str) -> Any:
    """Walk a path through nested dicts, yielding ``None`` on any miss.

    An absent field gives ``None``, never an exception: class averages, minima,
    maxima and coefficients are legitimately absent when the establishment does
    not publish them.
    """
    value: Any = source
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _list(source: Any, *path: str) -> list[Any]:
    """Read a ``{"V": [...]}`` list, or an empty list."""
    value = _get(source, *path)
    if isinstance(value, dict):
        value = value.get("V")
    return value if isinstance(value, list) else []


def _required_list(source: Any, *path: str, what: str) -> list[Any]:
    """Read a collection the protocol is expected to provide.

    Distinguishes "the key is absent" from "the list is empty", and refuses the
    first. That distinction is the difference between a broken integration and
    a quiet school week, and collapsing them is the worst failure mode this
    gateway has: if PRONOTE renamed ``ListeCours``, then ``_get(...) or []``
    returned an empty timetable, the tier reported **success**, the snapshot was
    replaced, ``binary_sensor.<eleve>_jour_de_classe`` read ``off`` and the
    wake-up automation simply stopped firing -- with nothing in the log, and
    staleness never triggering either, because the collection had succeeded.

    Raising instead makes the tier fail, which keeps the previous snapshot,
    marks it dated and eventually raises a repair (§5.4). "I know, but it is
    old" is a recoverable answer; "there are no lessons this week" when a field
    was renamed is not.
    """
    cursor: Any = source
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            _LOGGER.warning(
                "PRONOTE returned no %s: the response carries no %s. This "
                "usually means the protocol changed and the integration needs "
                "an update; treating the collection as failed rather than as "
                "empty so the previous data is kept",
                what,
                ".".join(path),
            )
            raise ProtocolChanged(what, ".".join(path))
        cursor = cursor[key]
    if isinstance(cursor, dict) and "V" in cursor:
        cursor = cursor["V"]
    if cursor is None:
        return []
    if not isinstance(cursor, list):
        _LOGGER.warning(
            "PRONOTE returned a %s that is not a list but a %s; treating the "
            "collection as failed rather than as empty",
            what,
            type(cursor).__name__,
        )
        raise ProtocolChanged(what, ".".join(path))
    return cursor


def _number(raw: Any) -> float | None:
    """Parse a PRONOTE numeric string.

    Values arrive with a **comma** decimal separator, and a sentinel like
    ``|1`` is not a number at all.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text.startswith("|"):
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _grade_sentinel(raw: Any) -> GradeStatus | None:
    """Map ``|1``…``|8`` to the enum, and anything else unrecognised to UNKNOWN.

    Upstream's table has exactly eight entries and is indexed by
    ``int(string[1]) - 1``, so a future ``|9`` raises ``IndexError`` there and
    fails *every* grade in the batch. This is how we decline to inherit that
    (§3.3.3).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.startswith("|"):
        return None
    return GRADE_SENTINELS.get(text[1:2], GradeStatus.UNKNOWN)


def _strings(values: Iterable[Any], key: str = "L") -> tuple[str, ...]:
    """Pull the label out of a list of ``{"L": …}`` records."""
    out: list[str] = []
    for value in values:
        if isinstance(value, dict):
            label = value.get(key)
            if isinstance(label, str) and label:
                out.append(label)
        elif isinstance(value, str) and value:
            out.append(value)
    return tuple(out)


class PronoteGateway:
    """Turns protocol responses into frozen DTOs, in the establishment's time.

    All datetime conversion happens here, once, with the establishment's
    timezone. PRONOTE dates are naive local text with no offset, and two of
    ``Util.date_parse``'s six forms complete the missing part with *today* --
    comparing those against the clock of a machine set to UTC, which is the
    default for a container, produces silent shifts and therefore automations
    that fire at the wrong hour (§4.2).
    """

    def __init__(
        self,
        timezone: str,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._tz = ZoneInfo(timezone)
        # `None` means "read the real clock". Passing one is how a test fixes
        # the date, which several decisions here depend on -- see `now`.
        self._clock = clock

    @property
    def timezone(self) -> ZoneInfo:
        """The establishment's timezone."""
        return self._tz

    def now(self) -> dt.datetime:
        """Current instant, in the establishment's timezone.

        Injectable, like the limiter's and the scheduler's clocks, and for the
        same reason: "does tomorrow fall in the next week?" and "which week
        does the school year start in?" are decided here, and a test that
        cannot fix the date cannot ask either question -- it can only pass on
        most days of the year.
        """
        if self._clock is not None:
            return self._clock().astimezone(self._tz)
        return dt.datetime.now(tz=self._tz)

    def today(self) -> dt.date:
        """Current date, in the establishment's timezone.

        Never ``date.today()``: that reads the host's timezone, which on a
        default container is UTC (§4.2).
        """
        return self.now().date()

    def _aware(self, value: dt.datetime | dt.date | None) -> dt.datetime | None:
        """Attach the establishment's timezone to a naive PRONOTE value.

        For fields that are legitimately absent. Where the protocol guarantees
        a value -- because upstream resolved it strictly and would have raised
        otherwise -- use :meth:`_instant`, which is total.
        """
        if value is None:
            return None
        return self._instant(value)

    def _instant(self, value: dt.datetime | dt.date) -> dt.datetime:
        """Attach the establishment's timezone to a value that is always there.

        Separate from :meth:`_aware` so the callers that cannot receive
        ``None`` do not have to write a branch that cannot be taken.
        ``Absence.from_date``, ``Delay.date``, ``Lesson.start`` and
        ``ScheduledPunishment.start`` are all resolved strictly upstream: they
        raise rather than return ``None``, and that exception is already caught
        one level up. An impossible ``if ... is None: return None`` in each of
        those four decoders was four branches no test could ever reach.

        A ``date`` becomes local midnight, so a calendar can compare an all-day
        event against a timed one -- which ``date`` and ``datetime`` mixed
        cannot.
        """
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=self._tz)
            return value.astimezone(self._tz)
        return dt.datetime.combine(value, dt.time.min, tzinfo=self._tz)

    # -- session facts: zero calls ----------------------------------------

    def session_facts(self, client: HardenedClient) -> GatewayResult[SessionFacts]:
        """Everything the login already handed us.

        ``periods``, ``class_name``, ``establishment`` and ``name`` are read out
        of ``func_options`` and ``parametres_utilisateur``, already in memory
        once authenticated -- **no call at all**. Specification v1 filed these
        under the ``static`` tier and budgeted requests for them (annexe A §2).
        """
        info = client.info
        student = Student(
            id=str(info.id),
            name=str(info.name),
            class_name=info.class_name or None,
            establishment=info.establishment or None,
            has_photo=bool(info.raw_resource.get("avecPhoto")),
        )

        periods: list[Period] = []
        raw_periods = _get(
            client.func_options, "dataSec", "data", "General", "ListePeriodes"
        )
        for index, raw in enumerate(raw_periods or [], start=1):
            start = self._aware(_parse_datetime(_get(raw, "dateDebut", "V")))
            end = self._aware(_parse_datetime(_get(raw, "dateFin", "V")))
            if start is None or end is None:
                continue
            periods.append(
                Period(
                    id=str(raw.get("N")),
                    name=str(raw.get("L", "")),
                    start=start,
                    end=end,
                    index=index,
                )
            )

        return GatewayResult(
            SessionFacts(
                student=student,
                periods=tuple(periods),
                current_period=self._current_period(client, periods),
            ),
            calls=0,
        )

    def _current_period(
        self, client: HardenedClient, periods: Sequence[Period]
    ) -> Period | None:
        """The period PRONOTE considers current, or ``None``.

        ``Client.current_period`` falls back to ``onglets[0]`` when tab 198 is
        absent, which silently names the wrong period in an establishment that
        does not publish grades. We prefer ``None``, and the entity goes
        unavailable rather than wrong (§3.3.3).
        """
        tabs = _list(
            client.parametres_utilisateur,
            "dataSec",
            "data",
            "ressource",
            "listeOngletsPourPeriodes",
        )
        marks_tab = next((tab for tab in tabs if _get(tab, "G") == 198), None)
        if marks_tab is None:
            _LOGGER.debug(
                "no marks tab (198) in listeOngletsPourPeriodes; "
                "current period left undetermined rather than guessed"
            )
            return None
        period_id = _get(marks_tab, "periodeParDefaut", "V", "N")
        if period_id is None:
            return None
        return next((p for p in periods if p.id == str(period_id)), None)

    # -- timetable ---------------------------------------------------------

    def timetable(
        self, client: HardenedClient, *, include_next_week: bool
    ) -> GatewayResult[TimetableFacts]:
        """The current week, plus next week only when it is needed.

        ``Client.lessons()`` loops ``for week in range(first_week, last_week +
        1)`` and posts ``PageEmploiDuTemps`` once *per week*, then filters
        client-side. Asking for "today and tomorrow" therefore costs exactly the
        same request as asking for the whole week, which is why the separate
        week tier was folded in here (§5.2).

        Raw posts rather than ``client.lessons()`` for one reason: the DTO needs
        ``place``, ``duree`` and whether ``end`` was supplied or inferred, and
        none of those survive ``pronotepy.Lesson``.
        """
        today = self.today()
        weeks = [client.get_week(today)]
        if include_next_week:
            next_week = client.get_week(today + dt.timedelta(days=1))
            if next_week not in weeks:
                weeks.append(next_week)

        lessons: list[Lesson] = []
        calls = 0
        for week in weeks:
            raw_lessons, used = self._fetch_week(client, week)
            calls += used
            lessons.extend(raw_lessons)

        return GatewayResult(
            TimetableFacts(
                lessons=deduplicate_lessons(lessons),
                # Sorted, but not de-duplicated: the change detector needs the
                # entries de-duplication discards. See `TimetableFacts`.
                all_lessons=tuple(
                    sorted(lessons, key=lambda lesson: (lesson.start, lesson.place))
                ),
                weeks_fetched=tuple(weeks),
            ),
            calls=calls,
        )

    def _fetch_week(
        self, client: HardenedClient, week: int
    ) -> tuple[list[Lesson], int]:
        """One ``PageEmploiDuTemps`` request, decoded."""
        resource = client.parametres_utilisateur["dataSec"]["data"]["ressource"]
        payload = {
            "ressource": resource,
            "Ressource": resource,
            "avecAbsencesEleve": False,
            "avecConseilDeClasse": True,
            "estEDTPermanence": False,
            "avecAbsencesRessource": True,
            "avecDisponibilites": True,
            "avecInfosPrefsGrille": True,
            "NumeroSemaine": week,
            "numeroSemaine": week,
        }
        raw = client.post(FUNC_TIMETABLE[0], FUNC_TIMETABLE[1], payload)
        entries = _required_list(raw, "dataSec", "data", "ListeCours", what="timetable")

        lessons = [
            lesson
            for lesson in (self._lesson(client, entry) for entry in entries)
            if lesson is not None
        ]
        return lessons, 1

    def _lesson(self, client: HardenedClient, entry: dict[str, Any]) -> Lesson | None:
        """Decode one timetable entry, keeping the raw slot coordinates.

        ``TypeError`` is in the catch list for a concrete reason: upstream's
        ``_resolver`` walks the path inside ``try: ... except KeyError``, so a
        segment holding ``null`` -- ``{"cahierDeTextes": {"V": null}}`` is a
        real response -- raises ``TypeError``, which is neither converted to
        ``ParsingError`` nor caught by the obvious tuple. One such entry used to
        fail the whole tier, which is exactly what §3.3.3 forbids.

        And when upstream does refuse the entry, the raw JSON is decoded
        directly rather than the slot being dropped: see :meth:`_lesson_raw`.
        """
        try:
            upstream = dataClasses.Lesson(client, entry)
        except _ENTRY_ERRORS:
            fallback = self._lesson_raw(entry)
            if fallback is None:
                _LOGGER.debug("skipping an undecodable timetable entry", exc_info=True)
            else:
                _LOGGER.debug(
                    "decoding timetable entry %s from raw JSON: pronotepy refused it",
                    entry.get("N"),
                    exc_info=True,
                )
            return fallback

        start = self._instant(upstream.start)
        end = self._sane_end(start, self._instant(upstream.end), upstream.id)

        subject = upstream.subject
        return Lesson(
            id=str(upstream.id),
            subject=subject.name if subject else None,
            subject_id=str(subject.id) if subject else None,
            teachers=tuple(upstream.teacher_names or ()),
            classrooms=tuple(upstream.classrooms or ()),
            groups=tuple(upstream.group_names or ()),
            start=start,
            end=end,
            canceled=bool(upstream.canceled),
            status=upstream.status,
            detention=bool(upstream.detention),
            outing=bool(upstream.outing),
            exempted=bool(upstream.exempted),
            test=bool(upstream.test),
            memo=upstream.memo,
            background_color=upstream.background_color,
            virtual_classrooms=tuple(upstream.virtual_classrooms or ()),
            num=int(upstream.num or 0),
            place=int(entry.get("place", 0) or 0),
            duration=int(entry.get("duree", 1) or 1),
            # `Util.place2time` carries the upstream comment "might be wrong...
            # works with demo", and everything downstream depends on `end`.
            end_inferred=_get(entry, "DateDuCoursFin", "V") is None,
        )

    def _sane_end(
        self, start: dt.datetime, end: dt.datetime, identifier: object
    ) -> dt.datetime:
        """Guarantee ``end > start``, re-deriving the end if it is not.

        When ``DateDuCoursFin`` is absent, upstream infers the end as
        ``place % (len(end_times) - 1) + duree - 1`` and then feeds *that*
        through ``place2time`` a second time, under its own comment "might be
        wrong... works with demo". At the last slots of the day the modulo
        wraps, so a 17:00 lesson came back ending at 09:00 -- an inverted
        interval that a calendar draws as a broken event, that
        ``binary_sensor.<eleve>_en_cours`` can never match, and that makes
        ``sensor.<eleve>_fin_des_cours`` wrong for the whole day.

        §4.1 makes this gateway the single place ``end`` is established, so it
        is also the only place that can refuse an impossible one. The fallback
        is a plain hour: never earlier than the start, and ``end_inferred`` is
        already ``True`` on every entry this can touch, so the entities that
        must not assert a doubtful end stay silent regardless.
        """
        if end > start:
            return end
        _LOGGER.debug(
            "lesson %s came back ending before it starts (%s -> %s); using a "
            "one-hour slot instead",
            identifier,
            start.isoformat(),
            end.isoformat(),
        )
        return start + dt.timedelta(hours=1)

    def _lesson_raw(self, entry: dict[str, Any]) -> Lesson | None:
        """Decode the minimum a timetable slot needs, from raw JSON only.

        Used when upstream refuses the entry. The most common cause is the most
        awkward one: ``Lesson.__init__`` resolves ``ListeContenus`` with no
        ``strict=False``, and a slot with no published content -- which is
        precisely how a **cancellation** often arrives -- has no
        ``ListeContenus`` at all. Dropping those entries meant the one event
        this integration exists to emit, ``event.<eleve>_cours_modifie`` with
        ``lesson_canceled``, could not fire for them.

        Everything derived from the content record (subject, teachers, rooms)
        is legitimately absent here. The slot's coordinates, its timing and its
        flags all live on the entry itself, and that is enough for
        de-duplication, for the delta and for the calendar.
        """
        identifier = entry.get("N")
        start = self._aware(_parse_datetime(_get(entry, "DateDuCours", "V")))
        if identifier is None or start is None:
            return None

        duration = int(entry.get("duree", 1) or 1)
        raw_end = self._aware(_parse_datetime(_get(entry, "DateDuCoursFin", "V")))
        end = raw_end if raw_end is not None else start + dt.timedelta(hours=duration)

        return Lesson(
            id=str(identifier),
            subject=None,
            subject_id=None,
            teachers=(),
            classrooms=(),
            groups=(),
            start=start,
            end=self._sane_end(start, end, identifier),
            canceled=bool(entry.get("estAnnule", False)),
            status=_get(entry, "Statut") or None,
            detention=bool(entry.get("estRetenue", False)),
            outing=bool(entry.get("estSortiePedagogique", False)),
            exempted=bool(entry.get("dispenseEleve", False)),
            test=False,
            memo=None,
            background_color=_get(entry, "CouleurFond") or None,
            virtual_classrooms=(),
            num=int(entry.get("P", 0) or 0),
            place=int(entry.get("place", 0) or 0),
            duration=duration,
            end_inferred=raw_end is None,
        )

    # -- homework ----------------------------------------------------------

    def homework(self, client: HardenedClient) -> GatewayResult[HomeworkFacts]:
        """Homework for the whole school year, in one request.

        Two things here were wrong and both were silent, so they are worth
        spelling out.

        **The span starts at the first day of the school year, not today.**
        Upstream filters its own response with ``if date_from <= hw.date <=
        date_to`` and builds the week domain from ``get_week(date_from)``, so
        passing ``self.today()`` discarded every *overdue* assignment. That made
        ``binary_sensor.<eleve>_devoirs_en_retard`` structurally incapable of
        ever being ``on``, and its ``count`` permanently ``0`` -- while the
        docstring claimed the opposite. Fixing it costs nothing: the request is
        a week *range*, so one post covers the year either way, and
        ``homework_horizon`` stays what §5.2 says it is, a presentation filter.

        **The decoding is by hand**, for the reason §3.3.3 gives.
        ``Homework.__init__`` resolves ``TAFFait``, ``descriptif.V`` and
        ``Matiere.V`` with no default, so a single entry served without one of
        them raised ``ParsingError`` from inside upstream's list comprehension
        and failed the **entire** tier -- taking the to-do list, the calendar
        and both homework sensors down with it.
        """
        payload = {
            "domaine": {
                "_T": 8,
                "V": f"[{self._first_week(client)}..{self._last_week(client)}]",
            }
        }
        raw = client.post(FUNC_HOMEWORK[0], FUNC_HOMEWORK[1], payload)
        entries = _required_list(
            raw, "dataSec", "data", "ListeTravauxAFaire", what="homework"
        )

        items = [
            item
            for item in (self._homework(entry) for entry in entries)
            if item is not None
        ]
        return GatewayResult(HomeworkFacts(homework=tuple(items)), calls=1)

    @staticmethod
    def _first_week(client: HardenedClient) -> int:
        """Week number of the first day of the school year.

        ``ClientBase.start_day`` comes from ``General.PremierLundi``, read once
        at login, and ``get_week`` is pure arithmetic on it -- no request.
        """
        return int(client.get_week(client.start_day))

    @staticmethod
    def _last_week(client: HardenedClient) -> int:
        """Week number of the last day of the school year.

        Mirrors what ``Client.homework`` does when ``date_to`` is omitted, but
        without inheriting its bare ``strptime``: a ``DerniereDate`` in an
        unexpected form would raise ``ValueError`` from the middle of the tier,
        and a bounded guess is a far better answer than no homework at all.
        """
        last = _parse_date(
            _get(client.func_options, "dataSec", "data", "General", "DerniereDate", "V")
        )
        if last is None:
            _LOGGER.debug("no usable DerniereDate; asking for a full 62-week year")
            return 62
        return int(client.get_week(last))

    def _homework(self, entry: dict[str, Any]) -> Homework | None:
        """Decode one homework item, tolerating any absent optional field."""
        identifier = entry.get("N")
        due = _parse_date(_get(entry, "PourLe", "V"))
        if identifier is None or due is None:
            return None

        description = _get(entry, "descriptif", "V")
        return Homework(
            id=str(identifier),
            subject=_get(entry, "Matiere", "V", "L") or None,
            description=str(description) if description else "",
            due=due,
            done=bool(entry.get("TAFFait", False)),
            background_color=_get(entry, "CouleurFond") or None,
            # Names only, never URLs. `Attachment.url` interpolates
            # `client.attributes["h"]` -- the live session token -- so keeping
            # it would write a credential-bearing URL into the snapshot, into
            # every recorder row and into the diagnostics download: the exact
            # hazard §8.2 removed the iCal URL from the state machine for. It
            # would also be dead by the time anyone clicked it, since the token
            # changes at the next login.
            attachments=tuple(
                name
                for name in (
                    _get(attachment, "L")
                    for attachment in _list(entry, "ListePieceJointe")
                )
                if name
            ),
        )

    # -- marks -------------------------------------------------------------

    def marks(
        self,
        client: HardenedClient,
        period: Period,
        *,
        with_report: bool,
    ) -> GatewayResult[MarksFacts]:
        """One ``DernieresNotes`` request, four datasets, plus the report.

        Upstream would spend four identical posts here: ``Period.grades``,
        ``.averages``, ``.overall_average`` and ``.class_overall_average`` each
        re-post ``DernieresNotes`` with the same body
        (dataClasses.py:525/534/548/578).
        """
        payload = {"Periode": {"N": period.id, "L": period.name}}
        raw = client.post(FUNC_MARKS[0], FUNC_MARKS[1], payload)
        data = _get(raw, "dataSec", "data") or {}
        calls = 1

        # Required, not optional. Upstream resolves both of these *strictly*
        # (dataClasses.py:526 and :535), which is upstream saying they are
        # always present -- so an absent key is a protocol change, and reporting
        # it as "no grades this term" would replace a good snapshot with a
        # believable lie: every subject average sensor would disappear, "last
        # grade" would go to None, and staleness would never fire because the
        # collection *succeeded*.
        grades = tuple(
            self._grade(entry)
            for entry in _required_list(data, "listeDevoirs", what="grades")
        )
        averages = tuple(
            self._average(entry)
            for entry in _required_list(data, "listeServices", what="subject averages")
        )

        report: Report | None = None
        if with_report:
            report, used = self._report(client, period)
            calls += used

        return GatewayResult(
            MarksFacts(
                period_id=period.id,
                period_index=period.index,
                grades=tuple(g for g in grades if g is not None),
                averages=tuple(a for a in averages if a is not None),
                overall_average=_number(_get(data, "moyGenerale", "V")),
                class_overall_average=_number(_get(data, "moyGeneraleClasse", "V")),
                report=report,
            ),
            calls=calls,
        )

    def _grade(self, entry: dict[str, Any]) -> Grade | None:
        """Decode one grade by hand -- the single documented exception (§3.3.2).

        Not for the sake of saving a call: for the two reasons in this module's
        docstring. ``value`` and ``status`` are mutually exclusive, which is
        what lets the "last grade" sensor hold a numeric state usable by a
        threshold trigger.
        """
        identifier = entry.get("N")
        if identifier is None:
            return None

        raw_value = _get(entry, "note", "V")
        status = _grade_sentinel(raw_value)
        value = None if status is not None else _number(raw_value)

        parsed_date = _parse_date(_get(entry, "date", "V"))
        if parsed_date is None:
            return None

        subject_name = _get(entry, "service", "V", "L")
        subject_id = _get(entry, "service", "V", "N")

        return Grade(
            id=str(identifier),
            subject=str(subject_name) if subject_name else None,
            subject_id=str(subject_id) if subject_id else None,
            value=value,
            status=status,
            out_of=_number(_get(entry, "bareme", "V")),
            default_out_of=_number(_get(entry, "baremeParDefaut", "V")),
            date=parsed_date,
            coefficient=_number(entry.get("coefficient")),
            class_average=_number(_get(entry, "moyenne", "V")),
            max_value=_number(_get(entry, "noteMax", "V")),
            min_value=_number(_get(entry, "noteMin", "V")),
            comment=entry.get("commentaire") or None,
            is_bonus=bool(entry.get("estBonus", False)),
            is_optional=bool(entry.get("estFacultatif", False))
            and not bool(entry.get("estBonus", False)),
            is_out_of_20=bool(entry.get("estRamenerSur20", False)),
        )

    def _average(self, entry: dict[str, Any]) -> Average | None:
        """Decode one per-subject average by hand.

        The second documented exception to "reuse upstream's decoder", and it
        is forced by a head-on conflict with §3.3.3. ``dataClasses.Average``
        resolves ``moyClasse``, ``moyMin`` and ``moyMax`` with neither a
        ``default`` nor ``strict=False``, so an establishment that does not
        publish class statistics -- which §3.3.3 names explicitly as a
        legitimate case -- made the constructor raise for **every single
        subject**. The result was not a degraded reading but no averages at
        all: ``MarksFacts.averages`` came back empty, every
        ``sensor.<eleve>_moyenne_<matiere>`` disappeared, and the only trace was
        one DEBUG line per subject.

        Keyed on the subject, because ``Average`` carries no identifier of its
        own and §2.4 rules out using a position in a list.
        """
        subject_name = _get(entry, "L")
        subject_id = entry.get("N")
        if not subject_name and subject_id is None:
            return None

        return Average(
            subject=str(subject_name) if subject_name else None,
            subject_id=str(subject_id) if subject_id is not None else None,
            student=_number(_get(entry, "moyEleve", "V")),
            class_average=_number(_get(entry, "moyClasse", "V")),
            min_average=_number(_get(entry, "moyMin", "V")),
            max_average=_number(_get(entry, "moyMax", "V")),
            out_of=_number(_get(entry, "baremeMoyEleve", "V")),
            background_color=_get(entry, "couleur") or None,
        )

    def _report(
        self, client: HardenedClient, period: Period
    ) -> tuple[Report | None, int]:
        """Fetch a report card, or ``None`` when it is not published."""
        payload = {"periode": {"G": 2, "N": period.id, "L": period.name}}
        raw = client.post(FUNC_REPORT[0], FUNC_REPORT[1], payload)
        data = _get(raw, "dataSec", "data") or {}
        if "Message" in data:
            # PRONOTE says so itself: not published yet, or unavailable.
            return None, 1

        if "ListeServices" not in data:
            # Both of `Report`'s resolvers carry `default=[]`, so a response
            # whose shape moved decoded happily into an *empty* report -- and
            # `sensor.<eleve>_bulletin` then said "published, zero subjects"
            # rather than "not published". Requiring the key keeps those two
            # very different answers apart.
            #
            # The capital L is upstream's, not a typo: `Report` resolves
            # `ListeServices` while `DernieresNotes` uses `listeServices`.
            # Checking the lower-case spelling here reported every published
            # report card as unpublished, which is what the test that asks for
            # one is now there to catch.
            _LOGGER.debug("report card response carries no ListeServices")
            return None, 1

        try:
            upstream = dataClasses.Report(data)
        except _ENTRY_ERRORS:
            _LOGGER.debug("skipping an undecodable report card", exc_info=True)
            return None, 1

        subjects = tuple(
            ReportSubject(
                id=str(subject.id),
                name=subject.name,
                color=subject.color,
                comments=tuple(subject.comments or ()),
                student_average=_number(subject.student_average),
                class_average=_number(subject.class_average),
                min_average=_number(subject.min_average),
                max_average=_number(subject.max_average),
                coefficient=_number(subject.coefficient),
                teachers=tuple(subject.teachers or ()),
            )
            for subject in upstream.subjects
        )
        return Report(subjects=subjects, comments=tuple(upstream.comments or ())), 1

    # -- attendance --------------------------------------------------------

    def attendance(
        self, client: HardenedClient, period: Period
    ) -> GatewayResult[AttendanceFacts]:
        """One ``PagePresence`` request, three datasets.

        ``Period.absences``, ``.delays`` and ``.punishments`` each re-post
        ``PagePresence`` and then read *the same* ``listeAbsences`` list,
        filtering on ``G`` being 13, 14 or 41 (dataClasses.py:606/621/636). One
        request, one filter, three tuples.
        """
        payload = {
            "periode": {"N": period.id, "L": period.name, "G": 2},
            "DateDebut": {
                "_T": 7,
                "V": period.start.strftime("%d/%m/%Y %H:%M:%S"),
            },
            "DateFin": {"_T": 7, "V": period.end.strftime("%d/%m/%Y %H:%M:%S")},
        }
        raw = client.post(FUNC_ATTENDANCE[0], FUNC_ATTENDANCE[1], payload)
        # Required for the same reason as the grades: "no absences" is the
        # answer a parent acts on, and it must mean the school said so.
        entries = _required_list(
            _get(raw, "dataSec", "data") or {}, "listeAbsences", what="attendance"
        )

        absences: list[Absence] = []
        delays: list[Delay] = []
        punishments: list[Punishment] = []

        for entry in entries:
            kind = _get(entry, "G")
            if kind == PRESENCE_KIND_ABSENCE:
                absence = self._absence(entry)
                if absence is not None:
                    absences.append(absence)
            elif kind == PRESENCE_KIND_DELAY:
                delay = self._delay(entry)
                if delay is not None:
                    delays.append(delay)
            elif kind == PRESENCE_KIND_PUNISHMENT:
                punishment = self._punishment(client, entry)
                if punishment is not None:
                    punishments.append(punishment)

        return GatewayResult(
            AttendanceFacts(
                period_id=period.id,
                absences=tuple(absences),
                delays=tuple(delays),
                punishments=tuple(punishments),
            ),
            calls=1,
        )

    def _absence(self, entry: dict[str, Any]) -> Absence | None:
        """Decode an absence. Note: ``hours``/``days``, never ``minutes``."""
        try:
            upstream = dataClasses.Absence(entry)
        except _ENTRY_ERRORS:
            _LOGGER.debug("skipping an undecodable absence", exc_info=True)
            return None

        return Absence(
            id=str(upstream.id),
            from_date=self._instant(upstream.from_date),
            to_date=self._instant(upstream.to_date),
            justified=bool(upstream.justified),
            hours=upstream.hours,
            days=int(upstream.days or 0),
            reasons=tuple(upstream.reasons or ()),
        )

    def _delay(self, entry: dict[str, Any]) -> Delay | None:
        """Decode a late arrival. ``minutes`` lives here, not on the absence."""
        try:
            upstream = dataClasses.Delay(entry)
        except _ENTRY_ERRORS:
            _LOGGER.debug("skipping an undecodable delay", exc_info=True)
            return None

        return Delay(
            id=str(upstream.id),
            at=self._instant(upstream.date),
            minutes=int(upstream.minutes or 0),
            justified=bool(upstream.justified),
            justification=upstream.justification,
            reasons=tuple(upstream.reasons or ()),
        )

    def _punishment(
        self, client: HardenedClient, entry: dict[str, Any]
    ) -> Punishment | None:
        """Decode a punishment and its scheduled slots.

        ``ScheduledPunishment.start`` is a ``date`` when the slot has no
        ``placeExecution`` and a ``datetime`` when it does, and ``place2time``
        raises ``DataError`` on an out-of-range slot -- so both are handled
        rather than assumed.
        """
        try:
            upstream = dataClasses.Punishment(client, entry)
        except _ENTRY_ERRORS:
            _LOGGER.debug("skipping an undecodable punishment", exc_info=True)
            return None

        slots: list[PunishmentSlot] = []
        for slot in upstream.schedule:
            start = self._instant(slot.start)
            duration = slot.duration
            slots.append(
                PunishmentSlot(
                    start=start,
                    duration_minutes=(
                        int(duration.total_seconds() // 60) if duration else 0
                    ),
                )
            )

        return Punishment(
            id=str(upstream.id),
            nature=upstream.nature,
            reasons=tuple(upstream.reasons or ()),
            giver=upstream.giver,
            given_at=self._aware(upstream.given),
            exclusion=bool(upstream.exclusion),
            during_lesson=bool(upstream.during_lesson),
            homework=upstream.homework,
            schedule=tuple(sorted(slots, key=lambda slot: slot.start)),
        )

    # -- evaluations -------------------------------------------------------

    def evaluations(
        self, client: HardenedClient, period: Period
    ) -> GatewayResult[EvaluationsFacts]:
        """Competency evaluations for one period, in one request."""
        payload = {"periode": {"N": period.id, "L": period.name, "G": 2}}
        raw = client.post(FUNC_EVALUATIONS[0], FUNC_EVALUATIONS[1], payload)
        entries = _required_list(
            _get(raw, "dataSec", "data") or {}, "listeEvaluations", what="assessments"
        )

        items: list[Evaluation] = []
        for entry in entries:
            evaluation = self._evaluation(entry)
            if evaluation is not None:
                items.append(evaluation)

        return GatewayResult(
            EvaluationsFacts(period_id=period.id, evaluations=tuple(items)),
            calls=1,
        )

    def _evaluation(self, entry: dict[str, Any]) -> Evaluation | None:
        """Decode one evaluation, reusing upstream's class."""
        try:
            upstream = dataClasses.Evaluation(entry)
        except _ENTRY_ERRORS:
            _LOGGER.debug("skipping an undecodable evaluation", exc_info=True)
            return None

        subject = upstream.subject
        return Evaluation(
            id=str(upstream.id),
            name=upstream.name,
            subject=subject.name if subject else None,
            subject_id=str(subject.id) if subject else None,
            teacher=upstream.teacher,
            description=upstream.description,
            date=upstream.date,
            acquisitions=tuple(
                Acquisition(
                    id=str(acquisition.id),
                    name=acquisition.name,
                    level=acquisition.level,
                    abbreviation=acquisition.abbreviation,
                    coefficient=_number(acquisition.coefficient),
                    domain=acquisition.domain,
                    pillar=acquisition.pillar,
                )
                for acquisition in upstream.acquisitions
            ),
        )

    # -- news --------------------------------------------------------------

    def news(self, client: HardenedClient) -> GatewayResult[NewsFacts]:
        """News items and surveys, in one request.

        ``PageActualites`` tab 8 -- the *read*. ``SaisieActualites`` on the same
        tab is the write used by ``mark_as_read``, and specification v1 had the
        two the wrong way round (§3.4).

        ``Information.content()`` is deliberately never touched: it is a lazy
        attribute that posts when read.

        Decoded by hand for the third time, and for the same reason as homework
        and averages: ``Information.__init__`` resolves ``auteur``, ``lue``,
        ``nature.V.L``, ``estSondage`` and ``reponseAnonyme`` strictly, and
        ``client.information_and_surveys()`` builds the whole list in a single
        comprehension. One item missing a category therefore emptied the news
        tier, took ``sensor.<eleve>_informations_non_lues`` with it, and left
        the school's actual announcement invisible.
        """
        payload = {"modesAffActus": {"_T": 26, "V": "[0..3]"}}
        raw = client.post(FUNC_NEWS[0], FUNC_NEWS[1], payload)
        groups = _required_list(raw, "dataSec", "data", "listeModesAff", what="news")

        items: list[Information] = []
        for group in groups:
            for entry in _list(group, "listeActualites"):
                item = self._information(entry)
                if item is not None:
                    items.append(item)
        return GatewayResult(NewsFacts(information=tuple(items)), calls=1)

    def _information(self, entry: dict[str, Any]) -> Information | None:
        """Decode one news item or survey, tolerating absent optional fields."""
        identifier = entry.get("N")
        created = self._aware(_parse_datetime(_get(entry, "dateCreation", "V")))
        if identifier is None or created is None:
            return None

        return Information(
            id=str(identifier),
            title=_get(entry, "L") or None,
            author=_get(entry, "auteur") or None,
            category=_get(entry, "nature", "V", "L") or None,
            read=bool(entry.get("lue", False)),
            survey=bool(entry.get("estSondage", False)),
            anonymous_response=bool(entry.get("reponseAnonyme", False)),
            created=created,
            start_date=self._aware(_parse_datetime(_get(entry, "dateDebut", "V"))),
            end_date=self._aware(_parse_datetime(_get(entry, "dateFin", "V"))),
        )

    # -- discussions -------------------------------------------------------

    def discussions(
        self,
        client: HardenedClient,
        *,
        previous_unread: dict[str, int] | None = None,
    ) -> GatewayResult[DiscussionsFacts]:
        """Discussion threads, expanding only the newly active ones.

        The list itself is one request. Message bodies are not: reading
        ``pronotepy.Discussion.messages`` posts ``ListeMessages`` **every time**,
        so expanding every thread would cost one request each -- ten threads on
        an hourly tier is ~176 requests a day, which roughly doubles the whole
        budget. The specification budgeted this tier at one request and did not
        anticipate that.

        So only threads whose unread count *went up* are expanded, capped at
        :data:`MAX_DISCUSSION_EXPANSIONS` per cycle. That is exactly what
        ``event.<student>_nouveau_message`` needs, and it normally costs zero or
        one extra request.
        """
        previous = previous_unread or {}
        threads = client.discussions()
        calls = 1

        # Drafts and Trash are filtered at the door: they are not conversations
        # anybody wants an automation on.
        visible = [
            thread
            for thread in threads
            if not ({"Drafts", "Trash"} & set(thread.labels or []))
        ]

        newly_active = [
            thread
            for thread in visible
            if int(thread.unread or 0) > previous.get(str(thread.id), 0)
        ]
        expandable = {
            str(thread.id) for thread in newly_active[:MAX_DISCUSSION_EXPANSIONS]
        }
        if len(newly_active) > MAX_DISCUSSION_EXPANSIONS:
            _LOGGER.debug(
                "%d newly active discussions; expanding %d this cycle to stay "
                "inside the token bucket",
                len(newly_active),
                MAX_DISCUSSION_EXPANSIONS,
            )

        items: list[Discussion] = []
        opened: set[str] = set()
        for thread in visible:
            messages: tuple[Message, ...] = ()
            if str(thread.id) in expandable:
                messages, used = self._messages(thread)
                calls += used
                opened.add(str(thread.id))
            items.append(
                Discussion(
                    id=str(thread.id),
                    subject=thread.subject or None,
                    creator=thread.creator,
                    unread=int(thread.unread or 0),
                    closed=bool(thread.closed),
                    labels=tuple(thread.labels or ()),
                    messages=messages,
                )
            )

        return GatewayResult(
            DiscussionsFacts(discussions=tuple(items), expanded=frozenset(opened)),
            calls=calls,
        )

    def _messages(
        self, thread: dataClasses.Discussion
    ) -> tuple[tuple[Message, ...], int]:
        """Expand one thread. Costs exactly one request."""
        try:
            upstream_messages = thread.messages
        except _ENTRY_ERRORS:
            _LOGGER.debug("could not expand a discussion thread", exc_info=True)
            return (), 1

        messages: list[Message] = []
        for message in upstream_messages:
            created = self._aware(message.created)
            if created is None:
                continue
            messages.append(
                Message(
                    id=str(message.id),
                    author=message.author,
                    created=created,
                    content=message.content or None,
                )
            )
        return tuple(messages), 1

    # -- menus -------------------------------------------------------------

    def menus(self, client: HardenedClient) -> GatewayResult[MenusFacts]:
        """Today's and tomorrow's menus.

        ``Client.menus()`` walks whole weeks, so this is one request except when
        tomorrow falls in the next week -- which is why annexe B budgets 1.14
        rather than 1.
        """
        today = self.today()
        tomorrow = today + dt.timedelta(days=1)
        calls = 1 if tomorrow.isocalendar()[1] == today.isocalendar()[1] else 2

        items = [
            Menu(
                id=str(upstream.id),
                day=upstream.date,
                name=upstream.name,
                is_lunch=bool(upstream.is_lunch),
                is_dinner=bool(upstream.is_dinner),
                first_meal=_food_names(upstream.first_meal),
                main_meal=_food_names(upstream.main_meal),
                side_meal=_food_names(upstream.side_meal),
                other_meal=_food_names(upstream.other_meal),
                cheese=_food_names(upstream.cheese),
                dessert=_food_names(upstream.dessert),
            )
            for upstream in client.menus(today, tomorrow)
        ]
        return GatewayResult(MenusFacts(menus=tuple(items)), calls=calls)

    # -- static ------------------------------------------------------------

    def static(self, client: HardenedClient) -> GatewayResult[StaticFacts]:
        """The teaching staff, and nothing else.

        One request. The iCal URL used to be collected here and is not any
        more: keeping it would drop an autonomous authentication bearer into the
        snapshot store, a long-lived structure whose purpose is to be dumped
        into a diagnostic report (§8.2).
        """
        members = tuple(
            TeachingStaffMember(
                name=member.name,
                role=member.type,
                subjects=tuple(subject.name for subject in member.subjects),
            )
            for member in client.get_teaching_staff()
        )
        return GatewayResult(StaticFacts(teaching_staff=members), calls=1)

    # -- on-demand reads, never stored ------------------------------------

    def ical_url(self, client: HardenedClient) -> tuple[str, int]:
        """Build the iCal URL on demand. One request, nothing stored.

        The caller must return this straight to the service response and drop
        it. Anyone holding this URL reads the student's timetable with no
        username and no password, and states go to the recorder, the backups,
        the screenshots and the bug reports (§8.2).
        """
        return client.export_ical(), 1

    def identity(self, client: HardenedClient) -> tuple[Identity, int]:
        """Read the full identity on demand. Never enters a state.

        ``ClientInfo._cache()`` posts through ``communication.post`` directly,
        bypassing ``ClientBase.post`` *and* the parent ``membre`` signature --
        which is why this has to be a gateway function called under the lock and
        after ``set_child``, and never a property read from an entity (§8.2).
        """
        info = client.info
        # Posted through `communication` with an explicit `ressource`, not
        # through `client.post`, which stamps the *account holder* as `membre`.
        # `ClientInfo._cache()` bypasses `ClientBase.post` for exactly this
        # reason, and says so: "we need to manually add the resource id". On a
        # parent account, `membre` returns the **parent's** birth date, e-mail,
        # telephone number and INE number -- which the service then handed back
        # attributed to the child. A wrong-attribution disclosure of precisely
        # the fields §8.2 keeps out of the state machine.
        raw = client.communication.post(
            FUNC_PERSONAL_INFO[0],
            {"Signature": {"onglet": 49, "ressource": {"N": info.id, "G": 4}}},
        )
        data = _get(raw, "dataSec", "data", "Informations") or {}

        guardians = tuple(
            Guardian(
                name=_get(entry, "L"),
                relation=_get(entry, "qualite", "V", "L"),
                email=_get(entry, "eMail"),
                phone=_get(entry, "telephonePortable"),
                address=_strings(
                    [
                        {"L": _get(entry, f"adresse{index}")}
                        for index in range(1, 5)
                        if _get(entry, f"adresse{index}")
                    ]
                ),
                is_legal=bool(_get(entry, "estResponsableLegal")),
            )
            for entry in _list(_get(raw, "dataSec", "data") or {}, "Responsables")
        )

        return (
            Identity(
                name=str(info.name),
                birth_date=_parse_date(_get(data, "dateNaiss", "V")),
                birth_place=_get(data, "villeNaiss"),
                email=_get(data, "eMail"),
                phone=_join_phone(
                    _get(data, "indicatifTel"), _get(data, "telephonePortable")
                ),
                address=_strings(
                    [
                        {"L": _get(data, f"adresse{index}")}
                        for index in range(1, 5)
                        if _get(data, f"adresse{index}")
                    ]
                ),
                ine_number=_get(data, "numeroINE"),
                guardians=guardians,
            ),
            1,
        )

    def profile_picture(self, client: HardenedClient) -> tuple[bytes | None, int]:
        """Fetch the profile photo under the lock, after ``set_child``.

        ``ClientInfo.profile_picture`` goes through ``ClientInfo._cache()``,
        which short-circuits ``ClientBase.post`` and the parent ``membre``
        signature -- so a parent account reading it as a property can show the
        wrong child's face (annexe A §5.4).
        """
        attachment = client.info.profile_picture
        if attachment is None:
            return None, 0
        data: bytes = attachment.data
        return data, 1

    def timetable_pdf_url(
        self,
        client: HardenedClient,
        day: dt.date | None,
        *,
        portrait: bool,
    ) -> tuple[str, int]:
        """Ask PRONOTE to render a timetable PDF and return its URL."""
        return client.generate_timetable_pdf(day=day, portrait=portrait), 1

    # -- writes ------------------------------------------------------------

    def set_homework_done(
        self, client: HardenedClient, homework_id: str, *, done: bool
    ) -> int:
        """Tick or untick one homework item.

        Posted directly rather than through ``Homework.set_done`` so no
        ``pronotepy.Homework`` has to be kept alive across the DTO boundary --
        an object read after its session closed raises ``Erreur.G = 22``
        (§3.1).
        """
        client.post(
            "SaisieTAFFaitEleve",
            88,
            {"listeTAF": [{"N": homework_id, "TAFFait": done}]},
        )
        return 1

    def reply_to_discussion(
        self, client: HardenedClient, discussion_id: str, content: str
    ) -> int:
        """Reply to one thread, identified by its ``N``.

        Three requests, not one. The thread has to be re-listed
        (``ListeMessagerie``) because a ``pronotepy.Discussion`` carries the
        ``listePossessionsMessages`` the reply needs and must **not** be kept
        alive across the DTO boundary -- read after its session closed it
        raises ``Erreur.G = 22`` (§3.1). ``Discussion.reply`` then posts
        ``ListeMessages`` to find the message being answered, and
        ``SaisieMessage`` to send.

        Billed honestly at three, because a write that under-reports its cost
        corrupts the very budget that protects the account (annexe B §8).
        """
        thread = next(
            (
                candidate
                for candidate in client.discussions()
                if str(candidate.id) == discussion_id
            ),
            None,
        )
        if thread is None:
            raise DiscussionNotFound(discussion_id)
        if thread.closed:
            raise DiscussionIsClosed(discussion_id)
        thread.reply(content)
        return 3

    def start_discussion(
        self,
        client: HardenedClient,
        subject: str,
        content: str,
        recipient_names: Sequence[str],
    ) -> int:
        """Open a new thread with the named recipients.

        Recipients are matched on the name PRONOTE publishes, because that is
        the only handle a user can read off the interface -- the ``N``
        identifier is not shown anywhere. An unmatched name is refused rather
        than silently dropped: a message that quietly went to nobody is worse
        than an error.
        """
        available = client.get_recipients()
        wanted = [name.strip().casefold() for name in recipient_names]
        chosen = [
            recipient
            for recipient in available
            if (recipient.name or "").strip().casefold() in wanted
        ]
        matched = {(recipient.name or "").strip().casefold() for recipient in chosen}
        missing = sorted(set(wanted) - matched)
        if missing:
            raise RecipientNotFound(
                missing, sorted((recipient.name or "") for recipient in available)
            )
        client.new_discussion(subject, content, chosen)
        return 3

    def mark_information_read(self, client: HardenedClient, information_id: str) -> int:
        """Mark one news item as read."""
        client.post(
            FUNC_NEWS_WRITE[0],
            FUNC_NEWS_WRITE[1],
            {
                "listeActualites": [
                    {
                        "N": information_id,
                        "validationDirecte": True,
                        "genrePublic": 4,
                        "public": {"N": client.info.id},
                        "lue": True,
                        "estUnSondage": False,
                    }
                ]
            },
        )
        return 1


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def deduplicate_lessons(lessons: Iterable[Lesson]) -> tuple[Lesson, ...]:
    """Keep one entry per slot: the one with the largest ``num``.

    This is step one of the two-step timetable rule, and it does **not** belong
    to the delta detector -- without it ``sensor.<student>_cours_du_jour``
    overcounts every day a lesson was changed, the calendar draws overlapping
    events, and "next lesson" can latch onto a superseded entry (§2.2.1).

    Upstream says it plainly: *"for the same lesson time, the biggest num is the
    one shown on pronote"*. ``Client.lessons()`` does not filter.
    """
    best: dict[tuple[str, int], Lesson] = {}
    for lesson in lessons:
        key = lesson.slot_key
        current = best.get(key)
        if current is None or _supersedes(lesson, current):
            best[key] = lesson
    return tuple(sorted(best.values(), key=lambda lesson: (lesson.start, lesson.place)))


def _supersedes(candidate: Lesson, current: Lesson) -> bool:
    """Whether ``candidate`` should replace ``current`` for the same slot.

    ``num`` decides, per upstream's rule. A **tie** needs an answer of its own,
    and response order is not one: ``num`` is the ``P`` field, which defaults to
    0 when absent, so two content-less entries on the same slot -- an outing and
    a detention, say -- both scored 0 and the second was dropped in silence,
    taking ``binary_sensor.<eleve>_sortie_pedagogique`` and one calendar event
    with it.

    On a tie the entry that is *not* cancelled wins, because a replacement is
    what PRONOTE displays; failing that, the one that names a subject, which is
    the more informative of two otherwise indistinguishable entries.
    """
    if candidate.num != current.num:
        return candidate.num > current.num
    if candidate.canceled != current.canceled:
        return current.canceled
    return current.subject is None and candidate.subject is not None


def _food_names(foods: Sequence[Any] | None) -> tuple[str, ...]:
    """Names of the dishes in one course, or an empty tuple."""
    if not foods:
        return ()
    return tuple(str(food.name) for food in foods if getattr(food, "name", None))


def _join_phone(prefix: Any, number: Any) -> str | None:
    """Assemble ``+<country><number>``, tolerating either part missing.

    Upstream returns ``"+" + indicatifTel + telephonePortable`` unguarded, so a
    guardian record with no country code -- which is common -- came out as
    ``"+600000000"``: a number that looks international and is not, and that no
    dialler will accept. With no prefix the number is handed back as it stands.
    """
    if not number:
        return None
    if not prefix:
        return str(number)
    return f"+{prefix}{number}"


def _parse_datetime(raw: Any) -> dt.datetime | None:
    """Parse a PRONOTE datetime string without raising.

    ``Util.datetime_parse`` raises ``DateParsingError`` on an unknown form, and
    a single unparseable field must not fail a whole tier (§3.3.3).
    """
    if raw is None:
        return None
    try:
        return dataClasses.Util.datetime_parse(str(raw))
    except Exception:  # noqa: BLE001 -- upstream raises several unrelated types
        _LOGGER.debug("could not parse datetime %r", raw)
        return None


def _parse_date(raw: Any) -> dt.date | None:
    """Parse a PRONOTE date string without raising.

    Worth knowing what is being tolerated: two of ``Util.date_parse``'s six
    accepted forms complete the missing part with ``date.today()``, read in the
    *host's* timezone. Those forms appear on short fields, and the values they
    produce are only ever used as calendar dates, never compared to an instant.
    """
    if raw is None:
        return None
    try:
        return dataClasses.Util.date_parse(str(raw))
    except Exception:  # noqa: BLE001 -- upstream raises several unrelated types
        _LOGGER.debug("could not parse date %r", raw)
        return None
