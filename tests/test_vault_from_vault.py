"""Tests for Vault.from_vault() (Step 2)."""
from pathlib import Path

import pytest

from vaults import Record, TypeSchema, Vault

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


@pytest.fixture(scope="module")
def vault():
    return Vault.from_vault(FIXTURE)


def test_schema_derived(vault):
    assert set(vault.schema.types.keys()) == {"Projekte", "Personen"}


def test_path_stored(vault):
    assert vault.path == FIXTURE


def test_records_keys_match_schema(vault):
    assert set(vault.records.keys()) == set(vault.schema.types.keys())


def test_record_count(vault):
    assert len(vault.records["Projekte"]) == 3
    assert len(vault.records["Personen"]) == 2


def test_record_is_record_instance(vault):
    assert isinstance(vault.records["Projekte"][0], Record)


def test_record_name(vault):
    names = {r.name for r in vault.records["Projekte"]}
    assert names == {"Alpha", "Beta", "Gamma"}


def test_record_type_property(vault):
    r = vault.records["Projekte"][0]
    assert r.type == "Projekte"


def test_record_type_schema_reference(vault):
    r = vault.records["Projekte"][0]
    assert isinstance(r.type_schema, TypeSchema)
    assert r.type_schema is vault.schema.types["Projekte"]


def test_record_path_is_md_file(vault):
    r = vault.records["Projekte"][0]
    assert r.path is not None
    assert r.path.suffix == ".md"
    assert r.path.exists()


def test_record_scalar_fields(vault):
    r = {r.name: r for r in vault.records["Projekte"]}["Alpha"]
    assert r.fields["Titel"] == "Alpha-Projekt"
    assert r.fields["Abgeschlossen"] is False
    assert r.fields["Budget"] == 50000


def test_record_list_field(vault):
    r = {r.name: r for r in vault.records["Projekte"]}["Alpha"]
    assert isinstance(r.fields["Traeger"], list)
    assert len(r.fields["Traeger"]) == 2


def test_record_full_text_present(vault):
    r = {r.name: r for r in vault.records["Projekte"]}["Alpha"]
    assert "full_text" in r.fields
    assert "Volltext" in r.fields["full_text"]


def test_record_no_full_text_when_body_empty(vault):
    r = {r.name: r for r in vault.records["Projekte"]}["Beta"]
    assert "full_text" not in r.fields


def test_relationship_pairs_default_empty(vault):
    assert vault.relationship_pairs == []


def test_relationship_pairs_stored():
    pairs = [("Projekte.Traeger", "Personen.Projekte")]
    v = Vault.from_vault(FIXTURE, relationship_pairs=pairs)
    assert v.relationship_pairs == pairs


def test_ignore_empty_propagates_to_schema():
    v = Vault.from_vault(
        Path(__file__).parent / "fixtures" / "malformed_vault",
        ignore_empty=True,
    )
    assert "Akteure" not in v.schema.types
    assert "Akteure" not in v.records
