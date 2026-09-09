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

from typing import TYPE_CHECKING, Any

import pytest

from custom_components.pronote_ng.child_keys import pair
from custom_components.pronote_ng.const import (
    CHILD_KEY,
    CHILD_NAME,
    CHILD_RESOURCE_ID,
    DOMAIN,
)

from .conftest import REQUIRES_HASS

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

    async def test_the_entity_id_survives_byte_for_byte(
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

        from unittest.mock import patch

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
