"""The child identity that does not follow PRONOTE's rotating identifier.

PRONOTE writes a child's resource identifier as ``46#<signature>`` and the
signature is not stable between sessions. Three distinct values were seen for
one pupil on one account in a single day. Because an entity's ``unique_id``
embedded it, a rotation was indistinguishable from a new child arriving: a
second device, a second full set of entities, and the previous 56 orphaned in
the registry for ever -- with every dashboard badge, tile and automation still
pointing at the dead ones, and nothing logged anywhere.

The pairing half of these tests needs no Home Assistant, deliberately: it is
the part that has to be exhaustive, and it therefore also runs in the HA-free
half of the suite -- the half that can be run natively on Windows, where the
adoption class below is skipped for want of the harness.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from custom_components.pronote_ng.child_keys import pair
from custom_components.pronote_ng.const import (
    CHILD_KEY,
    CHILD_NAME,
    CHILD_RESOURCE_ID,
    DOMAIN,
)

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures.client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Sequence


def _record(key: str, resource_id: str, name: str) -> dict[str, str]:
    """One stored record, spelled out so the tests read as data."""
    return {CHILD_KEY: key, CHILD_RESOURCE_ID: resource_id, CHILD_NAME: name}


class TestPairingChildren:
    """`pair` is the whole fix: everything else only stores what it decides."""

    def test_a_child_never_seen_before_is_minted_a_key(self) -> None:
        """The keys must come from us, so the first sight is where they start."""
        keys, table, notes = pair([], [("46#aaa", "Enfant Un")])

        assert keys == {"46#aaa": "child-1"}
        assert table == [_record("child-1", "46#aaa", "Enfant Un")]
        assert any("never seen before" in note for note in notes)

    def test_a_rotated_identifier_keeps_the_same_key(self) -> None:
        """This is the defect, reduced to one assertion.

        The identifier changes and the child has not. Before the key existed,
        this exact input produced a new device and orphaned 56 entities.
        """
        stored = [_record("child-1", "46#was", "Enfant Un")]

        keys, table, notes = pair(stored, [("46#now", "Enfant Un")])

        assert keys == {"46#now": "child-1"}
        assert table == [_record("child-1", "46#now", "Enfant Un")]
        assert any("kept its identity" in note for note in notes)

    def test_a_renamed_child_with_a_stable_identifier_keeps_its_key(self) -> None:
        """The name is a way to recognise a record, never the key itself.

        An establishment corrects a spelling and a family changes name, so a
        key derived from the name would move the defect rather than remove it.
        """
        stored = [_record("child-1", "46#aaa", "DUPONT Marie")]

        keys, table, _ = pair(stored, [("46#aaa", "Marie DUPONT")])

        assert keys == {"46#aaa": "child-1"}
        assert table == [_record("child-1", "46#aaa", "Marie DUPONT")]

    def test_two_children_sharing_a_name_do_not_collapse_onto_one_key(self) -> None:
        """Twins are the case that breaks a naive name match.

        A school does not disambiguate them, so both rotated identifiers match
        the same name. Matching by name without excluding records another child
        has already claimed gives both children one key -- and one of the two
        loses every entity it has.
        """
        stored = [
            _record("child-1", "46#one", "Meme Nom"),
            _record("child-2", "46#two", "Meme Nom"),
        ]

        keys, _, _ = pair(stored, [("46#new1", "Meme Nom"), ("46#new2", "Meme Nom")])

        assert len(set(keys.values())) == 2

    def test_a_new_sibling_does_not_disturb_the_others(self) -> None:
        """§2.4 forbids a position in the list, and this is why.

        Keyed on rank, a sibling arriving would renumber everybody and move
        every entity of every child.
        """
        stored = [
            _record("child-1", "46#aaa", "Enfant Un"),
            _record("child-2", "46#bbb", "Enfant Deux"),
        ]

        keys, table, _ = pair(
            stored,
            [
                ("46#new", "Enfant Trois"),
                ("46#aaa", "Enfant Un"),
                ("46#bbb", "Enfant Deux"),
            ],
        )

        assert keys["46#aaa"] == "child-1"
        assert keys["46#bbb"] == "child-2"
        assert keys["46#new"] == "child-3"
        assert len(table) == 3

    def test_pairing_the_same_account_twice_changes_nothing(self) -> None:
        """It runs at every set-up, so a second pass must be a no-op.

        A pass that rewrote the table would write the config entry on every
        start, and after set-up that write fires the update listener that
        reloads the entry -- a reload loop.
        """
        announced = [("46#aaa", "Enfant Un"), ("46#bbb", "Enfant Deux")]
        _, table, _ = pair([], announced)

        keys, again, notes = pair(table, announced)

        assert again == table
        assert notes == []
        assert set(keys.values()) == {"child-1", "child-2"}

    def test_a_child_who_leaves_the_account_keeps_its_record(self) -> None:
        """Its key must never be handed to somebody else.

        The orphaned registry rows still carry it. Re-issuing the key would
        make Home Assistant re-point those rows at a different child rather
        than reject them -- one pupil's history silently becoming another's.
        """
        stored = [
            _record("child-1", "46#gone", "Enfant Parti"),
            _record("child-2", "46#here", "Enfant Un"),
        ]

        keys, table, _ = pair(stored, [("46#here", "Enfant Un"), ("46#new", "Nouveau")])

        assert keys["46#new"] == "child-3"
        assert _record("child-1", "46#gone", "Enfant Parti") in table

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param([], id="no table at all"),
            pytest.param([{CHILD_KEY: "child-1"}], id="a record with no identifier"),
            pytest.param(
                [
                    {
                        CHILD_KEY: "not-a-number",
                        CHILD_RESOURCE_ID: "46#x",
                        CHILD_NAME: "X",
                    }
                ],
                id="a key the format cannot be read from",
            ),
        ],
    )
    def test_a_damaged_table_still_yields_a_usable_key(
        self, stored: Sequence[dict[str, str]]
    ) -> None:
        """`.storage` is editable by hand and restorable from a backup.

        Refusing to pair would leave every child without a key, and
        `stable_key` would fall back to the rotating identifier -- the defect,
        reintroduced by the recovery path.
        """
        keys, _, _ = pair(stored, [("46#aaa", "Enfant Un")])

        assert list(keys) == ["46#aaa"]
        assert keys["46#aaa"].startswith("child-")


# `importorskip` was wrong here and the mistake is worth naming: on Windows
# `homeassistant` imports perfectly well, so it skipped nothing. What is absent
# there is the *harness* -- the plugin that provides `hass` -- and the tests
# below then errored on a missing fixture rather than being skipped, which
# aborted the HA-free half of the suite. `REQUIRES_HASS` is the marker the rest
# of the suite uses, and a skip mark is consulted before any fixture is built.
from homeassistant.helpers import (  # noqa: E402
    device_registry as dr,
    entity_registry as er,
)


@REQUIRES_HASS
class TestAdoptingEntitiesCreatedBeforeTheKeys:
    """The promise the user guide makes: the migration keeps your dashboard.

    §10.5 tells users that removing the stale device breaks nothing because
    identifiers are rewritten *in place*. That is the heaviest promise in the
    documentation: if it is false, somebody reads "nothing to redo" and then
    finds 55 orphaned entities. `async_update_entity` preserves the row by
    contract, so the risk is not in the API but in how it is used -- a key
    minted differently on the second pass, or a row matched twice.
    """

    async def test_the_entity_id_survives_byte_for_byte_when_nothing_rotated(
        self,
        hass: Any,
        mock_entry: Any,
        parent_client: Any,
        school_day: Any,
        no_spacing: None,
    ) -> None:
        """A dashboard badge can only be wired to a literal `entity_id`.

        The live instance has four badges, two tiles and a visibility condition
        pointing at hard identifiers, so an `entity_id` that shifts by one
        character is a broken dashboard. Compared byte for byte rather than by
        shape for exactly that reason.

        Note what this covers and what it does not: the rows here are created
        under the identifier the account still announces. That was the whole of
        the first version's coverage, and it is the case with least to repair.
        `TestWhenTheIdentifierHasRotated` covers the other one.
        """
        del school_day, no_spacing
        entities = er.async_get(hass)
        devices = dr.async_get(hass)

        was = f"{mock_entry.entry_id}_STUDENT-1"
        device = devices.async_get_or_create(
            config_entry_id=mock_entry.entry_id,
            identifiers={(DOMAIN, was)},
            name="Enfant Un",
        )
        devices.async_update_device(device.id, name_by_user="Ma Fille")
        row = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{was}_next_lesson",
            suggested_object_id="enfant_un_prochain_cours",
            config_entry=mock_entry,
            device_id=device.id,
        )
        before = row.entity_id
        entities.async_update_entity(before, name="Mon libelle")

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

            after = entities.async_get(before)
            assert after is not None, (
                "the registry row was replaced rather than re-pointed, which "
                "is the breakage the migration exists to avoid"
            )
            # The identity moved; everything the user can see did not.
            assert after.unique_id == f"{mock_entry.entry_id}_child-1_next_lesson"
            assert after.entity_id == before
            assert after.name == "Mon libelle"

            adopted = devices.async_get_device_by_identifier(
                (DOMAIN, f"{mock_entry.entry_id}_child-1"), mock_entry.entry_id
            )
            assert adopted is not None
            assert adopted.id == device.id, "the device was re-created, not adopted"
            assert adopted.name_by_user == "Ma Fille"

            await hass.config_entries.async_unload(mock_entry.entry_id)
            await hass.async_block_till_done()

    async def test_a_second_set_up_does_not_move_anything_again(
        self,
        hass: Any,
        account: Any,
        mock_entry: Any,
    ) -> None:
        """Adoption runs at every set-up, so it has to be idempotent.

        It is not guarded by a version number on purpose -- a migration handler
        runs before any login, and only a login can say which children the
        account announces now. The price of that choice is that this must be
        safe to run for ever.
        """
        del account
        entities = (
            er.async_get(mock_entry.hass) if hasattr(mock_entry, "hass") else None
        )
        registry = entities or er.async_get(hass)
        identities = sorted(
            row.unique_id
            for row in er.async_entries_for_config_entry(registry, mock_entry.entry_id)
        )

        assert identities, "the fixture set up no entities at all"
        assert all("STUDENT-" not in identity for identity in identities), (
            "an identity still follows PRONOTE's rotating resource identifier"
        )
        assert any("_child-1_" in identity for identity in identities)
        assert any("_child-2_" in identity for identity in identities)


@REQUIRES_HASS
class TestWhenTheIdentifierHasRotated:
    """The case the repair exists for, and the one its first version missed.

    Shipped as v0.0.10 and installed on the instance that motivated it, the
    adoption looked up ``<entry_id>_<the identifier announced now>``. On that
    instance the identifier had rotated -- which is the only circumstance in
    which anything needs repairing at all -- so nothing matched, and 56
    entities were created beside the 56 already there: three automations
    stopped triggering and every badge went stale. The defect the fix exists to
    prevent, performed by the fix.

    It passed CI because the test above created its rows under the identifier
    the fake account announces. A fixture that never rotates cannot fail a
    rotation, so the coverage figure was met and the case was untested.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> Any:
        """One child, because the repair is only sound for one.

        With siblings nothing in the registry says which orphaned generation
        belonged to which child -- twins share a name -- so the repair refuses
        rather than guess. `TestWhenTheRepairCannotBeSound` covers that, and it
        keeps the shared two-child client.
        """
        return FakeClient(children=CHILDREN[:1])

    async def test_rows_created_under_a_rotated_identifier_are_adopted(
        self,
        hass: Any,
        mock_entry: Any,
        parent_client: Any,
        school_day: Any,
        no_spacing: None,
    ) -> None:
        """The registry holds an identifier PRONOTE no longer mentions.

        `46#_KlUcOLD` rather than a tidy name on purpose: one of the three real
        values observed contained an underscore, which is why the repair finds
        its rows through the device identifier instead of splitting a
        `unique_id` on `_`.
        """
        del school_day, no_spacing
        entities = er.async_get(hass)
        devices = dr.async_get(hass)

        legacy = f"{mock_entry.entry_id}_46#_KlUcOLD"
        device = devices.async_get_or_create(
            config_entry_id=mock_entry.entry_id,
            identifiers={(DOMAIN, legacy)},
            name="Enfant Un",
        )
        row = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{legacy}_next_lesson",
            suggested_object_id="enfant_un_prochain_cours",
            config_entry=mock_entry,
            device_id=device.id,
        )
        before = row.entity_id

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

            after = entities.async_get(before)
            assert after is not None, "the row was replaced rather than re-pointed"
            assert after.unique_id == f"{mock_entry.entry_id}_child-1_next_lesson"
            assert after.entity_id == before

            duplicates = [
                entry.unique_id
                for entry in er.async_entries_for_config_entry(
                    entities, mock_entry.entry_id
                )
                if entry.unique_id.endswith("_next_lesson")
            ]
            assert len(duplicates) == 1, (
                f"a second generation was created: {duplicates} -- this is the "
                "defect that shipped"
            )

            adopted = devices.async_get_device_by_identifier(
                (DOMAIN, f"{mock_entry.entry_id}_child-1"), mock_entry.entry_id
            )
            assert adopted is not None
            assert adopted.id == device.id, "the device was re-created, not adopted"

            await hass.config_entries.async_unload(mock_entry.entry_id)
            await hass.async_block_till_done()

    async def test_the_newest_generation_is_the_one_adopted(
        self,
        hass: Any,
        mock_entry: Any,
        parent_client: Any,
        school_day: Any,
        no_spacing: None,
    ) -> None:
        """An instance can carry several dead generations.

        The one that motivated the fix had two, two hours apart: an older
        device the user had renamed by hand, and the one every dashboard was
        actually built on. An older generation was already superseded by the
        one after it, so the last created is the one to keep.

        By creation time and not by name, because on that instance the name
        could not decide: the integration names both generations from PRONOTE,
        so ``default_name`` was byte-identical on the two devices and only the
        user's rename of the older one differed.

        The clock is ticked between the two, and that is not decoration --
        every timestamp is frozen for the rest of the suite, so without it both
        devices are created in the same instant and the repair refuses (see
        the tie test below) rather than preferring one.
        """
        del no_spacing
        entities = er.async_get(hass)
        devices = dr.async_get(hass)

        for suffix, object_id in (("OLDEST", "vieux"), ("NEWER", "recent")):
            identity = f"{mock_entry.entry_id}_46#{suffix}"
            device = devices.async_get_or_create(
                config_entry_id=mock_entry.entry_id,
                identifiers={(DOMAIN, identity)},
                name="Enfant Un",
            )
            entities.async_get_or_create(
                "sensor",
                DOMAIN,
                f"{identity}_next_lesson",
                suggested_object_id=object_id,
                config_entry=mock_entry,
                device_id=device.id,
            )
            school_day.tick(timedelta(hours=2))

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

            kept = entities.async_get("sensor.recent")
            assert kept is not None
            assert kept.unique_id == f"{mock_entry.entry_id}_child-1_next_lesson"

            # The superseded generation is left exactly as it was: deleting a
            # user's registry rows is not this integration's decision.
            stale = entities.async_get("sensor.vieux")
            assert stale is not None
            assert stale.unique_id == f"{mock_entry.entry_id}_46#OLDEST_next_lesson"

            await hass.config_entries.async_unload(mock_entry.entry_id)
            await hass.async_block_till_done()

    async def test_two_generations_of_the_same_age_are_left_alone(
        self,
        hass: Any,
        mock_entry: Any,
        parent_client: Any,
        school_day: Any,
        no_spacing: None,
        caplog: Any,
    ) -> None:
        """With nothing to prefer, choosing would be a coin toss.

        Which set of entities survives decides which dashboard keeps working,
        so it is not a decision to take at random on a user's behalf. A real
        server does not create two generations in the same instant, but a
        hand-edited or restored registry can.
        """
        del school_day, no_spacing
        devices = dr.async_get(hass)
        for suffix in ("ONE", "TWO"):
            devices.async_get_or_create(
                config_entry_id=mock_entry.entry_id,
                identifiers={(DOMAIN, f"{mock_entry.entry_id}_46#{suffix}")},
                name="Enfant Un",
            )

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

            assert "share the same creation time" in caplog.text

            await hass.config_entries.async_unload(mock_entry.entry_id)
            await hass.async_block_till_done()


@REQUIRES_HASS
class TestWhenTheRepairCannotBeSound:
    """Refusing is the right answer, and it has to be said out loud."""

    async def test_two_unmatched_children_are_refused_rather_than_guessed(
        self,
        hass: Any,
        mock_entry: Any,
        parent_client: Any,
        school_day: Any,
        no_spacing: None,
        caplog: Any,
    ) -> None:
        """One orphaned generation and two children is not solvable.

        Handing it to the wrong child would file one pupil's marks, absences
        and timetable under the other's name -- silently, and in recorded
        history that cannot be unpicked afterwards.
        """
        del school_day, no_spacing
        devices = dr.async_get(hass)
        devices.async_get_or_create(
            config_entry_id=mock_entry.entry_id,
            identifiers={(DOMAIN, f"{mock_entry.entry_id}_46#WHOSE")},
            name="Enfant Un",
        )

        with patch(
            "custom_components.pronote_ng.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

            assert "repairable for one child and not for several" in caplog.text

            await hass.config_entries.async_unload(mock_entry.entry_id)
            await hass.async_block_till_done()

    async def test_a_fresh_install_says_nothing_about_repairs(
        self,
        hass: Any,
        account: Any,
        caplog: Any,
    ) -> None:
        """Every child of a new entry has no rows, and that is not a fault.

        The first draft of this guard asked "has any child no rows?" before
        "is anything orphaned?", so a brand-new installation was greeted by a
        message about an unrepairable registry. A warning that fires when
        nothing is wrong teaches the reader to ignore warnings.
        """
        del account

        assert "repairable for one child" not in caplog.text
        assert "orphaned generation" not in caplog.text
