"""The gateway, to 100 %.

The gateway is the only place ``pronotepy`` is touched, so it is the only place
a protocol change can be absorbed -- and the only place a decoding bug can
silently empty an entity. Its coverage gate is total for that reason.

Two rules are tested over and over here, because they were each broken in
several different places:

* **per-item tolerance.** One undecodable entry costs *that entry*, never the
  whole tier. Upstream builds its lists in comprehensions with strict
  resolvers, so a single item missing an optional field used to fail
  everything.
* **an absent collection is not an empty one.** A renamed top-level key must
  fail the tier, so the previous snapshot is kept and staleness eventually
  says so. Returning ``[]`` reported *success* with no data, replaced the
  snapshot, and nothing in the log ever mentioned it.

No network, no Home Assistant: the fake client answers from hand-written
payloads. See ``tests/fixtures/`` for why none of them came off a real server.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.pronote_ng.const import GradeStatus
from custom_components.pronote_ng.gateway import (
    MAX_DISCUSSION_EXPANSIONS,
    DiscussionIsClosed,
    DiscussionNotFound,
    PronoteGateway,
    ProtocolChanged,
    RecipientNotFound,
    _food_names,
    _get,
    _grade_sentinel,
    _join_phone,
    _list,
    _number,
    _parse_date,
    _parse_datetime,
    _required_list,
    _shape,
    _strings,
    _supersedes,
    deduplicate_lessons,
)
from custom_components.pronote_ng.models import Lesson

from .clock import FakeClock  # noqa: TC001 -- a pytest fixture annotation
from .fixtures import protocol
from .fixtures.client import (
    FakeClient,
    FakeMessage,
    FakeRecipient,
    FakeThread,
)

PARIS = ZoneInfo("Europe/Paris")


def current_period(gateway: PronoteGateway, client: FakeClient) -> object:
    """The period the session facts name as current."""
    period = gateway.session_facts(client).facts.current_period
    assert period is not None
    return period


def a_lesson(**overrides: object) -> Lesson:
    """A decoded lesson, for the module-level helpers."""
    defaults: dict[str, object] = {
        "id": "LESSON-1",
        "subject": "Mathématiques",
        "subject_id": "SUBJECT-MATHS",
        "teachers": ("Prof. Un",),
        "classrooms": ("Salle 101",),
        "groups": (),
        "start": dt.datetime(2026, 3, 12, 8, 0, tzinfo=PARIS),
        "end": dt.datetime(2026, 3, 12, 9, 0, tzinfo=PARIS),
        "canceled": False,
        "status": None,
        "detention": False,
        "outing": False,
        "exempted": False,
        "test": False,
        "memo": None,
        "background_color": None,
        "virtual_classrooms": (),
        "num": 0,
        "place": 0,
        "duration": 2,
        "end_inferred": False,
    }
    defaults.update(overrides)
    return Lesson(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The raw-JSON helpers
# ---------------------------------------------------------------------------


def test_get_walks_a_path_and_never_raises() -> None:
    """An absent field yields ``None``, never an exception.

    Class averages, minima, maxima and coefficients are *legitimately* absent
    when the establishment does not publish them, so a walk that raised would
    make the normal case an error.
    """
    assert _get({"a": {"b": "c"}}, "a", "b") == "c"
    assert _get({"a": {"b": "c"}}, "a", "z") is None
    assert _get({"a": "not-a-dict"}, "a", "b") is None
    assert _get(None, "a") is None


def test_list_reads_the_v_wrapper_or_gives_up_quietly() -> None:
    """``{"V": [...]}`` is the shape; anything else is an empty list."""
    assert _list({"a": {"V": [1, 2]}}, "a") == [1, 2]
    assert _list({"a": [1, 2]}, "a") == [1, 2]
    assert _list({"a": {"V": None}}, "a") == []
    assert _list({}, "a") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, "absent", id="a key that is not there"),
        pytest.param({}, "an empty mapping", id="a section that is there and empty"),
        pytest.param(
            {"b": 1, "a": 2},
            "a mapping of ['a', 'b']",
            id="a section carrying other keys, named and sorted",
        ),
        pytest.param([1, 2, 3], "a list of 3", id="a collection, counted not printed"),
        pytest.param("Prof. Secret", "a str", id="a value, described by type alone"),
    ],
)
def test_a_shape_describes_without_disclosing(value: Any, expected: str) -> None:
    """Every branch, because each answers a different maintenance question.

    "absent" says the path is wrong and the protocol changed; "an empty
    mapping" says this establishment publishes nothing there; a named key set
    says what it publishes *instead*, which is what turns "the protocol
    changed" into "the protocol changed to this". A count and a type name are
    for the cases where the content itself is a child's school record: this
    text lands in the file users attach to public issues (§8.2), so the last
    case asserts the *value* never appears -- only its type.
    """
    assert _shape(value) == expected


def test_a_required_list_refuses_to_be_empty_when_its_key_is_absent() -> None:
    """The worst failure mode this gateway has, and it made no noise at all.

    If PRONOTE renamed ``ListeCours``, ``_get(...) or []`` returned an empty
    timetable, the tier reported **success**, the snapshot was replaced,
    ``binary_sensor.<eleve>_jour_de_classe`` read ``off``, and the wake-up
    automation simply stopped firing. Staleness never triggered either, because
    the collection had succeeded.
    """
    with pytest.raises(ProtocolChanged) as raised:
        _required_list({"dataSec": {"data": {}}}, "dataSec", "data", "X", what="things")

    assert raised.value.what == "things"
    assert raised.value.path == "dataSec.data.X"


def test_a_required_list_accepts_a_genuinely_empty_collection() -> None:
    """A quiet school week is a real answer, and it is not an error."""
    assert _required_list({"a": {"V": []}}, "a", what="things") == []
    assert _required_list({"a": {"V": None}}, "a", what="things") == []
    assert _required_list({"a": [1]}, "a", what="things") == [1]


def test_a_required_list_refuses_a_value_that_is_not_a_list() -> None:
    """A key that survived a redesign holding a different type."""
    with pytest.raises(ProtocolChanged):
        _required_list({"a": "text"}, "a", what="things")


def test_numbers_arrive_with_a_comma_and_sometimes_are_not_numbers() -> None:
    """``"14,5"`` is a number; ``"|1"`` is a sentinel; ``""`` is neither."""
    assert _number("14,5") == 14.5
    assert _number(12) == 12.0
    assert _number(1.5) == 1.5
    assert _number("|1") is None
    assert _number("") is None
    assert _number("   ") is None
    assert _number("abc") is None
    assert _number(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("|1", GradeStatus.ABSENT),
        ("|8", GradeStatus.CONGRATULATIONS),
        ("|9", GradeStatus.UNKNOWN),
        ("|", GradeStatus.UNKNOWN),
        ("14,5", None),
        (None, None),
    ],
)
def test_a_future_ninth_sentinel_does_not_fail_every_grade(
    raw: str | None, expected: GradeStatus | None
) -> None:
    """Upstream's table has eight entries and is indexed by ``int(s[1]) - 1``.

    A ``|9`` therefore raises ``IndexError`` there and takes down every grade
    in the batch. ``UNKNOWN`` is how this integration declines to inherit that:
    one unrecognised sentinel costs one grade's *status*, and nothing else.
    """
    assert _grade_sentinel(raw) is expected


def test_labels_are_pulled_out_of_record_lists() -> None:
    """Both shapes appear in the protocol: ``{"L": …}`` and a bare string."""
    assert _strings([{"L": "a"}, {"L": ""}, {"X": "b"}, "c", ""]) == ("a", "c")


def test_a_phone_number_tolerates_either_half_missing() -> None:
    """A guardian record with an indicator and no number, and vice versa."""
    assert _join_phone("33", "600000000") == "+33600000000"
    # Not "+600000000": upstream builds exactly that, and it looks
    # international without being dialable.
    assert _join_phone(None, "600000000") == "600000000"
    assert _join_phone("33", None) is None
    assert _join_phone("33", "") is None


def test_food_names_tolerate_an_absent_course() -> None:
    """``other_meal`` is routinely ``None``, and a dish may have no name."""

    class _Food:
        def __init__(self, name: str | None) -> None:
            self.name = name

    assert _food_names(None) == ()
    assert _food_names([]) == ()
    assert _food_names([_Food("Riz"), _Food(None)]) == ("Riz",)


def test_dates_are_parsed_without_raising() -> None:
    """``Util.date_parse`` raises on an unknown form; a tier must not."""
    assert _parse_date("12/03/2026") == dt.date(2026, 3, 12)
    assert _parse_date("not a date") is None
    assert _parse_date(None) is None
    assert _parse_datetime("12/03/2026 08:00:00") == dt.datetime(2026, 3, 12, 8, 0)
    assert _parse_datetime("not a date") is None
    assert _parse_datetime(None) is None


# ---------------------------------------------------------------------------
# Time, in the establishment's zone
# ---------------------------------------------------------------------------


def test_the_gateway_reads_the_real_clock_when_given_none() -> None:
    """The seam is optional: the running integration passes nothing."""
    gateway = PronoteGateway("Europe/Paris")
    assert gateway.now().tzinfo is PARIS
    assert gateway.today() == gateway.now().date()


def test_the_gateway_works_in_the_establishment_s_timezone(
    gateway: PronoteGateway,
) -> None:
    """Never ``date.today()``: that reads the host's zone, UTC on a container.

    The consequence is not subtle. A container in UTC asked for "today" at
    00:30 Paris time and got *yesterday*, so the timetable, the menus and the
    homework horizon were all one day behind for the first hour of every day.
    """
    assert gateway.timezone is PARIS
    assert gateway.now().tzinfo is PARIS
    assert gateway.today() == gateway.now().date()


def test_naive_protocol_values_are_given_the_establishment_s_zone(
    gateway: PronoteGateway,
) -> None:
    """PRONOTE sends local wall-clock times with no offset (§4.2)."""
    aware = gateway._aware(dt.datetime(2026, 3, 12, 8, 0))
    assert aware is not None
    assert aware.tzinfo is PARIS


def test_an_already_aware_value_is_converted_rather_than_relabelled(
    gateway: PronoteGateway,
) -> None:
    """Relabelling would move the instant; converting preserves it."""
    aware = gateway._aware(dt.datetime(2026, 3, 12, 7, 0, tzinfo=dt.UTC))
    assert aware == dt.datetime(2026, 3, 12, 8, 0, tzinfo=PARIS)


def test_a_plain_date_becomes_midnight_local(gateway: PronoteGateway) -> None:
    """So a calendar can compare an all-day event against a timed one."""
    assert gateway._aware(dt.date(2026, 3, 12)) == dt.datetime(
        2026, 3, 12, 0, 0, tzinfo=PARIS
    )
    assert gateway._aware(None) is None


# ---------------------------------------------------------------------------
# Session facts -- zero calls
# ---------------------------------------------------------------------------


def test_the_session_facts_cost_nothing(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Everything here is already in memory once authenticated.

    Specification v1 filed the child, the class, the establishment and the
    period list under the ``static`` tier and budgeted requests for them. They
    come out of ``func_options`` and ``parametres_utilisateur`` -- no call at
    all.
    """
    result = gateway.session_facts(client)

    assert result.calls == 0
    assert client.posts == []
    assert result.facts.student.id == protocol.STUDENT_ID
    assert result.facts.student.class_name == "4e A"
    assert result.facts.student.has_photo
    assert len(result.facts.periods) == 2


def test_the_current_period_is_never_guessed(
    gateway: PronoteGateway,
) -> None:
    """``Client.current_period`` falls back to ``onglets[0]``; we prefer ``None``.

    In an establishment that does not publish grades that fallback silently
    names the *wrong* period -- so grades, absences and the report card were
    all collected for a period that had ended. An unavailable entity is a
    better answer than a confidently wrong one (§3.3.3).
    """
    client = FakeClient(marks_tab=False)
    assert gateway.session_facts(client).facts.current_period is None


def test_a_marks_tab_with_no_default_period_yields_none(
    gateway: PronoteGateway,
) -> None:
    """The tab exists but names no period -- still not a reason to guess."""
    client = FakeClient()
    tabs = client.parametres_utilisateur["dataSec"]["data"]["ressource"][
        "listeOngletsPourPeriodes"
    ]["V"]
    tabs[0].pop("periodeParDefaut")
    assert gateway.session_facts(client).facts.current_period is None


def test_a_default_period_not_in_the_list_yields_none(
    gateway: PronoteGateway,
) -> None:
    """A period identifier that matches nothing is not a period."""
    client = FakeClient()
    tabs = client.parametres_utilisateur["dataSec"]["data"]["ressource"][
        "listeOngletsPourPeriodes"
    ]["V"]
    tabs[0]["periodeParDefaut"] = {"V": {"N": "PERIOD-NOWHERE"}}
    assert gateway.session_facts(client).facts.current_period is None


def test_a_period_with_unparseable_dates_is_skipped(
    gateway: PronoteGateway,
) -> None:
    """One bad period costs that period, not the whole period list."""
    periods = protocol.default_periods()
    periods[0]["dateDebut"] = {"V": "not a date"}
    client = FakeClient()
    client.func_options = protocol.func_options(periods=periods)

    facts = gateway.session_facts(client).facts
    assert [period.name for period in facts.periods] == ["Trimestre 2"]


def test_periods_are_indexed_from_one(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The index is what suffixes the history sensors, so it must be stable."""
    facts = gateway.session_facts(client).facts
    assert [period.index for period in facts.periods] == [1, 2]


def test_an_absent_period_list_yields_no_periods(gateway: PronoteGateway) -> None:
    """Tolerated rather than fatal: the session tier costs nothing to retry."""
    client = FakeClient()
    client.func_options["dataSec"]["data"]["General"].pop("ListePeriodes")
    assert gateway.session_facts(client).facts.periods == ()


# ---------------------------------------------------------------------------
# The timetable
# ---------------------------------------------------------------------------


def test_the_timetable_costs_one_request_per_week(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Client.lessons()`` bills by week, so "today and tomorrow" is one post.

    Which is why the separate week tier was folded in: asking for the whole
    week costs exactly the same request as asking for two days (§5.2).
    """
    result = gateway.timetable(client, include_next_week=False)
    assert result.calls == 1
    assert client.posted_names == ["PageEmploiDuTemps"]


def test_next_week_costs_nothing_extra_in_mid_week(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """On a Thursday, tomorrow is in the same week: still one post."""
    result = gateway.timetable(client, include_next_week=True)

    assert result.calls == 1
    assert client.posted_names == ["PageEmploiDuTemps"]


def test_next_week_is_fetched_at_a_boundary_crossing(
    clock: FakeClock, client: FakeClient
) -> None:
    """This is the one day in seven annexe B budgets 1.14 requests for.

    ``get_week`` counts whole weeks from ``PremierLundi``, so a Sunday and the
    Monday after it fall in different weeks -- and the timetable then costs two
    posts rather than one. It is the only reason the tier is not budgeted at a
    flat 1.
    """
    clock.set_wall(dt.datetime(2026, 3, 15, 18, 0, tzinfo=PARIS))
    gateway = PronoteGateway("Europe/Paris", clock=clock.now)

    result = gateway.timetable(client, include_next_week=True)

    assert result.calls == 2
    assert client.posted_names == ["PageEmploiDuTemps", "PageEmploiDuTemps"]
    assert result.facts.weeks_fetched == (
        client.get_week(dt.date(2026, 3, 15)),
        client.get_week(dt.date(2026, 3, 16)),
    )


def test_a_renamed_timetable_key_fails_the_tier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Rather than reporting an empty week, which reads as "no school"."""
    client.responses["PageEmploiDuTemps"] = {"dataSec": {"data": {}}}
    with pytest.raises(ProtocolChanged):
        gateway.timetable(client, include_next_week=False)


def test_a_lesson_carries_its_slot_coordinates(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``place``, ``duree`` and whether ``end`` was supplied or inferred.

    None of the three survive ``pronotepy.Lesson``, which is why the timetable
    is decoded from raw posts rather than through ``client.lessons()``.
    """
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [protocol.lesson(place=3, duration=2)]
    )
    lesson = gateway.timetable(client, include_next_week=False).facts.lessons[0]

    assert lesson.place == 3
    assert lesson.duration == 2
    assert lesson.end_inferred is True


def test_a_supplied_end_is_not_flagged_as_inferred(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``end_inferred`` is what lets an entity stay silent about a doubtful time."""
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [
            protocol.lesson(
                start=dt.datetime(2026, 3, 12, 8, 0),
                end=dt.datetime(2026, 3, 12, 9, 0),
            )
        ]
    )
    lesson = gateway.timetable(client, include_next_week=False).facts.lessons[0]

    assert lesson.end_inferred is False
    assert lesson.end == dt.datetime(2026, 3, 12, 9, 0, tzinfo=PARIS)


def test_an_inverted_interval_is_refused(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Upstream's inferred end wraps at the last slots of the day.

    ``place % (len(end_times) - 1) + duree - 1`` fed through ``place2time`` a
    second time -- under upstream's own comment "might be wrong... works with
    demo" -- so a 17:00 lesson came back ending at 09:00. A calendar draws that
    as a broken event, ``binary_sensor.<eleve>_en_cours`` can never match it,
    and ``sensor.<eleve>_fin_des_cours`` is wrong for the whole day.
    """
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [protocol.lesson(start=dt.datetime(2026, 3, 12, 17, 0), place=8, duration=2)]
    )
    lesson = gateway.timetable(client, include_next_week=False).facts.lessons[0]

    assert lesson.end > lesson.start


def test_an_end_the_guard_had_to_invent_is_flagged_even_when_one_was_sent(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A fabricated hour must never be declared reliable.

    ``end_inferred`` used to be exactly ``DateDuCoursFin is None``, while
    ``_sane_end`` replaces any end that is not after the start -- whatever the
    field said. So a server publishing a *present but impossible* end got
    ``start + 1h`` with the flag still ``False``: an hour this gateway made up,
    announced as the server's own, and announced so precisely where the data
    is least trustworthy. Every entity that stays silent on a doubtful end
    reads that flag, so they would all have asserted this one.

    Whether a real server sends an inverted end is unknown, and the test does
    not depend on it: the guard's docstring asserted this could not happen,
    nothing enforced it, and that gap is what is closed here.
    """
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [
            protocol.lesson(
                start=dt.datetime(2026, 3, 12, 10, 0),
                end=dt.datetime(2026, 3, 12, 9, 0),
            )
        ]
    )
    lesson = gateway.timetable(client, include_next_week=False).facts.lessons[0]

    assert lesson.end == dt.datetime(2026, 3, 12, 11, 0, tzinfo=PARIS)
    assert lesson.end_inferred is True


def test_a_slot_with_no_published_content_still_decodes(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``ListeContenus`` absent is how a cancellation often arrives.

    ``Lesson.__init__`` raises ``ParsingError`` on it, so dropping the entry
    meant the one event this integration exists to emit --
    ``event.<eleve>_cours_modifie`` with ``lesson_canceled`` -- could not fire
    for those slots at all.
    """
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [protocol.lesson(canceled=True, status="Prof. absent", with_contents=False)]
    )
    lessons = gateway.timetable(client, include_next_week=False).facts.lessons

    assert len(lessons) == 1
    assert lessons[0].canceled is True
    assert lessons[0].status == "Prof. absent"
    # The content record is genuinely absent, so these are empty rather than
    # invented.
    assert lessons[0].subject is None
    assert lessons[0].teachers == ()


def test_the_raw_fallback_uses_a_supplied_end_when_there_is_one(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """And says so, rather than flagging a real end as inferred."""
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [
            protocol.lesson(
                start=dt.datetime(2026, 3, 12, 8, 0),
                end=dt.datetime(2026, 3, 12, 10, 0),
                with_contents=False,
            )
        ]
    )
    lesson = gateway.timetable(client, include_next_week=False).facts.lessons[0]

    assert lesson.end == dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)
    assert lesson.end_inferred is False


def test_a_lesson_with_no_identifier_or_no_start_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The two fields without which a slot cannot be keyed or placed."""
    no_id = protocol.lesson(with_contents=False)
    no_id.pop("N")
    no_start = protocol.lesson(identifier="LESSON-2", with_contents=False)
    no_start["DateDuCours"] = {"V": "not a date"}

    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [no_id, no_start, protocol.lesson(identifier="LESSON-3")]
    )
    lessons = gateway.timetable(client, include_next_week=False).facts.lessons

    assert [lesson.id for lesson in lessons] == ["LESSON-3"]


def test_a_null_inside_a_path_does_not_fail_the_tier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``TypeError``, not ``KeyError``, and upstream converts only the latter.

    ``_resolver`` walks the path inside ``try: … except KeyError``, so a segment
    holding ``null`` -- ``{"cahierDeTextes": {"V": null}}`` is a real response
    -- raises ``TypeError`` straight through it. One such entry used to fail
    the whole tier.
    """
    entry = protocol.lesson()
    entry["cahierDeTextes"] = {"V": None}

    client.responses["PageEmploiDuTemps"] = protocol.timetable_response([entry])
    lessons = gateway.timetable(client, include_next_week=False).facts.lessons

    assert len(lessons) == 1


def test_the_timetable_hands_out_both_the_deduplicated_and_the_raw_week(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Two lists answering two different questions.

    De-duplication asks *what does PRONOTE display*; change detection asks
    *what moved*, and the entry de-duplication discards is often the answer.
    """
    client.responses["PageEmploiDuTemps"] = protocol.timetable_response(
        [
            protocol.lesson(identifier="LESSON-1", place=0, num=0, canceled=True),
            protocol.lesson(identifier="LESSON-2", place=0, num=1),
        ]
    )
    facts = gateway.timetable(client, include_next_week=False).facts

    assert [lesson.id for lesson in facts.lessons] == ["LESSON-2"]
    assert {lesson.id for lesson in facts.all_lessons} == {"LESSON-1", "LESSON-2"}


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


def test_the_biggest_num_wins() -> None:
    """Upstream states the rule: "the biggest num is the one shown on pronote"."""
    kept = deduplicate_lessons(
        [a_lesson(id="A", num=0), a_lesson(id="B", num=2), a_lesson(id="C", num=1)]
    )
    assert [lesson.id for lesson in kept] == ["B"]


def test_a_num_tie_is_broken_deliberately_not_by_response_order() -> None:
    """``num`` is ``P``, which defaults to 0 when absent.

    Two content-less entries on one slot -- an outing and a detention, say --
    therefore both scored 0, and the second was dropped in silence: the outing
    vanished from ``binary_sensor.<eleve>_sortie_pedagogique`` and from the
    calendar with it. On a tie the entry that is not cancelled wins, because a
    replacement is what PRONOTE displays.
    """
    kept = deduplicate_lessons(
        [a_lesson(id="cancelled", num=0, canceled=True), a_lesson(id="live", num=0)]
    )
    assert [lesson.id for lesson in kept] == ["live"]


def test_a_tie_between_two_live_entries_prefers_the_informative_one() -> None:
    """Failing everything else, the one that names a subject."""
    kept = deduplicate_lessons(
        [a_lesson(id="bare", subject=None, subject_id=None), a_lesson(id="named")]
    )
    assert [lesson.id for lesson in kept] == ["named"]


def test_a_tie_between_two_equal_entries_keeps_the_first() -> None:
    """Nothing distinguishes them, so nothing has to."""
    assert not _supersedes(a_lesson(id="b"), a_lesson(id="a"))


def test_a_substitution_is_one_lesson_not_two() -> None:
    """The key is the *slot*, and a slot is a coordinate on the grid.

    Including ``subject_id`` defeated the de-duplication it exists for: a
    substitution is two entries on one slot with different subjects, so keyed
    with the subject they landed in separate buckets and both survived. The day
    was then counted twice, drawn twice in the calendar, and "next lesson"
    could latch onto the superseded one.
    """
    kept = deduplicate_lessons(
        [
            a_lesson(id="original", subject="Mathématiques", subject_id="M", num=0),
            a_lesson(id="replacement", subject="Anglais", subject_id="A", num=1),
        ]
    )
    assert [lesson.id for lesson in kept] == ["replacement"]


def test_deduplication_keeps_distinct_slots_and_sorts_them() -> None:
    """Different day or different place is a different slot."""
    kept = deduplicate_lessons(
        [
            a_lesson(
                id="later",
                place=2,
                start=dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS),
            ),
            a_lesson(id="earlier", place=0),
        ]
    )
    assert [lesson.id for lesson in kept] == ["earlier", "later"]


# ---------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------


def test_homework_is_asked_for_from_the_start_of_the_year(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Not from today, and this was a silent, structural defect.

    Upstream filters its own response with ``date_from <= hw.date <= date_to``
    and builds the week domain from ``get_week(date_from)``, so passing today
    discarded every *overdue* assignment. ``binary_sensor.devoirs_en_retard``
    was therefore incapable of ever being ``on`` and its ``count`` permanently
    zero -- while the docstring claimed the opposite. The span costs nothing:
    it is one week-range post either way.
    """
    gateway.homework(client)
    domain = client.body_for("PageCahierDeTexte")["domaine"]["V"]

    assert domain == f"[1..{client.get_week(dt.date(2026, 7, 4))}]"


def test_homework_falls_back_to_a_full_year_with_no_last_date(
    gateway: PronoteGateway,
) -> None:
    """A bounded guess beats no homework at all.

    ``Client.homework`` would ``strptime`` ``DerniereDate`` unguarded, so an
    unexpected form raised ``ValueError`` from the middle of the tier.
    """
    client = FakeClient(last_date=None)
    gateway.homework(client)
    assert client.body_for("PageCahierDeTexte")["domaine"]["V"] == "[1..62]"


def test_one_undecodable_homework_item_costs_that_item(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Upstream resolves ``TAFFait``, ``descriptif`` and ``Matiere`` strictly.

    So a single entry served without one of them raised ``ParsingError`` from
    inside a list comprehension and failed the **entire** tier -- taking the
    to-do list, the calendar and both homework sensors with it.
    """
    client.responses["PageCahierDeTexte"] = protocol.homework_response(
        [
            protocol.homework(identifier="HOMEWORK-1", done=None, subject=None),
            protocol.homework(identifier="HOMEWORK-2", description=None),
            protocol.homework(identifier="HOMEWORK-3"),
        ]
    )
    items = gateway.homework(client).facts.homework

    assert [item.id for item in items] == [
        "HOMEWORK-1",
        "HOMEWORK-2",
        "HOMEWORK-3",
    ]
    assert items[0].done is False
    assert items[0].subject is None
    assert items[1].description == ""


def test_a_homework_description_is_published_as_prose_as_well_as_html(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``descriptif`` is a rich-text field, and a card can use neither end raw.

    Teachers type into an editor, so a description arrives as markup with
    character entities. Injecting that into a dashboard would turn every
    teacher's text box into an XSS route, and printing it makes the parent read
    the tags. Stripping it in each consumer would be three strippers and three
    answers to ``&amp;amp;``, so the module that knows the field is HTML does it
    once.

    The HTML is kept alongside: it carries the emphasis and the links, and
    discarding what upstream sent in favour of our reading of it is what §3.1
    refuses to do.
    """
    client.responses["PageCahierDeTexte"] = protocol.homework_response(
        [
            protocol.homework(
                description=(
                    "<div>Exercices 3 &amp; 4 p.&nbsp;52</div>"
                    "<div>Apporter l&#039;&eacute;querre</div>"
                )
            )
        ]
    )

    item = gateway.homework(client).facts.homework[0]

    assert item.description_text == "Exercices 3 & 4 p. 52\nApporter l'équerre"
    # Not replaced by it: the markup survives for whoever wants to render it.
    assert "<div>" in item.description


def test_an_entity_a_teacher_typed_by_hand_is_not_read_as_a_tag(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The order of the two passes is the whole correctness of the stripper.

    A maths or code exercise legitimately contains ``&lt;`` and ``&gt;``, which
    PRONOTE sends escaped. Decoding the entities before removing the tags would
    turn that text *into* a tag and then delete it -- the exercise would reach
    the parent with a hole in the middle. Tags go first for that reason.
    """
    client.responses["PageCahierDeTexte"] = protocol.homework_response(
        [protocol.homework(description="Comparer &lt;b&gt; et &lt;strong&gt;")]
    )

    item = gateway.homework(client).facts.homework[0]

    assert item.description_text == "Comparer <b> et <strong>"


def test_a_homework_item_without_an_id_or_a_due_date_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A to-do item with no due date has nowhere to go."""
    no_id = protocol.homework()
    no_id.pop("N")
    no_due = protocol.homework(identifier="HOMEWORK-2")
    no_due["PourLe"] = {"V": "not a date"}

    client.responses["PageCahierDeTexte"] = protocol.homework_response([no_id, no_due])
    assert gateway.homework(client).facts.homework == ()


def test_homework_attachments_are_names_and_never_urls(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Attachment.url`` interpolates the live session token.

    Storing it would write a credential-bearing URL into the snapshot, into
    every recorder row for the to-do list and into the diagnostics download --
    the exact hazard §8.2 removed the iCal URL from the state machine for. It
    would also be dead by the next login.
    """
    client.responses["PageCahierDeTexte"] = protocol.homework_response(
        [protocol.homework(attachments=("enonce.pdf", ""))]
    )
    item = gateway.homework(client).facts.homework[0]

    assert item.attachments == ("enonce.pdf",)
    assert not any("Session" in name for name in item.attachments)
    assert not any("http" in name for name in item.attachments)


def test_a_renamed_homework_key_fails_the_tier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Same rule as the timetable, for the same reason."""
    client.responses["PageCahierDeTexte"] = {"dataSec": {"data": {}}}
    with pytest.raises(ProtocolChanged):
        gateway.homework(client)


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


def test_marks_spends_one_request_on_four_datasets(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Upstream would spend four identical posts here.

    ``Period.grades``, ``.averages``, ``.overall_average`` and
    ``.class_overall_average`` each re-post ``DernieresNotes`` with the same
    body.
    """
    period = current_period(gateway, client)
    client.posts.clear()

    result = gateway.marks(client, period, with_report=False)  # type: ignore[arg-type]

    assert result.calls == 1
    assert client.posted_names == ["DernieresNotes"]
    assert result.facts.overall_average == 13.0
    assert result.facts.class_overall_average == 11.5


def test_the_report_card_rides_along_when_asked(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Which is why the tier is budgeted at two requests, not one."""
    period = current_period(gateway, client)
    client.posts.clear()

    result = gateway.marks(client, period, with_report=True)  # type: ignore[arg-type]

    assert result.calls == 2
    assert client.posted_names == ["DernieresNotes", "PageBulletins"]
    assert result.facts.report is not None
    assert result.facts.report.subjects[0].name == "Mathématiques"


def test_a_grade_holds_a_value_or_a_status_never_both(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """This separation is what lets the sensor hold a *numeric* state.

    A state that were sometimes ``14.5`` and sometimes ``Absent`` would be
    usable neither by a ``numeric_state`` trigger nor by a graph (§4.3).
    """
    client.responses["DernieresNotes"] = protocol.marks_response(
        grades=[
            protocol.grade(identifier="GRADE-1", value="14,5"),
            protocol.grade(identifier="GRADE-2", value="|1"),
        ]
    )
    period = current_period(gateway, client)
    grades = gateway.marks(client, period, with_report=False).facts.grades  # type: ignore[arg-type]

    assert grades[0].value == 14.5
    assert grades[0].status is None
    assert grades[1].value is None
    assert grades[1].status is GradeStatus.ABSENT


def test_a_bonus_grade_is_not_also_optional(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """PRONOTE sets both flags on a bonus; only one of them is meaningful."""
    client.responses["DernieresNotes"] = protocol.marks_response(
        grades=[protocol.grade(is_bonus=True, is_optional=True)]
    )
    period = current_period(gateway, client)
    grade = gateway.marks(client, period, with_report=False).facts.grades[0]  # type: ignore[arg-type]

    assert grade.is_bonus is True
    assert grade.is_optional is False


def test_a_grade_without_an_id_or_a_date_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A grade with no date cannot be ordered, and "last grade" needs order."""
    no_id = protocol.grade()
    no_id.pop("N")
    no_date = protocol.grade(identifier="GRADE-2")
    no_date["date"] = {"V": "not a date"}

    client.responses["DernieresNotes"] = protocol.marks_response(
        grades=[no_id, no_date]
    )
    period = current_period(gateway, client)
    assert gateway.marks(client, period, with_report=False).facts.grades == ()  # type: ignore[arg-type]


def test_a_grade_with_no_class_statistics_still_decodes(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The establishment that publishes no comparison figures."""
    client.responses["DernieresNotes"] = protocol.marks_response(
        grades=[protocol.grade(class_average=None, comment=None)]
    )
    period = current_period(gateway, client)
    grade = gateway.marks(client, period, with_report=False).facts.grades[0]  # type: ignore[arg-type]

    assert grade.value == 14.5
    assert grade.class_average is None
    assert grade.comment is None


def test_averages_survive_an_establishment_with_no_class_statistics(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """This one emptied every per-subject average entity, silently.

    ``dataClasses.Average`` resolves ``moyClasse``, ``moyMin`` and ``moyMax``
    with neither a default nor ``strict=False``, so an establishment that does
    not publish class statistics -- which §3.3.3 names as legitimate -- made
    the constructor raise for **every single subject**. The result was not a
    degraded reading but no averages at all, with one DEBUG line per subject as
    the only trace.
    """
    client.responses["DernieresNotes"] = protocol.marks_response(
        averages=[
            protocol.average(class_average=None, minimum=None, maximum=None),
            protocol.average(subject="Anglais", subject_id="SUBJECT-ANGLAIS"),
        ]
    )
    period = current_period(gateway, client)
    averages = gateway.marks(client, period, with_report=False).facts.averages  # type: ignore[arg-type]

    assert len(averages) == 2
    assert averages[0].student == 13.4
    assert averages[0].class_average is None
    assert averages[1].class_average == 11.1


def test_an_average_with_neither_a_name_nor_an_id_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Average`` has no identifier of its own, so the subject is the key.

    §2.4 rules out using a position in a list, so an entry naming no subject
    cannot be keyed at all.
    """
    anonymous = protocol.average()
    anonymous.pop("N")
    anonymous.pop("L")

    client.responses["DernieresNotes"] = protocol.marks_response(averages=[anonymous])
    period = current_period(gateway, client)
    assert gateway.marks(client, period, with_report=False).facts.averages == ()  # type: ignore[arg-type]


def test_an_average_keyed_only_by_name_is_kept(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One of the two halves of the key is enough."""
    named_only = protocol.average()
    named_only.pop("N")

    client.responses["DernieresNotes"] = protocol.marks_response(averages=[named_only])
    period = current_period(gateway, client)
    average = gateway.marks(client, period, with_report=False).facts.averages[0]  # type: ignore[arg-type]

    assert average.subject == "Mathématiques"
    assert average.subject_id is None


def test_marks_tolerate_an_absent_overall_average(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Not every establishment publishes a general average."""
    client.responses["DernieresNotes"] = protocol.marks_response(
        overall=None, class_overall=None
    )
    period = current_period(gateway, client)
    facts = gateway.marks(client, period, with_report=False).facts  # type: ignore[arg-type]

    assert facts.overall_average is None
    assert facts.class_overall_average is None


# ---------------------------------------------------------------------------
# The report card
# ---------------------------------------------------------------------------


def test_an_unpublished_report_is_reported_as_absent(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """PRONOTE says so itself, through a ``Message`` field."""
    client.responses["PageBulletins"] = protocol.report_response(published=False)
    period = current_period(gateway, client)
    result = gateway.marks(client, period, with_report=True)  # type: ignore[arg-type]

    assert result.facts.report is None
    assert result.calls == 2


def test_a_report_whose_shape_moved_is_absent_not_empty(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """ "Published, zero subjects" and "not published" are different answers.

    Both of ``Report``'s resolvers carry ``default=[]``, so a response whose
    shape moved decoded happily into an *empty* report -- and the entity then
    claimed a published bulletin with nothing in it.
    """
    client.responses["PageBulletins"] = protocol.report_response(
        with_services_key=False
    )
    period = current_period(gateway, client)
    assert gateway.marks(client, period, with_report=True).facts.report is None  # type: ignore[arg-type]


def test_an_undecodable_report_is_absent_rather_than_fatal(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One malformed subject list must not fail the marks tier."""
    client.responses["PageBulletins"] = protocol.report_response(services=5)
    period = current_period(gateway, client)
    assert gateway.marks(client, period, with_report=True).facts.report is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Attendance -- one request, three datasets
# ---------------------------------------------------------------------------


def test_attendance_spends_one_request_on_three_collections(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Period.absences``, ``.delays`` and ``.punishments`` each re-post.

    And then each reads *the same* ``listeAbsences`` list, filtering on ``G``.
    One request, one filter, three tuples.
    """
    period = current_period(gateway, client)
    client.posts.clear()

    result = gateway.attendance(client, period)  # type: ignore[arg-type]

    assert result.calls == 1
    assert client.posted_names == ["PagePresence"]
    assert len(result.facts.absences) == 1
    assert len(result.facts.delays) == 1
    assert len(result.facts.punishments) == 1


def test_an_absence_carries_hours_and_days_never_minutes(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """And a delay carries minutes. That asymmetry comes from the protocol.

    It is also why absences and delays get separate event entities: one entity
    with two differently-shaped payloads would force every automation to test
    the type before reading an attribute.
    """
    period = current_period(gateway, client)
    facts = gateway.attendance(client, period).facts  # type: ignore[arg-type]

    assert facts.absences[0].hours == "2h00"
    assert facts.absences[0].days == 0
    assert facts.delays[0].minutes == 12
    assert not hasattr(facts.absences[0], "minutes")


def test_a_punishment_slot_may_be_a_date_or_an_instant(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``placeExecution`` decides, and the gateway absorbs both shapes.

    So a calendar can compare every event against every other one, which
    ``date`` and ``datetime`` mixed cannot.
    """
    client.responses["PagePresence"] = protocol.attendance_response(
        [
            protocol.punishment(identifier="P-1", schedule_place=4),
            protocol.punishment(identifier="P-2", schedule_place=None),
        ]
    )
    period = current_period(gateway, client)
    punishments = gateway.attendance(client, period).facts.punishments  # type: ignore[arg-type]

    for punishment in punishments:
        for slot in punishment.schedule:
            assert slot.start.tzinfo is PARIS


def test_a_punishment_given_during_a_lesson_carries_an_instant(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``horsCours`` is inverted upstream, and the shape of ``given`` follows it."""
    client.responses["PagePresence"] = protocol.attendance_response(
        [protocol.punishment(during_lesson=True)]
    )
    period = current_period(gateway, client)
    punishment = gateway.attendance(client, period).facts.punishments[0]  # type: ignore[arg-type]

    assert punishment.during_lesson is True
    assert punishment.given_at.tzinfo is PARIS


def test_a_punishment_with_an_out_of_range_slot_is_dropped_alone(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``place2time`` raises ``ValueError``, which upstream turns into ``DataError``.

    One unschedulable punishment must not cost the absences and delays that
    came back in the same response.
    """
    client.responses["PagePresence"] = protocol.attendance_response(
        [
            protocol.punishment(identifier="P-BAD", schedule_place=999),
            protocol.absence(),
        ]
    )
    period = current_period(gateway, client)
    facts = gateway.attendance(client, period).facts  # type: ignore[arg-type]

    assert len(facts.absences) == 1


def test_a_non_schedulable_punishment_has_an_empty_schedule(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``estProgrammable`` false means upstream never reads ``programmation``."""
    client.responses["PagePresence"] = protocol.attendance_response(
        [protocol.punishment(schedulable=False)]
    )
    period = current_period(gateway, client)
    punishment = gateway.attendance(client, period).facts.punishments[0]  # type: ignore[arg-type]

    assert punishment.schedule == ()


def test_an_undecodable_absence_or_delay_costs_only_itself(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The per-item rule again, on the collection that mixes three kinds."""
    bad_absence = protocol.absence(identifier="A-BAD")
    bad_absence["dateDebut"] = {"V": "not a date"}
    bad_delay = protocol.delay(identifier="D-BAD")
    bad_delay["date"] = {"V": "not a date"}

    client.responses["PagePresence"] = protocol.attendance_response(
        [
            bad_absence,
            bad_delay,
            protocol.absence(identifier="A-GOOD"),
            protocol.delay(identifier="D-GOOD"),
            {"G": 99, "N": "SOMETHING-ELSE"},
        ]
    )
    period = current_period(gateway, client)
    facts = gateway.attendance(client, period).facts  # type: ignore[arg-type]

    assert [item.id for item in facts.absences] == ["A-GOOD"]
    assert [item.id for item in facts.delays] == ["D-GOOD"]


def test_an_absence_missing_a_required_field_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``ParsingError`` from upstream, caught per item."""
    broken = protocol.absence()
    broken.pop("dateFin")
    client.responses["PagePresence"] = protocol.attendance_response([broken])

    period = current_period(gateway, client)
    assert gateway.attendance(client, period).facts.absences == ()  # type: ignore[arg-type]


def test_a_delay_missing_a_required_field_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Same, on the delay branch."""
    broken = protocol.delay()
    broken.pop("date")
    client.responses["PagePresence"] = protocol.attendance_response([broken])

    period = current_period(gateway, client)
    assert gateway.attendance(client, period).facts.delays == ()  # type: ignore[arg-type]


def test_a_punishment_missing_a_required_field_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Same, on the punishment branch."""
    broken = protocol.punishment()
    broken.pop("nature")
    client.responses["PagePresence"] = protocol.attendance_response([broken])

    period = current_period(gateway, client)
    assert gateway.attendance(client, period).facts.punishments == ()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


def test_evaluations_decode_their_acquisitions(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One request, and the competency levels come with it."""
    period = current_period(gateway, client)
    client.posts.clear()

    result = gateway.evaluations(client, period)  # type: ignore[arg-type]

    assert result.calls == 1
    evaluation = result.facts.evaluations[0]
    assert evaluation.subject == "Mathématiques"
    assert evaluation.acquisitions[0].level == "Très bonne maîtrise"


def test_one_undecodable_evaluation_costs_that_evaluation(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``coefficient`` is resolved strictly upstream."""
    client.responses["DernieresEvaluations"] = protocol.evaluations_response(
        [
            protocol.evaluation(identifier="E-BAD", coefficient=None),
            protocol.evaluation(identifier="E-GOOD"),
        ]
    )
    period = current_period(gateway, client)
    evaluations = gateway.evaluations(client, period).facts.evaluations  # type: ignore[arg-type]

    assert [item.id for item in evaluations] == ["E-GOOD"]


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def test_news_reads_tab_8_and_never_touches_content(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``PageActualites`` is the read; ``SaisieActualites`` is the write.

    Specification v1 had the two the wrong way round. And
    ``Information.content`` is a lazy attribute that posts when *read*, so it
    is deliberately never touched.
    """
    result = gateway.news(client)

    assert result.calls == 1
    assert client.posted_names == ["PageActualites"]
    assert result.facts.information[0].title == "Sortie scolaire"


def test_one_undecodable_news_item_costs_that_item(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``auteur``, ``lue``, ``nature``, ``estSondage`` are all strict upstream.

    And ``client.information_and_surveys()`` builds the whole list in a single
    comprehension, so one item missing a category emptied the news tier, took
    ``sensor.<eleve>_informations_non_lues`` with it, and left the school's
    actual announcement invisible.
    """
    client.responses["PageActualites"] = protocol.news_response(
        [
            protocol.information(identifier="I-1", author=None, category=None),
            protocol.information(identifier="I-2", title=None),
            protocol.information(identifier="I-3"),
        ]
    )
    items = gateway.news(client).facts.information

    assert [item.id for item in items] == ["I-1", "I-2", "I-3"]
    assert items[0].author is None
    assert items[1].title is None


def test_a_news_item_without_an_id_or_a_creation_date_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The creation date orders the list, so it is not optional."""
    no_id = protocol.information()
    no_id.pop("N")
    no_date = protocol.information(identifier="I-2")
    no_date["dateCreation"] = {"V": "not a date"}

    client.responses["PageActualites"] = protocol.news_response([no_id, no_date])
    assert gateway.news(client).facts.information == ()


def test_a_news_item_carries_its_visibility_window(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Both optional, and both zone-attached when present."""
    client.responses["PageActualites"] = protocol.news_response(
        [
            protocol.information(
                start=dt.datetime(2026, 3, 11, 0, 0),
                end=dt.datetime(2026, 3, 20, 23, 59, 59),
            )
        ]
    )
    item = gateway.news(client).facts.information[0]

    assert item.start_date is not None
    assert item.start_date.tzinfo is PARIS
    assert item.end_date is not None


def test_a_renamed_news_key_fails_the_tier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Same rule, third collection."""
    client.responses["PageActualites"] = {"dataSec": {"data": {}}}
    with pytest.raises(ProtocolChanged):
        gateway.news(client)


# ---------------------------------------------------------------------------
# Discussions -- where the budget is decided
# ---------------------------------------------------------------------------


def test_the_thread_list_costs_one_request_and_bodies_cost_more(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Discussion.messages`` posts ``ListeMessages`` on **every** read.

    Expanding every thread would cost one request each -- ten threads on an
    hourly tier is about 176 requests a day, which roughly doubles the whole
    budget. The specification budgeted this tier at one request and did not
    anticipate that.
    """
    client.threads = [
        FakeThread("T-1", unread=0),
        FakeThread("T-2", unread=0),
    ]
    result = gateway.discussions(client, previous_unread={"T-1": 0, "T-2": 0})

    assert result.calls == 1
    assert result.facts.expanded == frozenset()
    assert all(thread.message_reads == 0 for thread in client.threads)


def test_only_a_thread_whose_counter_rose_is_expanded(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Which is exactly what the new-message event needs, and no more."""
    client.threads = [
        FakeThread("T-1", unread=2, messages=[FakeMessage("M-1")]),
        FakeThread("T-2", unread=0),
    ]
    result = gateway.discussions(client, previous_unread={"T-1": 1, "T-2": 0})

    assert result.calls == 2
    assert result.facts.expanded == frozenset({"T-1"})
    assert client.threads[0].message_reads == 1
    assert client.threads[1].message_reads == 0


def test_expansion_is_capped_per_cycle(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The cap is what keeps a busy morning from becoming a burst."""
    client.threads = [
        FakeThread(f"T-{index}", unread=1, messages=[FakeMessage(f"M-{index}")])
        for index in range(MAX_DISCUSSION_EXPANSIONS + 2)
    ]
    result = gateway.discussions(client, previous_unread={})

    assert len(result.facts.expanded) == MAX_DISCUSSION_EXPANSIONS
    assert result.calls == 1 + MAX_DISCUSSION_EXPANSIONS


def test_drafts_and_trash_are_filtered_at_the_door(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """They are not conversations anybody wants an automation on."""
    client.threads = [
        FakeThread("T-DRAFT", labels=["Drafts"]),
        FakeThread("T-TRASH", labels=["Trash"]),
        FakeThread("T-REAL"),
    ]
    facts = gateway.discussions(client, previous_unread={}).facts

    assert [thread.id for thread in facts.discussions] == ["T-REAL"]


def test_an_unexpandable_thread_is_reported_without_its_messages(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """ "A message arrived" is worth more than losing the event entirely."""

    class _Angry(FakeThread):
        @property
        def messages(self) -> list[FakeMessage]:
            raise KeyError("listeMessages")

    client.threads = [_Angry("T-1", unread=1)]
    result = gateway.discussions(client, previous_unread={})

    assert result.facts.discussions[0].messages == ()
    assert result.calls == 2


def test_a_message_without_a_creation_date_is_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Ordering the newest messages needs a date."""
    good = FakeMessage("M-GOOD")
    bad = FakeMessage("M-BAD")
    bad.created = None  # type: ignore[assignment]

    client.threads = [FakeThread("T-1", unread=2, messages=[good, bad])]
    facts = gateway.discussions(client, previous_unread={}).facts

    assert [message.id for message in facts.discussions[0].messages] == ["M-GOOD"]


def test_discussions_default_to_no_previous_counts(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The first cycle after a reload has nothing to compare against."""
    client.threads = [FakeThread("T-1", unread=1, messages=[FakeMessage("M-1")])]
    result = gateway.discussions(client)
    assert result.facts.expanded == frozenset({"T-1"})


# ---------------------------------------------------------------------------
# Menus and the teaching staff
# ---------------------------------------------------------------------------


def test_menus_cover_today_and_tomorrow(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One request in mid-week, because ``Client.menus`` walks whole weeks."""
    result = gateway.menus(client)

    assert result.calls == 1
    assert len(result.facts.menus) == 2
    assert result.facts.menus[0].main_meal == ("Poisson pané",)
    assert result.facts.menus[0].other_meal == ()


def test_menus_cost_two_requests_across_a_week_boundary(
    clock: FakeClock, client: FakeClient
) -> None:
    """The other half of annexe B's 1.14: a Sunday costs the extra post."""
    clock.set_wall(dt.datetime(2026, 3, 15, 18, 0, tzinfo=PARIS))
    gateway = PronoteGateway("Europe/Paris", clock=clock.now)

    assert gateway.menus(client).calls == 2


def test_the_teaching_staff_is_the_whole_static_tier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The iCal URL used to live here, and does not any more.

    Keeping it would drop an autonomous authentication bearer into the snapshot
    store -- a long-lived structure whose *purpose* is to be dumped into a
    diagnostic report (§8.2).
    """
    result = gateway.static(client)

    assert result.calls == 1
    assert result.facts.teaching_staff[0].name == "Prof. Un"
    assert result.facts.teaching_staff[0].subjects == ("Mathématiques",)
    assert not hasattr(result.facts, "ical_url")


def test_a_teaching_staff_response_without_its_list_names_what_it_did_carry(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The live failure that had to be reproduced before it could be understood.

    A PRONOTE 26.2 server answered tab 37 with no ``liste`` key.
    ``ClientBase.get_teaching_staff`` subscripts it blind, so the tier failed as
    ``KeyError: 'liste'`` -- a message naming nothing anyone can act on, in a
    module whose §3.3 rule is that the *reading* happens here.

    Failing the tier is still correct and deliberate: an absent key is not an
    empty staff list, and §5.4 keeps the previous snapshot rather than
    publishing a school with no teachers. What was missing is the cause. With
    the key names the response *does* carry, a renamed field is one log line
    away instead of one release away.
    """
    caplog.set_level(logging.WARNING)
    client.responses["PageEquipePedagogique"] = {
        "dataSec": {"data": {"listeEquipePedagogique": {"V": []}}}
    }

    with pytest.raises(ProtocolChanged):
        gateway.static(client)

    assert "the teaching staff" in caplog.text
    assert "dataSec.data.liste" in caplog.text
    # The keys it *does* carry, which is the whole point of the change.
    assert "listeEquipePedagogique" in caplog.text


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(
            {"dataSec": {"data": {}}},
            "dataSec.data carries no 'liste'",
            id="the section is reached and lacks the collection",
        ),
        pytest.param(
            {"dataSec": {}},
            "dataSec carries no 'data'",
            id="the walk stops one level higher",
        ),
    ],
)
def test_a_missing_section_and_an_empty_one_are_not_reported_alike(
    gateway: PronoteGateway,
    client: FakeClient,
    caplog: pytest.LogCaptureFixture,
    response: dict[str, Any],
    expected: str,
) -> None:
    """The warning existed to make this distinction and could not make it.

    Both cases used to print ``[]``, because the caller walked the path first
    and passed ``_get(...) or {}`` -- so "absent" had already become "empty"
    before the reader saw it. A live establishment reported that exact line and
    the question "is the field renamed, or does this school publish nothing?"
    could not be answered from the log, which is the only question the warning
    is for.

    Reporting the shape at the failure point was not enough either, and this
    test is what caught that: both responses below stop on an empty mapping,
    and from the outside both are "a mapping of ['dataSec']". Only naming the
    level actually reached and the key it lacks separates a section this
    school does not publish from a path that broke one level higher.
    """
    caplog.set_level(logging.WARNING)
    client.responses["PageEquipePedagogique"] = response

    with pytest.raises(ProtocolChanged):
        gateway.static(client)

    assert expected in caplog.text


def test_the_warning_names_shapes_and_never_a_value(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """It lands in a file users attach to public issues (§8.2).

    Key names and types are what makes a protocol change diagnosable; the
    values behind them are a child's school record. A helper that printed the
    response to describe it would be a leak in the one place we ask people to
    publish.
    """
    caplog.set_level(logging.WARNING)
    client.responses["PageEquipePedagogique"] = {
        "dataSec": {"data": {"listeEquipePedagogique": {"V": [{"L": "Prof. Secret"}]}}}
    }

    with pytest.raises(ProtocolChanged):
        gateway.static(client)

    assert "listeEquipePedagogique" in caplog.text
    assert "Prof. Secret" not in caplog.text


def test_an_empty_teaching_staff_list_is_not_a_failure(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """ "Absent" and "empty" stay two different answers.

    An establishment that publishes the tab with nobody in it is a quiet
    school, not a protocol change. It must succeed with no members, rather than
    keep yesterday's snapshot for ever and eventually raise a repair.
    """
    client.responses["PageEquipePedagogique"] = protocol.teaching_staff_response([])

    result = gateway.static(client)

    assert result.facts.teaching_staff == ()
    assert result.calls == 1


def test_a_staff_member_who_is_not_a_teacher_is_labelled_as_such(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``G`` is the discriminator, and it is upstream's to interpret.

    ``dataClasses.TeachingStaff`` maps ``G == 3`` to ``"teacher"`` and anything
    else to ``"staff"``. Decoding the tab ourselves must not mean re-deciding
    that: the entries still go through upstream's class, so a head teacher or a
    nurse keeps arriving as ``staff`` without this module owning the table.
    """
    client.responses["PageEquipePedagogique"] = protocol.teaching_staff_response(
        [
            protocol.teaching_staff(),
            protocol.teaching_staff(
                identifier="STAFF-2",
                name="Vie scolaire",
                teacher=False,
                subjects=(),
                order=2,
            ),
        ]
    )

    result = gateway.static(client)

    roles = {member.name: member.role for member in result.facts.teaching_staff}
    assert roles == {"Prof. Un": "teacher", "Vie scolaire": "staff"}


# ---------------------------------------------------------------------------
# On-demand reads, never stored
# ---------------------------------------------------------------------------


def test_the_ical_url_is_fetched_on_demand(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Anyone holding this URL reads the timetable with no credential.

    So it is never a state, never an attribute and never in a snapshot: the
    service fetches it, hands it back and drops it (§8.2).
    """
    url, calls = gateway.ical_url(client)

    assert calls == 1
    assert client.ical_calls == 1
    assert url.startswith("https://")


def test_the_identity_is_posted_with_a_resource_not_a_member(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The wrong signature returns the *parent's* details under the child.

    ``client.post`` stamps the account holder as ``membre``. On a parent
    account that returns the parent's birth date, e-mail, telephone number and
    INE number -- which the service then handed back attributed to the child:
    a wrong-attribution disclosure of precisely the fields §8.2 keeps out of
    the state machine. ``ClientInfo._cache()`` bypasses ``ClientBase.post`` for
    exactly this reason, and says so.
    """
    identity, calls = gateway.identity(client)

    assert calls == 1
    assert client.posts == []
    name, body = client.communication_posts[0]
    assert name == "PageInfosPerso"
    assert body == {
        "Signature": {"onglet": 49, "ressource": {"N": protocol.STUDENT_ID, "G": 4}}
    }
    assert identity.ine_number == "0000000000A"
    assert identity.phone == "+33600000001"
    assert identity.guardians[0].is_legal is True


def test_an_identity_with_no_guardians_still_decodes(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A student account has no ``Responsables`` block at all."""
    client.responses["PageInfosPerso"] = protocol.personal_info_response(
        guardians=False
    )
    identity, _calls = gateway.identity(client)
    assert identity.guardians == ()


def test_the_profile_photo_is_fetched_under_the_lock(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``ClientInfo.profile_picture`` goes through the misattributing cache.

    Read as a property from an entity, a parent account can show the wrong
    child's face (annexe A §5.4).
    """
    data, calls = gateway.profile_picture(client)

    assert calls == 1
    assert data == b"not-a-real-image"


def test_an_account_with_no_photo_costs_no_request(
    gateway: PronoteGateway,
) -> None:
    """Nothing to fetch, so nothing is charged."""
    client = FakeClient(has_photo=False)
    data, calls = gateway.profile_picture(client)

    assert data is None
    assert calls == 0


def test_a_timetable_pdf_is_rendered_on_demand(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One request, and the URL is returned rather than stored."""
    url, calls = gateway.timetable_pdf_url(client, dt.date(2026, 3, 12), portrait=True)

    assert calls == 1
    assert client.pdf_calls == 1
    assert "portrait=True" in url


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_ticking_homework_posts_directly(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """No ``pronotepy.Homework`` is kept alive across the DTO boundary.

    An object read after its session closed raises ``Erreur.G = 22`` (§3.1), so
    the write is posted from the identifier rather than from a live object.
    """
    calls = gateway.set_homework_done(client, "HOMEWORK-1", done=True)

    assert calls == 1
    assert client.body_for("SaisieTAFFaitEleve") == {
        "listeTAF": [{"N": "HOMEWORK-1", "TAFFait": True}]
    }


def test_replying_to_a_thread_costs_three_requests(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """And it is billed at three, because a write that under-reports its cost
    corrupts the very budget that protects the account (annexe B §8).

    The thread has to be re-listed, ``Discussion.reply`` posts
    ``ListeMessages`` to find the message being answered, then ``SaisieMessage``
    to send.
    """
    client.threads = [FakeThread("T-1")]
    calls = gateway.reply_to_discussion(client, "T-1", "Bien reçu")

    assert calls == 3
    assert client.threads[0].replies == ["Bien reçu"]


def test_replying_to_an_unknown_thread_is_refused(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Surfaced to the user as a validation error, not swallowed."""
    with pytest.raises(DiscussionNotFound):
        gateway.reply_to_discussion(client, "T-NOWHERE", "Bonjour")


def test_replying_to_a_closed_thread_is_refused(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """PRONOTE would accept the post and drop the message."""
    client.threads = [FakeThread("T-1", closed=True)]
    with pytest.raises(DiscussionIsClosed):
        gateway.reply_to_discussion(client, "T-1", "Bonjour")


def test_starting_a_thread_matches_recipients_by_name(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The name is the only handle a user can read off the interface.

    The ``N`` identifier is not shown anywhere, so matching on it would make
    the service unusable from a script.
    """
    client.recipients = [FakeRecipient("Prof. Un"), FakeRecipient("Prof. Deux")]
    calls = gateway.start_discussion(client, "Absence", "Bonjour", ["  prof. un  "])

    assert calls == 3
    assert client.new_discussions == [("Absence", "Bonjour", ["Prof. Un"])]


def test_an_unmatched_recipient_is_refused_rather_than_dropped(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A message that quietly went to nobody is worse than an error."""
    client.recipients = [FakeRecipient("Prof. Un")]
    with pytest.raises(RecipientNotFound) as raised:
        gateway.start_discussion(client, "Absence", "Bonjour", ["Prof. Trois"])

    assert raised.value.missing == ["prof. trois"]
    assert "Prof. Un" in raised.value.available


def test_a_recipient_with_no_name_does_not_crash_the_match(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Defensive: the protocol can return a recipient with an empty label."""
    nameless = FakeRecipient("Prof. Un")
    nameless.name = None  # type: ignore[assignment]
    client.recipients = [nameless, FakeRecipient("Prof. Deux")]

    calls = gateway.start_discussion(client, "Absence", "Bonjour", ["Prof. Deux"])
    assert calls == 3


def test_marking_a_news_item_read_uses_the_write_tab(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``SaisieActualites``, not ``PageActualites``. v1 had them swapped."""
    calls = gateway.mark_information_read(client, "INFORMATION-1")

    assert calls == 1
    body = client.body_for("SaisieActualites")
    assert body["listeActualites"][0]["N"] == "INFORMATION-1"
    assert body["listeActualites"][0]["lue"] is True
    assert body["listeActualites"][0]["public"]["N"] == protocol.STUDENT_ID
