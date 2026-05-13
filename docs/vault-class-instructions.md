# Vault Class — Design & Implementation Instructions

## Overview

Build a Python package centered on two classes — `Schema` and `Vault` — that treat an Obsidian vault as a serialization format for structured, relationally linked research data. The schema is always derivable from the data. The vault is a read-only in-memory snapshot that can be loaded from and written to multiple backends.

## Vault folder conventions

An Obsidian vault managed by this package always follows this layout:

```
<vault_root>/
  <data_folder>/        ← one subfolder per type; each .md file is a record
    TypeA/
      record1.md
      record2.md
    TypeB/
      ...
  <bases_folder>/       ← one .base file per type (Obsidian Bases config)
    TypeA.base
    TypeB.base
  _schema/              ← reserved; ignored during parsing
```

- **`data_folder`**: default name `"data"`. Contains one subfolder per type. Each subfolder maps to a table (type); each `.md` file in it is a row (record); YAML frontmatter keys are the columns (fields).
- **`bases_folder`**: default name `"bases"`. Contains Obsidian Bases configuration files (`.base`), one per type.
- Both folder names are stored on the `Schema` object so they travel with the schema when it is saved/loaded.
- The user can override both names at schema-derivation time or at vault-creation time (`from_schema`).

## Core concepts

### Schema

A `Schema` object describes the shape of the data:

- **Types**: each type corresponds to a subfolder inside `data_folder` in a vault, a table in a DB, or a node type in a graph.
- **Fields per type**: each field has a name and an inferred type (string, list of strings, boolean, number, date, link, list of links).
- **Link targets**: for link fields, the target type (i.e., which folder/table the links point to). Inferred by inspecting where links actually resolve.
- **Relationship pair mapping** (optional): an explicit declaration that two link fields on different types represent the same relationship (e.g., `Company.Shareholder` ↔ `Person.holds_shares`). This is the one piece of information that cannot be inferred and must be provided by the user if needed.
- **`data_folder`** and **`bases_folder`**: the folder names used by this vault (stored on the schema, defaults `"data"` and `"bases"`).

A Schema is never required as input (except for `Vault.from_schema()`). It is always derived from whatever data source is loaded.

A Schema can also be saved to and loaded from a file (JSON) for inspection, diffing, or version control.

#### `Schema.from_vault(path, data_folder="data", bases_folder="bases", ignore_empty=False)`

Derives a Schema directly from a vault folder, without loading record data. Useful for inspection and diffing without constructing a full `Vault`.

- `ignore_empty` (default `False`): if `True`, type folders that contain no `.md` files are excluded from the schema entirely. If `False` (default), they are included as `TypeSchema` instances with an empty fields list — this preserves round-trip fidelity when a vault has placeholder folders.
- Dangling links (wikilinks to non-existing files) are harmless here — the folder prefix still provides the link target type. No special handling needed at schema derivation time.

### Vault

A `Vault` object holds:

- A `Schema` (derived on load)
- All records in memory, organized by type
- A staleness fingerprint (see below)

The Vault is a **read-only snapshot**. It is not a live connection to files on disk. Editing happens in Obsidian. Analytics happens via the Vault object in Python.

## Factory methods

All factory methods return a `Vault` instance with a derived schema. None require a schema as input (except `from_schema`).

### `Vault.from_vault(path, data_folder="data", bases_folder="bases", relationship_pairs=None, dangling_links="keep", dangling_types="keep")`

Load an existing Obsidian vault from disk.

- `path`: root directory of the vault.
- `data_folder`: name of the subfolder inside `path` that contains all type folders (default `"data"`).
- `bases_folder`: name of the subfolder inside `path` that contains `.base` files (default `"bases"`).
- Scan all subfolders of `<path>/<data_folder>` — each is a type.
- Parse every `.md` file: YAML frontmatter → field values, document body → `full_text` field (if non-empty).
- Derive the schema by inspecting all fields across all records:
  - Scalars vs. lists inferred from values.
  - Link fields identified by wikilink syntax (`[[folder/filename]]`).
  - Link target type inferred from the folder portion of wikilinks.
  - If all links in a given field across all records of a type point to the same folder, that field is a valid link field. Mixed targets → lint violation.
- Store `data_folder` and `bases_folder` on the derived `Schema`.
- Compute a staleness fingerprint (see Staleness section).
- Ignore any folders starting with `_` (e.g., `_schema/`) — these are metadata, not types.

**Dangling link handling** (`dangling_links` parameter — applies to record data, not schema derivation):

Dangling links are wikilinks whose target file does not exist on disk. They are common in Obsidian (links are often prepared before the target file is created). At schema derivation time they are harmless — the folder prefix still gives us the link target type. The option only affects how record data is loaded:

| Value | Behaviour |
|---|---|
| `"keep"` (default) | Load the raw wikilink as-is. `lint()` will flag it as an orphaned link. |
| `"drop"` | Remove the dangling reference from the loaded record silently. |
| `"stub"` | Create a minimal record stub (name only, no other fields) for the missing file, if the target type is known from the schema. Useful when referential integrity matters for graph/DB output. |

**Dangling type handling** (`dangling_types` parameter): wikilinks whose target folder does not exist as a type in the vault at all.

| Value | Behaviour |
|---|---|
| `"keep"` (default) | Leave the wikilink as-is. |
| `"drop"` | Remove the wikilink from the field value. |
| `"stub"` | Create a stub `TypeSchema` (empty fields) and a stub `Record` for each referenced name. The stub type is added to the schema and to `vault.records`. |

**Nested file handling**: files found in subfolders within a type folder are ignored by the loader but flagged with a `logging.warning` so the caller is not silently losing data.

### `Vault.from_db(db_path, relationship_pairs=None)`

Load from a SQLite (or DuckDB) database.

- Each table that is not a join table → a type.
- Join tables (tables with exactly two foreign key columns and no other data columns) → link fields on the source type.
- Column types → field types.
- Foreign keys → link fields with inferred target types.
- Derive the schema from the table structure.

### `Vault.from_graph(graph, relationship_pairs=None)`

Load from a NetworkX graph.

- Node attributes `type` → types.
- Remaining node attributes → fields.
- Edge attribute `field_name` → link fields.
- Edge attribute `source_type` + `target_type` → link target inference.
- Derive the schema from the graph structure.

### `Vault.from_schema(schema, path, data_folder=None, bases_folder=None)`

Create an empty vault on disk from a schema definition.

- `data_folder` and `bases_folder` default to the values stored on the schema (which were set at derivation time). Pass explicit values here to override them for this specific output path.
- Create `<path>/<data_folder>/` with one subfolder per type.
- Create `<path>/<bases_folder>/` for Obsidian Bases configurations.
- Optionally write `_schema/` JSON files.
- Return a Vault instance with the schema and no records.

## Output methods

### `.to_vault(path)`

Write the in-memory data to disk as an Obsidian vault.

- One folder per type, one `.md` file per record.
- Frontmatter: all fields except `full_text`.
- Document body: `full_text` field content, if present.
- Wikilinks constructed as `[[folder/filename]]`.
- **Staleness check before writing** (see Staleness section).

### `.to_db(path=None)`

Compile the vault into a SQLite database (or return a DuckDB connection, or a dict of DataFrames — the exact interface can be decided during implementation).

- One table per type, containing all scalar fields.
- One join table per link field, named `{source_type}__{field_name}`. Two columns: `source` (filename/ID of the record containing the link) and `target` (filename/ID of the linked record).
- If relationship pairs are defined, paired fields produce a single join table rather than two.

### `.to_graph()`

Return a NetworkX graph.

- One node per record, with `type` attribute set to the type name and all scalar fields as node attributes.
- One edge per link, with attributes: `field_name` (the originating field), `source_type`, `target_type`.
- If relationship pairs are defined, paired fields produce edges of a single named type.

### `.lint()`

Run integrity checks. Return a structured report (not just print output) listing all violations.

**Rule 1 — Link target consistency**: All links within a single field across all records of a type must point to the same folder. Any link pointing to an unexpected folder is a violation.

**Rule 2 — Backlink policy**: Configurable per vault (or per relationship pair):
- `forbid`: if both sides of a relationship contain explicit links, flag one side as a violation.
- `merge`: silently treat both sides as the same relationship. No violation, but `.to_vault()` writes only the canonical side.
- `allow`: no check. Both sides are independent fields. This is the default if no relationship pairs are defined.

**Rule 3 — Flat folder structure**: Every record file must live in a top-level folder directly under the vault root. Subfolders within type folders, or files at the vault root, are violations.

Additional checks (non-critical, reported as warnings):
- Orphaned links: wikilink targets that don't resolve to an existing file.
- Empty required-like fields: fields that are populated in >90% of records but missing in a few (heuristic, not enforced).
- Unknown fields: fields present in frontmatter but not seen in most records of that type.

### `.schema`

Property that returns the derived `Schema` object. The schema can be:
- Inspected programmatically.
- Saved to JSON: `vault.schema.to_json(path)`.
- Compared to a previous schema: `vault.schema.diff(other_schema)` — returns added/removed types, added/removed fields, changed field types, changed link targets.

## Staleness detection

On instantiation via `from_vault()`, compute a fingerprint of the vault state. This can be:
- A hash of all file modification timestamps.
- Or a hash of all file contents (slower but more reliable).

The fingerprint is stored on the Vault instance. Before any write operation (`.to_vault()`), recompute the fingerprint from disk and compare:
- If unchanged: proceed.
- If changed: raise an error with a clear message ("Vault on disk has been modified since this snapshot was loaded. Re-instantiate with `from_vault()` or pass `force=True` to overwrite.").

`force=True` is available as an escape hatch, but the default behavior protects against accidental data loss.

## File and naming conventions

- **Folder names**: sanitized type names (no spaces, filesystem-safe). Unicode normalized to NFC (umlauts preserved as single characters, e.g., `Städte` stays `Städte`). A configuration option `ascii_only=True` can be set to replace umlauts with ASCII equivalents (ä→ae, ö→oe, ü→ue, ß→ss) for environments that don't handle unicode paths well. Default is `ascii_only=False`.
- **Filenames**: derived from the record's primary display field, sanitized with the same unicode/ASCII policy. Collisions resolved with numeric suffix (`_2`, `_3`).
- **`_schema/` folder**: reserved for JSON schema files. Ignored during vault parsing.
- **Wikilink format**: `[[folder/filename]]` — always includes the folder prefix for unambiguous resolution.

## Relationship pair mapping

The optional `relationship_pairs` parameter accepted by factory methods is a list of pairs:

```python
relationship_pairs = [
    ("E_Projekte.A_Projektträger", "E_Kollektive_Akteure.P_Projektträger"),
    ("E_A_Personen.Partei", "E_Kollektive_Akteure.A_Parteimitglieder"),
]
```

Each pair declares: these two fields represent the same relationship. Effects:
- `.to_db()`: one join table instead of two.
- `.to_graph()`: one edge type instead of two.
- `.lint()`: backlink policy applies to these pairs.
- `.to_vault()`: only the first field in the pair is written as an explicit link. The second is expected to be derived via backlinks/queries in Obsidian.

If no pairs are provided, every link field is treated as independent.

## Implementation notes

- Use `python-frontmatter` or `pyyaml` for YAML parsing.
- Use `sqlite3` or `duckdb` for database output.
- Use `networkx` for graph output.
- The package should be installable and usable as a library (`from vault import Vault`).
- All operations should work without Obsidian running. The vault is just a folder of markdown files.
- Logging: use Python's `logging` module. The migration log (records processed, collisions, unresolved links) should be available programmatically, not just printed.

## Out of scope (for now)

- Live file watching or sync.
- Obsidian plugin integration.
- Schema enforcement at write time (validation is always post-hoc via `.lint()`).
- GUI or web interface.
