"""Tests for Schema.to_json() / Schema.from_json() (Step 4)."""
import json
from pathlib import Path

import pytest

from vaults import FieldSchema, FieldType, Schema, TypeSchema

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"


@pytest.fixture
def derived_schema():
    return Schema.from_vault(FIXTURE)


def test_round_trip_types(derived_schema, tmp_path):
    p = tmp_path / "schema.json"
    derived_schema.to_json(p)
    loaded = Schema.from_json(p)
    assert set(loaded.types.keys()) == set(derived_schema.types.keys())


def test_round_trip_folder_names(derived_schema, tmp_path):
    p = tmp_path / "schema.json"
    derived_schema.to_json(p)
    loaded = Schema.from_json(p)
    assert loaded.data_folder == derived_schema.data_folder
    assert loaded.bases_folder == derived_schema.bases_folder


def test_round_trip_field_types(derived_schema, tmp_path):
    p = tmp_path / "schema.json"
    derived_schema.to_json(p)
    loaded = Schema.from_json(p)
    orig_fields = {f.name: f for f in derived_schema.types["Projekte"].fields}
    loaded_fields = {f.name: f for f in loaded.types["Projekte"].fields}
    for name, orig in orig_fields.items():
        assert loaded_fields[name].type == orig.type
        assert loaded_fields[name].link_target == orig.link_target


def test_round_trip_custom_folder_names(tmp_path):
    s = Schema(data_folder="records", bases_folder="views")
    s.types["Items"] = TypeSchema(name="Items", fields=[
        FieldSchema(name="Label", type=FieldType.STRING),
    ])
    p = tmp_path / "schema.json"
    s.to_json(p)
    loaded = Schema.from_json(p)
    assert loaded.data_folder == "records"
    assert loaded.bases_folder == "views"


def test_json_structure(derived_schema, tmp_path):
    p = tmp_path / "schema.json"
    derived_schema.to_json(p)
    raw = json.loads(p.read_text())
    assert "data_folder" in raw
    assert "bases_folder" in raw
    assert "types" in raw
    assert isinstance(raw["types"], dict)
    first_type = next(iter(raw["types"].values()))
    assert isinstance(first_type, list)
    assert all("name" in f and "type" in f for f in first_type)


def test_json_unicode_preserved(tmp_path):
    s = Schema()
    s.types["Städte"] = TypeSchema(name="Städte", fields=[
        FieldSchema(name="Name", type=FieldType.STRING),
    ])
    p = tmp_path / "schema.json"
    s.to_json(p)
    raw = p.read_text(encoding="utf-8")
    assert "Städte" in raw


def test_from_json_unknown_field_type_raises(tmp_path):
    bad = {"data_folder": "data", "bases_folder": "bases", "types": {
        "Items": [{"name": "X", "type": "not_a_real_type", "link_target": None}]
    }}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        Schema.from_json(p)
