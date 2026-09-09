"""Per-tier collection: one place that maps a tier to its request and its DTO.

Kept apart from :mod:`.account` so the orchestration stays readable, and kept
out of :mod:`.gateway` so the gateway keeps its property: one public function per
*protocol call*, not one per displayed value (§3.3).

Each function returns the snapshot, what it cost, and the events its change
detector found. The cost is returned rather than inferred, which is what makes
the per-tier contract test of §11.1 possible -- an accidental property access
doubles the cost with nothing breaking, and that is the regression this project
exists to prevent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .const import TIER_PRIORITY, Priority, Tier
from .models import (
    AttendanceFacts,
    EvaluationsFacts,
    HistoryFacts,
    MarksFacts,
    Snapshot,
)

if TYPE_CHECKING:
    from .account import PronoteAccount
    from .delta import DeltaEvent
    from .hardened_client import HardenedClient
    from .models import Period


async def collect_tier(  # noqa: PLR0911 -- one arm per tier is the point
    account: PronoteAccount,
    tier: Tier,
    student_id: str,
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Collect one tier for one student and derive its events."""
    priority = TIER_PRIORITY[tier]
    if not account.has_data(tier, student_id):
        # A tier that has never produced a snapshot is publishing
        # `unavailable`, and §2.5 is explicit that an unavailable entity breaks
        # automations rather than merely looking empty. So its *first*
        # collection outranks quiet hours, which is the one dispensation
        # annexe B already grants: `CRITICAL` is the priority the login itself
        # uses, and the quiet-hours branch of `RateLimiter.admit` exempts it.
        #
        # Without this, an instance restarted at 23:00 -- or installed at
        # 23:00 -- showed nothing at all until 06:00 and looked broken. The
        # first collection is exactly the collection a user is watching for.
        #
        # It is self-limiting, which is what makes it safe to grant: the
        # moment the tier holds a snapshot this branch stops applying, so it
        # is one batch per tier and per child, not a standing exemption. A
        # tier that keeps *failing* stays data-less, but it does not spin --
        # `mark_failed` floors its retry at a quarter of its own interval, and
        # the limiter's backoff hold is checked before the quiet-hours branch
        # and so still applies to it.
        priority = Priority.CRITICAL

    match tier:
        case Tier.TIMETABLE:
            return await _timetable(account, student_id, priority)
        case Tier.HOMEWORK:
            return await _homework(account, student_id, priority)
        case Tier.NEWS:
            return await _news(account, student_id, priority)
        case Tier.DISCUSSIONS:
            return await _discussions(account, student_id, priority)
        case Tier.MARKS:
            return await _marks(account, student_id, priority)
        case Tier.ATTENDANCE:
            return await _attendance(account, student_id, priority)
        case Tier.EVALUATIONS:
            return await _evaluations(account, student_id, priority)
        case Tier.MENUS:
            return await _menus(account, student_id, priority)
        case Tier.STATIC:
            return await _static(account, student_id, priority)
        case Tier.HISTORY:
            return await _history(account, student_id, priority)
        case _:
            message = f"tier {tier} is not collectable"
            raise ValueError(message)


def _snapshot(
    account: PronoteAccount, tier: Tier, student_id: str, data: Any, calls: int
) -> Snapshot[Any]:
    """Wrap facts with the instant they were obtained."""
    return Snapshot(
        data=data,
        fetched_at=account.gateway.now(),
        tier=tier,
        calls=calls,
        student_id=student_id,
    )


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


async def _timetable(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """The current week, plus next week's request only at a boundary crossing.

    ``include_next_week`` is decided here rather than in the gateway because it
    is a scheduling question: tomorrow is only in another week on one day in
    seven, and that is exactly the 1.14 requests per batch annexe B budgets.
    """
    today = account.gateway.today()
    tomorrow = today + timedelta(days=1)
    include_next_week = tomorrow.isocalendar()[1] != today.isocalendar()[1]

    result = await account.session.run(
        str(Tier.TIMETABLE),
        priority,
        lambda client: account.gateway.timetable(
            client, include_next_week=include_next_week
        ),
        student_id=student_id,
        cost=2 if include_next_week else 1,
    )
    events = account.delta.timetable(student_id, result.facts)
    return (
        _snapshot(account, Tier.TIMETABLE, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------


async def _homework(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """The whole school year, in one request (§5.2)."""
    result = await account.session.run(
        str(Tier.HOMEWORK),
        priority,
        account.gateway.homework,
        student_id=student_id,
    )
    events = account.delta.homework(student_id, result.facts)
    return (
        _snapshot(account, Tier.HOMEWORK, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


async def _news(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """News items and surveys."""
    result = await account.session.run(
        str(Tier.NEWS),
        priority,
        account.gateway.news,
        student_id=student_id,
    )
    events = account.delta.news(student_id, result.facts)
    return (
        _snapshot(account, Tier.NEWS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Discussions
# ---------------------------------------------------------------------------


async def _discussions(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Thread list, expanding only the threads that became active.

    The previous unread counts are handed in so the gateway can decide which
    threads are worth a second request -- reading
    ``pronotepy.Discussion.messages`` costs one each, and doing it for every
    thread would roughly double the integration's whole budget.
    """
    previous = account.previous_unread(student_id)
    result = await account.session.run(
        str(Tier.DISCUSSIONS),
        priority,
        lambda client: account.gateway.discussions(client, previous_unread=previous),
        student_id=student_id,
    )
    events = account.delta.discussions(student_id, result.facts)
    # A thread the gateway did **not** expand keeps its previous count, so that
    # it is still "newly active" next cycle and gets expanded then. Recording
    # the new count for every thread meant a thread pushed past
    # `MAX_DISCUSSION_EXPANSIONS` was never eligible again: its counter never
    # rose from the stored value a second time, so every message it ever
    # received afterwards arrived with `author: null` and `created: null`.
    account.remember_unread(
        student_id,
        {
            thread.id: (
                thread.unread
                if thread.id in result.facts.expanded
                else previous.get(thread.id, 0)
            )
            for thread in result.facts.discussions
        },
    )
    return (
        _snapshot(account, Tier.DISCUSSIONS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


async def _marks(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """One ``DernieresNotes`` request plus the current period's report card.

    Nothing to collect when the current period cannot be determined: pronotepy
    would fall back to ``onglets[0]``, which silently names the wrong period in
    an establishment that does not publish grades, and a wrong average is worse
    than a missing one (§3.3.3).
    """
    period = account.state.current_period
    if period is None:
        empty = MarksFacts(
            period_id="",
            period_index=0,
            grades=(),
            averages=(),
            overall_average=None,
            class_overall_average=None,
            report=None,
        )
        return _snapshot(account, Tier.MARKS, student_id, empty, 0), 0, []

    result = await account.session.run(
        str(Tier.MARKS),
        priority,
        lambda client: account.gateway.marks(client, period, with_report=True),
        student_id=student_id,
        cost=2,
    )
    events = account.delta.marks(student_id, result.facts)
    return (
        _snapshot(account, Tier.MARKS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


async def _attendance(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """One ``PagePresence`` request, three datasets."""
    period = account.state.current_period
    if period is None:
        empty = AttendanceFacts(period_id="", absences=(), delays=(), punishments=())
        return _snapshot(account, Tier.ATTENDANCE, student_id, empty, 0), 0, []

    result = await account.session.run(
        str(Tier.ATTENDANCE),
        priority,
        lambda client: account.gateway.attendance(client, period),
        student_id=student_id,
    )
    events = account.delta.attendance(student_id, result.facts)
    return (
        _snapshot(account, Tier.ATTENDANCE, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


async def _evaluations(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Competency evaluations for the current period."""
    period = account.state.current_period
    if period is None:
        empty = EvaluationsFacts(period_id="", evaluations=())
        return _snapshot(account, Tier.EVALUATIONS, student_id, empty, 0), 0, []

    result = await account.session.run(
        str(Tier.EVALUATIONS),
        priority,
        lambda client: account.gateway.evaluations(client, period),
        student_id=student_id,
    )
    events = account.delta.evaluations(student_id, result.facts)
    return (
        _snapshot(account, Tier.EVALUATIONS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------


async def _menus(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Today's and tomorrow's canteen menus."""
    result = await account.session.run(
        str(Tier.MENUS),
        priority,
        account.gateway.menus,
        student_id=student_id,
    )
    return (
        _snapshot(account, Tier.MENUS, student_id, result.facts, result.calls),
        result.calls,
        [],
    )


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


async def _static(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """The teaching staff -- one request, and the iCal URL is *not* here.

    Collecting the iCal URL on a schedule would put an autonomous
    authentication bearer into the snapshot store, a long-lived structure whose
    purpose is to be dumped into a diagnostic report. ``get_ical_url`` fetches
    it on demand instead (§8.2).
    """
    result = await account.session.run(
        str(Tier.STATIC),
        priority,
        account.gateway.static,
        student_id=student_id,
    )
    return (
        _snapshot(account, Tier.STATIC, student_id, result.facts, result.calls),
        result.calls,
        [],
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


async def _history(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Closed periods, read once a day.

    A closed period cannot change, so re-reading it every three hours -- as the
    integration this replaces does -- spends requests on a constant result
    (§5.2). No events either: a closed period produces no news.
    """
    periods: tuple[Period, ...] = account.periods_for(Tier.HISTORY)
    limit = int(account.entry.options.get("history_periods", 0) or 0)
    if limit:
        periods = periods[-limit:]

    marks: list[MarksFacts] = []
    attendance: list[AttendanceFacts] = []
    evaluations: list[EvaluationsFacts] = []
    calls = 0

    for period in periods:
        marks_result = await account.session.run(
            str(Tier.HISTORY),
            priority,
            _bind_marks(account, period),
            student_id=student_id,
            cost=2,
        )
        marks.append(marks_result.facts)
        calls += marks_result.calls

        attendance_result = await account.session.run(
            str(Tier.HISTORY),
            priority,
            _bind_attendance(account, period),
            student_id=student_id,
        )
        attendance.append(attendance_result.facts)
        calls += attendance_result.calls

        evaluations_result = await account.session.run(
            str(Tier.HISTORY),
            priority,
            _bind_evaluations(account, period),
            student_id=student_id,
        )
        evaluations.append(evaluations_result.facts)
        calls += evaluations_result.calls

    facts = HistoryFacts(
        marks=tuple(marks),
        attendance=tuple(attendance),
        evaluations=tuple(evaluations),
    )
    return _snapshot(account, Tier.HISTORY, student_id, facts, calls), calls, []


def _bind_marks(account: PronoteAccount, period: Period) -> Any:
    """Bind a period into a marks call.

    Bound explicitly rather than captured in a loop closure: a lambda closing
    over the loop variable would read whichever period the loop finished on, and
    every closed period would come back with the last one's data.
    """

    def call(client: HardenedClient) -> Any:
        return account.gateway.marks(client, period, with_report=True)

    return call


def _bind_attendance(account: PronoteAccount, period: Period) -> Any:
    """Bind a period into an attendance call."""

    def call(client: HardenedClient) -> Any:
        return account.gateway.attendance(client, period)

    return call


def _bind_evaluations(account: PronoteAccount, period: Period) -> Any:
    """Bind a period into an evaluations call."""

    def call(client: HardenedClient) -> Any:
        return account.gateway.evaluations(client, period)

    return call
