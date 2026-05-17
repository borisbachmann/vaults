# Vault Enrichment Layer — Implementation Plan

## Context

This plan extends the existing Vault/Schema package (see separate spec document) with a write-capable enrichment layer. The core package treats an Obsidian vault as a read-only in-memory snapshot derived from disk. This plan adds the ability to write new data to disk through a controlled interface that preserves the snapshot-faithfulness contract.

---

## Core data contract

**The Vault remains a faithful mirror of disk.** Enrichment operations do not mutate the in-memory Vault. They write directly to disk and then reload the Vault from disk. The user never observes a divergent in-memory state.

**All integrity machinery lives on the Vault.** Schema knowledge, staleness detection, and reload functionality are Vault responsibilities. The enrichment layer calls into the Vault for these; it does not maintain its own copies.

**`vault.enrichment` is a namespace on the Vault, not a separate object.** This prevents the two from getting detached. Every enrichment call is `vault.enrichment.<method>(...)`.

---

## Staleness

- **Fingerprint**: hash over `(relative_path, mtime, size)` tuples for all files in the vault, excluding `_`-prefixed folders. Computed on `from_vault()` and stored.
- **`vault.stale`**: a property (not a settable attribute) that recomputes the disk fingerprint on access and compares to the stored value. No `_stale` flag to mutate. The property is cheap enough to call freely.
- **Content hashing is dropped** as the default fingerprint strategy. No opt-in for content hashing; users who need stronger guarantees can diff schemas explicitly.
- **`vault.reload()`**: re-reads from disk, updates internal state in place. Returns the same Vault instance (mutation-in-place). The user's `vault` variable remains valid across reloads.

---

## Enrichment write flow

Every enrichment operation follows this sequence:

1. Check `vault.stale`. If stale, refuse with a clear error message instructing the user to reload (or pass `force=True` to override).
2. Perform the write to disk.
3. Call `vault.reload()` to refresh the in-memory snapshot.

Result: the user never holds a stale Vault between operations. Linting, graph export, etc., always reflect current disk state.

**Cost note**: every enrichment call triggers a full reload. Acceptable for single operations; wasteful for batches. See *Deferred* section.

---

## Build order

Build in this sequence. Each phase reuses primitives from the previous one.

1. **Rows first** — `add_records`. Creates new files. No existing data touched. Simplest blast radius.
2. **Columns second** — `add_column`. Touches every existing record of a type. Higher complexity (null handling, overwrite policy, missing filename matches).
3. **Whole dataframes last** — `add_type` / `import_dataframe`. Combines both, plus type creation and link-column resolution.

---

## Internal representation

**List of dicts with basic Python types.** The sole null sentinel is `None`. No pandas or polars types inside the enrichment core.

**Adapters at the input boundary** convert external types to the internal format:

- `pd.DataFrame` → `df.to_dict(orient='records')`; anything matching `pd.isna(x)` becomes `None`.
- `pl.DataFrame` → `df.to_dicts()`; `pl.Null` already maps to `None`.
- `pd.Series` / `pl.Series` → dict of `{index: value}` with the same null handling.
- `dict[str, list]` (column-oriented) → transposed to list of dicts.
- `list[dict]` → passed through.

Adapter imports are lazy: pandas/polars are only imported when those types appear as input.

---

## Null contract

**Input side.** Any of the following becomes "empty":

- `None`, `pd.NA`, `np.nan`, `pl.Null`.
- Empty string `""` for text fields.
- Empty list `[]` for list fields.

`0`, `0.0`, and `False` are values, not empties. Empty string is null *only* for text fields; for other types it is user error (warn).

**Output side.** Write the canonical empty for each field type:

- Text, number, date, link: bare key `field:` (no value).
- List (any element type): bare key `field:`. Do not write `[]` — Obsidian rewrites it to bare/null on edit.
- Boolean: bare key `field:` for empty; `false` is reserved as an actual value. Preserves three-valued logic where the source supports it.

**Type inference is removed.** Source types are known from the input (polars dtype, pandas dtype, explicit declaration). The Enricher does not infer field types. Type collisions are not the Enricher's concern; they surface on the next read via the linter.

**Why this works despite Obsidian's quirks.** Obsidian's property editor collapses empty-string, null, and bare-key representations in its own UI. Preserving distinctions between these is futile once a user touches a file. Pick one canonical form per type and accept the loss.

---

## Filenames and identifiers

- **Within a folder, filename is the identifier.** Filesystem uniqueness suffices. Enrichment operations are scoped to one type/folder at a time; no cross-folder ambiguity.
- **Sanitization**: use the existing centralized sanitization function. Do not duplicate logic.
- **Collisions** are resolved with `_2`, `_3`, etc., suffixes appended to the sanitized name before the `.md` extension. Collision check is against (a) existing files on disk and (b) other records being created in the same batch.
- **Warnings** fire when:
  - A suffix is appended (collision occurred).
  - The sanitization function changed the input string.
  Routine sanitization (already-valid input) does not warn.
- **`add_records` returns the final filenames.** Users need these to do anything else with the records.

**Aliases are deferred.** Enrichment writes plain `[[folder/filename]]` links with no alias logic. Alias normalization across the vault is a janitor task.

---

## New properties

**`allow_new_properties: bool = True`** parameter on `add_records`.

- **True (default)**: if a record contains a property not in the existing schema for that type, write it. Emit one aggregated warning per (type, property) pair within the call:

  > "Property `X` is new for type `Y` and now exists only in the newly created record(s) [list]. To propagate it across all records of this type, use the janitor."

- **False**: drop unknown properties before writing. Emit a different aggregated warning:

  > "Property `X` was dropped from record(s) [list] because it is not in the current schema for type `Y`."

Warnings are aggregated per (type, property), not per record.

---

## Paired link fields (interim behavior)

For relationship pairs declared in `relationship_pairs`:

- **Outer-join semantics**: when enriching either side of a pair, gather all link pairs from both fields across all affected records, take the union, write both sides.
- **No removal**: outer join preserves existing links. Enrichment cannot remove a link via this mechanism.

This is interim behavior pending the derived-fields work (see *Deferred*), which will replace one side of every pair with a formula-driven lookup and eliminate the outer-join logic entirely.

---

## YAML library

**Use `ruamel.yaml`**, not `pyyaml`. Key order preservation matters because Obsidian's Properties panel displays in file order, and the planned janitor will impose canonical property orders.

`python-frontmatter` may be used as a thin convenience wrapper, but only if configured with `ruamel.yaml` as the underlying handler. If it fights us, drop it and use `ruamel.yaml` directly.

---

## Linter division of labor

The community Linter plugin already handles file-level YAML and markdown hygiene. Do not replicate.

**Defer to the Linter plugin:**

- YAML formatting consistency, quoting, array styles.
- Within-file key ordering (single file).
- Trailing whitespace, blank line handling around frontmatter.
- Generic tag and alias formatting.

**`Vault.lint()` covers (relational and cross-record concerns):**

- Link target consistency across all records of a type.
- Backlink policy for relationship pairs.
- Flat folder structure.
- Orphaned links.
- Cross-record schema concerns: missing fields, type drift, mixed link targets.

Document the split in `.lint()` docstring so users know to also run the Linter plugin.

---

## Failure and conflict policy

**Overall policy: conservative by default. Every operation that could silently alter or skip data errors by default. Permissive behavior is always an explicit opt-in. When the conservative path proceeds (e.g., `skip` or `suffix` modes), a warning fires.**

### Error and warning quality requirements

- **Errors that exit the operation must contain enough information to locate the problems in the source data.** Not "validation failed" but: which records, which fields, which filenames, which indices. Example: "filename collision: 3 records in batch would produce `Müller_2.md`, which already exists. Affected record indices: [12, 47, 89]. Pass `on_collision='suffix'` to append suffixes, or deduplicate the source."
- **Warnings must be detailed enough to trace affected records in the vault after the operation completes.** List specific filenames, types, and properties. The user must be able to find every affected record without re-running the operation.

### 1. Partial-failure semantics for batches

`on_error: Literal["atomic", "continue"] = "atomic"` on `add_records`.

- **`atomic` (default)**: if any record in the batch fails validation, write nothing. Report all failures with record indices and reasons.
- **`continue`**: write records that pass validation; skip records that fail. Emit a warning listing every skipped record with index and reason.

### 2. Collision policy

`on_collision: Literal["error", "suffix"] = "error"` on `add_records`.

- **`error` (default)**: any filename collision (against existing files on disk or within the batch) fails the batch. Error lists every colliding filename and the affected record indices.
- **`suffix`**: apply `_2`, `_3`, etc., suffixes. Warning fires per (type, filename) pair, listing the original sanitized name, the resulting suffixed filename, and the source record index.

This is deliberately stricter than auto-suffixing. Silent suffixing across hundreds of records would be hard to audit; requiring explicit opt-in surfaces the collision rate to the user.

### 3. Overwrite policy for `add_column`

`on_existing: Literal["error", "skip", "overwrite"] = "error"` on `add_column`.

- **`error` (default)**: if the target property already exists on any record, the batch fails. Error lists affected filenames.
- **`skip`**: records that already have the property keep their existing value. Only records missing the property are written. Warning lists every skipped filename. Useful for gap-filling existing data.
- **`overwrite`**: every targeted record gets the new value regardless of prior state. Warning lists every overwritten filename and (if feasible) the previous value.

`error` does not need a warning — the error itself is the signal.

### 4. Missing filename matches in `add_column`

`on_missing: Literal["error", "skip"] = "error"` on `add_column`.

- **`error` (default)**: if the column mapping references filenames that do not exist in the target type folder, fail the batch. Error lists every unmatched filename.
- **`skip`**: ignore unmatched filenames. Warning lists every skipped filename.

Hard error by default because an unmatched filename usually means something is off with the dataset (typo, stale export, wrong type folder).

### 5. Missing values in the filename column for `add_records`

`on_missing_filename: Literal["error", "skip"] = "error"` on `add_records`.

- **`error` (default)**: any record with a null/empty value in the filename column fails the whole batch. Error lists affected record indices.
- **`skip`**: skip records with missing filename values. Warning lists every skipped record index.

Same rationale as (4): a missing filename signals a problem with the source data, not a routine condition.

---

## Deferred (do not build now)

**Enrichment-adjacent:**

- **Batch / pipeline context**: `with vault.enrichment.batch(): ...` to accumulate writes and reload once at exit. Build only if per-call reload cost becomes a problem in practice.
- **Dry-run / preview mode**: `preview=True` on write methods, runs validation without writing. Useful for large CSV imports.

**Janitor (separate work, listed for cross-reference):**

1. Cross-type field reconciliation (propagate new properties, fill missing fields with canonical empties).
2. Base view sync — keep each type-folder's primary `.base` file showing all properties of that type.
3. Alias normalization across the vault.
4. Property order imposition per type.
5. Fill missing fields with canonical empties on hand-created notes.

**Other (separate design tasks):**

- **Derived fields / formula-backed reverse relationships.** Replace one side of paired links with formula-driven lookups (Bases or Dataview). Eliminates the outer-join problem and the "enrichment cannot remove links" limitation. Higher priority than initially scoped — simplifies enrichment if done.
- **DB join table naming / views.** Simplification of `{source_type}__{field_name}` naming, with collision rules. Not yet specified.

---

## Return value: per-record result ledger

Every enrichment operation returns a `list[EntryResult]` — one entry per record the operation touched. The list is the full ledger of what happened.

```python
@dataclass
class EntryResult:
    filename: str                    # final filename after sanitization/suffixing
    status: Literal["processed", "warning", "skipped"]
    warning_types: list[str]         # empty list if no warnings
```

**Status values:**

- **`processed`**: record was written successfully with no caveats.
- **`warning`**: record was written, but at least one warning applies (collision suffix applied, name sanitized, new property introduced, value overwritten, etc.). `warning_types` lists every warning that fired for this record.
- **`skipped`**: record was intentionally not written under an opt-in permissive flag (`on_error="continue"`, `on_existing="skip"`, `on_missing="skip"`, `on_missing_filename="skip"`). `warning_types` indicates why.

**Multiple warnings per record** are supported via the `warning_types: list[str]` field. A single record can be both sanitized and collision-suffixed; both warning types appear in its entry.

**Usage:**

```python
result = vault.enrichment.add_records(...)

# Default: ignore the return value entirely. Warnings still go to logging.
vault.enrichment.add_records(...)

# Capture for inspection on large batches:
result = vault.enrichment.add_records(...)
warnings = [e for e in result if e.status == "warning"]
collisions = [e for e in result if "collision" in e.warning_types]
skipped = [e for e in result if e.status == "skipped"]
filenames = [e.filename for e in result]
```

**Parallel logging.** Every warning captured in an `EntryResult` is *also* emitted via `logging.warning`. The structured return is additive, not a replacement. Users who don't capture the return value still see warnings in log output.

**Errors do not appear in the result ledger.** Errors raise exceptions; the result object is for successful operations (which may include warnings or intentional skips). An operation that errors does not return a partial ledger.

**Warning type vocabulary** (initial set; extend as needed):

- `collision` — filename collision resolved by suffix.
- `sanitization` — input string altered by the sanitization function.
- `new_property` — record introduced a property not in the existing schema (`allow_new_properties=True` path).
- `dropped_property` — record had a property dropped (`allow_new_properties=False` path).
- `overwrite` — existing property value replaced (`on_existing="overwrite"`).
- `skipped_existing` — record left untouched because property already exists (`on_existing="skip"`).
- `skipped_missing` — record skipped because filename match failed (`on_missing="skip"`).
- `skipped_missing_filename` — record skipped because filename column value was null (`on_missing_filename="skip"`).
- `skipped_validation` — record skipped because validation failed (`on_error="continue"`).

---



The enrichment layer is complete when:

- `vault.enrichment.add_records(...)` writes new records to disk, reloads the vault, and the resulting vault passes `lint()`.
- `vault.enrichment.add_column(...)` writes a new property across existing records of a type, reloads, and passes `lint()`.
- `vault.enrichment.add_type(...)` (or equivalent dataframe import) creates a new type with records and link columns, reloads, and passes `lint()`.
- All staleness, null, and collision contracts behave as specified.
- All failure and conflict policy defaults behave as specified, with error and warning messages meeting the quality requirements (locatable problems, traceable affected records).
