"""Tests for core Schema data model (Step 1)."""
from vaults import FieldSchema, FieldType, Schema, TypeSchema


def test_schema_defaults():
    s = Schema()
    assert s.types == {}
    assert s.data_folder == "data"
    assert s.bases_folder == "bases"


def test_schema_custom_folders():
    s = Schema(data_folder="records", bases_folder="views")
    assert s.data_folder == "records"
    assert s.bases_folder == "views"


def test_type_schema():
    ts = TypeSchema(name="E_Projekte")
    assert ts.name == "E_Projekte"
    assert ts.fields == []


def test_field_schema_no_link_target():
    f = FieldSchema(name="Beginn", type=FieldType.DATE)
    assert f.name == "Beginn"
    assert f.type == FieldType.DATE
    assert f.link_target is None


def test_field_schema_with_link_target():
    f = FieldSchema(name="A_Projektträger", type=FieldType.LIST_LINKS, link_target="E_A_Kollektive_Akteure")
    assert f.link_target == "E_A_Kollektive_Akteure"


def test_field_type_values():
    assert FieldType.STRING == "string"
    assert FieldType.LIST_LINKS == "list[link]"
    assert FieldType.UNKNOWN == "unknown"
