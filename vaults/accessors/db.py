"""DB accessor — exposes the vault as a DuckDB in-memory database."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..schema import FieldType
from ..links import LINK_TYPES_PAIRED, iter_link_names

if TYPE_CHECKING:
    import duckdb
    from ..vault import Vault

_FIELD_TO_DB_TYPE: dict[FieldType, str] = {
    FieldType.STRING: "VARCHAR",
    FieldType.INTEGER: "BIGINT",
    FieldType.NUMBER: "DOUBLE",
    FieldType.BOOLEAN: "BOOLEAN",
    FieldType.DATE: "DATE",
    FieldType.DATETIME: "TIMESTAMP",
    FieldType.LIST_STRINGS: "VARCHAR[]",
    FieldType.LIST_MIXED: "VARCHAR[]",
    FieldType.UNKNOWN: "VARCHAR",
}


def _coerce_for_db(value: Any, ft: FieldType) -> Any:
    if value is None:
        return None
    if ft == FieldType.LIST_MIXED:
        return [str(item) if item is not None else "" for item in value] if isinstance(value, list) else [str(value)]
    if ft == FieldType.UNKNOWN:
        return str(value)
    return value


class DbAccessor:
    def __init__(self, vault: "Vault") -> None:
        self._vault = vault

    def to_duckdb(self) -> "duckdb.DuckDBPyConnection":
        import duckdb

        con = duckdb.connect()
        vault = self._vault

        for type_name, type_schema in vault.schema.types.items():
            recs = vault.records.get(type_name, [])

            scalar_fields = [f for f in type_schema.fields if f.effective_type not in LINK_TYPES_PAIRED]

            col_defs = ["record VARCHAR PRIMARY KEY"]
            for f in scalar_fields:
                effective = (f.output_type or FieldType.UNKNOWN) if f.type == FieldType.FORMULA else f.type
                db_type = _FIELD_TO_DB_TYPE.get(effective, "VARCHAR")
                col_defs.append(f'"{f.name}" {db_type}')

            con.execute(f'CREATE TABLE "{type_name}" ({", ".join(col_defs)})')

            if recs:
                placeholders = ", ".join(["?"] * (1 + len(scalar_fields)))
                insert_sql = f'INSERT INTO "{type_name}" VALUES ({placeholders})'
                rows = []
                for rec in recs:
                    row: list[Any] = [rec.name]
                    for f in scalar_fields:
                        row.append(_coerce_for_db(rec.fields.get(f.name), f.effective_type))
                    rows.append(row)
                con.executemany(insert_sql, rows)

            for f in type_schema.fields:
                if f.effective_type not in LINK_TYPES_PAIRED:
                    continue
                table_name = f"{type_name}__{f.name}"
                rows = []
                for rec in recs:
                    for target in iter_link_names(rec.fields.get(f.name)):
                        rows.append((rec.name, target))
                con.execute(
                    f'CREATE TABLE "{table_name}" '
                    f'(source VARCHAR, target VARCHAR, PRIMARY KEY (source, target))'
                )
                unique = list({(s, t) for s, t in rows})
                if unique:
                    con.executemany(f'INSERT INTO "{table_name}" VALUES (?, ?)', unique)

        return con
