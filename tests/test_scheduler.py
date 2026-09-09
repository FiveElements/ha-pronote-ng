"""The scheduler, to 100 %.

One heartbeat, deadlines rather than timers, and a deferral that moves a
deadline instead of dropping a tier. Like the limiter this module takes its
clocks as arguments, so every branch is reachable without patching anything.

The properties worth defending here are subtle and were all wrong at least
once: a refresh button must not outrank the session tier, a deferral must be
bounded by the tier's own interval, and an options save must not make all ten
tiers due at the same instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pronote_ng.const import (
    DEFAULT_TIER_INTERVALS,
    PRIORITY_RANK,
    TIER_PRIORITY,
    Priority,
    Tier,
)
from custom_components.pronote_ng.scheduler import (
    SECONDS_PER_MINUTE,
    FetchScheduler,
    TierPlan,
    default_plans,
)

from .clock import FakeClock  # noqa: TC001 -- a pytest fixture annotation


def build(
    clock: FakeClock, plans: dict[Tier, TierPlan] | None = None
) -> FetchScheduler:
    """A scheduler on the default plans unless told otherwise."""
    return FetchScheduler(
        plans if plans is not None else default_plans(),
        clock=clock.monotonic,
        now=clock.now,
    )


def one(tier: Tier, minutes: int, priority: Priority) -> dict[Tier, TierPlan]:
    """A single-tier plan set, for tests about one tier's behaviour."""
    return {tier: TierPlan(tier=tier, interval_minutes=minutes, priority=priority)}


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def test_the_default_plans_cover_every_tier_with_its_own_priority() -> None:
    """Ten tiers, each with the priority the sacrifice table gives it."""
    plans = default_plans()
    assert set(plans) == set(DEFAULT_TIER_INTERVALS)
    for tier, plan in plans.items():
        assert plan.priority is TIER_PRIORITY[tier]
        assert plan.interval_minutes == DEFAULT_TIER_INTERVALS[tier]
        assert plan.enabled


def test_options_override_intervals_and_switch_tiers_off() -> None:
    """Both option maps are honoured, and absent keys fall back."""
    plans = default_plans(intervals={Tier.TIMETABLE: 5}, enabled={Tier.HISTORY: False})
    assert plans[Tier.TIMETABLE].interval_minutes == 5
    assert not plans[Tier.HISTORY].enabled
    assert plans[Tier.HOMEWORK].enabled


def test_an_interval_is_reported_in_seconds() -> None:
    """The rest of the module works in seconds; the options are in minutes."""
    plan = TierPlan(tier=Tier.TIMETABLE, interval_minutes=15, priority=Priority.HIGH)
    assert plan.interval_seconds == 15 * SECONDS_PER_MINUTE


# ---------------------------------------------------------------------------
# What is due
# ---------------------------------------------------------------------------


def test_everything_is_due_on_a_cold_start(clock: FakeClock) -> None:
    """``last_collected is None`` makes a tier infinitely overdue.

    Which is right: after a restart the integration knows nothing, and the
    first batch is what fills every entity.
    """
    scheduler = build(clock)
    assert set(scheduler.due()) == set(default_plans())


def test_nothing_is_due_before_its_interval_elapses(clock: FakeClock) -> None:
    """A collected tier waits out its interval."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)

    clock.advance(14 * 60)
    assert scheduler.due() == []

    clock.advance(2 * 60)
    assert scheduler.due() == [Tier.TIMETABLE]


def test_a_disabled_tier_is_never_due(clock: FakeClock) -> None:
    """Switching a tier off removes it from the schedule, not merely from the UI."""
    plans = one(Tier.HISTORY, 1440, Priority.LOW)
    scheduler = build(clock, plans)
    scheduler.set_enabled(Tier.HISTORY, enabled=False)
    assert scheduler.due() == []
    assert scheduler.next_due_in() is None


def test_due_is_ordered_by_priority_then_by_lateness(clock: FakeClock) -> None:
    """Ordering is not cosmetic: the limiter sheds in this order.

    Serving in priority order means the tiers that get dropped when the budget
    tightens are the ones annexe B §2.4 already nominated. Served in dictionary
    order, the tier that happened to be declared first won -- so a tightening
    budget sacrificed whichever tier the source file listed last.
    """
    plans = {
        Tier.HISTORY: TierPlan(Tier.HISTORY, 1, Priority.LOW),
        Tier.SESSION: TierPlan(Tier.SESSION, 1, Priority.CRITICAL),
        Tier.TIMETABLE: TierPlan(Tier.TIMETABLE, 1, Priority.HIGH),
        Tier.HOMEWORK: TierPlan(Tier.HOMEWORK, 1, Priority.NORMAL),
    }
    scheduler = build(clock, plans)
    for tier in plans:
        scheduler.mark_collected(tier)
    clock.advance(120)

    assert scheduler.due() == [
        Tier.SESSION,
        Tier.TIMETABLE,
        Tier.HOMEWORK,
        Tier.HISTORY,
    ]


def test_the_later_of_two_equal_tiers_goes_first(clock: FakeClock) -> None:
    """Within one priority, the most overdue is served first."""
    plans = {
        Tier.NEWS: TierPlan(Tier.NEWS, 1, Priority.NORMAL),
        Tier.HOMEWORK: TierPlan(Tier.HOMEWORK, 1, Priority.NORMAL),
    }
    scheduler = build(clock, plans)
    scheduler.mark_collected(Tier.HOMEWORK)
    clock.advance(300)
    scheduler.mark_collected(Tier.NEWS)
    clock.advance(120)

    assert scheduler.due() == [Tier.HOMEWORK, Tier.NEWS]


def test_the_order_is_deterministic_for_identical_tiers(clock: FakeClock) -> None:
    """Same priority, same lateness: the tier name breaks the tie.

    Not for elegance -- a non-deterministic batch order makes an intermittent
    budget failure impossible to reproduce.
    """
    plans = {
        Tier.NEWS: TierPlan(Tier.NEWS, 1, Priority.NORMAL),
        Tier.HOMEWORK: TierPlan(Tier.HOMEWORK, 1, Priority.NORMAL),
        Tier.MENUS: TierPlan(Tier.MENUS, 1, Priority.NORMAL),
    }
    scheduler = build(clock, plans)
    for tier in plans:
        scheduler.mark_collected(tier)
    clock.advance(120)

    first = scheduler.due()
    assert first == sorted(first, key=lambda tier: tier.value)


# ---------------------------------------------------------------------------
# The refresh button
# ---------------------------------------------------------------------------


def test_a_boost_makes_a_tier_due_before_its_interval(clock: FakeClock) -> None:
    """The button's whole purpose: collect now rather than in fourteen minutes."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    assert scheduler.due() == []

    scheduler.request([Tier.TIMETABLE])
    assert scheduler.due() == [Tier.TIMETABLE]


def test_a_boost_raises_a_tier_by_one_rank_and_no_further(clock: FakeClock) -> None:
    """A refresh button must not outrank the session tier.

    Given rank ``-1`` it did. A refresh on ``history`` -- eight requests, LOW
    priority -- was then served *first*, drained the bucket and the ``max_wait``
    allowance, and ``timetable`` -- HIGH -- was deferred behind it: the exact
    inverse of the sacrifice order. The button asks for a priority pass, not
    for supremacy.
    """
    plans = {
        Tier.HISTORY: TierPlan(Tier.HISTORY, 1440, Priority.LOW),
        Tier.SESSION: TierPlan(Tier.SESSION, 60, Priority.CRITICAL),
        Tier.TIMETABLE: TierPlan(Tier.TIMETABLE, 15, Priority.HIGH),
    }
    scheduler = build(clock, plans)
    for tier in plans:
        scheduler.mark_collected(tier)
    clock.advance(70 * 60)

    scheduler.request([Tier.HISTORY])
    order = scheduler.due()

    assert order[0] is Tier.SESSION
    assert order.index(Tier.HISTORY) > order.index(Tier.SESSION)


def test_a_boost_cannot_reach_the_critical_rank(clock: FakeClock) -> None:
    """Even a HIGH tier boosted lands just below CRITICAL, never level with it."""
    plans = {
        Tier.TIMETABLE: TierPlan(Tier.TIMETABLE, 15, Priority.HIGH),
        Tier.SESSION: TierPlan(Tier.SESSION, 15, Priority.CRITICAL),
    }
    scheduler = build(clock, plans)
    for tier in plans:
        scheduler.mark_collected(tier)
    clock.advance(20 * 60)

    scheduler.request([Tier.TIMETABLE])
    assert scheduler.due()[0] is Tier.SESSION
    assert PRIORITY_RANK[Priority.CRITICAL] < PRIORITY_RANK[Priority.HIGH]


def test_a_boost_lasts_exactly_one_tick(clock: FakeClock) -> None:
    """Collected clears it, so the next tick sees an ordinary tier."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    scheduler.request([Tier.TIMETABLE])
    assert scheduler.due() == [Tier.TIMETABLE]

    scheduler.mark_collected(Tier.TIMETABLE)
    assert scheduler.due() == []


def test_a_boost_clears_an_outstanding_deferral(clock: FakeClock) -> None:
    """A user pressing refresh is asking to stop waiting."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    scheduler.defer(Tier.TIMETABLE, 600)
    assert scheduler.due() == []

    scheduler.request([Tier.TIMETABLE])
    assert scheduler.due() == [Tier.TIMETABLE]


def test_requesting_nothing_in_particular_boosts_every_enabled_tier(
    clock: FakeClock,
) -> None:
    """``request()`` with no argument is the "refresh everything" button."""
    scheduler = build(clock)
    scheduler.set_enabled(Tier.HISTORY, enabled=False)
    for tier in default_plans():
        scheduler.mark_collected(tier)

    scheduler.request()

    due = scheduler.due()
    assert Tier.HISTORY not in due
    assert Tier.TIMETABLE in due


def test_requesting_an_unknown_tier_is_ignored(clock: FakeClock) -> None:
    """A stale automation naming a removed tier must not raise."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.request([Tier.HISTORY])
    assert scheduler.due() == [Tier.TIMETABLE]


# ---------------------------------------------------------------------------
# Deferral
# ---------------------------------------------------------------------------


def test_a_deferral_moves_a_deadline_and_never_drops_a_tier(
    clock: FakeClock,
) -> None:
    """Annexe B §2.4: a deferred tier keeps its snapshot and comes back."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.defer(Tier.TIMETABLE, 300)
    assert scheduler.due() == []

    clock.advance(301)
    assert scheduler.due() == [Tier.TIMETABLE]


def test_a_deferral_is_bounded_by_the_tier_s_own_interval(clock: FakeClock) -> None:
    """ "Retry at midnight" must not silence a fifteen-minute tier for eleven hours.

    The limiter answers the daily cap with the seconds to midnight, which is a
    perfectly good answer to *its* question and a terrible deadline for a fast
    tier: the budget usually frees up long before, and a tier that skipped the
    whole evening has nothing to show for it.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.defer(Tier.TIMETABLE, 11 * 3600)

    clock.advance(15 * 60 + 1)
    assert scheduler.due() == [Tier.TIMETABLE]


def test_a_negative_retry_delay_is_clamped(clock: FakeClock) -> None:
    """Defensive: a deadline in the past is the same as no deadline."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.defer(Tier.TIMETABLE, -100)
    assert scheduler.due() == [Tier.TIMETABLE]


def test_deferrals_are_counted_for_the_diagnostic(clock: FakeClock) -> None:
    """A setting nobody can observe does not get tuned; it gets endured."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.defer(Tier.TIMETABLE, 10)
    scheduler.defer(Tier.TIMETABLE, 10)
    assert scheduler.diagnostics()["timetable"]["deferrals"] == 2

    clock.advance(11)
    scheduler.mark_collected(Tier.TIMETABLE)
    assert scheduler.diagnostics()["timetable"]["deferrals"] == 0


def test_a_failure_is_treated_as_a_deferral(clock: FakeClock) -> None:
    """Retry later, keep the snapshot -- a failure is not a reason to forget."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    collected = scheduler.last_collected(Tier.TIMETABLE)

    scheduler.mark_failed(Tier.TIMETABLE, 300)

    assert scheduler.last_collected(Tier.TIMETABLE) == collected
    assert scheduler.due() == []


def test_deferring_an_unknown_tier_is_ignored(clock: FakeClock) -> None:
    """Same tolerance as ``request``, for the same reason."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.defer(Tier.HISTORY, 300)
    assert scheduler.due() == [Tier.TIMETABLE]


def test_marking_an_unknown_tier_collected_is_ignored(clock: FakeClock) -> None:
    """A tier removed by a reconfigure between tick and completion."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.HISTORY)
    assert scheduler.last_collected(Tier.HISTORY) is None


# ---------------------------------------------------------------------------
# next_due_in
# ---------------------------------------------------------------------------


def test_next_due_in_is_zero_when_something_is_overdue(clock: FakeClock) -> None:
    """``sensor.<compte>_prochaine_collecte`` reads *now*, and that is honest.

    The previous docstring claimed ``None`` meant "nothing is due", which was
    two different things confused: ``None`` means there is no enabled tier at
    all.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    assert scheduler.next_due_in() == 0.0


def test_a_missed_deadline_is_reported_as_how_late_it_is(clock: FakeClock) -> None:
    """The sign is the fix, and the reason is what the value is used for.

    ``sensor.<compte>_prochaine_collecte`` publishes ``now + this``. Clamped at
    zero, that made the entity report the instant of its own last refresh --
    and it is refreshed *by a collection*, so a tier that was due and failing
    froze the timestamp in the past while the clock moved on. A dashboard read
    "next collection: 40 minutes ago", which is not a deadline at all.

    Unclamped, the number is an offset to a fixed instant instead of a
    countdown from an arbitrary one, so the timestamp stops depending on when
    anybody happened to ask.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)

    clock.advance(15 * 60 + 40 * 60)

    assert scheduler.next_due_in() == pytest.approx(-40 * 60)
    # And it is genuinely due -- late, not silently dropped.
    assert scheduler.tiers_due_names() == ("timetable",)


def test_next_due_in_counts_down_to_the_earliest_deadline(clock: FakeClock) -> None:
    """The earliest of the enabled tiers, not the average or the first."""
    plans = {
        Tier.TIMETABLE: TierPlan(Tier.TIMETABLE, 15, Priority.HIGH),
        Tier.HISTORY: TierPlan(Tier.HISTORY, 1440, Priority.LOW),
    }
    scheduler = build(clock, plans)
    for tier in plans:
        scheduler.mark_collected(tier)

    clock.advance(5 * 60)
    assert scheduler.next_due_in() == pytest.approx(10 * 60)


def test_next_due_in_respects_a_deferral(clock: FakeClock) -> None:
    """A deferred tier is not due sooner than its deferral allows."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.defer(Tier.TIMETABLE, 300)
    assert scheduler.next_due_in() == pytest.approx(300)


def test_next_due_in_is_none_with_no_enabled_tier(clock: FakeClock) -> None:
    """The only meaning of ``None``."""
    scheduler = build(clock, {})
    assert scheduler.next_due_in() is None


def test_tier_names_due_are_reported_as_strings(clock: FakeClock) -> None:
    """For the diagnostic attribute, which is text."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    assert scheduler.tiers_due_names() == ("timetable",)


# ---------------------------------------------------------------------------
# Reconfiguration
# ---------------------------------------------------------------------------


def test_reconfiguring_keeps_the_collection_history(clock: FakeClock) -> None:
    """The change is measured from real history, not from the save (§7.3)."""
    scheduler = build(clock, one(Tier.TIMETABLE, 60, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    collected = scheduler.last_collected(Tier.TIMETABLE)

    clock.advance(30 * 60)
    scheduler.reconfigure(one(Tier.TIMETABLE, 15, Priority.HIGH))

    assert scheduler.last_collected(Tier.TIMETABLE) == collected
    # And it *is* due, because thirty minutes have really passed on a
    # fifteen-minute interval. Claiming otherwise would be the false version of
    # this property.
    assert scheduler.due() == [Tier.TIMETABLE]


def test_reconfiguring_adds_and_removes_tiers(clock: FakeClock) -> None:
    """State follows the plans, so a removed tier leaves no orphan bookkeeping."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)

    scheduler.reconfigure(one(Tier.HISTORY, 1440, Priority.LOW))

    assert set(scheduler.plans) == {Tier.HISTORY}
    assert scheduler.last_collected(Tier.TIMETABLE) is None
    assert scheduler.due() == [Tier.HISTORY]


def test_setting_an_interval_keeps_the_history(clock: FakeClock) -> None:
    """The single-tier version of the same property."""
    scheduler = build(clock, one(Tier.TIMETABLE, 60, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    collected = scheduler.last_collected(Tier.TIMETABLE)

    scheduler.set_interval(Tier.TIMETABLE, 15)

    assert scheduler.plans[Tier.TIMETABLE].interval_minutes == 15
    assert scheduler.last_collected(Tier.TIMETABLE) == collected


def test_setting_an_interval_on_an_unknown_tier_is_ignored(clock: FakeClock) -> None:
    """No plan, nothing to change."""
    scheduler = build(clock, one(Tier.TIMETABLE, 60, Priority.HIGH))
    scheduler.set_interval(Tier.HISTORY, 15)
    scheduler.set_enabled(Tier.HISTORY, enabled=False)
    assert set(scheduler.plans) == {Tier.TIMETABLE}


# ---------------------------------------------------------------------------
# Surviving a reload
# ---------------------------------------------------------------------------


def test_a_reload_does_not_make_every_tier_due(clock: FakeClock) -> None:
    """Saving the options page used to cost a login plus a full batch.

    Home Assistant *reloads* a config entry rather than reconfiguring it in
    place, so a new scheduler was built with every ``last_collected`` unset --
    which made all ten tiers immediately due. Preserving history inside
    ``reconfigure`` was the right idea aimed at the wrong seam: nothing ever
    called it. The schedule is handed across the reload instead.
    """
    before = build(clock)
    for tier in default_plans():
        before.mark_collected(tier)
    saved = before.export_state()

    clock.advance(60)
    after = build(clock)
    after.import_state(saved)

    assert after.due() == []
    assert after.age(Tier.TIMETABLE) == pytest.approx(60)


def test_a_saved_schedule_from_the_future_is_discarded(clock: FakeClock) -> None:
    """A value ahead of the clock means the monotonic origin moved.

    That is a *restart*, not a reload, and a restart legitimately knows
    nothing. Trusting the value would leave every tier waiting out an interval
    it never served.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.import_state({"timetable": clock.monotonic() + 10_000})
    assert scheduler.last_collected(Tier.TIMETABLE) is None
    assert scheduler.due() == [Tier.TIMETABLE]


def test_a_saved_schedule_naming_an_unknown_tier_is_ignored(
    clock: FakeClock,
) -> None:
    """An entry saved by a version that had a tier this one does not."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.import_state({"a_tier_that_no_longer_exists": clock.monotonic()})
    assert scheduler.last_collected(Tier.TIMETABLE) is None


def test_an_uncollected_tier_is_absent_from_the_export(clock: FakeClock) -> None:
    """Absent rather than ``None``, so ``import_state`` needs no sentinel."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    assert scheduler.export_state() == {}


# ---------------------------------------------------------------------------
# Age and staleness
# ---------------------------------------------------------------------------


def test_age_is_none_before_the_first_collection(clock: FakeClock) -> None:
    """ "I have never known" is not "I knew a long time ago"."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    assert scheduler.age(Tier.TIMETABLE) is None
    assert scheduler.last_collected_at(Tier.TIMETABLE) is None


def test_a_tier_with_no_data_is_stale(clock: FakeClock) -> None:
    """Before the first collection an entity has nothing to keep."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    assert scheduler.is_stale(Tier.TIMETABLE, stale_after=3)


def test_an_unknown_tier_is_stale(clock: FakeClock) -> None:
    """No plan means no interval to measure against."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    assert scheduler.is_stale(Tier.HISTORY, stale_after=3)


def test_staleness_begins_past_stale_after_intervals(clock: FakeClock) -> None:
    """Below the boundary an entity keeps its value and flags its age.

    Keeping the last known value and marking it dated is safer than admitting
    ignorance every ten minutes: a ``numeric_state`` trigger on an entity that
    goes unavailable and comes back fires spuriously.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)

    clock.advance(3 * 15 * 60)
    assert not scheduler.is_stale(Tier.TIMETABLE, stale_after=3)

    clock.advance(60)
    assert scheduler.is_stale(Tier.TIMETABLE, stale_after=3)


def test_excused_time_is_subtracted_from_the_age(clock: FakeClock) -> None:
    """This is what stops every fast tier going unavailable at 06:00.

    An eight-hour night is longer than three fifteen-minute intervals, so
    without the excuse the timetable entity of *every* installation went
    ``unavailable`` each morning -- and §2.5 rules out saying "I no longer
    know" because the integration was asleep on purpose.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)

    # Eight hours of quiet plus ten minutes of daytime. Three fifteen-minute
    # intervals is forty-five minutes, so the night alone is what pushed the
    # tier past the boundary -- and excusing it brings the age back to ten
    # minutes, which is fresh.
    clock.advance(8 * 3600 + 600)
    assert scheduler.is_stale(Tier.TIMETABLE, stale_after=3)
    assert not scheduler.is_stale(Tier.TIMETABLE, stale_after=3, excused=8 * 3600)


def test_a_negative_excuse_cannot_age_a_tier(clock: FakeClock) -> None:
    """Defensive: an excuse only ever forgives time, never adds it."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    clock.advance(60)
    assert not scheduler.is_stale(Tier.TIMETABLE, stale_after=3, excused=-100_000)


def test_the_wall_clock_instant_is_recorded_alongside_the_monotonic_one(
    clock: FakeClock,
) -> None:
    """Only a wall clock can say whether the night intervened."""
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    assert scheduler.last_collected_at(Tier.TIMETABLE) == clock.now()


def test_a_scheduler_without_a_wall_clock_still_works(clock: FakeClock) -> None:
    """The wall clock is optional, because the estimator has no use for one."""
    scheduler = FetchScheduler(
        one(Tier.TIMETABLE, 15, Priority.HIGH), clock=clock.monotonic
    )
    scheduler.mark_collected(Tier.TIMETABLE)
    assert scheduler.last_collected_at(Tier.TIMETABLE) is None
    assert scheduler.due() == []


def test_the_monotonic_clock_is_what_measures_intervals(clock: FakeClock) -> None:
    """A wall-clock jump must not make a tier due, or overdue by a day.

    An NTP correction of a few hours would otherwise fire every tier at once,
    which is precisely the burst the single heartbeat exists to prevent.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)

    clock.set_wall(datetime(2026, 3, 13, 8, 0, tzinfo=UTC))

    assert scheduler.due() == []
    assert scheduler.age(Tier.TIMETABLE) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_the_diagnostics_describe_every_planned_tier(clock: FakeClock) -> None:
    """One row per tier, with the six facts annexe A §7 asks for."""
    scheduler = build(clock)
    scheduler.mark_collected(Tier.TIMETABLE)
    scheduler.request([Tier.NEWS])

    rows = scheduler.diagnostics()

    assert set(rows) == {str(tier) for tier in default_plans()}
    assert rows["timetable"]["priority"] == str(Priority.HIGH)
    assert rows["timetable"]["enabled"] is True
    assert rows["timetable"]["age_seconds"] == pytest.approx(0.0)
    assert rows["news"]["boosted"] is True
    assert rows["history"]["age_seconds"] is None


def test_the_diagnostics_are_json_serialisable(clock: FakeClock) -> None:
    """They go into a download and into a service response."""
    scheduler = build(clock)
    for row in scheduler.diagnostics().values():
        for value in row.values():
            assert isinstance(value, (int, float, str, bool, type(None)))


def test_a_stale_check_uses_the_tier_s_own_interval(clock: FakeClock) -> None:
    """A daily tier is not stale after an hour; a fast one is."""
    plans = {
        Tier.TIMETABLE: TierPlan(Tier.TIMETABLE, 15, Priority.HIGH),
        Tier.HISTORY: TierPlan(Tier.HISTORY, 1440, Priority.LOW),
    }
    scheduler = build(clock, plans)
    for tier in plans:
        scheduler.mark_collected(tier)

    clock.advance(timedelta(hours=2).total_seconds())

    assert scheduler.is_stale(Tier.TIMETABLE, stale_after=3)
    assert not scheduler.is_stale(Tier.HISTORY, stale_after=3)


def test_a_second_boost_inside_the_interval_is_refused(clock: FakeClock) -> None:
    """§5.1: ten presses inside one interval cost one batch, not ten.

    This is the ceiling, and it is the substance of `request` rather than a
    refinement of it. A boosted tier is due *regardless* of its deadline --
    that is what makes the button do anything at all -- so without a ceiling
    each press re-armed the dispensation and each press bought another
    collection. Ten presses on `history`, at six requests per child, were 120
    requests against a server whose one sanction applies to an IP address.

    It used to hold by accident: the collections overlapped, so all but the
    first hit the account's "a batch is already running" guard. That is a
    property of the event loop rather than of this code, and it stopped
    holding between two Home Assistant releases with nothing changed here --
    measured at 2 requests on 2026.2 and 20 on 2026.8 for the same ten presses.
    """
    scheduler = build(clock, one(Tier.HISTORY, 1440, Priority.LOW))
    scheduler.mark_collected(Tier.HISTORY)

    # The first press is served: the tier becomes due and is collected.
    scheduler.request([Tier.HISTORY])
    assert scheduler.due() == [Tier.HISTORY]
    scheduler.mark_collected(Tier.HISTORY)

    # Nine more presses inside the same interval buy nothing.
    for _ in range(9):
        clock.advance(30)
        scheduler.request([Tier.HISTORY])
        assert scheduler.due() == [], "a second boost was served inside the interval"


def test_the_button_works_again_in_the_next_interval(clock: FakeClock) -> None:
    """The ceiling is a rate, not a one-shot.

    A tier boosted once must not be boost-proof for ever: the refusal has to
    expire with the interval it was measured against, or the button silently
    stops working after its first use.
    """
    scheduler = build(clock, one(Tier.TIMETABLE, 15, Priority.HIGH))
    scheduler.mark_collected(Tier.TIMETABLE)
    scheduler.request([Tier.TIMETABLE])
    scheduler.mark_collected(Tier.TIMETABLE)

    clock.advance(15 * 60)
    scheduler.request([Tier.TIMETABLE])

    assert scheduler.due() == [Tier.TIMETABLE]


def test_a_boost_that_was_deferred_does_not_count_against_the_ceiling(
    clock: FakeClock,
) -> None:
    """A request the limiter refused was never honoured.

    `defer` clears `boosted` just as `mark_collected` does, so the two are easy
    to conflate -- and conflating them would spend the user's one boost per
    interval on a collection that never happened. Somebody pressing refresh
    during a budget squeeze would then be told nothing and get nothing, twice.
    """
    scheduler = build(clock, one(Tier.MARKS, 180, Priority.NORMAL))
    scheduler.mark_collected(Tier.MARKS)

    scheduler.request([Tier.MARKS])
    assert scheduler.due() == [Tier.MARKS]
    scheduler.defer(Tier.MARKS, 60.0)
    assert scheduler.due() == []

    # The hold expires, the user presses again: it must be served.
    clock.advance(61)
    scheduler.request([Tier.MARKS])

    assert scheduler.due() == [Tier.MARKS]
