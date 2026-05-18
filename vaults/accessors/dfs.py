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


def _full_column_order(type_schema, field_map: dict[str, FieldSchema]) -> list[str]:
    """Build column order for full-table (no view) access."""
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
        if not coerce_types and self._arrow_cache is not None:
            return self._arrow_cache
        tbl, self._group_col = _build_arrow_table(
            self._vault, self._type_name, self._view_config, coerce_types=coerce_types
        )
        if not coerce_types:
            self._arrow_cache = tbl
        return tbl

    def to_pandas(self, *, coerce_types: bool = False):
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

    @property
    def views(self) -> ViewsAccessor:
        if self._views_cache is None:
            self._views_cache = ViewsAccessor(self._vault, self._type_name)
        return self._views_cache

    def __repr__(self) -> str:
        recs = len(self._vault.records.get(self._type_name, []))
        views_n = len(self.views)
        view_part = f", {views_n} view{'s' if views_n != 1 else ''}" if views_n else ""
        if self._view_config:
            return f"TableAccessor({self._type_name!r}, view={self._view_config.get('name')!r}, {recs} records)"
        return f"TableAccessor({self._type_name!r}, {recs} records{view_part})"


class DfsAccessor:
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

    def __repr__(self) -> str:
        types = list(self._vault.schema.types)
        return f"DfsAccessor({types})"
