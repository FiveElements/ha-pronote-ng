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

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import CHILDREN, REQUIRES_HASS
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
