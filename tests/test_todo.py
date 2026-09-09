"""The to-do list: the only write a user can place by tapping something.

Every other write in this integration goes through a service call, which means
a script, which means somebody wrote it on purpose. Ticking a homework item is
different -- it is a checkbox on a dashboard -- and a checkbox makes three
promises that this module is here to hold.

**It is honest about being read-only.** With write operations off -- the default
(§8.3) -- the entity declares no ``UPDATE_TODO_ITEM`` feature, so the frontend
renders the list as read-only instead of offering a box that refuses when
tapped. The option guard behind it is tested too, because a feature flag keeps
the *interface* honest and only the guard keeps the *account* safe.

**It sends the status and nothing else.** PRONOTE owns the subject, the wording
and the deadline of a homework item. Home Assistant's ``todo.update_item`` is a
partial update over the whole item, so a caller may legitimately pass
``rename`` -- and pushing that back would overwrite a teacher. The happy path
here therefore renames on purpose and checks the wire stayed clean.

**It never lies about having worked.** A tick the limiter postponed, or one
whose item carries no PRONOTE identifier, fails with a message. Silence would
read exactly like success while the establishment saw nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.components.todo import (
    DATA_COMPONENT,
    TodoItem,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.pronote_ng.const import (
    OPT_WRITE_OPERATIONS_ENABLED,
    Priority,
    Tier,
)
from custom_components.pronote_ng.ratelimit import DeferReason, TierDeferred
from custom_components.pronote_ng.todo import PARALLEL_UPDATES, async_setup_entry

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures import protocol
from .fixtures.client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pronote_ng.account import PronoteAccount
    from custom_components.pronote_ng.todo import PronoteHomeworkTodoList

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)

#: Applied to a test that needs the checkbox to actually be tappable. It sets
#: the option **before** the entry is set up rather than flipping it
#: afterwards, and that is not a shortcut: the scheduler's cadence deliberately
#: survives a reload -- an option change must not buy a free collection -- so an
#: entry reloaded under a frozen clock finds no tier due, and every data entity
#: stays unavailable until the next natural interval. Home Assistant then drops
#: unavailable entities from an entity service call, so a list reached that way
#: could not be ticked at all. ``test_turning_writes_on_re_declares_the_feature``
#: covers the runtime flip on its own.
writes_on = pytest.mark.parametrize(
    "writes_enabled", [True], indirect=True, ids=["writes-on"]
)

#: The two items the fake server answers with, chosen so one homework response
#: exercises both shapes the decoder can produce. The second is the interesting
#: one: PRONOTE genuinely returns entries with no ``Matiere`` and no
#: ``descriptif``, and an item that ended up with an empty summary would be
#: unaddressable -- ``todo.update_item`` matches on uid *or summary*.
HOMEWORK_ENTRIES = [
    protocol.homework(
        identifier="HOMEWORK-1",
        subject="Histoire",
        # As PRONOTE sends it. Teachers type into a rich-text editor, so the
        # markup is the normal case and not the exotic one.
        description="<div>Lire le chapitre 4</div>",
        done=False,
    ),
    protocol.homework(
        identifier="HOMEWORK-2",
        subject=None,
        description=None,
        done=True,
    ),
]


@pytest.fixture(name="parent_client")
def parent_client_fixture() -> FakeClient:
    """Override the shared client so every child has the two items above.

    Overridden here rather than in ``conftest.py`` because the shape of the
    homework response is this module's subject and no other module's.
    """
    client = FakeClient(children=CHILDREN)
    client.responses["PageCahierDeTexte"] = protocol.homework_response(HOMEWORK_ENTRIES)
    return client


@pytest.fixture(name="writes_enabled")
def writes_enabled_fixture(request: pytest.FixtureRequest) -> bool:
    """Whether this test's entry permits writing to PRONOTE.

    ``False`` unless a test asks otherwise through the :data:`writes_on`
    marker, because that is the shipped default (§8.3) and the read-only
    behaviour deserves to be what a test gets by accident.
    """
    return bool(getattr(request, "param", False))


@pytest.fixture(name="mock_entry")
def mock_entry_with_write_option(
    hass: HomeAssistant, mock_entry: MockConfigEntry, writes_enabled: bool
) -> MockConfigEntry:
    """Extend the shared entry with the write option already decided.

    Requested by its own name, which is pytest's way of extending a fixture
    rather than replacing it: the entry, its synthetic credentials and the
    widened limiter ceilings all still come from ``conftest.py``.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: writes_enabled},
    )
    return mock_entry


def _list_id(student_name: str) -> str:
    """The entity id of one child's homework list."""
    slug = student_name.lower().replace(" ", "_")
    return f"todo.{slug}_homework"


def _entity(hass: HomeAssistant, entity_id: str) -> PronoteHomeworkTodoList:
    """The live entity object the platform built, by entity id.

    Reached for only where the behaviour under test is genuinely unreachable
    through the service layer: Home Assistant drops unavailable entities from
    an entity service call, and refuses those whose supported features do not
    cover the service. Both of those are states asserted on below.
    """
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None, f"no entity {entity_id}"
    return entity  # type: ignore[return-value]


async def _items(hass: HomeAssistant, entity_id: str) -> list[dict[str, Any]]:
    """One list's items, read back through the real service."""
    response = await hass.services.async_call(
        "todo",
        "get_items",
        {"entity_id": entity_id},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    items: list[dict[str, Any]] = response[entity_id]["items"]
    return items


# ---------------------------------------------------------------------------
# Setting the platform up
# ---------------------------------------------------------------------------


async def test_one_list_per_child_and_not_one_per_account(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """Homework belongs to a child, so a two-child account gets two lists.

    One list holding both children's homework would be unusable: the reminder
    automation every parent writes -- what is left for tomorrow -- could not
    name a child, and ticking an item would be ambiguous.
    """
    assert sorted(hass.states.async_entity_ids("todo")) == sorted(
        _list_id(name) for _child_id, name in CHILDREN
    )


async def test_a_missing_homework_tier_creates_no_list_rather_than_raising(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The platform has to survive an account with no homework coordinator.

    Today every tier gets one, so this guard is defence in depth -- but the
    cost of it being wrong is not local: a platform that raised while being
    forwarded fails the *whole* config entry, taking the timetable, the marks
    and the attendance down with a list nobody asked for.
    """
    without_homework = {
        tier: coordinator
        for tier, coordinator in account.coordinators.items()
        if tier is not Tier.HOMEWORK
    }
    added: list[Any] = []

    def _add(new_entities: Any, *_args: Any, **_kwargs: Any) -> None:
        added.extend(new_entities)

    with patch.object(account, "coordinators", without_homework):
        await async_setup_entry(hass, mock_entry, _add)

    assert added == []


def test_the_platform_writes_one_item_at_a_time() -> None:
    """``PARALLEL_UPDATES`` is the layer Home Assistant itself honours.

    The limiter already serialises the wire, but zero here would let a script
    that ticks six homework items off fire six writes at once -- and six
    concurrent posts from one session is precisely the traffic shape annexe B
    exists to keep this integration from producing.
    """
    assert PARALLEL_UPDATES == 1


# ---------------------------------------------------------------------------
# What the list shows
# ---------------------------------------------------------------------------


async def test_an_item_the_server_left_blank_is_still_addressable(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """A blank subject falls back to ``?`` rather than to an empty summary.

    ``todo.update_item`` matches on the uid *or the summary*, and the frontend
    shows the summary. An item with neither would be a row a user can see and
    cannot tick. The wording is separate: an absent ``descriptif`` becomes
    ``None`` and not ``""``, so the frontend omits the detail line rather than
    rendering an empty one.
    """
    items = await _items(hass, _list_id("Enfant Un"))
    blank = next(item for item in items if item["uid"] == "HOMEWORK-2")

    assert blank["summary"] == "?"
    assert not blank.get("description")


async def test_the_detail_line_is_prose_and_not_the_markup_pronote_sent(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The one place in the integration where a parent reads a teacher's text.

    ``descriptif`` is HTML. The to-do card renders ``description`` as text, so
    forwarding it verbatim put ``<div>`` and ``&#039;`` in front of the reader;
    the frontend would not interpret them, which is the correct behaviour and
    also exactly why they must not be there.
    """
    items = {item["uid"]: item for item in await _items(hass, _list_id("Enfant Un"))}

    assert items["HOMEWORK-1"]["description"] == "Lire le chapitre 4"


async def test_a_ticked_item_reads_as_completed_and_an_open_one_as_needing_action(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The mapping the whole list rests on, in both directions.

    Inverted, every finished homework would show as outstanding: the list would
    never empty, and the count in the state -- which is what a "homework left"
    automation triggers on -- would be permanently wrong.
    """
    items = {item["uid"]: item for item in await _items(hass, _list_id("Enfant Un"))}

    assert items["HOMEWORK-1"]["status"] == TodoItemStatus.NEEDS_ACTION
    assert items["HOMEWORK-2"]["status"] == TodoItemStatus.COMPLETED
    # The state is the number of items still needing action, so the mapping is
    # observable from an automation without reading the list at all.
    state = hass.states.get(_list_id("Enfant Un"))
    assert state is not None
    assert state.state == "1"


async def test_the_list_is_unknown_and_not_empty_before_any_collection(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """Nothing collected yet must not be reported as no homework.

    An empty list is an answer: it would tell the reminder automation there is
    nothing to do for tomorrow, on the evening the integration happened to
    start up or to lose its snapshot. ``None`` is not an answer, which is why
    the entity is ``unavailable`` in that window instead.

    Read off the entity rather than through ``todo.get_items``, because Home
    Assistant drops unavailable entities from an entity service call and the
    service therefore cannot observe this state at all.
    """
    entity_id = _list_id("Enfant Un")
    entity = _entity(hass, entity_id)
    assert entity.todo_items is not None

    account.coordinators[Tier.HOMEWORK].async_set_updated_data({})
    await hass.async_block_till_done()

    assert entity.todo_items is None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unavailable"


# ---------------------------------------------------------------------------
# The checkbox is offered only when writes are on (§8.3)
# ---------------------------------------------------------------------------


async def test_the_list_is_visibly_read_only_while_writes_are_off(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """Declared through the supported features, so the frontend shows no box.

    A checkbox that appears tappable and then refuses is worse than one that is
    plainly absent: the user cannot tell a disabled option from a broken
    integration. Home Assistant enforces the same declaration on the service,
    which is what the second half checks -- and nothing reaches the wire.
    """
    entity_id = _list_id("Enfant Un")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["supported_features"] == TodoListEntityFeature(0)

    before = len(parent_client.posts)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": entity_id, "item": "HOMEWORK-1", "status": "completed"},
            blocking=True,
        )

    assert len(parent_client.posts) == before


async def test_a_tick_is_refused_by_the_option_and_not_only_by_the_feature_flag(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """Defence in depth, and the reason the guard is not redundant.

    The supported features keep the *interface* honest; they are advisory, and
    anything speaking to the entity directly -- a websocket client, another
    integration, a future change to core's service layer -- sails past them.
    The option check is what keeps the *account* safe, so it is asserted on its
    own, with the translation key the user actually reads.
    """
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await _entity(hass, _list_id("Enfant Un")).async_update_todo_item(
            TodoItem(
                uid="HOMEWORK-1", summary="Histoire", status=TodoItemStatus.COMPLETED
            )
        )

    assert raised.value.translation_key == "writes_disabled"
    assert len(parent_client.posts) == before


async def test_turning_writes_on_re_declares_the_feature(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The option reloads the entry, so the checkbox appears (§7.3).

    Without the reload a user would turn writing on and find the list still
    read-only until Home Assistant restarted, which reads as the option having
    no effect at all. Asserted for both children, because the feature is
    declared per entity in ``__init__`` and one list left behind would be one
    child whose homework could not be ticked.
    """
    for _child_id, name in CHILDREN:
        state = hass.states.get(_list_id(name))
        assert state is not None
        assert state.attributes["supported_features"] == TodoListEntityFeature(0)

    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()

    for _child_id, name in CHILDREN:
        state = hass.states.get(_list_id(name))
        assert state is not None
        assert (
            state.attributes["supported_features"]
            & TodoListEntityFeature.UPDATE_TODO_ITEM
        )


# ---------------------------------------------------------------------------
# Ticking one off
# ---------------------------------------------------------------------------


@writes_on
async def test_an_item_with_no_pronote_identifier_is_refused_rather_than_guessed(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """``uid`` is the ``N`` the write is addressed to, and there is no fallback.

    Sent without one, ``SaisieTAFFaitEleve`` would carry ``{"N": None}``: the
    server accepts the request, ticks nothing, and the request is spent. An
    error naming the problem is the only outcome that does not cost a call and
    then lie about it.
    """
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await _entity(hass, _list_id("Enfant Un")).async_update_todo_item(
            TodoItem(uid=None, summary="Histoire", status=TodoItemStatus.COMPLETED)
        )

    assert raised.value.translation_key == "todo_item_unknown"
    assert len(parent_client.posts) == before


@writes_on
async def test_ticking_an_item_sends_the_status_and_never_the_wording(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """PRONOTE owns the subject, the wording and the deadline of a homework.

    ``todo.update_item`` is a partial update over the whole item, so a caller
    may pass ``rename`` -- the frontend offers it, and core validates it
    against no extra feature. This call renames on purpose: a client that
    pushed the summary back would overwrite what a teacher wrote, and the only
    symptom would be a homework diary slowly filling with a parent's own
    phrasing.
    """
    entity_id = _list_id("Enfant Un")

    await hass.services.async_call(
        "todo",
        "update_item",
        {
            "entity_id": entity_id,
            "item": "HOMEWORK-1",
            "rename": "Ce que le parent en pense",
            "status": "completed",
        },
        blocking=True,
    )

    assert parent_client.body_for("SaisieTAFFaitEleve") == {
        "listeTAF": [{"N": "HOMEWORK-1", "TAFFait": True}]
    }
    # And the rename did not reach the list either: the summary still comes
    # from the snapshot, which is to say from the establishment.
    items = {item["uid"]: item for item in await _items(hass, entity_id)}
    assert items["HOMEWORK-1"]["summary"] == "Histoire"


@writes_on
async def test_unticking_an_item_sends_the_negative_and_not_nothing(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """Marking a finished homework unfinished again is a write of its own.

    Treating ``needs_action`` as "no change" would make the checkbox one-way: a
    mis-tap could never be corrected from Home Assistant, and the child's own
    diary would go on showing work as done that is not.
    """
    await hass.services.async_call(
        "todo",
        "update_item",
        {
            "entity_id": _list_id("Enfant Un"),
            "item": "HOMEWORK-2",
            "status": "needs_action",
        },
        blocking=True,
    )

    assert parent_client.body_for("SaisieTAFFaitEleve") == {
        "listeTAF": [{"N": "HOMEWORK-2", "TAFFait": False}]
    }


@writes_on
async def test_a_tick_is_billed_to_the_right_child_at_high_priority(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """Four things about the call, each with its own failure mode.

    It goes through the session manager at all -- a write placed beside the
    limiter is a write outside the budget that protects the account. It is
    billed to the ``homework`` tier and costs one, because a write that
    under-reports corrupts the very budget it spends. It is ``HIGH`` and never
    ``CRITICAL``: a human tapping a checkbox outranks a scheduled collection,
    but nothing done by hand may pre-empt the session tier (annexe B §2.4). And
    it names the child, because ``ParentClient`` keeps the first child selected
    by default -- a write that forgot to say which child would tick the
    *sibling's* homework and raise nothing at all.
    """
    recorded: list[dict[str, Any]] = []
    real_run = account.session.run

    async def _recording_run(
        tier: str,
        priority: Priority,
        fn: Callable[[Any], Any],
        *,
        student_id: str | None = None,
        cost: int = 1,
    ) -> Any:
        recorded.append(
            {
                "tier": tier,
                "priority": priority,
                "student_id": student_id,
                "cost": cost,
            }
        )
        return await real_run(tier, priority, fn, student_id=student_id, cost=cost)

    mark = len(parent_client.journal)
    with patch.object(account.session, "run", _recording_run):
        await hass.services.async_call(
            "todo",
            "update_item",
            {
                "entity_id": _list_id("Enfant Deux"),
                "item": "HOMEWORK-1",
                "status": "completed",
            },
            blocking=True,
        )

    assert recorded == [
        {
            "tier": str(Tier.HOMEWORK),
            "priority": Priority.HIGH,
            "student_id": STUDENT_TWO,
            "cost": 1,
        }
    ]
    # The selection is only observable in the *order*, so it is asserted there:
    # in two separate ledgers a `set_child` placed after the post would look
    # exactly like one placed before it.
    assert parent_client.journal[mark:] == [
        ("select", STUDENT_TWO),
        ("post", "SaisieTAFFaitEleve"),
    ]


@writes_on
async def test_a_tick_asks_for_a_re_read_instead_of_paying_for_one(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The local truth is already correct, so the checkbox costs one request.

    Re-reading the homework tier on the spot would double the price of every
    tap, for information the caller already has. The tier is boosted so the
    next scheduled collection picks the item up, and the state is written
    locally in the meantime.
    """
    reads_before = parent_client.posted_names.count("PageCahierDeTexte")

    await hass.services.async_call(
        "todo",
        "update_item",
        {
            "entity_id": _list_id("Enfant Un"),
            "item": "HOMEWORK-1",
            "status": "completed",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert parent_client.posted_names.count("PageCahierDeTexte") == reads_before
    assert account.scheduler.diagnostics()[str(Tier.HOMEWORK)]["boosted"] is True


@writes_on
async def test_a_tick_that_was_deferred_fails_visibly(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """A postponed tick reads exactly like a tick that did not work.

    ``TierDeferred`` is a control-flow signal, which is right for a scheduled
    collection -- it keeps its snapshot and comes back later -- and wrong for a
    gesture: the box would spring back with no explanation. It is translated
    into an error carrying the reason and roughly how long to wait, and nothing
    is boosted, because nothing happened.
    """

    async def _deferred(*_args: Any, **_kwargs: Any) -> Any:
        raise TierDeferred(DeferReason.DAILY_CAP, 42.7)

    with (
        patch.object(account.session, "run", _deferred),
        pytest.raises(ServiceValidationError) as raised,
    ):
        await hass.services.async_call(
            "todo",
            "update_item",
            {
                "entity_id": _list_id("Enfant Un"),
                "item": "HOMEWORK-1",
                "status": "completed",
            },
            blocking=True,
        )

    assert raised.value.translation_key == "service_deferred"
    assert raised.value.translation_placeholders == {
        "reason": "daily_cap",
        # Whole seconds: "try again in about 42 seconds" is advice, and 42.7
        # would read as a measurement.
        "seconds": "42",
    }
    assert "SaisieTAFFaitEleve" not in parent_client.posted_names
    assert account.scheduler.diagnostics()[str(Tier.HOMEWORK)]["boosted"] is False
