from __future__ import annotations

from typing import Any


def _to_records(data) -> list[dict]:
    """
    Normalise tabular input to a list of row dicts with None as the null sentinel.

    Accepts list-of-dicts, column-oriented dicts, pandas DataFrame/Series, and
    polars DataFrame/Series. pandas NA and NaN values are collapsed to None.

    Parameters
    ----------
    data : list[dict], dict[str, list], pd.DataFrame, pd.Series, pl.DataFrame, or pl.Series
        The tabular data to convert.

    Returns
    -------
    list[dict]
        One dict per row. Keys are column names; values use None for missing data.

    Raises
    ------
    TypeError
        When ``data`` is none of the supported types.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if not data:
            return []
        length = len(next(iter(data.values())))
        return [{k: data[k][i] for k in data} for i in range(length)]
    try:
        import pandas as pd
        if isinstance(data, pd.DataFrame):
            return [
                {k: (None if pd.isna(v) else v) for k, v in row.items()}
                for row in data.to_dict(orient="records")
            ]
        if isinstance(data, pd.Series):
            return [
                {str(idx): (None if pd.isna(val) else val)}
                for idx, val in data.items()
            ]
    except ImportError:
        pass
    try:
        import polars as pl
        if isinstance(data, pl.DataFrame):
            return data.to_dicts()
        if isinstance(data, pl.Series):
            return [{data.name: v} for v in data.to_list()]
    except ImportError:
        pass
    raise TypeError(
        f"Cannot convert {type(data).__name__} to records; "
        "expected list[dict], dict[str, list], DataFrame, or Series"
    )


def _to_column(data) -> dict[str, Any]:
    """
    Normalise column input to a dict mapping filename stems to values.

    Parameters
    ----------
    data : dict[str, Any], pd.Series, or pl.Series
        The column data to convert. For a pandas Series, the index is used as
        keys. For a polars Series, the integer position is used as key.

    Returns
    -------
    dict[str, Any]
        Mapping of record name (filename stem) to the column value.

    Raises
    ------
    TypeError
        When ``data`` is none of the supported types.
    """
    if isinstance(data, dict):
        return data
    try:
        import pandas as pd
        if isinstance(data, pd.Series):
            return {str(idx): (None if pd.isna(val) else val) for idx, val in data.items()}
    except ImportError:
        pass
    try:
        import polars as pl
        if isinstance(data, pl.Series):
            return {str(i): v for i, v in enumerate(data.to_list())}
    except ImportError:
        pass
    raise TypeError(
        f"Cannot convert {type(data).__name__} to column mapping; "
        "expected dict[str, Any] or pd.Series (index=filename stems)"
    )


def _normalize_null(value: Any) -> Any:
    """
    Collapse universal null sentinels to None.

    Handles None, empty list, pandas NA, and numpy NaN. Empty string is
    intentionally left unchanged — callers apply field-type context to decide
    whether ``""`` should be treated as null.

    Parameters
    ----------
    value : Any
        The value to normalise.

    Returns
    -------
    Any
        None when ``value`` is a recognised null sentinel; ``value`` unchanged
        otherwise.
    """
    if value is None or value == []:
        return None
    try:
        import pandas as pd
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    except ImportError:
        pass
    return value
