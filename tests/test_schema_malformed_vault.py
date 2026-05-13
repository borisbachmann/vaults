"""Tests for Schema.from_vault() on malformed or edge-case vault structures.

Fixture layout:
  malformed_vault/data/Projekte/
    Alpha.md                  ← Beteiligte links to two different folders (Personen + Organisationen)
    subfolder/
      nested_record.md        ← nested file; should be silently ignored
  malformed_vault/data/Akteure/
    (empty type folder)       ← no .md files
"""
from pathlib import Path

import pytest

from vaults import FieldType, Schema

FIXTURE = Path(__file__).parent / "fixtures" / "malformed_vault"


@pytest.fixture(scope="module")
def schema():
    return Schema.from_vault(FIXTURE)


# --- Mixed link targets ---

def test_mixed_link_targets_field_type(schema):
    # Beteiligte has links to both Personen/ and Organisationen/ → still LIST_LINKS
    f = _field(schema, "Projekte", "Beteiligte")
    assert f.type == FieldType.LIST_LINKS


def test_mixed_link_targets_no_link_target(schema):
    # link_target must be None because targets are ambiguous
    f = _field(schema, "Projekte", "Beteiligte")
    assert f.link_target is None


# --- Nested subfolder ---

def test_nested_file_not_included(schema):
    # subfolder/nested_record.md must not contribute any fields
    titles = [f for f in schema.types["Projekte"].fields if f.name == "Titel"]
    assert len(titles) == 1  # only from Alpha.md
    # The nested file's Titel value "Should not appear…" must not be present
    # (we can't inspect raw values at schema level, but field count is stable)


def test_subfolder_not_treated_as_type(schema):
    # 'subfolder' inside Projekte/ must not appear as a top-level type
    assert "subfolder" not in schema.types


# --- Empty type folder ---

def test_empty_type_folder_produces_no_fields(schema):
    # Akteure has no .md files → TypeSchema with empty fields list (ignore_empty=False default)
    assert "Akteure" in schema.types
    assert schema.types["Akteure"].fields == []


def test_ignore_empty_excludes_empty_type():
    s = Schema.from_vault(FIXTURE, ignore_empty=True)
    assert "Akteure" not in s.types


def test_ignore_empty_keeps_populated_types():
    s = Schema.from_vault(FIXTURE, ignore_empty=True)
    assert "Projekte" in s.types


# helper

def _field(schema, type_name, field_name):
    fields = {f.name: f for f in schema.types[type_name].fields}
    assert field_name in fields, f"Field '{field_name}' not found in type '{type_name}'"
    return fields[field_name]
