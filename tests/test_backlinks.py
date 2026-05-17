"""Tests for _FileProxy.backlinks — the file.backlinks runtime property."""
import datetime
from pathlib import Path

from vaults import FieldSchema, FieldType, Record, Schema, TypeSchema, Vault
from vaults.syntax.compiler import BasesCompiler
from vaults.syntax.context import EvalContext
from vaults.syntax.runtime import _FileProxy

_NOW = datetime.datetime(2026, 1, 1)
_BASE = Path("/vault")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _vault(akteur_recs, projekte_recs):
    """Minimal in-memory vault: Projekte.Traeger links into Akteure."""
    a_schema = TypeSchema(name="Akteure", fields=[
        FieldSchema(name="Name", type=FieldType.STRING),
    ])
    p_schema = TypeSchema(name="Projekte", fields=[
        FieldSchema(name="Titel", type=FieldType.STRING),
        FieldSchema(name="Traeger", type=FieldType.LIST_LINKS, link_target="Akteure"),
    ])
    schema = Schema(types={"Akteure": a_schema, "Projekte": p_schema})
    records = {
        "Akteure": [Record(name=n, type_schema=a_schema, fields=f, path=p) for n, f, p in akteur_recs],
        "Projekte": [Record(name=n, type_schema=p_schema, fields=f, path=p) for n, f, p in projekte_recs],
    }
    return Vault(schema=schema, records=records)


def _proxy(vault, type_name, record_name):
    rec = next(r for r in vault.records[type_name] if r.name == record_name)
    return _FileProxy(rec, vault)


def _ctx(vault, type_name, record_name):
    rec = next(r for r in vault.records[type_name] if r.name == record_name)
    return EvalContext(record=rec, vault=vault, base_path=_BASE, now=_NOW)


# ── backlinks property ────────────────────────────────────────────────────────


def test_backlinks_no_vault():
    ts = TypeSchema(name="Akteure")
    rec = Record(name="Anna", type_schema=ts)
    assert _FileProxy(rec, None).backlinks == []


def test_backlinks_no_links():
    vault = _vault(
        akteur_recs=[("Anna", {"Name": "Anna"}, None)],
        projekte_recs=[("Alpha", {"Titel": "Alpha", "Traeger": []}, None)],
    )
    assert _proxy(vault, "Akteure", "Anna").backlinks == []


def test_backlinks_single_link():
    vault = _vault(
        akteur_recs=[("Anna", {"Name": "Anna"}, None)],
        projekte_recs=[("Alpha", {"Titel": "Alpha", "Traeger": "[[Akteure/Anna]]"}, None)],
    )
    assert _proxy(vault, "Akteure", "Anna").backlinks == ["[[Projekte/Alpha]]"]


def test_backlinks_list_link_field():
    vault = _vault(
        akteur_recs=[("Anna", {"Name": "Anna"}, None)],
        projekte_recs=[("Alpha", {"Titel": "Alpha", "Traeger": ["[[Akteure/Anna]]", "[[Akteure/Ben]]"]}, None)],
    )
    assert _proxy(vault, "Akteure", "Anna").backlinks == ["[[Projekte/Alpha]]"]


def test_backlinks_multiple_records():
    vault = _vault(
        akteur_recs=[("Anna", {"Name": "Anna"}, None)],
        projekte_recs=[
            ("Alpha", {"Titel": "Alpha", "Traeger": "[[Akteure/Anna]]"}, None),
            ("Beta",  {"Titel": "Beta",  "Traeger": ["[[Akteure/Anna]]"]}, None),
        ],
    )
    assert set(_proxy(vault, "Akteure", "Anna").backlinks) == {"[[Projekte/Alpha]]", "[[Projekte/Beta]]"}


def test_backlinks_deduplication_multiple_fields():
    """A record linking via two different fields should appear only once."""
    a_schema = TypeSchema(name="Akteure", fields=[FieldSchema(name="Name", type=FieldType.STRING)])
    p_schema = TypeSchema(name="Projekte", fields=[
        FieldSchema(name="Traeger",      type=FieldType.LIST_LINKS, link_target="Akteure"),
        FieldSchema(name="Beteiligte",   type=FieldType.LIST_LINKS, link_target="Akteure"),
    ])
    schema = Schema(types={"Akteure": a_schema, "Projekte": p_schema})
    anna = Record(name="Anna", type_schema=a_schema, fields={"Name": "Anna"})
    alpha = Record(name="Alpha", type_schema=p_schema, fields={
        "Traeger":    "[[Akteure/Anna]]",
        "Beteiligte": "[[Akteure/Anna]]",
    })
    vault = Vault(schema=schema, records={"Akteure": [anna], "Projekte": [alpha]})
    proxy = _FileProxy(anna, vault)
    assert proxy.backlinks == ["[[Projekte/Alpha]]"]


def test_backlinks_excludes_self():
    """A record that links to itself must not appear in its own backlinks."""
    a_schema = TypeSchema(name="Akteure", fields=[
        FieldSchema(name="Related", type=FieldType.LINK),
    ])
    schema = Schema(types={"Akteure": a_schema})
    anna = Record(name="Anna", type_schema=a_schema, fields={"Related": "[[Akteure/Anna]]"})
    vault = Vault(schema=schema, records={"Akteure": [anna]})
    assert _FileProxy(anna, vault).backlinks == []


# ── Formula chain integration ─────────────────────────────────────────────────


def test_backlinks_formula_map_asfile():
    """file.backlinks.map(value.asFile()) yields FileProxy objects."""
    vault = _vault(
        akteur_recs=[("Anna", {"Name": "Anna"}, None)],
        projekte_recs=[("Alpha", {"Titel": "Alpha", "Traeger": "[[Akteure/Anna]]"},
                        Path("/vault/data/Projekte/Alpha.md"))],
    )
    fn = BasesCompiler().translate("file.backlinks.map(value.asFile())")
    result = fn(_ctx(vault, "Akteure", "Anna"))

    assert len(result) == 1
    assert isinstance(result[0], _FileProxy)
    assert result[0].basename == "Alpha"


def test_backlinks_formula_infolder_filter():
    """file.backlinks.map(value.asFile()).filter(value.inFolder(...)) filters by path."""
    vault = _vault(
        akteur_recs=[("Anna", {"Name": "Anna"}, None)],
        projekte_recs=[
            ("Alpha", {"Titel": "Alpha", "Traeger": "[[Akteure/Anna]]"},
             Path("/vault/data/Projekte/Alpha.md")),
            ("Beta",  {"Titel": "Beta",  "Traeger": "[[Akteure/Anna]]"},
             Path("/vault/data/Sonstiges/Beta.md")),
        ],
    )
    fn = BasesCompiler().translate(
        'file.backlinks.map(value.asFile()).filter(value.inFolder("data/Projekte"))'
    )
    result = fn(_ctx(vault, "Akteure", "Anna"))

    assert len(result) == 1
    assert result[0].basename == "Alpha"


def test_backlinks_formula_properties_filter():
    """filter(value.properties.X.contains(file.asLink())) matches on linked property."""
    a_schema = TypeSchema(name="Akteure", fields=[FieldSchema(name="Name", type=FieldType.STRING)])
    p_schema = TypeSchema(name="Projekte", fields=[
        FieldSchema(name="Traeger",    type=FieldType.LIST_LINKS, link_target="Akteure"),
        FieldSchema(name="Beteiligte", type=FieldType.LIST_LINKS, link_target="Akteure"),
    ])
    schema = Schema(types={"Akteure": a_schema, "Projekte": p_schema})

    anna = Record(name="Anna", type_schema=a_schema, fields={"Name": "Anna"},
                  path=Path("/vault/data/Akteure/Anna.md"))
    ben  = Record(name="Ben",  type_schema=a_schema, fields={"Name": "Ben"},
                  path=Path("/vault/data/Akteure/Ben.md"))

    # Alpha: Anna is Traeger; Beta: Anna is only Beteiligte
    alpha = Record(name="Alpha", type_schema=p_schema,
                   fields={"Traeger": "[[Akteure/Anna]]", "Beteiligte": "[[Akteure/Ben]]"},
                   path=Path("/vault/data/Projekte/Alpha.md"))
    beta  = Record(name="Beta",  type_schema=p_schema,
                   fields={"Traeger": "[[Akteure/Ben]]", "Beteiligte": "[[Akteure/Anna]]"},
                   path=Path("/vault/data/Projekte/Beta.md"))

    vault = Vault(schema=schema, records={"Akteure": [anna, ben], "Projekte": [alpha, beta]})

    fn = BasesCompiler().translate(
        'file.backlinks'
        '.map(value.asFile())'
        '.filter(value.inFolder("data/Projekte"))'
        '.filter(value.properties.Traeger.contains(file.asLink()))'
    )
    result = fn(EvalContext(record=anna, vault=vault, base_path=_BASE, now=_NOW))

    # Result is still _FileProxy here (formula evaluated in isolation, not through from_vault
    # serialization). In the vault loading path, _FileProxy objects are converted to wikilinks.
    assert len(result) == 1
    assert result[0].basename == "Alpha"


def test_map_flatmap_nested_list():
    """map() auto-flattens list results — needed for .map(value.properties.X) where X is multi-value."""
    a_schema = TypeSchema(name="Akteure", fields=[FieldSchema(name="Name", type=FieldType.STRING)])
    p_schema = TypeSchema(name="Projekte", fields=[
        FieldSchema(name="Traeger", type=FieldType.LIST_LINKS, link_target="Akteure"),
        FieldSchema(name="Städte",  type=FieldType.LIST_LINKS),
    ])
    schema = Schema(types={"Akteure": a_schema, "Projekte": p_schema})
    anna  = Record(name="Anna",  type_schema=a_schema, fields={"Name": "Anna"})
    alpha = Record(name="Alpha", type_schema=p_schema,
                   fields={"Traeger": "[[Akteure/Anna]]", "Städte": ["[[Aachen|Aachen]]", "[[Berlin|Berlin]]"]},
                   path=Path("/vault/data/Projekte/Alpha.md"))
    beta  = Record(name="Beta",  type_schema=p_schema,
                   fields={"Traeger": "[[Akteure/Anna]]", "Städte": ["[[Berlin|Berlin]]", "[[Köln|Köln]]"]},
                   path=Path("/vault/data/Projekte/Beta.md"))
    vault = Vault(schema=schema, records={"Akteure": [anna], "Projekte": [alpha, beta]})

    fn = BasesCompiler().translate(
        'file.backlinks'
        '.map(value.asFile())'
        '.filter(value.inFolder("data/Projekte"))'
        '.map(value.properties.Städte)'
        '.unique()'
    )
    result = fn(EvalContext(record=anna, vault=vault, base_path=_BASE, now=_NOW))

    assert isinstance(result, list)
    assert len(result) == 3  # Aachen, Berlin, Köln — Berlin deduplicated across both projects
