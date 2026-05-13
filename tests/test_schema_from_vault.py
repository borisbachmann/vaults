"""Tests for Schema.from_vault() (Step 3)."""
from pathlib import Path

import pytest

from vaults import FieldType, Schema

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


@pytest.fixture(scope="module")
def schema():
    return Schema.from_vault(FIXTURE)


def test_types_discovered(schema):
    assert set(schema.types.keys()) == {"Projekte", "Personen"}


def test_underscore_folder_ignored(schema):
    assert "_ignored" not in schema.types


def test_folder_conventions_stored(schema):
    assert schema.data_folder == "data"
    assert schema.bases_folder == "bases"


def test_custom_folder_names():
    s = Schema.from_vault(FIXTURE, data_folder="data", bases_folder="views")
    assert s.bases_folder == "views"


# --- Projekte type ---

def test_projekte_field_names(schema):
    names = {f.name for f in schema.types["Projekte"].fields}
    assert {"Titel", "Beginn", "Abgeschlossen", "Budget", "Traeger", "Staedte", "Erstellt", "full_text"} <= names


def test_projekte_titel_is_string(schema):
    f = _field(schema, "Projekte", "Titel")
    assert f.type == FieldType.STRING


def test_projekte_beginn_is_date(schema):
    f = _field(schema, "Projekte", "Beginn")
    assert f.type == FieldType.DATE


def test_projekte_erstellt_is_datetime(schema):
    f = _field(schema, "Projekte", "Erstellt")
    assert f.type == FieldType.DATETIME


def test_projekte_abgeschlossen_is_boolean(schema):
    f = _field(schema, "Projekte", "Abgeschlossen")
    assert f.type == FieldType.BOOLEAN


def test_projekte_budget_is_number(schema):
    f = _field(schema, "Projekte", "Budget")
    assert f.type == FieldType.NUMBER


def test_projekte_traeger_is_list_links(schema):
    f = _field(schema, "Projekte", "Traeger")
    assert f.type == FieldType.LIST_LINKS
    assert f.link_target == "Personen"


def test_projekte_staedte_is_list_strings(schema):
    f = _field(schema, "Projekte", "Staedte")
    assert f.type == FieldType.LIST_STRINGS
    assert f.link_target is None


def test_projekte_full_text_captured(schema):
    f = _field(schema, "Projekte", "full_text")
    assert f.type == FieldType.STRING


# --- Personen type ---

def test_personen_partei_is_link(schema):
    f = _field(schema, "Personen", "Partei")
    assert f.type == FieldType.LINK
    assert f.link_target == "Parteien"


def test_personen_aktiv_is_boolean(schema):
    f = _field(schema, "Personen", "Aktiv")
    assert f.type == FieldType.BOOLEAN


# helper

def _field(schema, type_name, field_name):
    fields = {f.name: f for f in schema.types[type_name].fields}
    assert field_name in fields, f"Field '{field_name}' not found in type '{type_name}'"
    return fields[field_name]
