"""The sensor attributes a dashboard actually reads.

Most of this platform is a table of descriptions, and a table is not worth a
test per row. What is worth testing is the handful of places where the shape of
an attribute set -- not its value -- is what a card depends on, because those
are the failures that reach a parent as a blank tile with no error anywhere.

Two of them live here.

**A menu says whether there is a menu.** The attributes were absent entirely on
a day the canteen published nothing, so a template reading ``main_meal`` got
``None`` -- exactly what it gets before the tier has ever run. "No lunch today"
and "we have not looked yet" are different sentences to say to a parent, and
with no key to read there was no way to say either.

**A homework description is prose.** ``descriptif`` is HTML, and a card can
neither print it nor inject it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import CHILDREN, PARIS, REQUIRES_HASS
from .fixtures import protocol
from .fixtures.client import FakeClient

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from custom_components.pronote_ng.account import PronoteAccount

pytestmark = REQUIRES_HASS

#: The courses the shared fake canteen serves, and the keys a card reads them
#: from. Listed once so the "nothing published" case can assert the *same* set
#: rather than a hand-copied subset that would drift away from it.
COURSE_KEYS = (
    "first_meal",
    "main_meal",
    "side_meal",
    "other_meal",
    "cheese",
    "dessert",
)


def _attributes(hass: HomeAssistant, entity_id: str) -> dict[str, object]:
    """One sensor's attributes, read the way a template reads them."""
    state = hass.states.get(entity_id)
    assert state is not None, f"no entity {entity_id}"
    return dict(state.attributes)


async def test_a_published_menu_carries_its_courses_and_says_it_is_published(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The ordinary day, which is the baseline the empty one is compared to."""
    attributes = _attributes(hass, "sensor.enfant_un_menu_today")

    assert attributes["published"] is True
    assert attributes["main_meal"] == ["Poisson pané"]
    assert attributes["is_lunch"] is True


class TestWhenTheCanteenPublishesNothing:
    """A day with no menu at all -- a holiday, or a school with no canteen."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """The shared client, with the canteen silent.

        Overridden here and not in ``conftest.py`` because a fake server that
        serves no menus would make every other module's menu assertions
        vacuous rather than failing them.
        """
        return FakeClient(children=CHILDREN, menus_published=False)

    async def test_the_keys_are_still_there(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Empty lists, not absent keys.

        This is the defect. ``state_attr(entity, 'main_meal')`` returned
        ``None`` here and ``None`` again before the first collection, so a card
        could not distinguish them and showed nothing in both cases. With the
        keys present and empty, "no menu" is renderable.
        """
        attributes = _attributes(hass, "sensor.enfant_un_menu_today")

        assert attributes["published"] is False
        for key in COURSE_KEYS:
            assert attributes[key] == [], key
        # Not guessed: there is no meal, so there is no service to report.
        assert attributes["is_lunch"] is None

    async def test_the_state_is_still_unknown_rather_than_zero(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """``published: False`` must not have turned into "a menu of nothing".

        The state is the dish count, and zero dishes is a claim about a menu
        that exists. The attributes gained a shape; the state keeps saying
        there is nothing to count.
        """
        state = hass.states.get("sensor.enfant_un_menu_today")

        assert state is not None
        assert state.state == "unknown"


class TestHomeworkAttributes:
    """The homework list a card renders, rather than the to-do entity."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One item, described the way PRONOTE describes it: in HTML."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(description="<p>Exercice 3 p.&nbsp;52</p>")]
        )
        return client

    async def test_a_card_gets_the_prose_and_the_markup(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Both, because neither one substitutes for the other.

        A card that prints ``description`` shows the tags; a card that injects
        it hands every teacher's text field a route into the dashboard. So the
        printable form travels next to it, converted once here rather than
        three times in three cards.
        """
        attributes = _attributes(hass, "sensor.enfant_un_homework_to_do")
        item = attributes["items"][0]  # type: ignore[index]

        assert item["description_text"] == "Exercice 3 p. 52"
        assert item["description"] == "<p>Exercice 3 p.&nbsp;52</p>"


class TestTheDiagnosticReadingsRefreshOnTheirOwn:
    """The eight limiter readings, and what they were actually doing: nothing.

    They all carried ``_attr_should_poll = True``, and it was inert --
    ``BaseCoordinatorEntity`` declares ``should_poll`` as a property returning
    ``False``, which shadows the attribute. So a tile called "next collection"
    was refreshed only when a collection happened, which is precisely the
    circumstance in which it is wrong to trust it: a tier due and failing left
    the timestamp frozen while the clock moved on, and a dashboard read "next
    collection: 40 minutes ago".
    """

    async def test_a_reading_is_rewritten_without_any_collection(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        parent_client: FakeClient,
        school_day: FrozenDateTimeFactory,
    ) -> None:
        """Time passes, the state is rewritten, and PRONOTE is not asked.

        Both halves matter and the second is the reason the first was hard.
        ``CoordinatorEntity.async_update`` calls ``async_request_refresh``, so
        simply switching polling on would have bought a fresh timestamp at the
        price of a server request every thirty seconds -- the one thing the
        budget exists to prevent (§7.1). The override is a no-op body, and this
        asserts the ledger stays where it was.
        """
        entity_id = "sensor.college_d_essai_session_age"
        before = hass.states.get(entity_id)
        assert before is not None, "no diagnostic sensor"
        posts = len(parent_client.posts)

        school_day.tick(timedelta(minutes=2))
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done()

        after = hass.states.get(entity_id)
        assert after is not None
        assert after.last_reported > before.last_reported
        assert len(parent_client.posts) == posts

    async def test_a_missed_deadline_is_named_rather_than_left_to_be_guessed(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A timestamp in the past is a diagnosis only if it says what is late.

        ``overdue_by`` and ``failing`` are here because a deadline in the past
        looks exactly like a sensor that stopped updating, and a reader has to
        be able to tell those apart from the tile alone.
        """
        state = hass.states.get("sensor.college_d_essai_next_collection")

        assert state is not None
        assert "overdue_by" in state.attributes
        assert "failing" in state.attributes


def _day(hour: int, minute: int = 0) -> datetime:
    """A naive instant on the frozen school day, as the protocol sends them."""
    return datetime(2026, 3, 12, hour, minute)


class TestWhenTheMorningEnds:
    """`morning_end` answers "when do I collect the child for lunch?".

    The state is a timestamp, so an automation triggers on it directly and
    needs no template walking `attributes.lessons` -- which is the §1 test any
    new entity has to pass.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """A day with a real lunch break: 8-12, then 14-16."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(14),
                    end=_day(16),
                    place=12,
                    duration=4,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_the_state_is_the_end_of_the_last_morning_lesson(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A timestamp, and the context a card needs beside it."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        # Compared as an instant, not as text: Home Assistant normalises a
        # timestamp state to UTC, so the establishment's 12:00+01:00 is stored
        # as 11:00+00:00 and a string comparison tests the serialisation
        # rather than the rule.
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 12, 0, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"
        assert state.attributes["resumes_at"] == "2026-03-12T14:00:00+01:00"
        assert state.attributes["break_minutes"] == 120


class TestWhenThereIsNoMiddayBreak:
    """Nothing, rather than a plausible wrong hour."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One morning lesson and one late-afternoon lesson.

        This is the day that makes "the day's largest gap" the wrong rule: the
        gap is five hours and it starts at 10:00, which is not a lunch break
        and would send a parent out mid-morning.
        """
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(9),
                    end=_day(10),
                    place=2,
                    duration=2,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(15),
                    end=_day(16),
                    place=14,
                    duration=2,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_the_largest_gap_of_the_day_is_not_a_lunch_break(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """`unknown`, not `unavailable`, and not 10:00.

        The collection succeeded and the honest answer is "no midday break
        today". `end_of_lessons` is the entity that says when this child comes
        home, and publishing the same instant twice would fire two
        automations for one homecoming.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        assert state.state == "unknown"
        assert "resumes_at" not in state.attributes


class TestWhenTheGapIsTooShortToBeLunch:
    """A changeover between two lessons is not a break."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """8-12, then 12:30-16: inside the window, but half an hour."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(12, 30),
                    end=_day(16),
                    place=9,
                    duration=7,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_thirty_minutes_at_noon_is_not_reported_as_a_break(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The window alone is not the rule; the duration is the other half.

        Without it, any timetable with a short changeover near noon would
        announce a lunch break the child never gets.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_morning")

        assert state is not None
        assert state.state == "unknown"


class TestTheNextCancellation:
    """One filter for the state and the list, so they cannot disagree.

    The state is the start of the next cancelled lesson that is not over yet;
    `items` is every such lesson. Both use `end > now`, deliberately: two
    filters would drift, and a card would then show a list whose first entry
    is not the entity's own state.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Four slots: a cancellation already over, two to come, one exemption."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-OVER",
                    start=_day(7),
                    end=_day(7, 30),
                    place=0,
                    duration=1,
                    subject="Anglais",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-NEXT",
                    start=_day(10),
                    end=_day(11),
                    place=4,
                    duration=2,
                    subject="Mathématiques",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-LATER",
                    start=_day(14),
                    end=_day(15),
                    place=12,
                    duration=2,
                    subject="Histoire",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-EXEMPT",
                    start=_day(16),
                    end=_day(17),
                    place=16,
                    duration=2,
                    subject="Sport",
                    exempted=True,
                ),
            ]
        )
        return client

    async def test_the_state_is_the_start_of_the_next_one(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A timestamp, so an automation triggers without reading `items`."""
        del account
        state = hass.states.get("sensor.enfant_un_next_cancellation")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 10, 0, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"
        assert state.attributes["end"] == "2026-03-12T11:00:00+01:00"

    async def test_a_cancellation_already_over_is_in_neither_the_state_nor_the_list(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The single filter, checked on both outputs at once.

        The 07:00 slot was cancelled and has finished; reporting it would make
        the entity announce a cancellation nobody can still act on.
        """
        del account
        state = hass.states.get("sensor.enfant_un_next_cancellation")

        assert state is not None
        starts = [item["start"] for item in state.attributes["items"]]
        assert starts == [
            "2026-03-12T10:00:00+01:00",
            "2026-03-12T14:00:00+01:00",
        ]
        assert all("07:00" not in start for start in starts)

    async def test_an_exemption_is_not_reported_as_a_cancellation(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The lesson happens; this child is simply not required at it.

        Announcing it as cancelled would tell a parent the class was called
        off, which is a different fact with a different consequence.
        """
        del account
        state = hass.states.get("sensor.enfant_un_next_cancellation")

        assert state is not None
        subjects = [item["subject"] for item in state.attributes["items"]]
        assert "Sport" not in subjects


class TestWhenTheAfternoonWasCalledOff:
    """Why the child comes home early, without a template.

    `end_of_lessons` answers *when*, cancellations already removed. These two
    attributes answer *why*, and they are two because one would lie: an
    exemption moves the timetabled end just as a cancellation does, so
    `scheduled_end != state` is not "a class was called off". The test for
    that reading is `canceled_after > 0`.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """8-12 held, 14-15 cancelled, 15-17 cancelled."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(14),
                    end=_day(15),
                    place=12,
                    duration=2,
                    subject="Histoire",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-C",
                    start=_day(15),
                    end=_day(17),
                    place=14,
                    duration=4,
                    subject="Anglais",
                    canceled=True,
                ),
            ]
        )
        return client

    async def test_the_state_still_ignores_the_cancelled_lessons(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The attributes are added beside the state, not in place of it.

        An automation that triggers on the homecoming must keep working
        untouched; the new attributes are for the notification it sends.
        """
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 12, 0, tzinfo=PARIS
        )
        assert state.attributes["subject"] == "Mathématiques"

    async def test_the_timetabled_end_and_the_count_of_what_was_dropped(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A count, not a boolean: two cancelled hours is not one."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert state.attributes["scheduled_end"] == "2026-03-12T17:00:00+01:00"
        assert state.attributes["canceled_after"] == 2


class TestWhenTheChildIsMerelyExempted:
    """The case that makes `canceled_after` worth publishing separately.

    A day whose last slot is an exemption has a timetabled end later than its
    state and **nothing** cancelled. A consumer deriving "a class was called
    off" from `scheduled_end != state` would announce a cancellation here, on
    a day the class is being taught -- a claim a parent acts on once and never
    trusts again.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """8-12 held, then 14-16 taught but not required of this child."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(12),
                    place=0,
                    duration=8,
                    subject="Mathématiques",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(14),
                    end=_day(16),
                    place=12,
                    duration=4,
                    subject="Sport",
                    exempted=True,
                ),
            ]
        )
        return client

    async def test_the_timetabled_end_moves_but_nothing_was_cancelled(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """`scheduled_end` differs from the state and `canceled_after` is zero."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert dt_util.parse_datetime(state.state) == datetime(
            2026, 3, 12, 12, 0, tzinfo=PARIS
        )
        assert state.attributes["scheduled_end"] == "2026-03-12T16:00:00+01:00"
        assert state.attributes["canceled_after"] == 0


class TestWhenTheWholeDayIsCancelled:
    """No lesson is attended, so the state is unknown -- and that is not enough.

    Every slot cancelled is exactly the day a parent must be told about, and
    the entity used to publish no attribute at all in that case, leaving the
    fact reachable only through a template over `lessons`. With no retained
    end, every cancellation counts as being after it.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Two slots, both cancelled."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(10),
                    place=0,
                    duration=4,
                    subject="Mathématiques",
                    canceled=True,
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(10),
                    end=_day(12),
                    place=4,
                    duration=4,
                    subject="Histoire",
                    canceled=True,
                ),
            ]
        )
        return client

    async def test_the_day_reports_its_timetable_and_counts_every_slot(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Unknown state, but the two attributes still carry the day."""
        del account
        state = hass.states.get("sensor.enfant_un_end_of_lessons")

        assert state is not None
        assert state.state == "unknown"
        assert state.attributes["scheduled_end"] == "2026-03-12T12:00:00+01:00"
        assert state.attributes["canceled_after"] == 2
        # No lesson closes the day, so there is nothing to name.
        assert "subject" not in state.attributes


class TestTheSubjectColourReachesTheStateMachine:
    """The colour PRONOTE gives a subject, published where a card can read it.

    This class exists because of a chain that was complete except for its last
    metre: the gateway decoded ``CouleurFond`` on four paths, three DTOs
    carried the field, and ``sensor.py`` mentioned it nowhere. Every dashboard
    therefore had to hand-write a colour table to show what PRONOTE already
    knew, and a card asking for the server's colour got nothing -- silently,
    which is the worst form.

    Three tiers and not one, because the field is spelled differently on each
    (``CouleurFond`` on a lesson and on a homework item, ``couleur`` on a
    subject average) and because upstream treats them differently: it resolves
    the homework one *strictly*, which is upstream saying the server always
    sends it there.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One coloured lesson and one uncoloured one, in that order."""
        client = FakeClient(children=CHILDREN)
        client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
            [
                protocol.lesson(
                    identifier="LESSON-A",
                    start=_day(8),
                    end=_day(9),
                    place=0,
                    duration=2,
                    subject="Mathematiques",
                    background_color="#336699",
                ),
                protocol.lesson(
                    identifier="LESSON-B",
                    start=_day(9),
                    end=_day(10),
                    place=2,
                    duration=2,
                    subject="Histoire",
                ),
            ]
        )
        return client

    async def test_a_lesson_publishes_the_colour_the_server_sent(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The requirement, stated as a dashboard experiences it."""
        del account
        lessons = _attributes(hass, "sensor.enfant_un_lessons_today")["lessons"]

        assert lessons[0]["background_color"] == "#336699"

    async def test_a_lesson_without_a_colour_publishes_the_key_holding_none(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The key is always there; only its value can be absent.

        A key that appears and disappears makes every consumer write a
        membership test before a value test, and a template that forgets it
        renders the string ``None`` into a dashboard. Publishing ``None`` says
        "asked, and there is none", which is a different sentence from "this
        integration does not publish colours" -- and the two were confused for
        long enough to cost an afternoon.
        """
        del account
        lessons = _attributes(hass, "sensor.enfant_un_lessons_today")["lessons"]

        assert "background_color" in lessons[1]
        assert lessons[1]["background_color"] is None

    async def test_a_lesson_publishes_the_subject_identifier_beside_the_name(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A card mapping subjects to something of its own needs a stable key.

        The name is a bad one: PRONOTE writes it in capitals, with accents, and
        an establishment can rename it mid-year. The identifier was already on
        the DTO and published nowhere.
        """
        del account
        lessons = _attributes(hass, "sensor.enfant_un_lessons_today")["lessons"]

        assert lessons[0]["subject_id"] == "SUBJECT-MATHS"

    async def test_a_homework_item_publishes_its_colour(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The tier upstream resolves strictly, so the likeliest to carry one.

        The entity id is read from the translated NAME, not guessed from the
        translation key. This test asserted `sensor.enfant_un_homework_todo`
        first -- the key -- and there is no such entity: Home Assistant builds
        the suffix by slugifying the name, so `homework_todo` named "Homework
        to do" becomes `homework_to_do`. The same mistake had just been
        catalogued eleven times over in annexe A, which is what makes it worth
        a comment rather than a silent fix.
        """
        del account
        items = _attributes(hass, "sensor.enfant_un_homework_to_do")["items"]

        assert items
        assert items[0]["background_color"] == "#336699"

    async def test_a_subject_average_publishes_its_colour(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Spelled ``couleur`` upstream, absorbed by the gateway.

        `averages` is named "Subject averages", hence `subject_averages` and
        not `averages`. See the note on the homework test above.
        """
        del account
        items = _attributes(hass, "sensor.enfant_un_subject_averages")["items"]

        assert items
        assert items[0]["background_color"] == "#AA3366"
