from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, KeysView, Optional

import pyarrow as pa
import yaml

from ..syntax import BasesCompiler, EvalContext
from ..schema import FieldSchema, FieldType, serialize_filter
from ..links import LIST_LINK_TYPES, parse_wikilink_name
from ..vault import coerce_target, coerce_value

if TYPE_CHECKING:
    from ..vault import Vault

_compiler = BasesCompiler()

logger = logging.getLogger(__name__)

# ── Arrow type mapping ──────────────────────────────────────────────────────

_FIELD_TO_ARROW: dict[FieldType, pa.DataType] = {
    FieldType.STRING:       pa.string(),
    FieldType.INTEGER:      pa.int64(),
    FieldType.NUMBER:       pa.float64(),
    FieldType.BOOLEAN:      pa.bool_(),
    FieldType.DATE:         pa.date32(),
    FieldType.DATETIME:     pa.timestamp("us"),
    FieldType.LIST_STRINGS: pa.list_(pa.string()),
    FieldType.LIST_LINKS:   pa.list_(pa.string()),
    FieldType.LIST_MIXED:   pa.list_(pa.string()),
    FieldType.LINK:         pa.string(),
    FieldType.UNKNOWN:      pa.string(),
    FieldType.FORMULA:      pa.string(),  # fallback; overridden via output_type
}

# Mapping from coercion target Python type to the Arrow type used when
# coerce_types=True overrides the schema-inferred Arrow type.
_COERCE_ARROW: dict[type, pa.DataType] = {
    str:   pa.string(),
    float: pa.float64(),
    int:   pa.int64(),
}

# ── Column ordering slots ───────────────────────────────────────────────────

_TYPE_SLOT: dict[FieldType, int] = {
    FieldType.STRING:       1,
    FieldType.BOOLEAN:      2,
    FieldType.INTEGER:      3,
    FieldType.NUMBER:       3,
    FieldType.DATE:         4,
    FieldType.DATETIME:     4,
    FieldType.LIST_STRINGS: 5,
    FieldType.LINK:         6,
    FieldType.LIST_LINKS:   7,
    FieldType.LIST_MIXED:   7,
    FieldType.UNKNOWN:      8,
    FieldType.FORMULA:      8,  # fallback; overridden via output_type
}


def _arrow_type(f: FieldSchema) -> pa.DataType:
    return _FIELD_TO_ARROW.get(f.effective_type, pa.string())


def _convert_value(value: Any, field_type: FieldType) -> Any:
    """
    Normalise a raw field value for Arrow array construction.

    Wikilink strings in LINK and LIST_LINKS/LIST_MIXED fields are reduced to
    their record-name component so Arrow columns contain plain strings rather
    than ``[[folder/name]]`` markup.

    Parameters
    ----------
    value : Any
        Raw field value from ``record.fields``.
    field_type : FieldType
        The effective field type, used to select the conversion path.

    Returns
    -------
    Any
        For LINK fields: the record name string, or the original string when
        parsing fails. For list-link fields: a list of record name strings.
        For all other types: ``value`` unchanged.
    """
    if value is None:
        return None
    if field_type == FieldType.LINK:
        name = parse_wikilink_name(str(value))
        return name if name is not None else str(value)
    if field_type in LIST_LINK_TYPES:
        if not isinstance(value, list):
            return [str(value)]
        result = []
        for item in value:
            if item is None:
                continue
            name = parse_wikilink_name(str(item))
            result.append(name if name is not None else str(item))
        return result
    return value


def _parse_order_entry(entry: str, property_display: dict[str, str] | None = None) -> str:
    """
    Translate a ``.base`` view order/sort entry to the column name used in the Arrow table.

    Handles Obsidian Bases dot-notation (``file.name``, ``note.field``,
    ``formula.field``) and display-name aliases from ``property_display``.

    Parameters
    ----------
    entry : str
        A raw order or sort property string from a view config (e.g. ``"note.title"``,
        ``"formula.revenue"``, ``"file.name"``).
    property_display : dict[str, str] or None
        Mapping of property path to display name from the ``.base`` file. When
        provided, ``entry`` and ``"note.<entry>"`` are checked as keys first.

    Returns
    -------
    str
        The column name as it appears in the Arrow table (i.e. the display name
        or the stripped field name without its prefix).
    """
    if entry == "file.name":
        return "_record"
    if property_display:
        if entry in property_display:
            return property_display[entry]
        if f"note.{entry}" in property_display:
            return property_display[f"note.{entry}"]
    if entry.startswith("formula."):
        return entry[len("formula."):]
    if entry.startswith("note."):
        return entry[len("note."):]
    return entry


def _flatten_lists_for_csv(tbl: pa.Table) -> pa.Table:
    """
    Return a copy of ``tbl`` with list-typed columns cast to comma-separated strings.

    ``pyarrow.csv.write_csv`` does not support list types; this coercion is applied
    only for CSV export and does not touch the cached Arrow table.

    Parameters
    ----------
    tbl : pa.Table
        The Arrow table to transform.

    Returns
    -------
    pa.Table
        A new table where every list-typed column is replaced with a string
        column of comma-joined values; all other columns are passed through
        unchanged.
    """
    cols: dict[str, pa.Array] = {}
    for name in tbl.schema.names:
        col = tbl.column(name)
        if pa.types.is_list(col.type):
            py_vals = [
                ", ".join(str(v) for v in val.as_py()) if val.is_valid else None
                for val in col
            ]
            cols[name] = pa.array(py_vals, type=pa.string())
        else:
            cols[name] = col
    return pa.table(cols)


def _full_column_order(type_schema, field_map: dict[str, FieldSchema]) -> list[str]:
    """
    Build the column order for full-table (no view) Arrow output.

    Sorts fields by their type slot (scalars first, then links, then lists,
    then unknowns) and alphabetically within each slot. ``_record`` is always
    first; ``full_text`` is always last when present.

    Parameters
    ----------
    type_schema : TypeSchema
        The schema for the type being built (currently unused but kept for
        future extension).
    field_map : dict[str, FieldSchema]
        Mapping of field name to FieldSchema for all fields of the type.

    Returns
    -------
    list of str
        Ordered column names including ``_record`` as the first entry.
    """
    non_record = [name for name in field_map if name != "full_text"]

    def sort_key(name: str) -> tuple:
        f = field_map[name]
        eff = f.effective_type
        slot = _TYPE_SLOT.get(eff, 8)
        return (slot, name)

    ordered = sorted(non_record, key=sort_key)
    cols = ["_record"] + ordered
    if "full_text" in field_map:
        cols.append("full_text")
    return cols


def _filter_records(vault: "Vault", type_name: str, view_config: dict) -> list:
    """
    Apply a view's filter expression to a type's records and return the matching subset.

    Compiles the filter from ``view_config["filters"]`` on each call (not cached).
    Records for which the filter raises are kept (fail-open) to avoid silent data
    loss from transient evaluation errors.

    Parameters
    ----------
    vault : Vault
        The vault providing records and path context.
    type_name : str
        The type whose records are filtered.
    view_config : dict
        A single view dict from the ``.base`` file. The ``"filters"`` key is
        read; all other keys are ignored.

    Returns
    -------
    list of Record
        The records that pass the filter. Returns all records when ``view_config``
        has no ``"filters"`` key or when the filter serialises to an empty string.
    """
    raw = view_config.get("filters")
    if not raw:
        return vault.records.get(type_name, [])
    expr = serialize_filter(raw)
    if not expr:
        return vault.records.get(type_name, [])
    fn = _compiler.translate_filter(expr)
    base_path = (
        vault.path / vault.schema.bases_folder / f"{type_name}.base"
        if vault.path else Path(f"{type_name}.base")
    )
    now = datetime.now()
    result = []
    for rec in vault.records.get(type_name, []):
        ctx = EvalContext(record=rec, vault=vault, base_path=base_path, now=now)
        try:
            if fn(ctx):
                result.append(rec)
        except Exception:
            logger.debug("View filter failed on %s/%s", type_name, rec.name, exc_info=True)
            result.append(rec)
    return result


def _build_arrow_table(
    vault: "Vault",
    type_name: str,
    view_config: Optional[dict] = None,
    coerce_types: bool = False,
) -> pa.Table:
    """
    Build an Arrow table for a single vault type, with optional view config and type coercion.

    Column selection and order follow the view's ``order`` list when a
    ``view_config`` is provided, falling back to ``_full_column_order`` for the
    full-table case. Sorting is applied after array construction using
    ``pyarrow.compute.sort_indices`` on non-list columns.

    When ``coerce_types=True``, scalar non-link fields with mixed Python types
    (e.g. int and float) are unified to the least-lossy common type via
    ``coerce_target`` before the Arrow array is built. Falls back to casting
    values to strings if Arrow rejects the resulting array.

    Parameters
    ----------
    vault : Vault
        The vault to read records and schema from.
    type_name : str
        The type to build the table for. Must be a key in ``vault.schema.types``.
    view_config : dict or None
        A single view dict from the ``.base`` file. When None, all records are
        included with full-table column order and no sorting.
    coerce_types : bool
        When True, mixed-type scalar fields are coerced to a common Python type
        before Arrow array construction.

    Returns
    -------
    pa.Table
        The constructed Arrow table.
    str or None
        The group-by column name used for downstream pandas/polars index/sort
        handling, or None when no ``groupBy`` is defined in the view.
    """
    type_schema = vault.schema.types[type_name]
    prop_display = type_schema.property_display or None
    recs = (
        _filter_records(vault, type_name, view_config)
        if view_config is not None
        else vault.records.get(type_name, [])
    )
    field_map = {f.name: f for f in type_schema.fields}

    if view_config is not None:
        raw_order = view_config.get("order") or []
        cols = []
        for entry in raw_order:
            col = _parse_order_entry(str(entry), prop_display)
            if col == "_record" or col in field_map:
                cols.append(col)
        group_by = view_config.get("groupBy")
        if group_by:
            group_col_name = _parse_order_entry(str(group_by.get("property", "")), prop_display)
            if group_col_name and group_col_name not in cols and group_col_name in field_map:
                cols.append(group_col_name)
    else:
        cols = _full_column_order(type_schema, field_map)

    arrays: dict[str, pa.Array] = {}
    for col in cols:
        if col == "_record":
            arrays[col] = pa.array([r.name for r in recs], type=pa.string())
            continue
        f = field_map.get(col)
        if f is None:
            arrays[col] = pa.array([None] * len(recs), type=pa.string())
            continue
        eff_type = f.effective_type
        raw = [r.fields.get(col) for r in recs]

        # coerce_types: for scalar non-link fields, unify mixed Python types to
        # the least-lossy common type before building the Arrow array.
        coerce_to = None
        if coerce_types and f.type != FieldType.LINK and f.type not in LIST_LINK_TYPES:
            non_null = [v for v in raw if v is not None]
            if non_null and not any(isinstance(v, list) for v in non_null):
                coerce_to = coerce_target({type(v) for v in non_null})

        if coerce_to is not None:
            values = [coerce_value(v, coerce_to) if v is not None else None for v in raw]
            at = _COERCE_ARROW.get(coerce_to, pa.string())
        else:
            values = [_convert_value(v, eff_type) for v in raw]
            at = _arrow_type(f)

        try:
            arrays[col] = pa.array(values, type=at)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arrays[col] = pa.array([str(v) if v is not None else None for v in values], type=pa.string())

    tbl = pa.table(arrays)

    if view_config is not None:
        pa_sort_keys = []

        group_by = view_config.get("groupBy")
        group_col = _parse_order_entry(str(group_by["property"]), prop_display) if group_by else None
        if group_col and group_col in tbl.column_names:
            if pa.types.is_list(tbl.schema.field(group_col).type):
                logger.warning(
                    "Grouping by list columns is not supported. "
                    "Using group column %r as primary sort key.", group_col
                )
                # Arrow can't sort list columns — sort applied post-conversion
            else:
                group_dir = "descending" if str(group_by.get("direction", "ASC")).upper() == "DESC" else "ascending"
                pa_sort_keys.append((group_col, group_dir))

        for sk in (view_config.get("sort") or []):
            col = _parse_order_entry(str(sk.get("property", "")), prop_display)
            direction = "descending" if str(sk.get("direction", "ASC")).upper() == "DESC" else "ascending"
            if col not in tbl.column_names or col == group_col:
                continue
            if pa.types.is_list(tbl.schema.field(col).type):
                logger.debug("Skipping sort on list column '%s' — not supported by Arrow", col)
                continue
            pa_sort_keys.append((col, direction))

        if pa_sort_keys:
            import pyarrow.compute as pc
            tbl = tbl.take(pc.sort_indices(tbl, sort_keys=pa_sort_keys))

    return tbl, (group_col if view_config is not None else None)


# ── Accessor classes ────────────────────────────────────────────────────────

class ViewsAccessor:
    """
    Lazy-loading dict-like access to the named views defined in a type's ``.base`` file.

    Views are loaded from disk on first access and cached. Supports ``in``,
    iteration over view names, ``len``, and ``[]`` lookup (which returns a
    ``TableAccessor`` scoped to that view's filter and column config).

    Accessed via ``vault.dfs[type_name].views``.
    """

    def __init__(self, vault: "Vault", type_name: str) -> None:
        self._vault = vault
        self._type_name = type_name
        self._views: Optional[dict[str, dict]] = None

    def _load(self) -> dict[str, dict]:
        if self._views is not None:
            return self._views
        self._views = {}
        if self._vault.path is None:
            return self._views
        base_file = (
            self._vault.path
            / self._vault.schema.bases_folder
            / f"{self._type_name}.base"
        )
        if not base_file.exists():
            return self._views
        data = yaml.safe_load(base_file.read_text(encoding="utf-8")) or {}
        for view in data.get("views") or []:
            name = view.get("name")
            if not name:
                continue
            if name in self._views:
                logger.warning(
                    "Duplicate view name '%s' in %s — last definition wins",
                    name, base_file.name,
                )
            self._views[name] = view
        return self._views

    def __getitem__(self, view_name: str) -> "TableAccessor":
        views = self._load()
        if view_name not in views:
            raise KeyError(view_name)
        return TableAccessor(
            _vault=self._vault,
            _type_name=self._type_name,
            _view_config=views[view_name],
        )

    def __contains__(self, view_name: object) -> bool:
        return view_name in self._load()

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def keys(self) -> KeysView[str]:
        return self._load().keys()

    def __len__(self) -> int:
        return len(self._load())

    def __repr__(self) -> str:
        n = len(self._load())
        return f"ViewsAccessor({self._type_name!r}, {n} view{'s' if n != 1 else ''})"


class TableAccessor:
    """
    DataFrame and file export interface for a single vault type or named view.

    Wraps ``_build_arrow_table`` and caches the resulting Arrow table for
    repeated access. Obtained via ``vault.dfs[type_name]`` (full table) or
    ``vault.dfs[type_name].views[view_name]`` (view-scoped).

    Parameters
    ----------
    _vault : Vault
        The vault providing records and schema.
    _type_name : str
        The type this accessor is bound to.
    _view_config : dict or None
        View configuration dict from the ``.base`` file, or None for full-table
        access.
    """

    def __init__(
        self,
        _vault: "Vault",
        _type_name: str,
        _view_config: Optional[dict] = None,
    ) -> None:
        self._vault = _vault
        self._type_name = _type_name
        self._view_config = _view_config
        self._arrow_cache: Optional[pa.Table] = None
        self._group_col: Optional[str] = None
        self._views_cache: Optional[ViewsAccessor] = None

    def to_arrow(self, *, coerce_types: bool = False) -> pa.Table:
        """
        Return the type's records as a PyArrow Table.

        The result is cached when ``coerce_types=False``; each call with
        ``coerce_types=True`` rebuilds the table without caching.

        Parameters
        ----------
        coerce_types : bool
            When True, scalar fields with mixed Python types across records are
            unified to the least-lossy common type before Arrow array construction.

        Returns
        -------
        pa.Table
            Arrow table with a ``_record`` string column as the first column,
            followed by all other fields in schema-defined order.
        """
        if not coerce_types and self._arrow_cache is not None:
            return self._arrow_cache
        tbl, self._group_col = _build_arrow_table(
            self._vault, self._type_name, self._view_config, coerce_types=coerce_types
        )
        if not coerce_types:
            self._arrow_cache = tbl
        return tbl

    def to_pandas(self, *, coerce_types: bool = False):
        """
        Return the type's records as a pandas DataFrame.

        When a ``groupBy`` is defined in the view config, the group column and
        ``_record`` are set as a MultiIndex (or the DataFrame is sorted by the
        group column when it is a list type, which Arrow cannot use as an index).
        Integer columns use ``pd.Int64Dtype()`` to preserve nullable integers.

        Parameters
        ----------
        coerce_types : bool
            Forwarded to ``to_arrow()``.

        Returns
        -------
        pandas.DataFrame

        Raises
        ------
        ImportError
            When pandas is not installed.
        """
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError(
                "to_pandas() requires pandas. Install with: pip install vaults[pandas]"
            ) from e
        tbl = self.to_arrow(coerce_types=coerce_types)
        group_col = self._group_col
        df = tbl.to_pandas(
            types_mapper=lambda t: pd.Int64Dtype() if t == pa.int64() else None
        )
        if group_col and group_col in df.columns:
            if pa.types.is_list(tbl.schema.field(group_col).type):
                other = [c for c in df.columns if c != group_col]
                df = df[[group_col] + other].sort_values(
                    group_col, na_position="last", kind="stable"
                )
            else:
                df = df.set_index([group_col, "_record"])
        return df

    def to_polars(self, *, coerce_types: bool = False):
        """
        Return the type's records as a Polars DataFrame.

        When a ``groupBy`` is defined in the view config, the group column is
        moved to the front and the DataFrame is sorted by it (nulls last).

        Parameters
        ----------
        coerce_types : bool
            Forwarded to ``to_arrow()``.

        Returns
        -------
        polars.DataFrame

        Raises
        ------
        ImportError
            When polars is not installed.
        """
        try:
            import polars as pl
        except ImportError as e:
            raise ImportError(
                "to_polars() requires polars. Install with: pip install vaults[polars]"
            ) from e
        tbl = self.to_arrow(coerce_types=coerce_types)
        group_col = self._group_col
        df = pl.from_arrow(tbl)
        if group_col and group_col in df.columns:
            other = [c for c in df.columns if c != group_col]
            df = df.select([group_col] + other)
            if pa.types.is_list(tbl.schema.field(group_col).type):
                df = df.sort(group_col, nulls_last=True)
        return df

    def to_parquet(self, path: str | Path, *, coerce_types: bool = False) -> Path:
        """
        Write this table to a Parquet file and return the path.

        Uses the Arrow table from ``to_arrow()``, preserving all field types
        exactly — including lists and dates.

        Parameters
        ----------
        path : str or Path
            Destination file path. Parent directory must exist.
        coerce_types : bool
            Forwarded to ``to_arrow()``.

        Returns
        -------
        Path
            The resolved path to the written file.
        """
        import pyarrow.parquet as pq
        tbl = self.to_arrow(coerce_types=coerce_types)
        path = Path(path)
        pq.write_table(tbl, str(path))
        return path

    def to_csv(self, path: str | Path, *, coerce_types: bool = False) -> Path:
        """
        Write this table to a CSV file and return the path.

        List-typed columns are flattened to comma-separated strings before
        writing because the CSV format does not support arrays.

        Parameters
        ----------
        path : str or Path
            Destination file path. Parent directory must exist.
        coerce_types : bool
            Forwarded to ``to_arrow()``.

        Returns
        -------
        Path
            The resolved path to the written file.
        """
        import pyarrow.csv as pa_csv
        tbl = self.to_arrow(coerce_types=coerce_types)
        tbl = _flatten_lists_for_csv(tbl)
        path = Path(path)
        pa_csv.write_csv(tbl, str(path))
        return path

    @property
    def views(self) -> ViewsAccessor:
        """
        The named views defined for this type in its ``.base`` file.

        Returns
        -------
        ViewsAccessor
            A lazy-loaded dict-like object keyed by view name. Use
            ``table.views["My View"].to_pandas()`` to access a specific view.
        """
        if self._views_cache is None:
            self._views_cache = ViewsAccessor(self._vault, self._type_name)
        return self._views_cache

    def __repr__(self) -> str:
        views_n = len(self.views)
        view_part = f", {views_n} view{'s' if views_n != 1 else ''}" if views_n else ""
        if self._view_config:
            filtered = len(_filter_records(self._vault, self._type_name, self._view_config))
            total = len(self._vault.records.get(self._type_name, []))
            return f"TableAccessor({self._type_name!r}, view={self._view_config.get('name')!r}, {filtered}/{total} records)"
        recs = len(self._vault.records.get(self._type_name, []))
        return f"TableAccessor({self._type_name!r}, {recs} records{view_part})"


class DfsAccessor:
    """
    Dict-like access to all vault types as ``TableAccessor`` objects.

    Iterating, ``in`` checks, and ``len`` operate over type names. Index with
    a type name to get a ``TableAccessor`` for that type.

    Accessed via ``vault.dfs``, which lazily constructs and caches this accessor.
    """

    def __init__(self, vault: "Vault") -> None:
        self._vault = vault

    def __getitem__(self, type_name: str) -> TableAccessor:
        if type_name not in self._vault.schema.types:
            raise KeyError(type_name)
        return TableAccessor(_vault=self._vault, _type_name=type_name)

    def __contains__(self, type_name: object) -> bool:
        return type_name in self._vault.schema.types

    def __iter__(self) -> Iterator[str]:
        return iter(self._vault.schema.types)

    def keys(self) -> KeysView[str]:
        return self._vault.schema.types.keys()

    def __len__(self) -> int:
        return len(self._vault.schema.types)

    def to_arrow(self, *, coerce_types: bool = False) -> dict[str, pa.Table]:
        """
        Return all types as a dict of Arrow tables.

        Parameters
        ----------
        coerce_types : bool
            Forwarded to each ``TableAccessor.to_arrow()`` call.

        Returns
        -------
        dict[str, pa.Table]
            Mapping of type name to its Arrow table.
        """
        return {t: self[t].to_arrow(coerce_types=coerce_types) for t in self._vault.schema.types}

    def to_parquet(self, directory: str | Path, *, coerce_types: bool = False) -> dict[str, Path]:
        """
        Write all types as Parquet files into a directory and return a path dict.

        Creates the directory (and any missing parents) if it does not exist.

        Parameters
        ----------
        directory : str or Path
            Target directory. Each type is written as ``{type_name}.parquet``.
        coerce_types : bool
            Forwarded to each ``TableAccessor.to_arrow()`` call.

        Returns
        -------
        dict[str, Path]
            Mapping of type name to the path of the written Parquet file.
        """
        import pyarrow.parquet as pq
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        for type_name in self._vault.schema.types:
            path = directory / f"{type_name}.parquet"
            pq.write_table(self[type_name].to_arrow(coerce_types=coerce_types), str(path))
            result[type_name] = path
        return result

    def __repr__(self) -> str:
        types = list(self._vault.schema.types)
        return f"DfsAccessor({types})"
