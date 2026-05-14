"""Tests for Vault._resolve_pairs() — load-time pair reconciliation."""
import logging
from pathlib import Path

import pytest

from vaults import FieldSchema, FieldType, Record, Schema, TypeSchema, Vault

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _recs(vault, type_name):
    return {r.name: r for r in vault.records.get(type_name, [])}


def _make_vault(projekte_recs, personen_recs, pairs=None, personen_extra_fields=None):
    """Build a minimal in-memory Vault without touching disk."""
    proj_schema = TypeSchema(name="Projekte", fields=[
        FieldSchema(name="Titel", type=FieldType.STRING),
        FieldSchema(name="Traeger", type=FieldType.LIST_LINKS, link_target="Personen"),
    ])
    pers_fields = [FieldSchema(name="Name", type=FieldType.STRING)]
    pers_fields.extend(personen_extra_fields or [])
    pers_schema = TypeSchema(name="Personen", fields=pers_fields)
    schema = Schema(types={"Projekte": proj_schema, "Personen": pers_schema})
    records = {
        "Projekte": [Record(name=n, type_schema=proj_schema, fields=f) for n, f in projekte_recs],
        "Personen": [Record(name=n, type_schema=pers_schema, fields=f) for n, f in personen_recs],
    }
    vault = Vault(schema=schema, records=records, relationship_pairs=pairs or [])
    vault._resolve_pairs()
    return vault


# ── No pairs ─────────────────────────────────────────────────────────────────

def test_no_pairs_records_unchanged():
    vault = _make_vault(
        projekte_recs=[("Alpha", {"Traeger": ["[[Personen/Anna]]"]})],
        personen_recs=[("Anna", {"Name": "Anna"})],
        pairs=None,
    )
    assert "Projekte" not in _recs(vault, "Personen")["Anna"].fields


# ── Primary side populates secondary ─────────────────────────────────────────

def test_secondary_populated_from_primary():
    vault = _make_vault(
        projekte_recs=[("Alpha", {"Traeger": ["[[Personen/Anna]]", "[[Personen/Ben]]"]})],
        personen_recs=[("Anna", {"Name": "Anna"}), ("Ben", {"Name": "Ben"})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    assert _recs(vault, "Personen")["Anna"].fields.get("Projekte") == ["[[Projekte/Alpha]]"]
    assert _recs(vault, "Personen")["Ben"].fields.get("Projekte") == ["[[Projekte/Alpha]]"]


def test_unlinked_record_gets_no_field():
    vault = _make_vault(
        projekte_recs=[("Alpha", {"Traeger": ["[[Personen/Anna]]"]})],
        personen_recs=[("Anna", {"Name": "Anna"}), ("Clara", {"Name": "Clara"})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    assert _recs(vault, "Personen")["Clara"].fields.get("Projekte") is None


def test_multiple_projects_accumulated_on_same_person():
    vault = _make_vault(
        projekte_recs=[
            ("Alpha", {"Traeger": ["[[Personen/Anna]]"]}),
            ("Beta",  {"Traeger": ["[[Personen/Anna]]"]}),
        ],
        personen_recs=[("Anna", {"Name": "Anna"})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    projekte = _recs(vault, "Personen")["Anna"].fields.get("Projekte", [])
    assert "[[Projekte/Alpha]]" in projekte
    assert "[[Projekte/Beta]]" in projekte


# ── Secondary side populates primary ─────────────────────────────────────────

def test_primary_populated_from_secondary():
    vault = _make_vault(
        projekte_recs=[("Alpha", {})],
        personen_recs=[("Anna", {"Name": "Anna", "Projekte": ["[[Projekte/Alpha]]"]})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
        personen_extra_fields=[
            FieldSchema(name="Projekte", type=FieldType.LIST_LINKS, link_target="Projekte")
        ],
    )
    traeger = _recs(vault, "Projekte")["Alpha"].fields.get("Traeger", [])
    assert "[[Personen/Anna]]" in traeger


# ── Outer union ───────────────────────────────────────────────────────────────

def test_outer_union_merges_both_sides():
    vault = _make_vault(
        projekte_recs=[("Alpha", {"Traeger": ["[[Personen/Anna]]"]})],
        personen_recs=[
            ("Anna", {"Name": "Anna"}),
            ("Ben",  {"Name": "Ben", "Projekte": ["[[Projekte/Alpha]]"]}),
        ],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
        personen_extra_fields=[
            FieldSchema(name="Projekte", type=FieldType.LIST_LINKS, link_target="Projekte")
        ],
    )
    traeger = _recs(vault, "Projekte")["Alpha"].fields.get("Traeger", [])
    assert "[[Personen/Anna]]" in traeger
    assert "[[Personen/Ben]]" in traeger


# ── Schema patching ───────────────────────────────────────────────────────────

def test_schema_patched_for_new_field():
    vault = _make_vault(
        projekte_recs=[("Alpha", {"Traeger": ["[[Personen/Anna]]"]})],
        personen_recs=[("Anna", {"Name": "Anna"})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    field_names = {f.name for f in vault.schema.types["Personen"].fields}
    assert "Projekte" in field_names


def test_schema_patch_type_and_link_target():
    vault = _make_vault(
        projekte_recs=[("Alpha", {"Traeger": ["[[Personen/Anna]]"]})],
        personen_recs=[("Anna", {"Name": "Anna"})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    field_map = {f.name: f for f in vault.schema.types["Personen"].fields}
    pf = field_map["Projekte"]
    assert pf.type == FieldType.LIST_LINKS
    assert pf.link_target == "Projekte"


def test_no_schema_patch_when_no_edges():
    vault = _make_vault(
        projekte_recs=[("Alpha", {})],
        personen_recs=[("Anna", {"Name": "Anna"})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    field_names = {f.name for f in vault.schema.types["Personen"].fields}
    assert "Projekte" not in field_names


# ── Existing wikilinks preserved ──────────────────────────────────────────────

def test_existing_wikilinks_preserved_and_new_appended():
    vault = _make_vault(
        projekte_recs=[
            ("Alpha", {"Traeger": ["[[Personen/Anna]]"]}),
            ("Beta",  {"Traeger": ["[[Personen/Anna]]"]}),
        ],
        personen_recs=[("Anna", {"Name": "Anna", "Projekte": ["[[Projekte/Alpha]]"]})],
        pairs=[("Projekte.Traeger", "Personen.Projekte")],
        personen_extra_fields=[
            FieldSchema(name="Projekte", type=FieldType.LIST_LINKS, link_target="Projekte")
        ],
    )
    projekte = _recs(vault, "Personen")["Anna"].fields.get("Projekte", [])
    assert "[[Projekte/Alpha]]" in projekte
    assert "[[Projekte/Beta]]" in projekte
    assert projekte.index("[[Projekte/Alpha]]") < projekte.index("[[Projekte/Beta]]")


# ── Self-referential pair ─────────────────────────────────────────────────────

def test_self_referential_pair():
    schema = Schema(types={"Personen": TypeSchema(name="Personen", fields=[
        FieldSchema(name="Name", type=FieldType.STRING),
        FieldSchema(name="father", type=FieldType.LINK, link_target="Personen"),
        FieldSchema(name="children", type=FieldType.LIST_LINKS, link_target="Personen"),
    ])})
    ts = schema.types["Personen"]
    alice = Record(name="Alice", type_schema=ts, fields={"father": "[[Personen/Bob]]"})
    bob   = Record(name="Bob",   type_schema=ts, fields={})
    vault = Vault(schema=schema, records={"Personen": [alice, bob]},
                  relationship_pairs=[("Personen.father", "Personen.children")])
    vault._resolve_pairs()
    assert "[[Personen/Alice]]" in bob.fields.get("children", [])


# ── Multiple pairs on different fields of the same type ──────────────────────

def test_multiple_pairs_both_contribute():
    proj_schema  = TypeSchema(name="Projekte",       fields=[FieldSchema(name="Traeger",    type=FieldType.LIST_LINKS, link_target="Personen")])
    event_schema = TypeSchema(name="Veranstaltungen", fields=[FieldSchema(name="Teilnehmer", type=FieldType.LIST_LINKS, link_target="Personen")])
    pers_schema  = TypeSchema(name="Personen",        fields=[FieldSchema(name="Name",       type=FieldType.STRING)])
    schema = Schema(types={"Projekte": proj_schema, "Veranstaltungen": event_schema, "Personen": pers_schema})

    alpha  = Record(name="Alpha",  type_schema=proj_schema,  fields={"Traeger":    ["[[Personen/Anna]]"]})
    summit = Record(name="Summit", type_schema=event_schema, fields={"Teilnehmer": ["[[Personen/Anna]]"]})
    anna   = Record(name="Anna",   type_schema=pers_schema,  fields={"Name": "Anna"})

    vault = Vault(schema=schema,
                  records={"Projekte": [alpha], "Veranstaltungen": [summit], "Personen": [anna]},
                  relationship_pairs=[
                      ("Projekte.Traeger",           "Personen.Projekte"),
                      ("Veranstaltungen.Teilnehmer", "Personen.Veranstaltungen"),
                  ])
    vault._resolve_pairs()

    field_names = {f.name for f in vault.schema.types["Personen"].fields}
    assert "Projekte" in field_names
    assert "Veranstaltungen" in field_names
    assert anna.fields.get("Projekte")       == ["[[Projekte/Alpha]]"]
    assert anna.fields.get("Veranstaltungen") == ["[[Veranstaltungen/Summit]]"]


# ── expand_to_lists behavior ──────────────────────────────────────────────────

def _make_teacher_vault(expand_to_lists):
    """Two teachers both list the same student — student.teacher (LINK) would overflow."""
    schema = Schema(types={"Personen": TypeSchema(name="Personen", fields=[
        FieldSchema(name="Name",     type=FieldType.STRING),
        FieldSchema(name="students", type=FieldType.LIST_LINKS, link_target="Personen"),
        FieldSchema(name="teacher",  type=FieldType.LINK,       link_target="Personen"),
    ])})
    ts = schema.types["Personen"]
    alice = Record(name="Alice", type_schema=ts, fields={"students": ["[[Personen/Bob]]"]})
    carol = Record(name="Carol", type_schema=ts, fields={"students": ["[[Personen/Bob]]"]})
    bob   = Record(name="Bob",   type_schema=ts, fields={})
    vault = Vault(schema=schema, records={"Personen": [alice, carol, bob]},
                  relationship_pairs=[("Personen.students", "Personen.teacher")])
    vault._resolve_pairs(expand_to_lists=expand_to_lists)
    return bob


def test_expand_to_lists_true_expands_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="vaults.vault"):
        bob = _make_teacher_vault(expand_to_lists=True)
    assert isinstance(bob.fields.get("teacher"), list)
    assert len(bob.fields["teacher"]) == 2
    assert any("singular LINK" in m for m in caplog.messages)


def test_expand_to_lists_false_raises():
    with pytest.raises(ValueError, match="singular LINK field"):
        _make_teacher_vault(expand_to_lists=False)


# ── Integration via from_vault ────────────────────────────────────────────────

def test_from_vault_populates_secondary():
    v = Vault.from_vault(
        FIXTURE,
        dangling_refs="stub",
        relationship_pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    anna = _recs(v, "Personen").get("Anna")
    assert anna is not None
    assert anna.fields.get("Projekte") == ["[[Projekte/Alpha]]"]


def test_from_vault_stub_gets_back_reference():
    v = Vault.from_vault(
        FIXTURE,
        dangling_refs="stub",
        relationship_pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    clara = _recs(v, "Personen").get("Clara")
    assert clara is not None
    assert clara.fields.get("Projekte") == ["[[Projekte/Beta]]"]


def test_from_vault_schema_includes_pair_field():
    v = Vault.from_vault(
        FIXTURE,
        dangling_refs="stub",
        relationship_pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )
    field_names = {f.name for f in v.schema.types["Personen"].fields}
    assert "Projekte" in field_names
