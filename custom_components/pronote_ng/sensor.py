"""Sensors: one fact, one entity, one primitive state.

The rule of §2.1 is what this file exists to honour. Anything an automation may
want to trigger on has its own entity, whose **state** carries the fact -- a
timestamp, a number, a boolean -- and not a structure to walk. Full lists stay
available as attributes for cards; they are never the *only* route to a fact.

Two consequences show up throughout:

* a numeric sensor declares ``device_class``, ``state_class`` and a unit
  wherever they exist, so a ``numeric_state`` threshold works with no
  conversion;
* a state is an ISO-8601 aware timestamp or a number, never formatted text. A
  sensor reading ``8h30`` or ``14,5`` loses sorting, graphing, thresholds and the
  browser's locale in one stroke (§10.7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import callback

from .account import PronoteAccount
from .const import (
    DEFAULT_HOMEWORK_HORIZON,
    DEFAULT_WAKE_MARGIN,
    OPT_HOMEWORK_HORIZON,
    OPT_WAKE_MARGIN,
    LimiterState,
    Tier,
)
from .entity import ClockDrivenMixin, PronoteAccountEntity, PronoteEntity
from .models import HistoryFacts
from .options import bounded_option

if TYPE_CHECKING:
    from collections.abc import Sequence

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .coordinator import PronoteTierCoordinator
    from .models import (
        AttendanceFacts,
        DiscussionsFacts,
        EvaluationsFacts,
        Homework,
        HomeworkFacts,
        Lesson,
        MarksFacts,
        MenusFacts,
        NewsFacts,
        Period,
        SessionFacts,
        StaticFacts,
        Student,
        TimetableFacts,
    )

#: Nothing on this platform fetches. Entities read a snapshot the scheduler has
#: already published, so there is no update to serialise and no ceiling to set.
#: Zero states that, rather than leaving a reader to infer it from the absence
#: of an ``async_update``.
PARALLEL_UPDATES = 0

#: 30 seconds, which is the cadence these entities already ran at: with
#: ``_attr_should_poll`` set and no ``SCAN_INTERVAL``, Home Assistant applies
#: its own 30-second default. Declaring it changes no behaviour and records two
#: things -- that the cadence is deliberate, and that it is free. The polled
#: entities here are diagnostic readings of the limiter, the scheduler and the
#: session, all held in memory: polling them places no request, which is why a
#: sub-minute cadence is affordable on this platform and nowhere else.
SCAN_INTERVAL = timedelta(seconds=30)

type StateValue = str | int | float | datetime | date | None


@dataclass(frozen=True, kw_only=True)
class PronoteSensorDescription(SensorEntityDescription):
    """A sensor, its tier, and how to read its state out of the facts."""

    tier: Tier
    value_fn: Callable[[Any, PronoteAccount], StateValue]
    attributes_fn: Callable[[Any, PronoteAccount], dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Timetable helpers
# ---------------------------------------------------------------------------


def _lessons_on(facts: TimetableFacts, day: date) -> list[Lesson]:
    """Lessons of one day, already de-duplicated by the gateway."""
    return [lesson for lesson in facts.lessons if lesson.start.date() == day]


def _teaching_lessons(lessons: Sequence[Lesson]) -> list[Lesson]:
    """Lessons that actually put the child somewhere.

    Cancelled lessons are excluded, and so is anything the child is exempted
    from: both would otherwise drag a wake-up time earlier for a lesson nobody
    attends.
    """
    return [lesson for lesson in lessons if not lesson.canceled and not lesson.exempted]


def _next_lesson(facts: TimetableFacts, now: datetime) -> Lesson | None:
    """The next lesson that has not started yet."""
    upcoming = [
        lesson for lesson in _teaching_lessons(facts.lessons) if lesson.start > now
    ]
    return min(upcoming, key=lambda lesson: lesson.start) if upcoming else None


def _next_lesson_start(facts: TimetableFacts, account: PronoteAccount) -> StateValue:
    """Start of the next lesson."""
    lesson = _next_lesson(facts, account.gateway.now())
    return lesson.start if lesson else None


def _next_lesson_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Context of the next lesson."""
    lesson = _next_lesson(facts, account.gateway.now())
    if lesson is None:
        return {}
    return {
        "subject": lesson.subject,
        "teachers": list(lesson.teachers),
        "classroom": lesson.classroom,
        "end": lesson.end.isoformat(),
        "canceled": lesson.canceled,
        # Surfaced so a card can say "approximate": when `DateDuCoursFin` is
        # absent, pronotepy infers the end time through code whose own comment
        # says "might be wrong... works with demo" (§4.1).
        "end_inferred": lesson.end_inferred,
    }


def _end_of_day(facts: TimetableFacts, account: PronoteAccount) -> StateValue:
    """End of the last lesson of today."""
    today = _teaching_lessons(_lessons_on(facts, account.gateway.today()))
    return max((lesson.end for lesson in today), default=None)


def _end_of_day_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Which lesson closes the day, and whether its end was inferred."""
    today = _teaching_lessons(_lessons_on(facts, account.gateway.today()))
    if not today:
        return {}
    last = max(today, key=lambda lesson: lesson.end)
    return {"subject": last.subject, "end_inferred": last.end_inferred}


def _wake_target(
    facts: TimetableFacts, account: PronoteAccount
) -> tuple[datetime, Lesson] | None:
    """The lesson the wake-up sensor should aim at, and when to wake.

    Three rules, all from annexe A §1: cancelled lessons and lesson-free days
    are ignored, the sensor does not move on to tomorrow before today's lessons
    have finished, and the arithmetic happens in the establishment's timezone.
    """
    now = account.gateway.now()
    margin = timedelta(
        minutes=int(
            bounded_option(account.entry.options, OPT_WAKE_MARGIN, DEFAULT_WAKE_MARGIN)
        )
    )

    today = _teaching_lessons(_lessons_on(facts, now.date()))
    if today:
        last_end = max(lesson.end for lesson in today)
        if now < last_end:
            first = min(today, key=lambda lesson: lesson.start)
            return first.start - margin, first

    # Today is done (or empty): look forward to the next day that has lessons.
    future = _teaching_lessons(
        [lesson for lesson in facts.lessons if lesson.start > now]
    )
    if not future:
        return None
    next_day = min(lesson.start.date() for lesson in future)
    first_next = min(
        (lesson for lesson in future if lesson.start.date() == next_day),
        key=lambda lesson: lesson.start,
    )
    return first_next.start - margin, first_next


def _wake_time(facts: TimetableFacts, account: PronoteAccount) -> StateValue:
    """When to wake the child up."""
    target = _wake_target(facts, account)
    return target[0] if target else None


def _wake_attributes(facts: TimetableFacts, account: PronoteAccount) -> dict[str, Any]:
    """Which lesson the wake-up time was derived from."""
    target = _wake_target(facts, account)
    if target is None:
        return {}
    _, lesson = target
    return {
        "first_lesson": lesson.start.isoformat(),
        "subject": lesson.subject,
        "margin_minutes": int(
            bounded_option(account.entry.options, OPT_WAKE_MARGIN, DEFAULT_WAKE_MARGIN)
        ),
    }


def _next_test(facts: TimetableFacts, account: PronoteAccount) -> Lesson | None:
    """The next lesson flagged as a test."""
    upcoming = [
        lesson
        for lesson in _teaching_lessons(facts.lessons)
        if lesson.test and lesson.start > account.gateway.now()
    ]
    return min(upcoming, key=lambda lesson: lesson.start) if upcoming else None


def _next_test_start(facts: TimetableFacts, account: PronoteAccount) -> StateValue:
    """Timestamp of the next test.

    This entity is what makes "revise the evening before" writable. A binary
    ``controle_prevu`` only answers "today", so without a timestamp the most
    requested automation falls back to a template walking
    ``attributes.lessons`` -- exactly the failure §1 sets as its criterion.
    """
    lesson = _next_test(facts, account)
    return lesson.start if lesson else None


def _next_test_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Subject and room of the next test."""
    lesson = _next_test(facts, account)
    if lesson is None:
        return {}
    return {"subject": lesson.subject, "classroom": lesson.classroom}


def _lessons_today_count(facts: TimetableFacts, account: PronoteAccount) -> StateValue:
    """How many lessons today.

    Correct only because the gateway de-duplicated by slot: without that step
    this overcounts every day a lesson was changed, since PRONOTE returns the
    original *and* its replacement (§2.2.1).
    """
    return len(_lessons_on(facts, account.gateway.today()))


def _lessons_today_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Today's lessons, with the day's boundaries."""
    today = _lessons_on(facts, account.gateway.today())
    return {
        "lessons": [_lesson_dict(lesson) for lesson in today],
        "first_start": min((item.start for item in today), default=None),
        "last_end": max((item.end for item in today), default=None),
        "canceled_count": sum(1 for lesson in today if lesson.canceled),
    }


def _lesson_dict(lesson: Lesson) -> dict[str, Any]:
    """One lesson, flattened for a card."""
    return {
        "id": lesson.id,
        "subject": lesson.subject,
        "teachers": list(lesson.teachers),
        "classroom": lesson.classroom,
        "start": lesson.start.isoformat(),
        "end": lesson.end.isoformat(),
        "canceled": lesson.canceled,
        "status": lesson.status,
        "test": lesson.test,
        "outing": lesson.outing,
        "detention": lesson.detention,
        "exempted": lesson.exempted,
        "memo": lesson.memo,
        "end_inferred": lesson.end_inferred,
    }


def _lessons_tomorrow_count(
    facts: TimetableFacts, account: PronoteAccount
) -> StateValue:
    """How many lessons tomorrow."""
    return len(_lessons_on(facts, account.gateway.today() + timedelta(days=1)))


def _lessons_tomorrow_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Tomorrow's lessons."""
    day = account.gateway.today() + timedelta(days=1)
    return {"lessons": [_lesson_dict(lesson) for lesson in _lessons_on(facts, day)]}


def _lessons_week_count(facts: TimetableFacts, _account: PronoteAccount) -> StateValue:
    """How many lessons in the fetched window.

    Fresh at the timetable tier's cadence rather than six hours behind: folding
    the week tier into the timetable tier cost nothing, because
    ``Client.lessons()`` bills by the week either way (§5.2).
    """
    return len(facts.lessons)


def _lessons_week_attributes(
    facts: TimetableFacts, _account: PronoteAccount
) -> dict[str, Any]:
    """Every lesson in the fetched window."""
    return {
        "lessons": [_lesson_dict(lesson) for lesson in facts.lessons],
        "weeks": list(facts.weeks_fetched),
    }


# ---------------------------------------------------------------------------
# Homework helpers
# ---------------------------------------------------------------------------


def _horizon(account: PronoteAccount) -> date:
    """Last day the homework sensors display.

    A presentation filter and nothing more: ``Client.homework()`` requests a
    *week range* and defaults to the end of the school year, so the span asked
    for does not change the cost (§5.2).
    """
    days = int(
        bounded_option(
            account.entry.options, OPT_HOMEWORK_HORIZON, DEFAULT_HOMEWORK_HORIZON
        )
    )
    return account.gateway.today() + timedelta(days=days)


def _visible_homework(facts: HomeworkFacts, account: PronoteAccount) -> list[Homework]:
    """Homework inside the display horizon."""
    limit = _horizon(account)
    return [item for item in facts.homework if item.due <= limit]


def _homework_dict(item: Homework) -> dict[str, Any]:
    """One homework item, flattened for a card."""
    return {
        "id": item.id,
        "subject": item.subject,
        "description": item.description,
        "due": item.due.isoformat(),
        "done": item.done,
        "attachments": list(item.attachments),
    }


def _homework_todo_count(facts: HomeworkFacts, account: PronoteAccount) -> StateValue:
    """How many homework items are still to do."""
    return sum(1 for item in _visible_homework(facts, account) if not item.done)


def _homework_todo_attributes(
    facts: HomeworkFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Outstanding homework, and the next deadline."""
    pending = [item for item in _visible_homework(facts, account) if not item.done]
    return {
        "items": [_homework_dict(item) for item in pending],
        "next_due": min((item.due for item in pending), default=None),
    }


def _homework_tomorrow_count(
    facts: HomeworkFacts, account: PronoteAccount
) -> StateValue:
    """How many homework items are due tomorrow."""
    tomorrow = account.gateway.today() + timedelta(days=1)
    return sum(1 for item in facts.homework if item.due == tomorrow)


def _homework_tomorrow_attributes(
    facts: HomeworkFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Homework due tomorrow."""
    tomorrow = account.gateway.today() + timedelta(days=1)
    return {
        "items": [
            _homework_dict(item) for item in facts.homework if item.due == tomorrow
        ]
    }


def _homework_all_count(facts: HomeworkFacts, account: PronoteAccount) -> StateValue:
    """How many homework items inside the horizon."""
    return len(_visible_homework(facts, account))


def _homework_all_attributes(
    facts: HomeworkFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Every homework item inside the horizon."""
    return {
        "items": [_homework_dict(item) for item in _visible_homework(facts, account)]
    }


# ---------------------------------------------------------------------------
# Marks helpers
# ---------------------------------------------------------------------------


def _latest_grade_value(facts: MarksFacts, _account: PronoteAccount) -> StateValue:
    """The most recent grade, as a **number**.

    ``None`` -- so the state reads ``unknown`` -- when the most recent grade is
    a sentinel, with the reason in the ``status`` attribute. A state that were
    sometimes ``14.5`` and sometimes ``Absent`` would be usable neither by a
    threshold nor by a graph (§4.3).
    """
    if not facts.grades:
        return None
    latest = max(facts.grades, key=lambda grade: grade.date)
    return latest.value


def _latest_grade_attributes(
    facts: MarksFacts, _account: PronoteAccount
) -> dict[str, Any]:
    """Context of the most recent grade, including a sentinel's reason."""
    if not facts.grades:
        return {}
    latest = max(facts.grades, key=lambda grade: grade.date)
    return {
        "subject": latest.subject,
        "out_of": latest.out_of,
        "coefficient": latest.coefficient,
        "date": latest.date.isoformat(),
        "class_average": latest.class_average,
        "status": str(latest.status) if latest.status else None,
    }


def _grade_dict(grade: Any) -> dict[str, Any]:
    """One grade, flattened for a card."""
    return {
        "id": grade.id,
        "subject": grade.subject,
        "value": grade.value,
        "status": str(grade.status) if grade.status else None,
        "out_of": grade.out_of,
        "coefficient": grade.coefficient,
        "date": grade.date.isoformat(),
        "class_average": grade.class_average,
        "min": grade.min_value,
        "max": grade.max_value,
        "comment": grade.comment,
        "is_bonus": grade.is_bonus,
        "is_optional": grade.is_optional,
    }


def _average_dict(average: Any) -> dict[str, Any]:
    """One per-subject average, keyed by subject rather than by rank.

    ``pronotepy.Average`` carries no identifier, and §2.4 forbids using a
    position in a list as a key.
    """
    return {
        "subject_id": average.subject_id,
        "subject": average.subject,
        "student": average.student,
        "class_average": average.class_average,
        "min": average.min_average,
        "max": average.max_average,
        "out_of": average.out_of,
    }


def _overall_average(facts: MarksFacts, _account: PronoteAccount) -> StateValue:
    """The student's overall average."""
    return facts.overall_average


def _class_average(facts: MarksFacts, _account: PronoteAccount) -> StateValue:
    """The class's overall average."""
    return facts.class_overall_average


def _period_attributes(facts: MarksFacts, account: PronoteAccount) -> dict[str, Any]:
    """Which period an average belongs to."""
    period = next(
        (item for item in account.state.periods if item.id == facts.period_id), None
    )
    return {"period": period.name if period else None, "out_of": 20}


def _grades_count(facts: MarksFacts, _account: PronoteAccount) -> StateValue:
    """How many grades in the period."""
    return len(facts.grades)


def _grades_attributes(facts: MarksFacts, _account: PronoteAccount) -> dict[str, Any]:
    """Every grade in the period."""
    return {"items": [_grade_dict(grade) for grade in facts.grades]}


def _averages_count(facts: MarksFacts, _account: PronoteAccount) -> StateValue:
    """How many subjects have an average."""
    return len(facts.averages)


def _averages_attributes(facts: MarksFacts, _account: PronoteAccount) -> dict[str, Any]:
    """Every per-subject average."""
    return {"items": [_average_dict(average) for average in facts.averages]}


def _report_count(facts: MarksFacts, _account: PronoteAccount) -> StateValue:
    """How many subjects on the report card, or ``None`` if unpublished."""
    return len(facts.report.subjects) if facts.report else None


def _report_attributes(facts: MarksFacts, account: PronoteAccount) -> dict[str, Any]:
    """The report card's subjects and comments."""
    if facts.report is None:
        return {}
    period = next(
        (item for item in account.state.periods if item.id == facts.period_id), None
    )
    return {
        "subjects": [
            {
                "id": subject.id,
                "name": subject.name,
                "student_average": subject.student_average,
                "class_average": subject.class_average,
                "coefficient": subject.coefficient,
                "comments": list(subject.comments),
                "teachers": list(subject.teachers),
            }
            for subject in facts.report.subjects
        ],
        "comments": list(facts.report.comments),
        "period": period.name if period else None,
    }


# ---------------------------------------------------------------------------
# Attendance helpers
# ---------------------------------------------------------------------------


def _unjustified_absences(
    facts: AttendanceFacts, _account: PronoteAccount
) -> StateValue:
    """How many absences are still unjustified."""
    return sum(1 for absence in facts.absences if not absence.justified)


def _unjustified_absences_attributes(
    facts: AttendanceFacts, _account: PronoteAccount
) -> dict[str, Any]:
    """The unjustified absences."""
    return {
        "items": [
            _absence_dict(absence)
            for absence in facts.absences
            if not absence.justified
        ]
    }


def _absence_dict(absence: Any) -> dict[str, Any]:
    """One absence, flattened. ``hours``/``days``, never ``minutes``."""
    return {
        "id": absence.id,
        "from_date": absence.from_date.isoformat(),
        "to_date": absence.to_date.isoformat(),
        "justified": absence.justified,
        "hours": absence.hours,
        "days": absence.days,
        "reasons": list(absence.reasons),
    }


def _delay_dict(delay: Any) -> dict[str, Any]:
    """One late arrival, flattened. ``minutes`` lives here."""
    return {
        "id": delay.id,
        "date": delay.at.isoformat(),
        "minutes": delay.minutes,
        "justified": delay.justified,
        "justification": delay.justification,
        "reasons": list(delay.reasons),
    }


def _punishment_dict(punishment: Any) -> dict[str, Any]:
    """One punishment, flattened, with its scheduled slots."""
    return {
        "id": punishment.id,
        "nature": punishment.nature,
        "reasons": list(punishment.reasons),
        "giver": punishment.giver,
        "exclusion": punishment.exclusion,
        "schedule": [
            {
                "start": slot.start.isoformat(),
                "duration_minutes": slot.duration_minutes,
            }
            for slot in punishment.schedule
        ],
    }


def _absences_count(facts: AttendanceFacts, _a: PronoteAccount) -> StateValue:
    """How many absences in the period."""
    return len(facts.absences)


def _absences_attributes(facts: AttendanceFacts, _a: PronoteAccount) -> dict[str, Any]:
    """Every absence in the period."""
    return {"items": [_absence_dict(item) for item in facts.absences]}


def _delays_count(facts: AttendanceFacts, _a: PronoteAccount) -> StateValue:
    """How many late arrivals in the period."""
    return len(facts.delays)


def _delays_attributes(facts: AttendanceFacts, _a: PronoteAccount) -> dict[str, Any]:
    """Every late arrival in the period."""
    return {"items": [_delay_dict(item) for item in facts.delays]}


def _punishments_count(facts: AttendanceFacts, _a: PronoteAccount) -> StateValue:
    """How many punishments in the period."""
    return len(facts.punishments)


def _punishments_attributes(
    facts: AttendanceFacts, _a: PronoteAccount
) -> dict[str, Any]:
    """Every punishment in the period."""
    return {"items": [_punishment_dict(item) for item in facts.punishments]}


def _next_punishment_slot(
    facts: AttendanceFacts, account: PronoteAccount
) -> StateValue:
    """When the next detention starts.

    Somebody has to get the child there, which is precisely why this is a
    timestamp and not a flag.
    """
    now = account.gateway.now()
    slots = [
        slot
        for punishment in facts.punishments
        for slot in punishment.schedule
        if slot.start > now
    ]
    return min((slot.start for slot in slots), default=None)


def _next_punishment_attributes(
    facts: AttendanceFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Nature and duration of the next detention."""
    now = account.gateway.now()
    candidates = [
        (slot, punishment)
        for punishment in facts.punishments
        for slot in punishment.schedule
        if slot.start > now
    ]
    if not candidates:
        return {}
    slot, punishment = min(candidates, key=lambda pair: pair[0].start)
    return {
        "nature": punishment.nature,
        "duration": slot.duration_minutes,
        "giver": punishment.giver,
    }


# ---------------------------------------------------------------------------
# News, discussions, menus, static, session
# ---------------------------------------------------------------------------


def _unread_news(facts: NewsFacts, _a: PronoteAccount) -> StateValue:
    """How many news items are unread."""
    return sum(1 for item in facts.information if not item.read)


def _unread_news_attributes(facts: NewsFacts, _a: PronoteAccount) -> dict[str, Any]:
    """The unread news items."""
    return {
        "items": [
            _information_dict(item) for item in facts.information if not item.read
        ]
    }


def _information_dict(item: Any) -> dict[str, Any]:
    """One news item, flattened. Content is deliberately absent.

    ``Information.content()`` is a lazy attribute that posts when read, so it
    never crosses the gateway (§3.1).
    """
    return {
        "id": item.id,
        "title": item.title,
        "author": item.author,
        "category": item.category,
        "read": item.read,
        "survey": item.survey,
        "created": item.created.isoformat(),
    }


def _news_count(facts: NewsFacts, _a: PronoteAccount) -> StateValue:
    """How many news items."""
    return len(facts.information)


def _news_attributes(facts: NewsFacts, _a: PronoteAccount) -> dict[str, Any]:
    """Every news item."""
    return {"items": [_information_dict(item) for item in facts.information]}


def _unread_messages(facts: DiscussionsFacts, _a: PronoteAccount) -> StateValue:
    """Total unread messages -- a **sum**, not a thread count.

    ``Discussion.unread`` is an integer (``nbNonLus``), not a flag. Counting
    threads with at least one unread message would give a different number and
    the sensor would contradict its own ``items`` attribute (annexe A §1).
    """
    return sum(thread.unread for thread in facts.discussions)


def _unread_messages_attributes(
    facts: DiscussionsFacts, _a: PronoteAccount
) -> dict[str, Any]:
    """The threads holding unread messages."""
    return {
        "items": [
            _discussion_dict(thread) for thread in facts.discussions if thread.unread
        ]
    }


def _discussion_dict(thread: Any) -> dict[str, Any]:
    """One thread, flattened."""
    return {
        "id": thread.id,
        "subject": thread.subject,
        "creator": thread.creator,
        "unread": thread.unread,
        "closed": thread.closed,
        "messages": [
            {
                "id": message.id,
                "author": message.author,
                "created": message.created.isoformat(),
            }
            for message in thread.messages
        ],
    }


def _discussions_count(facts: DiscussionsFacts, _a: PronoteAccount) -> StateValue:
    """How many threads."""
    return len(facts.discussions)


def _discussions_attributes(
    facts: DiscussionsFacts, _a: PronoteAccount
) -> dict[str, Any]:
    """Every thread."""
    return {"items": [_discussion_dict(thread) for thread in facts.discussions]}


def _menu_for(facts: MenusFacts, day: date) -> Any | None:
    """The lunch menu for one day, falling back to any meal that day."""
    same_day = [menu for menu in facts.menus if menu.day == day]
    lunch = next((menu for menu in same_day if menu.is_lunch), None)
    return lunch or (same_day[0] if same_day else None)


def _menu_today_count(facts: MenusFacts, account: PronoteAccount) -> StateValue:
    """How many dishes on today's menu."""
    menu = _menu_for(facts, account.gateway.today())
    return menu.dish_count if menu else None


def _menu_attributes_for(menu: Any | None) -> dict[str, Any]:
    """One menu's courses."""
    if menu is None:
        return {}
    return {
        "first_meal": list(menu.first_meal),
        "main_meal": list(menu.main_meal),
        "side_meal": list(menu.side_meal),
        "other_meal": list(menu.other_meal),
        "cheese": list(menu.cheese),
        "dessert": list(menu.dessert),
        "is_lunch": menu.is_lunch,
    }


def _menu_today_attributes(
    facts: MenusFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Today's menu."""
    return _menu_attributes_for(_menu_for(facts, account.gateway.today()))


def _menu_tomorrow_count(facts: MenusFacts, account: PronoteAccount) -> StateValue:
    """How many dishes on tomorrow's menu."""
    menu = _menu_for(facts, account.gateway.today() + timedelta(days=1))
    return menu.dish_count if menu else None


def _menu_tomorrow_attributes(
    facts: MenusFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Tomorrow's menu."""
    return _menu_attributes_for(
        _menu_for(facts, account.gateway.today() + timedelta(days=1))
    )


def _staff_count(facts: StaticFacts, _a: PronoteAccount) -> StateValue:
    """How many members of teaching staff."""
    return len(facts.teaching_staff)


def _staff_attributes(facts: StaticFacts, _a: PronoteAccount) -> dict[str, Any]:
    """The teaching staff."""
    return {
        "items": [
            {
                "name": member.name,
                "role": member.role,
                "subjects": list(member.subjects),
            }
            for member in facts.teaching_staff
        ]
    }


def _evaluations_count(facts: EvaluationsFacts, _a: PronoteAccount) -> StateValue:
    """How many competency evaluations."""
    return len(facts.evaluations)


def _evaluations_attributes(
    facts: EvaluationsFacts, _a: PronoteAccount
) -> dict[str, Any]:
    """Every competency evaluation."""
    return {
        "items": [
            {
                "id": evaluation.id,
                "name": evaluation.name,
                "subject": evaluation.subject,
                "date": evaluation.date.isoformat(),
                "acquisitions": [
                    {
                        "name": acquisition.name,
                        "level": acquisition.level,
                        "abbreviation": acquisition.abbreviation,
                        "domain": acquisition.domain,
                    }
                    for acquisition in evaluation.acquisitions
                ],
            }
            for evaluation in facts.evaluations
        ]
    }


def _current_period_name(facts: SessionFacts, _a: PronoteAccount) -> StateValue:
    """The current period's name, or ``None``.

    ``None`` -- so the entity goes unavailable -- rather than wrong.
    ``Client.current_period`` falls back to ``onglets[0]`` when tab 198 is
    absent, which silently names the wrong period in an establishment that does
    not publish grades (annexe A §1).
    """
    return facts.current_period.name if facts.current_period else None


def _current_period_attributes(
    facts: SessionFacts, _a: PronoteAccount
) -> dict[str, Any]:
    """The current period's bounds and index."""
    period = facts.current_period
    if period is None:
        return {}
    return {
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "index": period.index,
    }


def _class_name(facts: SessionFacts, _a: PronoteAccount) -> StateValue:
    """The child's class."""
    return facts.student.class_name


def _class_attributes(facts: SessionFacts, _a: PronoteAccount) -> dict[str, Any]:
    """The establishment the class belongs to."""
    return {"establishment": facts.student.establishment}


def _periods_count(facts: SessionFacts, _a: PronoteAccount) -> StateValue:
    """How many periods in the school year."""
    return len(facts.periods)


def _periods_attributes(facts: SessionFacts, _a: PronoteAccount) -> dict[str, Any]:
    """Every period, and which one is current."""
    return {
        "items": [
            {
                "index": period.index,
                "name": period.name,
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
            }
            for period in facts.periods
        ],
        "current_index": (facts.current_period.index if facts.current_period else None),
    }


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------

PRIMITIVE_SENSORS: Final[tuple[PronoteSensorDescription, ...]] = (
    PronoteSensorDescription(
        key="next_lesson",
        tier=Tier.TIMETABLE,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_lesson_start,
        attributes_fn=_next_lesson_attributes,
    ),
    PronoteSensorDescription(
        key="end_of_lessons",
        tier=Tier.TIMETABLE,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_end_of_day,
        attributes_fn=_end_of_day_attributes,
    ),
    PronoteSensorDescription(
        key="next_wake_up",
        tier=Tier.TIMETABLE,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_wake_time,
        attributes_fn=_wake_attributes,
    ),
    PronoteSensorDescription(
        key="next_test",
        tier=Tier.TIMETABLE,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_test_start,
        attributes_fn=_next_test_attributes,
    ),
    PronoteSensorDescription(
        key="lessons_today",
        tier=Tier.TIMETABLE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_lessons_today_count,
        attributes_fn=_lessons_today_attributes,
    ),
    PronoteSensorDescription(
        key="homework_todo",
        tier=Tier.HOMEWORK,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_homework_todo_count,
        attributes_fn=_homework_todo_attributes,
    ),
    PronoteSensorDescription(
        key="homework_tomorrow",
        tier=Tier.HOMEWORK,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_homework_tomorrow_count,
        attributes_fn=_homework_tomorrow_attributes,
    ),
    PronoteSensorDescription(
        key="latest_grade",
        tier=Tier.MARKS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_latest_grade_value,
        attributes_fn=_latest_grade_attributes,
    ),
    PronoteSensorDescription(
        key="overall_average",
        tier=Tier.MARKS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_overall_average,
        attributes_fn=_period_attributes,
    ),
    PronoteSensorDescription(
        key="class_average",
        tier=Tier.MARKS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_class_average,
        attributes_fn=_period_attributes,
    ),
    PronoteSensorDescription(
        key="next_punishment",
        tier=Tier.ATTENDANCE,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_punishment_slot,
        attributes_fn=_next_punishment_attributes,
    ),
    PronoteSensorDescription(
        key="unjustified_absences",
        tier=Tier.ATTENDANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_unjustified_absences,
        attributes_fn=_unjustified_absences_attributes,
    ),
    PronoteSensorDescription(
        key="unread_information",
        tier=Tier.NEWS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_unread_news,
        attributes_fn=_unread_news_attributes,
    ),
    PronoteSensorDescription(
        key="unread_messages",
        tier=Tier.DISCUSSIONS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_unread_messages,
        attributes_fn=_unread_messages_attributes,
    ),
    PronoteSensorDescription(
        key="current_period",
        tier=Tier.SESSION,
        value_fn=_current_period_name,
        attributes_fn=_current_period_attributes,
    ),
)

LIST_SENSORS: Final[tuple[PronoteSensorDescription, ...]] = (
    PronoteSensorDescription(
        key="timetable_tomorrow",
        tier=Tier.TIMETABLE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_lessons_tomorrow_count,
        attributes_fn=_lessons_tomorrow_attributes,
    ),
    PronoteSensorDescription(
        key="timetable_week",
        tier=Tier.TIMETABLE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_lessons_week_count,
        attributes_fn=_lessons_week_attributes,
    ),
    PronoteSensorDescription(
        key="homework",
        tier=Tier.HOMEWORK,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_homework_all_count,
        attributes_fn=_homework_all_attributes,
    ),
    PronoteSensorDescription(
        key="grades",
        tier=Tier.MARKS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_grades_count,
        attributes_fn=_grades_attributes,
    ),
    PronoteSensorDescription(
        key="averages",
        tier=Tier.MARKS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_averages_count,
        attributes_fn=_averages_attributes,
    ),
    PronoteSensorDescription(
        key="report_card",
        tier=Tier.MARKS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_report_count,
        attributes_fn=_report_attributes,
    ),
    PronoteSensorDescription(
        key="absences",
        tier=Tier.ATTENDANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_absences_count,
        attributes_fn=_absences_attributes,
    ),
    PronoteSensorDescription(
        key="delays",
        tier=Tier.ATTENDANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_delays_count,
        attributes_fn=_delays_attributes,
    ),
    PronoteSensorDescription(
        key="punishments",
        tier=Tier.ATTENDANCE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_punishments_count,
        attributes_fn=_punishments_attributes,
    ),
    PronoteSensorDescription(
        key="evaluations",
        tier=Tier.EVALUATIONS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_evaluations_count,
        attributes_fn=_evaluations_attributes,
    ),
    PronoteSensorDescription(
        key="information",
        tier=Tier.NEWS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_news_count,
        attributes_fn=_news_attributes,
    ),
    PronoteSensorDescription(
        key="discussions",
        tier=Tier.DISCUSSIONS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_discussions_count,
        attributes_fn=_discussions_attributes,
    ),
    PronoteSensorDescription(
        key="menu_today",
        tier=Tier.MENUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_menu_today_count,
        attributes_fn=_menu_today_attributes,
    ),
    PronoteSensorDescription(
        key="menu_tomorrow",
        tier=Tier.MENUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_menu_tomorrow_count,
        attributes_fn=_menu_tomorrow_attributes,
    ),
    PronoteSensorDescription(
        key="teaching_staff",
        tier=Tier.STATIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_staff_count,
        attributes_fn=_staff_attributes,
    ),
    PronoteSensorDescription(
        key="class_name",
        tier=Tier.SESSION,
        value_fn=_class_name,
        attributes_fn=_class_attributes,
    ),
    PronoteSensorDescription(
        key="periods",
        tier=Tier.SESSION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_periods_count,
        attributes_fn=_periods_attributes,
    ),
)

#: Entities whose state depends on the *clock* and not only on the data. They
#: are re-evaluated at their next known transition rather than at their tier's
#: pace: fifteen minutes late on a wake-up sensor removes its whole purpose
#: (annexe A §3.1).
CLOCK_DRIVEN_KEYS: Final = frozenset(
    {
        "next_lesson",
        "end_of_lessons",
        "next_wake_up",
        "next_test",
        "next_punishment",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create every sensor for every child, plus the account diagnostics."""
    account = entry.runtime_data
    entities: list[SensorEntity] = []

    for student in account.students:
        for description in (*PRIMITIVE_SENSORS, *LIST_SENSORS):
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None:
                continue
            cls = (
                PronoteClockSensor
                if description.key in CLOCK_DRIVEN_KEYS
                else PronoteSensor
            )
            entities.append(cls(account, coordinator, student, description))

        entities.extend(_history_sensors(account, student))

    entities.extend(_diagnostic_sensors(account))
    async_add_entities(entities)


def _history_sensors(account: PronoteAccount, student: Student) -> list[SensorEntity]:
    """One set of sensors per closed period being followed.

    Suffixed by the period's **index** rather than its name: an establishment
    renaming "Trimestre 1" to "Semestre 1" must not rename anybody's entities
    (§2.4). The *displayed* name carries the label through a placeholder, so it
    stays readable.
    """
    coordinator = account.coordinators.get(Tier.HISTORY)
    if coordinator is None:
        return []

    entities: list[SensorEntity] = []
    for period in account.periods_for(Tier.HISTORY):
        for key, extract in _HISTORY_EXTRACTORS.items():
            entities.append(
                PronoteHistorySensor(
                    account,
                    coordinator,
                    student,
                    period=period,
                    key=key,
                    extract=extract,
                )
            )
    return entities


def _history_grades(facts: HistoryFacts, period_id: str) -> tuple[Any, dict[str, Any]]:
    """Grades of one closed period."""
    marks = next((item for item in facts.marks if item.period_id == period_id), None)
    if marks is None:
        return None, {}
    return len(marks.grades), {"items": [_grade_dict(grade) for grade in marks.grades]}


def _history_averages(
    facts: HistoryFacts, period_id: str
) -> tuple[Any, dict[str, Any]]:
    """Per-subject averages of one closed period."""
    marks = next((item for item in facts.marks if item.period_id == period_id), None)
    if marks is None:
        return None, {}
    return len(marks.averages), {
        "items": [_average_dict(average) for average in marks.averages]
    }


def _history_overall(facts: HistoryFacts, period_id: str) -> tuple[Any, dict[str, Any]]:
    """Overall average of one closed period."""
    marks = next((item for item in facts.marks if item.period_id == period_id), None)
    if marks is None:
        return None, {}
    return marks.overall_average, {"class_average": marks.class_overall_average}


def _history_report(facts: HistoryFacts, period_id: str) -> tuple[Any, dict[str, Any]]:
    """Report card of one closed period."""
    marks = next((item for item in facts.marks if item.period_id == period_id), None)
    if marks is None or marks.report is None:
        return None, {}
    return len(marks.report.subjects), {
        "subjects": [
            {
                "name": subject.name,
                "student_average": subject.student_average,
                "class_average": subject.class_average,
                "comments": list(subject.comments),
            }
            for subject in marks.report.subjects
        ],
        "comments": list(marks.report.comments),
    }


def _history_absences(
    facts: HistoryFacts, period_id: str
) -> tuple[Any, dict[str, Any]]:
    """Absences of one closed period."""
    record = next(
        (item for item in facts.attendance if item.period_id == period_id), None
    )
    if record is None:
        return None, {}
    return len(record.absences), {
        "items": [_absence_dict(item) for item in record.absences]
    }


def _history_delays(facts: HistoryFacts, period_id: str) -> tuple[Any, dict[str, Any]]:
    """Late arrivals of one closed period."""
    record = next(
        (item for item in facts.attendance if item.period_id == period_id), None
    )
    if record is None:
        return None, {}
    return len(record.delays), {"items": [_delay_dict(item) for item in record.delays]}


def _history_punishments(
    facts: HistoryFacts, period_id: str
) -> tuple[Any, dict[str, Any]]:
    """Punishments of one closed period."""
    record = next(
        (item for item in facts.attendance if item.period_id == period_id), None
    )
    if record is None:
        return None, {}
    return len(record.punishments), {
        "items": [_punishment_dict(item) for item in record.punishments]
    }


def _history_evaluations(
    facts: HistoryFacts, period_id: str
) -> tuple[Any, dict[str, Any]]:
    """Competency evaluations of one closed period."""
    record = next(
        (item for item in facts.evaluations if item.period_id == period_id), None
    )
    if record is None:
        return None, {}
    return len(record.evaluations), {
        "items": [
            {"id": item.id, "name": item.name, "subject": item.subject}
            for item in record.evaluations
        ]
    }


_HISTORY_EXTRACTORS: Final[
    dict[str, Callable[[HistoryFacts, str], tuple[Any, dict[str, Any]]]]
] = {
    "grades_period": _history_grades,
    "averages_period": _history_averages,
    "overall_average_period": _history_overall,
    "report_card_period": _history_report,
    "absences_period": _history_absences,
    "delays_period": _history_delays,
    "punishments_period": _history_punishments,
    "evaluations_period": _history_evaluations,
}


class PronoteSensor(PronoteEntity, SensorEntity):
    """A sensor whose state is derived from its tier's snapshot."""

    entity_description: PronoteSensorDescription

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        description: PronoteSensorDescription,
    ) -> None:
        super().__init__(account, coordinator, student, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateValue:
        """The fact this sensor carries."""
        facts = self.facts
        if facts is None:
            return None
        return self.entity_description.value_fn(facts, self.account)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Context, plus the snapshot's age."""
        attributes = dict(super().extra_state_attributes)
        facts = self.facts
        if facts is not None and self.entity_description.attributes_fn is not None:
            attributes.update(
                self.entity_description.attributes_fn(facts, self.account)
            )
        return attributes


class PronoteClockSensor(ClockDrivenMixin, PronoteSensor):
    """A sensor that must change state at a wall-clock instant.

    "Next lesson" stops being the next lesson the moment it starts, whatever the
    timetable tier is doing. Re-evaluated with
    ``async_track_point_in_time`` on its own value rather than by polling
    (annexe A §3.1).
    """

    async def async_added_to_hass(self) -> None:
        """Subscribe, then arm the first transition."""
        await super().async_added_to_hass()
        self._arm()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending transition."""
        self._cancel_transition()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """New data means a new transition instant."""
        super()._handle_coordinator_update()
        self._arm()

    @callback
    def _arm(self) -> None:
        """Schedule a re-evaluation at this sensor's own timestamp."""
        value = self.native_value
        if not isinstance(value, datetime):
            self._cancel_transition()
            return
        now = self.account.gateway.now()
        if value <= now:
            self._cancel_transition()
            return
        # One second past the instant, so the comparison that produced it has
        # actually flipped by the time we look again.
        self._schedule_next_transition(
            value + timedelta(seconds=1), self._on_transition
        )

    @callback
    def _on_transition(self, _now: datetime) -> None:
        """Recompute and re-arm."""
        self._clock_unsub = None
        self.async_write_ha_state()
        self._arm()


class PronoteHistorySensor(PronoteEntity, SensorEntity):
    """A sensor for one closed period, keyed by period index."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        *,
        period: Period,
        key: str,
        extract: Callable[[HistoryFacts, str], tuple[Any, dict[str, Any]]],
    ) -> None:
        super().__init__(account, coordinator, student, f"{key}_p{period.index}")
        self._period = period
        self._extract = extract
        self._attr_translation_key = key
        # The label comes from PRONOTE and is not translated; only the sentence
        # around it is (§10.2, §10.6).
        self._attr_translation_placeholders = {"period": period.name}

    @property
    def native_value(self) -> StateValue:
        """The closed period's figure."""
        facts = self.facts
        if not isinstance(facts, HistoryFacts):
            return None
        value, _ = self._extract(facts, self._period.id)
        return value  # type: ignore[no-any-return]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The closed period's detail."""
        attributes = dict(super().extra_state_attributes)
        facts = self.facts
        if isinstance(facts, HistoryFacts):
            _, extra = self._extract(facts, self._period.id)
            attributes.update(extra)
        attributes["period"] = self._period.name
        attributes["period_index"] = self._period.index
        return attributes


# ---------------------------------------------------------------------------
# Diagnostics -- attached to the account device, because the budget is shared
# ---------------------------------------------------------------------------


def _diagnostic_sensors(account: PronoteAccount) -> list[SensorEntity]:
    """The entities that make the rate limiter observable.

    A setting nobody can observe does not get tuned; it gets endured (§6.6).
    """
    coordinator = account.coordinators[Tier.SESSION]
    return [
        PronoteLimiterSensor(account, coordinator, key)
        for key in (
            "calls_today",
            "remaining_budget",
            "last_collection",
            "next_collection",
            "session_age",
            "session_lifetime",
            "logins_today",
            "limiter_state",
        )
    ]


class PronoteLimiterSensor(PronoteAccountEntity, SensorEntity):
    """One diagnostic reading of the limiter, scheduler or session."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        key: str,
    ) -> None:
        super().__init__(account, coordinator, key)
        self._key = key
        if key in ("last_collection", "next_collection"):
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif key in ("session_age",):
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key in ("session_lifetime",):
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "limiter_state":
            # A closed value set, so an automation compares the raw state while
            # the UI shows a translated label (§10.3).
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [str(state) for state in LimiterState]
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> StateValue:  # noqa: PLR0911 -- one arm per reading
        """The reading."""
        limiter = self.account.limiter
        match self._key:
            case "calls_today":
                return limiter.calls_today
            case "remaining_budget":
                return max(0, limiter.config.max_requests_per_day - limiter.calls_today)
            case "last_collection":
                tier = self.account.state.last_collection_tier
                record = self.account.state.records.get(tier) if tier else None
                return record.last_success if record else None
            case "next_collection":
                remaining = self.account.scheduler.next_due_in()
                if remaining is None:
                    return None
                return self.account.gateway.now() + timedelta(seconds=remaining)
            case "session_age":
                age = self.account.session.session_age
                return int(age) if age is not None else None
            case "session_lifetime":
                measured = self.account.session.lifetime.observed_minutes
                return round(measured, 1) if measured is not None else None
            case "logins_today":
                return limiter.logins_today
            case "limiter_state":
                return str(limiter.state)
            case _:  # pragma: no cover - the match above is exhaustive
                return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # noqa: PLR0911
        """Context for the reading."""
        limiter = self.account.limiter
        session = self.account.session
        match self._key:
            case "calls_today":
                return {
                    "by_tier": limiter.calls_by_tier,
                    "logins": limiter.logins_today,
                    "failed_logins": limiter.failed_logins_last_hour,
                }
            case "remaining_budget":
                return {
                    "daily_cap": limiter.config.max_requests_per_day,
                    "hourly_rate": limiter.config.max_requests_per_hour,
                    "tokens": round(limiter.tokens, 2),
                }
            case "last_collection":
                tier = self.account.state.last_collection_tier
                record = self.account.state.records.get(tier) if tier else None
                if record is None:
                    return {}
                return {
                    "tier": str(tier),
                    "duration_ms": record.last_duration_ms,
                    "calls": record.last_calls,
                }
            case "next_collection":
                return {"tiers_due": list(self.account.scheduler.tiers_due_names())}
            case "session_lifetime":
                # The measured value, and how it is being acted on. This is what
                # turns the session strategy from a bet into an observation
                # (§6.5).
                return {
                    "samples": len(session.lifetime.samples),
                    "last_expiry": (
                        session.lifetime.last_expiry.isoformat()
                        if session.lifetime.last_expiry
                        else None
                    ),
                    "strategy": str(session.strategy),
                    "effective_strategy": str(session.effective_strategy),
                }
            case "logins_today":
                return {
                    "failed": limiter.failed_logins_last_hour,
                    "cap": limiter.config.max_logins_per_day,
                }
            case "limiter_state":
                counters = limiter.snapshot_counters()
                return {
                    "reason": counters["reason"],
                    "until": (
                        limiter.hold_until_wallclock.isoformat()
                        if limiter.hold_until_wallclock
                        else None
                    ),
                    "consecutive_failures": limiter.consecutive_failures,
                }
            case _:
                return {}
