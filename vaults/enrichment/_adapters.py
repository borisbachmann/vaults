from __future__ import annotations

from typing import Any


def _to_records(data) -> list[dict]:
    """Normalize tabular input to a list of dicts with None as the null sentinel."""
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
    """Normalize column input to a dict mapping filename stems to values."""
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
    """Collapse universal null sentinels (None, [], pd.NA, np.nan) to None.

    Empty string is not handled here — callers apply field-type context."""
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
