"""Mapping contract from EcoleDirecte payloads to shared DTOs."""

# The plan's tests are copied verbatim, including its intentionally long names.
# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.pronote_ng.connectors.ecoledirecte.ed_mapping import (
    attendance_facts,
    grades_from_notes,
    homework_facts,
    lesson_from_ed,
    marks_facts,
    timetable_facts,
)
from custom_components.pronote_ng.connectors.errors import ConnectorUndecodableError

FIXTURES = Path(__file__).parent / "fixtures" / "ecoledirecte"


def load_json(name: str) -> dict[str, Any]:
    """Load one hand-written EcoleDirecte fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def minimal_ed_lesson() -> dict[str, Any]:
    """Return the smallest valid EcoleDirecte timetable entry."""
    return {
        "id": 1,
        "matiere": "Mathématiques",
        "start_date": "2026-09-14 08:00",
        "end_date": "2026-09-14 09:00",
    }


# fmt: off
def test_an_annulled_ed_lesson_maps_to_a_canceled_lesson() -> None:
    """binary_sensor.cours_annules must turn on without a Jinja walk of the JSON."""
    facts = timetable_facts(load_json("emploi_du_temps.json"), zone="Europe/Paris")
    canceled = [lesson for lesson in facts.lessons if lesson.canceled]
    assert len(canceled) == 1
    assert canceled[0].end.tzinfo is not None
    assert canceled[0].num == 0
    assert facts.lessons == facts.all_lessons


def test_a_permanence_is_a_followed_slot_not_a_detention() -> None:
    """Study hall is not a detention; _in_class filters only canceled and exempted."""
    facts = timetable_facts(load_json("emploi_du_temps.json"), zone="Europe/Paris")
    study = next(lesson for lesson in facts.lessons if lesson.status == "PERMANENCE")
    assert study.detention is False
    assert study.canceled is False
    assert study.duration == 0


def test_an_unknown_type_cours_does_not_become_a_calendar_status() -> None:
    """_lesson_events puts Lesson.status on the first line of the calendar description."""
    raw = {**minimal_ed_lesson(), "typeCours": "JETON-INCONNU"}
    lesson = lesson_from_ed(raw, zone="Europe/Paris")
    assert lesson.status is None
    assert lesson.detention is False


def test_homework_v1_has_no_body_because_the_list_endpoint_does_not_pay_for_it() -> None:
    """Fetching each due date would explode the daily cap; empty prose is honest."""
    facts = homework_facts(load_json("cahier_de_texte.json"))
    item = facts.homework[0]
    assert item.description == ""
    assert item.done in {True, False}
    assert item.id  # from idDevoir


def test_a_french_decimal_grade_becomes_a_float_and_never_a_string_state() -> None:
    """A state of '14,5' cannot trip numeric_state."""
    grade = grades_from_notes(load_json("notes.json"))[0]
    assert isinstance(grade.value, float)
    assert grade.status is None


def test_a_missing_notes_key_is_undecodable_not_an_empty_term() -> None:
    """An empty tuple is a plausible term; a broken payload must fail the tier."""
    with pytest.raises(ConnectorUndecodableError):
        marks_facts({}, current_period_id="A001")


def test_an_ed_absence_does_not_invent_a_period_unique_id() -> None:
    """History sensors are Pronote-only; a sentinel period_id would mint _pN anyway."""
    facts = attendance_facts(load_json("vie_scolaire.json"), period_id="")
    assert facts.period_id == ""
    assert facts.absences[0].days is None
    assert facts.delays[0].minutes is None
    assert all(p.schedule == () for p in facts.punishments)


def test_an_ed_lesson_serializes_to_the_card_contract_not_the_aplim_keys() -> None:
    """ha-pronote-ng-cards read subject/canceled; a matiere/is_annule payload would render empty."""
    from custom_components.pronote_ng.sensor import _lesson_dict

    facts = timetable_facts(load_json("emploi_du_temps.json"), zone="Europe/Paris")
    payload = _lesson_dict(facts.lessons[0])
    assert "subject" in payload
    assert "canceled" in payload
    assert "classroom" in payload
    assert "matiere" not in payload
    assert "is_annule" not in payload
    assert payload["detention"] is False
    assert "salle" not in payload
# fmt: on
