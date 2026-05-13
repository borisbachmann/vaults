"""Tests for Schema.diff() (Step 5)."""
from vaults import FieldSchema, FieldType, Schema, TypeSchema


def _base() -> Schema:
    s = Schema()
    s.types["Projekte"] = TypeSchema(name="Projekte", fields=[
        FieldSchema(name="Titel", type=FieldType.STRING),
        FieldSchema(name="Beginn", type=FieldType.DATE),
        FieldSchema(name="Traeger", type=FieldType.LIST_LINKS, link_target="Personen"),
    ])
    s.types["Personen"] = TypeSchema(name="Personen", fields=[
        FieldSchema(name="Name", type=FieldType.STRING),
    ])
    return s


def test_identical_schemas_empty_diff():
    a = _base()
    b = _base()
    d = a.diff(b)
    assert d == {"added_types": [], "removed_types": [], "changed_fields": {}}


def test_added_type():
    a = _base()
    b = _base()
    b.types["Städte"] = TypeSchema(name="Städte", fields=[])
    d = a.diff(b)
    assert d["added_types"] == ["Städte"]
    assert d["removed_types"] == []


def test_removed_type():
    a = _base()
    b = _base()
    del b.types["Personen"]
    d = a.diff(b)
    assert d["removed_types"] == ["Personen"]
    assert d["added_types"] == []


def test_added_field():
    a = _base()
    b = _base()
    b.types["Projekte"].fields.append(FieldSchema(name="Budget", type=FieldType.NUMBER))
    d = a.diff(b)
    assert "Projekte" in d["changed_fields"]
    assert d["changed_fields"]["Projekte"]["added_fields"] == ["Budget"]
    assert d["changed_fields"]["Projekte"]["removed_fields"] == []


def test_removed_field():
    a = _base()
    b = _base()
    b.types["Projekte"].fields = [f for f in b.types["Projekte"].fields if f.name != "Beginn"]
    d = a.diff(b)
    assert d["changed_fields"]["Projekte"]["removed_fields"] == ["Beginn"]


def test_changed_field_type():
    a = _base()
    b = _base()
    for f in b.types["Projekte"].fields:
        if f.name == "Beginn":
            f.type = FieldType.STRING
    d = a.diff(b)
    assert "Beginn" in d["changed_fields"]["Projekte"]["changed_fields"]


def test_changed_link_target():
    a = _base()
    b = _base()
    for f in b.types["Projekte"].fields:
        if f.name == "Traeger":
            f.link_target = "Organisationen"
    d = a.diff(b)
    assert "Traeger" in d["changed_fields"]["Projekte"]["changed_fields"]


def test_unchanged_type_not_in_changed_fields():
    a = _base()
    b = _base()
    b.types["Projekte"].fields.append(FieldSchema(name="Budget", type=FieldType.NUMBER))
    d = a.diff(b)
    assert "Personen" not in d["changed_fields"]


def test_diff_is_directional():
    a = _base()
    b = _base()
    b.types["Neu"] = TypeSchema(name="Neu", fields=[])
    assert "Neu" in a.diff(b)["added_types"]
    assert "Neu" in b.diff(a)["removed_types"]
