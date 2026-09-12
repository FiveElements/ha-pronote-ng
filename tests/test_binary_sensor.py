"""The holiday flag, which is inferred and has to be inferred correctly.

PRONOTE publishes no holiday calendar, so ``binary_sensor.<eleve>_vacances``
reads the only evidence there is: the absence of lessons. That makes it the one
entity whose correctness depends on **how far the timetable was fetched**, and
it got two whole classes of day wrong at once.

A weekend read ``on`` because a seven-day question was asked of a two-day
window. And the last week of every long holiday read ``off`` because looking
seven days ahead found the return. Both matter for the same automation -- "no
school tomorrow, no alarm" -- and both fail it in the direction that wakes a
child at six in the morning, or fails to.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from custom_components.pronote_ng.binary_sensor import (
    _holiday_attributes,
    _is_holiday,
)

from .test_delta import a_lesson, timetable

if TYPE_CHECKING:
    from custom_components.pronote_ng.account import PronoteAccount
    from custom_components.pronote_ng.models import TimetableFacts

PARIS = ZoneInfo("Europe/Paris")


class _Today:
    """The only thing ``_is_holiday`` asks of an account."""

    def __init__(self, day: dt.date) -> None:
        self._day = day

    def today(self) -> dt.date:
        """The day the entity is being evaluated on."""
        return self._day


def _account(day: dt.date) -> PronoteAccount:
    """An account stand-in pinned to one day."""
    return _Today(day)  # type: ignore[return-value]


def _school_week(monday: dt.date) -> tuple[object, ...]:
    """Five ordinary days of lessons, Monday to Friday."""
    return tuple(
        a_lesson(
            id=f"LESSON-{monday:%Y%m%d}-{offset}",
            start=dt.datetime.combine(
                monday + dt.timedelta(days=offset), dt.time(8, 0), tzinfo=PARIS
            ),
            end=dt.datetime.combine(
                monday + dt.timedelta(days=offset), dt.time(9, 0), tzinfo=PARIS
            ),
        )
        for offset in range(5)
    )


def _facts(*mondays: dt.date) -> TimetableFacts:
    """A fetched window holding a full school week for each Monday given."""
    lessons: list[object] = []
    for monday in mondays:
        lessons.extend(_school_week(monday))
    return timetable(*lessons)  # type: ignore[arg-type]


#: The two weeks the timetable tier now always fetches, in the fixture's year.
THIS_MONDAY = dt.date(2026, 3, 9)
NEXT_MONDAY = dt.date(2026, 3, 16)


@pytest.mark.parametrize(
    "day",
    [dt.date(2026, 3, 14), dt.date(2026, 3, 15)],
    ids=["saturday", "sunday"],
)
def test_an_ordinary_weekend_is_not_a_holiday(day: dt.date) -> None:
    """The defect that started this: a weekend that read ``on``.

    With the old two-day horizon a Saturday held Monday to Friday and nothing
    else, so "no lesson in the next seven days" was true and the flag turned
    ``on`` at one second past midnight -- every weekend, for forty-eight hours.
    The fix is upstream, in what the tier fetches; this pins the answer the
    predicate must give once it has both weeks.
    """
    assert _is_holiday(_facts(THIS_MONDAY, NEXT_MONDAY), _account(day)) is False


def test_the_saturday_a_holiday_starts_is_a_holiday() -> None:
    """The week behind is full, the week ahead is empty: only looking ahead sees it."""
    assert _is_holiday(_facts(THIS_MONDAY), _account(dt.date(2026, 3, 14))) is True


def test_every_day_of_an_empty_week_is_a_holiday() -> None:
    """A fetched week with no lesson in it at all is a break, not a gap."""
    facts = _facts(NEXT_MONDAY)
    for offset in range(7):
        day = THIS_MONDAY + dt.timedelta(days=offset)
        assert _is_holiday(facts, _account(day)) is True, day


def test_the_last_week_of_a_long_holiday_is_still_a_holiday() -> None:
    """The tail of a two-week break, which the forward look alone gets wrong.

    From the Monday of the second week, "is there a lesson in the next seven
    days" finds the return and answers ``off`` while the child is still on
    holiday -- a wrong week at Toussaint, at Noël, in February, at Easter, and
    for the last week of the summer. Asking whether *this* week holds a lesson
    answers it correctly on every one of those days.
    """
    facts = _facts(NEXT_MONDAY)

    assert _is_holiday(facts, _account(dt.date(2026, 3, 9))) is True
    assert _is_holiday(facts, _account(dt.date(2026, 3, 13))) is True
    assert _is_holiday(facts, _account(NEXT_MONDAY)) is False


def test_a_free_monday_does_not_read_as_a_holiday() -> None:
    """The obvious weaker rule -- "no lesson today" -- would fire here.

    A pupil with no Monday lessons is not on holiday, and the week around them
    says so. This is why the clause is about the whole week rather than about
    today.
    """
    lessons = [
        lesson
        for lesson in _school_week(THIS_MONDAY)
        if lesson.start.date() != THIS_MONDAY  # type: ignore[attr-defined]
    ]
    facts = timetable(*lessons, *_school_week(NEXT_MONDAY))  # type: ignore[arg-type]

    assert _is_holiday(facts, _account(THIS_MONDAY)) is False


def test_the_summer_holidays_read_as_holidays_for_as_long_as_they_last() -> None:
    """Nothing fetched at all, because no week of the new year exists yet.

    Outside the school year the tier asks for nothing and publishes an empty
    snapshot. That is not "we have not looked" -- it is the correct answer for
    two months, and the entity has to give it rather than go unavailable.
    """
    empty = timetable()

    assert _is_holiday(empty, _account(dt.date(2026, 7, 20))) is True


def test_a_cancelled_lesson_is_not_evidence_of_school() -> None:
    """A week whose only entries are cancellations is still an empty week."""
    facts = timetable(
        *(
            a_lesson(
                id=f"CANCELED-{offset}",
                canceled=True,
                start=dt.datetime.combine(
                    THIS_MONDAY + dt.timedelta(days=offset),
                    dt.time(8, 0),
                    tzinfo=PARIS,
                ),
                end=dt.datetime.combine(
                    THIS_MONDAY + dt.timedelta(days=offset),
                    dt.time(9, 0),
                    tzinfo=PARIS,
                ),
            )
            for offset in range(5)
        )
    )

    assert _is_holiday(facts, _account(THIS_MONDAY)) is True


def test_the_attributes_say_which_clause_can_answer() -> None:
    """A card should not have to re-derive "no school this week" from the list."""
    attributes = _holiday_attributes(
        _facts(NEXT_MONDAY), _account(THIS_MONDAY + dt.timedelta(days=2))
    )

    assert attributes["inferred"] is True
    assert attributes["lookahead_days"] == 7
    assert attributes["current_week_without_lessons"] is True
    assert (
        attributes["next_lesson"]
        == dt.datetime.combine(NEXT_MONDAY, dt.time(8, 0), tzinfo=PARIS).isoformat()
    )
