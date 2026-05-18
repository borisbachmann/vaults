# Code Map

Structural reference for the `vaults` package. Update when the code changes.

## Module overview

| Module | Lines | Purpose |
|--------|------:|---------|
| `vaults/__init__.py` | 16 | Public API re-exports |
| `vaults/vault.py` | 350 | `Vault` dataclass, `from_vault()` loader, coercion helpers, formula/filter helpers |
| `vaults/record.py` | 19 | `Record` dataclass |
| `vaults/schema.py` | 224 | `Schema`, `TypeSchema`, `FieldSchema`, `FieldType` enum, type inference |
| `vaults/linter.py` | 456 | `Linter` class, `LintViolation`, per-rule check methods (S-*, R-*, F-*) |
| `vaults/links.py` | 296 | Wikilink parsing, `resolve_pairs()`, `apply_dangling_refs()`, link type constants |
| `vaults/syntax/__init__.py` | 4 | Re-exports `BasesCompiler`, `EvalContext` |
| `vaults/syntax/compiler.py` | 288 | `BasesCompiler` — Lark AST to Python callables |
| `vaults/syntax/context.py` | 21 | `EvalContext` dataclass |
| `vaults/syntax/grammar.py` | 65 | Lark grammar definition, `_parser` singleton |
| `vaults/syntax/runtime.py` | 527 | Proxy classes (`_FileProxy`, `_NoteProxy`, etc.), safe operators, method/global dispatch |
| `vaults/accessors/__init__.py` | 5 | Re-exports accessor classes |
| `vaults/accessors/dfs.py` | 405 | `DfsAccessor`, `TableAccessor`, `ViewsAccessor` — Arrow/pandas/polars output |
| `vaults/accessors/db.py` | 86 | `DbAccessor` — DuckDB export |
| `vaults/accessors/graph.py` | 391 | `GraphAccessor` — NetworkX, Kuzu, RDF/Turtle export |
| `vaults/enrichment/__init__.py` | 3 | Re-exports `Enrichment`, `EntryResult` |
| `vaults/enrichment/core.py` | 417 | `Enrichment` class — `add_records()`, `add_column()`, `add_type()` |
| `vaults/enrichment/writers.py` | 165 | File I/O — `_write_md_file`, `_patch_frontmatter_property`, `sanitize_filename` |
| `vaults/enrichment/adapters.py` | 80 | Input normalization — `_to_records()`, `_to_column()`, `_normalize_null()` |

## Dataclass fields

### `FieldType(str, Enum)` — `schema.py`
`STRING`, `NUMBER`, `BOOLEAN`, `DATE`, `DATETIME`, `LINK`, `LIST_STRINGS`, `LIST_LINKS`, `LIST_MIXED`, `UNKNOWN`, `FORMULA`, `INTEGER`

### `FieldSchema` — `schema.py`
| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | |
| `type` | `FieldType` | |
| `link_target` | `Optional[str]` | Target type folder name |
| `formula` | `Optional[str]` | Raw Bases formula expression |
| `output_type` | `Optional[FieldType]` | Inferred after formula evaluation |
| `compiled` | `Optional[Callable]` | Compiled formula callable, `repr=False` |

Property: `effective_type` -> resolves FORMULA to `output_type` or UNKNOWN.

### `TypeSchema` — `schema.py`
| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | |
| `fields` | `list[FieldSchema]` | |
| `base_filter` | `Optional[str]` | Serialized filter expression |
| `compiled_filter` | `Optional[Callable]` | Compiled filter callable |
| `property_display` | `dict[str, str]` | Bases property path -> display name |

### `Schema` — `schema.py`
| Field | Type | Notes |
|-------|------|-------|
| `types` | `dict[str, TypeSchema]` | Keyed by type folder name |
| `data_folder` | `str` | Default `"data"` |
| `bases_folder` | `str` | Default `"bases"` |

Methods: `_from_vault(path, ...)` classmethod, `diff(other)`.

### `Record` — `record.py`
| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | Filename stem |
| `type_schema` | `TypeSchema` | Back-reference |
| `fields` | `dict[str, Any]` | Frontmatter key-values |
| `path` | `Optional[Path]` | Filesystem path |

Property: `type` -> `type_schema.name`.

### `Vault` — `vault.py`
| Field | Type | Notes |
|-------|------|-------|
| `schema` | `Schema` | |
| `records` | `dict[str, list[Record]]` | Keyed by type name |
| `path` | `Optional[Path]` | Vault root |
| `relationship_pairs` | `list[tuple[str, str]]` | e.g. `[("Projekte.Leitung", "Personen.leitet")]` |
| `violations` | `list[LintViolation]` | |
| `fingerprint` | `str` | SHA-256 of data folder contents |
| `_load_kwargs` | `dict[str, Any]` | Kwargs for `reload()` |
| `_backlinks_index` | `Optional[dict[str, list[str]]]` | Lazy cache, `init=False` |
| `_db_cache` | `Optional[DbAccessor]` | Lazy cache, `init=False` |
| `_dfs_cache` | `Optional[DfsAccessor]` | Lazy cache, `init=False` |
| `_graph_cache` | `Optional[GraphAccessor]` | Lazy cache, `init=False` |
| `_enrichment_cache` | `Optional[Enrichment]` | Lazy cache, `init=False` |

Properties: `db`, `dfs`, `graph`, `enrichment` (cached lazy accessor creation), `is_stale`.
Methods: `from_vault(path, ...)` classmethod, `reload()` (invalidates all caches), `lint()`, `_resolve_pairs()`, `_get_backlinks_index()`, `_build_backlinks_index()`.
Module-level helpers: `coerce_target()`, `coerce_value()`, `_evaluate_formulas()`, `_apply_base_filters()`, `_serialize_formula_result()`.

### `LintViolation` — `linter.py`
| Field | Type | Notes |
|-------|------|-------|
| `severity` | `str` | `"error"` / `"warning"` / `"suggestion"` |
| `message` | `str` | |
| `rule` | `Optional[str]` | e.g. `"R-1"`, `"S-3"` |
| `type_name` | `Optional[str]` | |
| `field_name` | `Optional[str]` | |
| `details` | `Optional[dict]` | Rule-specific detail payload |

### `EvalContext` — `syntax/context.py`
| Field | Type | Notes |
|-------|------|-------|
| `record` | `Record` | Current record being evaluated |
| `vault` | `Vault` | Full vault for cross-record lookups |
| `base_path` | `Path` | Path to the `.base` file |
| `now` | `datetime.datetime` | Frozen timestamp for determinism |

### `EntryResult` — `enrichment/core.py`
| Field | Type | Notes |
|-------|------|-------|
| `filename` | `str` | |
| `status` | `Literal["processed", "warning", "skipped"]` | |
| `warning_types` | `list[str]` | e.g. `"sanitization"`, `"collision"`, `"new_property"` |

## Data flow

```
Vault.from_vault(path)
  |
  +--> Schema._from_vault(path)
  |      |- iterate data/{TypeDir}/*.md -> frontmatter.load()
  |      |- infer_field_type() / infer_link_target() per field
  |      |- read bases/{TypeName}.base -> YAML
  |      |- BasesCompiler.translate() for formulas
  |      +- BasesCompiler.translate_filter() for base filters
  |
  +--> Load records: frontmatter.load() per .md -> Record(name, type_schema, fields, path)
  |
  +--> Proto-vault (temporary Vault for formula eval cross-record access)
  |
  +--> Formula evaluation loop:
  |      |- _topo_sort_formulas() -> ordered, cyclic
  |      |- For each formula field: EvalContext -> compiled(ctx) -> _serialize() -> rec.fields[f.name]
  |      +- infer_field_type() / infer_link_target() on results
  |
  +--> Base filter application (if apply_base_filters=True):
  |      +- EvalContext -> compiled_filter(ctx) -> keep/drop record
  |
  +--> Construct final Vault, compute fingerprint
  |
  +--> vault.lint() -> Linter(vault).lint()
  |      |- _check_structure()  -> S-1..S-5
  |      |- _check_records()    -> R-1..R-6, F-4
  |      +- _check_formulas()   -> F-1..F-3
  |
  +--> apply_dangling_refs(mode, schema, records)
  |      +- "drop": remove dangling links / "stub": create stub records+types
  |
  +---> resolve_pairs(vault) -> bidirectional link reconciliation
```

### Accessor data flow

```
vault.dfs["TypeName"]          -> TableAccessor -> to_arrow() / to_pandas() / to_polars()
vault.dfs["TypeName"].views    -> ViewsAccessor -> ["ViewName"] -> TableAccessor (with view config)
vault.db.to_duckdb()           -> DuckDB connection (type tables + join tables for links)
vault.graph.to_networkx()      -> nx.DiGraph (nodes=records, edges=links)
vault.graph.to_kuzu()          -> Kuzu connection (node tables + rel tables)
vault.graph.to_rdf()           -> rdflib.Graph (OWL classes, properties, individuals)
```

### Enrichment data flow

```
vault.enrichment.add_records(type_name, data, filename_col=...)
  |- _to_records(data)       -> list[dict]  (adapters.py)
  |- _process_batch(...)     -> failures, results, to_write
  |- _write_md_file(...)     -> disk  (writers.py)
  |- _update_reverse_links() -> patch paired files
  +- vault.reload()

vault.enrichment.add_column(type_name, column, property_name=...)
  |- _to_column(column)      -> dict[str, Any]
  |- _patch_frontmatter_property(...)  -> surgical YAML insert/update
  +- vault.reload()

vault.enrichment.add_type(type_name, data, filename_col=...)
  |- _to_records() + _process_batch()
  |- _write_base_file(...)   -> creates .base file
  |- _write_md_file(...)     -> creates record files
  +- vault.reload()
```

## Cross-module dependencies

```
vault.py
  <- schema.py (FieldSchema, FieldType, Schema, infer_field_type, infer_link_target)
  <- record.py (Record)
  <- links.py  (apply_dangling_refs, resolve_pairs)
  <- linter.py (Linter, LintViolation, topo_sort_formulas)
  <- syntax/   (EvalContext)
  <- syntax/runtime.py (_FileProxy — inside _serialize_formula_result)

schema.py
  <- syntax/ (BasesCompiler — inside _from_vault)
  <- links.py (is_wikilink, wikilink_target_folder — inside infer_*)

linter.py
  <- schema.py (FieldSchema, FieldType)
  <- links.py  (LINK_TYPES, iter_link_names, parse_wikilink, wikilink_target_folder)
  <- syntax/   (BasesCompiler)

links.py
  <- schema.py (FieldSchema, FieldType, Schema, TypeSchema)
  <- record.py (Record)

syntax/compiler.py
  <- syntax/context.py (EvalContext)
  <- syntax/grammar.py (_parser)
  <- syntax/runtime.py (proxies, operators, dispatch tables)

syntax/runtime.py
  <- syntax/context.py (EvalContext)
  <- record.py (Record)
  <- links.py (WIKILINK_RE, parse_wikilink_name)

accessors/dfs.py
  <- syntax/ (BasesCompiler, EvalContext)
  <- schema.py (FieldSchema, FieldType, serialize_filter)
  <- links.py (LIST_LINK_TYPES, parse_wikilink_name)
  <- vault.py (coerce_target, coerce_value)

accessors/db.py
  <- schema.py (FieldType)
  <- links.py  (LINK_TYPES_PAIRED, iter_link_names)

accessors/graph.py
  <- schema.py (FieldSchema, FieldType)
  <- links.py  (iter_link_names, LINK_TYPES)

enrichment/core.py
  <- enrichment/adapters.py
  <- enrichment/writers.py
  <- links.py (parse_wikilink — inside _update_reverse_links)

enrichment/writers.py
  (no internal deps besides vault.py TYPE_CHECKING)

enrichment/adapters.py
  (no internal deps)
```

## Key constants

### Linter rules (`linter.py: RULES`)
| Rule | Description |
|------|-------------|
| S-1 | Missing `.base` file for a type folder |
| S-2 | Bases folder not found |
| S-3 | Base file without matching type folder |
| S-4 | Non-record file or subdirectory inside a type folder |
| S-5 | Cross-platform incompatible name |
| R-1 | Field not present across all records of a type |
| R-2 | Field value type inconsistent across records |
| R-3 | Links in one field point to multiple target folders |
| R-4 | Link targets a folder outside known types |
| R-5 | Paired link field has missing backlinks |
| R-6 | Link target file does not exist on disk |
| F-1 | Formula not translatable |
| F-2 | Formula references unknown field |
| F-3 | Circular formula dependency |
| F-4 | Formula output type inconsistent across records |

### Link type tuples (`links.py`)
- `LINK_TYPES = (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED)`
- `LIST_LINK_TYPES = (FieldType.LIST_LINKS, FieldType.LIST_MIXED)`
- `LINK_TYPES_PAIRED = (FieldType.LINK, FieldType.LIST_LINKS)`

### Wikilink regex (`links.py`)
`WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")`
Captures `folder/name` path inside `[[...]]`, ignoring optional `|display` alias.

## Public API (`__init__.py`)

```python
__all__ = [
    "FieldType", "FieldSchema", "TypeSchema", "Schema",
    "Vault", "Record", "Linter", "LintViolation", "RULES",
    "BasesCompiler", "EvalContext",
    "DfsAccessor",
    "infer_field_type", "infer_link_target",
    "EntryResult",
]
```

## Test structure

### Fixture vaults (`tests/fixtures/`)
| Fixture | Types | Purpose |
|---------|-------|---------|
| `sample_vault` | Projekte (2), Personen (2) | Primary: covers STRING, DATE, DATETIME, BOOLEAN, INTEGER, LIST_LINKS, LINK, LIST_STRINGS, formulas |
| `dangling_vault` | Projekte (1), Personen (1) | Links to missing records and unknown types |
| `malformed_vault` | Projekte (1+nested), Akteure (empty) | Mixed link targets, nested files, empty folders |

### Test files (18 files, ~3,100 lines)
| File | Tests | Covers |
|------|------:|--------|
| `test_schema_model.py` | 4 | Schema/TypeSchema/FieldSchema construction |
| `test_schema_from_vault.py` | 14 | `Schema._from_vault()`, field type inference |
| `test_schema_malformed_vault.py` | 12 | Edge cases: mixed links, nested files, empty types |
| `test_schema_diff.py` | 15 | `Schema.diff()` |
| `test_inference.py` | 23 | `infer_field_type()`, `infer_link_target()` |
| `test_vault_from_vault.py` | 17 | `Vault.from_vault()` record/schema population |
| `test_vault_dangling_links.py` | 18 | Dangling ref modes: drop/stub |
| `test_vault_staleness.py` | 7 | Fingerprint and `is_stale` |
| `test_resolve_pairs.py` | 28 | `resolve_pairs()` bidirectional reconciliation |
| `test_backlinks.py` | 22 | `_FileProxy.backlinks`, formula chain integration |
| `test_linter_new_rules.py` | ~30 | Linter rules R-2, R-6, S-*, F-* |
| `test_dfs_accessor.py` | 40 | Arrow/pandas/polars output, column ordering, views |
| `test_coerce_types.py` | 20 | `_coerce_target`, `_coerce_value`, cache isolation |
| `test_graph_accessor.py` | 28 | NetworkX, Kuzu names, RDF export |
| `test_vault_to_db.py` | 30 | DuckDB tables, join tables, formula integration |
| `test_enrichment_add_records.py` | 32 | `add_records()`, collision, sanitization, reverse links |
| `test_enrichment_add_column.py` | 30 | `add_column()`, on_existing/on_missing modes |
| `test_enrichment_add_type.py` | 28 | `add_type()`, base file generation |
