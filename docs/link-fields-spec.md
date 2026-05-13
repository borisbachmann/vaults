# Link fields: join tables and relationship pairs

This section specifies how link fields (`LINK` and `LIST_LINKS`) are represented in DuckDB. The DuckDB layer is treated here purely as a query interface. Round-trip considerations are deferred and addressed separately.

## Join table per link field

Each link field on each type produces one join table in DuckDB.

- **Name**: `{source_type}__{field_name}`.
- **Columns**:
  - `source` (`VARCHAR`): filename/ID of the record containing the link.
  - `target` (`VARCHAR`): filename/ID of the linked record.
- **Row semantics**: one row per link instance. A `LINK` field produces at most one row per source record; a `LIST_LINKS` field produces zero or more. The join table structure is identical in both cases — the scalar/list distinction lives in the source type's schema, not in the join table.
- **Empty fields**: a record with no value in a link field produces zero rows.
- **Orphaned links**: not present at this layer. Dangling links are resolved upstream during vault materialization via the `dangling_links` parameter on `Vault.from_vault()` (see below). By the time data reaches `.to_db()`, every `target` value is guaranteed to exist as a `record` in some type table.

## Relationship pairs

A relationship pair is an explicit user declaration that two link fields on different types represent the same underlying relationship. It is the one piece of structural information that cannot be inferred from the data and must be provided by the user.

```python
relationship_pairs = [
    ("Person.holds_shares", "Company.shareholder"),
]
```

The declaration says: these two fields are the same relationship. The library does not infer this; it acts on it when told.

### Storage: one base table per pair

When a pair is declared, the two fields share a single base table.

- The **first field** in the pair declaration determines the base table name and the orientation of `source` / `target`.
- For `("Person.holds_shares", "Company.shareholder")`:
  - Base table: `persons__holds_shares`.
  - `source` holds person IDs; `target` holds company IDs.
- No separate `companies__shareholder` base table is created.

### Query symmetry: views

To allow the user to query the relationship from either side using the natural field name, a DuckDB view is created for the second field in the pair.

```sql
CREATE VIEW companies__shareholder AS
SELECT target AS source, source AS target
FROM persons__holds_shares;
```

From the user's perspective, both `persons__holds_shares` and `companies__shareholder` are queryable. Each presents `source` and `target` columns oriented for its own side of the relationship. The view is transparently derived from the base table; there is no data duplication and no synchronization concern.

This applies to every declared pair. Unpaired link fields produce only their own base table, no view (see below).

## Unpaired link fields: no reverse views

For link fields without a declared pair, only the base join table is created. No reverse view is generated, even when multiple unpaired fields target the same type.

The reasoning: the pair declaration is the user's vocabulary for asserting that a reverse direction has a name. Without that declaration, the target type has no field name for the reverse direction, and any name the library invented would be fiction. Multiple source fields can point to the same target type (e.g., `project.lead_actor` and `project.former_lead_actor` both targeting `organization`), and there is no single "projects" relationship from the organization's perspective — there are several, with no Obsidian-declared name to distinguish them.

Users who want to query from the reverse side of an unpaired field do so explicitly against the base table:

```sql
-- "Which projects have this organization as lead actor?"
SELECT source FROM projects__lead_actor WHERE target = 'acme';
```

This is one SQL statement, no more verbose than a forward query, and it makes no claim about structure that Obsidian did not declare.

If reverse queries against a particular field become frequent, the user is expected to declare the relationship pair — at which point the reverse view is created and queryable by the declared field name. The pair declaration is the mechanism for promoting an ad-hoc reverse query into a first-class queryable direction.

### Summary rule

- **Declared pair**: base table + reverse view. Both directions queryable by their declared field names.
- **No declared pair**: base table only. Reverse queries go through the base table with the target ID in the `WHERE` clause.

### Naming convention

The base table is always named after the first field in the pair declaration. The user controls direction by ordering the pair. This convention is internal to the library; users who want a specific table name should order the pair accordingly, but otherwise need not concern themselves with which side is the base.

## Column types and constraints

- `source` and `target` are `VARCHAR`. The record ID — the filename with `.md` stripped — is used. Record tables expose this ID as a `record` column, which serves as the primary key.
- `(source, target)` is declared as a composite primary key on each base join table. Duplicate link instances (the same source linking to the same target twice in the same field) are not stored. If duplicates appear in the source data, `.lint()` flags them.
- **Foreign keys are not declared.** By the time data reaches `.to_db()`, the vault is internally consistent (see "Dangling links" below), so FK enforcement would re-validate something already validated. The DuckDB layer is treated as a query interface; validation is upstream. Skipping FKs keeps DDL uniform across modes and avoids insertion-order constraints during table population.

## Dangling links

Links to non-existent targets are resolved upstream during vault materialization, not at the DuckDB layer. `Vault.from_vault()` accepts a `dangling_links` parameter with two values:

- `"drop"`: dangling references are silently removed during load. The materialized vault contains only well-formed, resolvable links.
- `"stub"`: minimal stub records are created for missing targets. If the target type itself does not exist (the folder is missing), a stub type is created with no fields beyond `record`.

In both modes, the resulting vault is internally consistent. Every link in every record resolves to a real record of a real type. Consequently, every row in every join table has `source` and `target` values that exist as `record` values in their respective type tables. No special handling is required at the DuckDB layer.

## Population from data

The link target type for each field is inferred from the folder portion of wikilinks (e.g., `[[companies/acme]]` → target type `companies`). All links in a single field across all records of a type must resolve to the same folder; mixed targets are a lint violation (Rule 1 in the design doc).

For each record:

- Each `LINK` field contributes 0 or 1 rows to its join table.
- Each `LIST_LINKS` field contributes 0 or more rows.
- All link rows reference targets that exist in their respective type tables (guaranteed by the `dangling_links` resolution upstream).

For declared pairs:

- Rows from both fields populate the single base table.
- A row from the second field is written with `source` and `target` swapped relative to its native orientation, so that the base table's column semantics are consistent.
- If both sides of the relationship contain the same link instance (i.e., the link is declared in both Obsidian fields), the composite primary key collapses them to a single row. This is intentional and matches the "merge" backlink policy.
