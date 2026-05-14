from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, KeysView, Optional

import pyarrow as pa
import yaml

from ..schema import FieldSchema, FieldType

if TYPE_CHECKING:
    from ..vault import Vault

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

_LINK_TYPES = (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED)
_LIST_LINK_TYPES = (FieldType.LIST_LINKS, FieldType.LIST_MIXED)

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

_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")


def _parse_wikilink_name(value: str) -> Optional[str]:
    m = _WIKILINK_RE.match(str(value).strip())
    if not m:
        return None
    parts = m.group(1).split("/")
    return parts[-1]


def _effective_type(f: FieldSchema) -> FieldType:
    if f.type == FieldType.FORMULA:
        return f.output_type or FieldType.UNKNOWN
    return f.type


def _arrow_type(f: FieldSchema) -> pa.DataType:
    return _FIELD_TO_ARROW.get(_effective_type(f), pa.string())


def _convert_value(value: Any, field_type: FieldType) -> Any:
    if value is None:
        return None
    if field_type == FieldType.LINK:
        name = _parse_wikilink_name(str(value))
        return name if name is not None else str(value)
    if field_type in _LIST_LINK_TYPES:
        if not isinstance(value, list):
            return [str(value)]
        result = []
        for item in value:
            if item is None:
                continue
            name = _parse_wikilink_name(str(item))
            result.append(name if name is not None else str(item))
        return result
    return value


def _parse_order_entry(entry: str) -> str:
    if entry == "file.name":
        return "_record"
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
        eff = _effective_type(f)
        slot = _TYPE_SLOT.get(eff, 8)
        return (slot, name)

    ordered = sorted(non_record, key=sort_key)
    cols = ["_record"] + ordered
    if "full_text" in field_map:
        cols.append("full_text")
    return cols


def _build_arrow_table(
    vault: "Vault",
    type_name: str,
    view_config: Optional[dict] = None,
    warn_filter: bool = False,
) -> pa.Table:
    type_schema = vault.schema.types[type_name]
    recs = vault.records.get(type_name, [])
    field_map = {f.name: f for f in type_schema.fields}

    if view_config is not None:
        raw_order = view_config.get("order") or []
        cols = []
        for entry in raw_order:
            col = _parse_order_entry(str(entry))
            if col == "_record" or col in field_map:
                cols.append(col)
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
        eff_type = _effective_type(f)
        values = [_convert_value(r.fields.get(col), eff_type) for r in recs]
        at = _arrow_type(f)
        try:
            arrays[col] = pa.array(values, type=at)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arrays[col] = pa.array([str(v) if v is not None else None for v in values], type=pa.string())

    return pa.table(arrays)


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
        self._views_cache: Optional[ViewsAccessor] = None
        self._filter_warned = False

    def to_arrow(self) -> pa.Table:
        if self._arrow_cache is not None:
            return self._arrow_cache
        if self._view_config is not None and not self._filter_warned:
            if self._view_config.get("filters") or self._view_config.get("sort"):
                logger.warning(
                    "View '%s' defines filters/sort — these are not applied in v1; "
                    "all rows returned.",
                    self._view_config.get("name", self._type_name),
                )
            self._filter_warned = True
        self._arrow_cache = _build_arrow_table(
            self._vault, self._type_name, self._view_config
        )
        return self._arrow_cache

    def to_pandas(self):
        try:
            import pandas as pd
        except ImportError as e:
            raise ImportError(
                "to_pandas() requires pandas. Install with: pip install vaults[pandas]"
            ) from e
        return self.to_arrow().to_pandas(
            types_mapper=lambda t: pd.Int64Dtype() if t == pa.int64() else None
        )

    def to_polars(self):
        try:
            import polars as pl
        except ImportError as e:
            raise ImportError(
                "to_polars() requires polars. Install with: pip install vaults[polars]"
            ) from e
        return pl.from_arrow(self.to_arrow())

    @property
    def views(self) -> ViewsAccessor:
        if self._views_cache is None:
            self._views_cache = ViewsAccessor(self._vault, self._type_name)
        return self._views_cache

    def __repr__(self) -> str:
        recs = len(self._vault.records.get(self._type_name, []))
        views_n = len(ViewsAccessor(self._vault, self._type_name))
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
