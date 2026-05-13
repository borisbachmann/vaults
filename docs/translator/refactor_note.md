# Refactoring note: what to extract from BasesCompiler into `runtime.py`

The runtime layer is reusable beyond the compiler. Two plausible future consumers:

1. **`to_db()` / `to_graph()`** — if these ever need to understand link structure beyond raw wikilink strings
2. **Future augmentation layer** — reading existing record data via the same proxy model when writing computed properties back

## What belongs in `runtime.py`

- `_FileProxy`, `_ThisProxy`, `_NoteProxy` — Bases-like object model over vault records
- `_resolve_link()` — general vault link traversal
- `_safe_add()`, `_safe_sub()`, `_safe_compare()` — safe operators
- `_parse_duration()`, `_parse_date()`, `_to_number()`, `_format_date()`, `_relative_date()` — type coercion and date utilities
