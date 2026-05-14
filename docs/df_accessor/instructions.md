DataFrame Accessor — Design & Implementation Instructions
Overview
Add a dfs accessor to the Vault class that exposes per-type tabular data as DataFrames. Each type can be materialized as a pandas or Polars DataFrame. If the vault contains Obsidian .base files, the views defined in those files are accessible as additional DataFrames with view-specific column selection and ordering.
This is an output-only interface. No round-trip back to records is supported at this stage.
Architecture
Top-level access pattern
pythonvault.dfs["my_type"].to_pandas()
vault.dfs["my_type"].to_polars()
vault.dfs["my_type"].to_arrow()

vault.dfs["my_type"].views["my_view"].to_pandas()
vault.dfs["my_type"].views["my_view"].to_polars()
vault.dfs["my_type"].views["my_view"].to_arrow()
All access is dict-style (__getitem__). No attribute access. This sidesteps Python identifier constraints on type and view names.
Accessor classes
Three accessor classes, each thin:

DfsAccessor: returned by Vault.dfs. Dict-like, keyed by type name. Implements __getitem__, __contains__, __iter__, keys(), __len__, __repr__.
TableAccessor: returned by DfsAccessor[type_name] and by ViewsAccessor[view_name]. Holds a reference to the Vault, the type name, and an optional view config. Exposes to_pandas(), to_polars(), to_arrow(), and .views. Useful __repr__ (type name, record count, view count if applicable).
ViewsAccessor: returned by TableAccessor.views. Dict-like, keyed by view name. Same dict interface as DfsAccessor. Empty (not erroring) for types with no .base file.

TableAccessor is used for both the full-table case (no view) and the view case (view config attached). Same class, different state.
Package layout
vault/
  __init__.py
  vault.py             # Vault class — add `dfs` property
  schema.py
  forumla.py
  record.py
  accessors/
    __init__.py
    dfs.py             # DfsAccessor, TableAccessor, ViewsAccessor
  bases.py             # .base file parsing
  io/
    ...
Arrow as intermediate
Build a pyarrow.Table once per (type, view) combination, then convert to pandas or Polars on demand.

pyarrow is a hard dependency.
pandas and polars are optional dependencies. Import them lazily inside to_pandas() and to_polars(). If missing, raise ImportError with a message pointing to the relevant extras install (pip install vault[pandas]).
Cache the Arrow table on the TableAccessor instance. The Vault is a read-only snapshot per existing spec, so caching is safe.

Column ordering convention
When no view is applied, columns are ordered as follows:

Identifier (filename) — always first, column name _filename (or similar — confirm naming with existing record representation).
Strings, sorted alphabetically by field name.
Booleans, sorted alphabetically.
Numbers (int, float), sorted alphabetically.
Dates / datetimes, sorted alphabetically.
Lists of scalars (strings, numbers), sorted alphabetically.
Links (single), sorted alphabetically.
Lists of links, sorted alphabetically.
full_text — always last, if present.

Computed (formula-result) columns are not distinguished from authored columns. They appear in their natural type slot.
The type classification comes from the derived Schema. If a field is missing from the schema for some reason, place it after full_text (or raise — implementer's call, but be consistent).
Cell representations

Scalars: native Python types, converted to Arrow native types.
List of scalars: Arrow list type.
Link: filename string (no folder prefix, no [[ ]] wrapping).
List of links: Arrow list of strings, each a filename.
Missing values: null in Arrow, which surfaces as NaN / None / null depending on backend.
full_text: string, possibly long. Included by default.

View parsing
For v1, parse .base files for:

View definitions (name, type/source).
Column selection per view (which properties are shown).
Column ordering per view.

Not in v1 scope:

Filter evaluation. Views that define filters are loaded with their column config but filters are not applied. Document this limitation in the view's __repr__ or raise a warning when accessed. Decide which — I'd lean toward a one-time warning per view on first access.
Sort. Same treatment as filter — parsed but not applied.
Formula re-evaluation. Formula results are already frozen on records; views inherit them.

.base file format notes
.base files are YAML. Each file can define multiple views over one source type. The parser should:

Locate .base files in the vault root and in type folders (confirm Obsidian's actual conventions during implementation — check the user's existing vault for examples).
Map each view to a (type, view_name) pair.
Extract column list and order.
Ignore filter and sort fields with a logged warning.

If a .base file references a type that doesn't exist in the vault, log a warning and skip.
If multiple .base files define views for the same type, merge them — all views become accessible under that type's views accessor. View name collisions across files: last-loaded wins, with a warning.
Method signatures
pythonclass DfsAccessor:
    def __getitem__(self, type_name: str) -> TableAccessor: ...
    def __contains__(self, type_name: str) -> bool: ...
    def __iter__(self) -> Iterator[str]: ...
    def keys(self) -> KeysView[str]: ...
    def __len__(self) -> int: ...
    def __repr__(self) -> str: ...

class TableAccessor:
    def to_arrow(self) -> pyarrow.Table: ...
    def to_pandas(self) -> "pandas.DataFrame": ...
    def to_polars(self) -> "polars.DataFrame": ...
    @property
    def views(self) -> ViewsAccessor: ...
    def __repr__(self) -> str: ...

class ViewsAccessor:
    def __getitem__(self, view_name: str) -> TableAccessor: ...
    def __contains__(self, view_name: str) -> bool: ...
    def __iter__(self) -> Iterator[str]: ...
    def keys(self) -> KeysView[str]: ...
    def __len__(self) -> int: ...
    def __repr__(self) -> str: ...
Vault gains:
python@property
def dfs(self) -> DfsAccessor: ...
Dependencies
Update pyproject.toml:

Add pyarrow as a required dependency.
Add pandas under [project.optional-dependencies] as pandas.
Add polars under [project.optional-dependencies] as polars.
Add a dfs extra that pulls in both.

Lazy import pattern inside to_pandas() / to_polars():
pythondef to_pandas(self):
    try:
        import pandas as pd
    except ImportError as e:
        raise ImportError(
            "to_pandas() requires pandas. Install with: pip install vault[pandas]"
        ) from e
    return self.to_arrow().to_pandas()
Tests
At minimum:

DfsAccessor dict interface (getitem, contains, iter, keys, len) on a small test vault.
Column ordering convention on a type with fields of every supported kind.
Link fields render as filenames (no folder, no brackets).
List-of-link fields render as lists of filenames.
Missing fields → nulls.
full_text included and ordered last.
to_pandas() and to_polars() produce equivalent data (same row count, same column names, same values modulo backend type differences).
to_pandas() raises a clear ImportError if pandas is missing (mock the import).
View accessor returns empty ViewsAccessor for types with no .base file.
View with explicit column selection returns only those columns, in the specified order.
View with a filter logs a warning but returns all rows.
Arrow table is cached — second call to to_arrow() returns the same object.

Open questions for the implementer

Confirm the existing record representation: how are missing fields stored? (None? Key absent?) The Arrow builder needs to know.
Confirm the existing schema representation: does it already classify fields by type (string/number/list/link/etc.)? If yes, reuse. If no, this is a prerequisite — flag before starting.
Confirm where .base files live in the user's actual vault. The spec assumes vault root and type folders; verify.
Decide on the filename column name (_filename, file, name, …). Match whatever convention the rest of the package uses.