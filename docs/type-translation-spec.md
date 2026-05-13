# Type translation: FieldType → DuckDB

The schema-derivation logic infers a `FieldType` for each field by inspecting all values across all records of a type. That `FieldType` then maps to a DuckDB column type as follows.

## FieldType enum

```python
class FieldType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    LINK = "link"
    LIST_STRINGS = "list[string]"
    LIST_LINKS = "list[link]"
    LIST_MIXED = "list[mixed]"
    UNKNOWN = "unknown"
```

`LINK` and `LIST_LINKS` are genuinely distinct types, mirroring the YAML distinction between a string and a sequence. Do not collapse them. The same applies to `STRING` vs `LIST_STRINGS`.

`LIST_MIXED` and `UNKNOWN` are diagnostic types for malformed or unrecognized fields. They map to permissive DuckDB types and are flagged by `.lint()`.

## Inference rules

- **`STRING` vs `LINK`**: detected by syntax of the parsed value. A string matching `[[...]]` is a link; otherwise a string. Applied per-value, then aggregated per-field: if all values in a field across all records of a type are links, the field is `LINK`; if all are non-link strings, `STRING`; mixed → `UNKNOWN`.
- **`LIST_STRINGS` vs `LIST_LINKS`**: same syntax check applied element-wise. A list of all-link elements is `LIST_LINKS`; all-string elements `LIST_STRINGS`; mixed elements within a list (or across records) is `LIST_MIXED`.
- **`NUMBER`**: YAML parses numeric values as Python `int` or `float`. The field is `NUMBER` if all non-null values parse as numeric.
- **`BOOLEAN`**: YAML parses `true`/`false` as Python `bool`. The field is `BOOLEAN` if all non-null values parse as booleans.
- **`DATE`**: the field is `DATE` if all non-null values are ISO 8601 date strings parseable by `datetime.date.fromisoformat`. Other date formats fall back to `STRING`. Datetimes (with time component) are out of scope for now — treat as `STRING`.

## DuckDB type mapping

| FieldType | DuckDB column type | Notes |
|---|---|---|
| `STRING` | `VARCHAR` | |
| `NUMBER` | `BIGINT` or `DOUBLE` | See numeric refinement below. |
| `BOOLEAN` | `BOOLEAN` | |
| `DATE` | `DATE` | |
| `LINK` | — | No column in the main table. Represented entirely via a join table. |
| `LIST_STRINGS` | `VARCHAR[]` | DuckDB native array. |
| `LIST_LINKS` | — | No column in the main table. Represented entirely via a join table. |
| `LIST_MIXED` | `VARCHAR[]` | Best-effort: store as array of strings. Flagged by `.lint()`. |
| `UNKNOWN` | `VARCHAR` | Best-effort: store as text. Flagged by `.lint()`. |

## Numeric refinement: `BIGINT` vs `DOUBLE`

For fields inferred as `NUMBER`, perform a second pass on the parsed Python values (not the raw YAML strings):

- If every non-null parsed value is a Python `int`, the column is `BIGINT`.
- If any non-null parsed value is a Python `float`, the column is `DOUBLE`.

This matches the behavior of pandas/DataFrame type inference: `1` and `1.0` are distinguishable at the parser level, and the presence of any float promotes the column to floating-point.

## Link fields are not columns

Fields of type `LINK` or `LIST_LINKS` do not produce columns in the main table for their source type. They are represented exclusively in join tables (specified separately). This applies uniformly: there is no special case where a scalar link becomes a foreign-key column in the main table.
