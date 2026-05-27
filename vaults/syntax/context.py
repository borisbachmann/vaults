"""EvalContext — isolated here so vault.py can import it without circularity."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..record import Record

if TYPE_CHECKING:
    from ..vault import Vault


@dataclass
class EvalContext:
    """
    Execution context passed to every compiled formula and filter callable.

    Provides the formula with access to the current record, the full vault
    (for cross-record lookups such as backlinks), the ``.base`` file path
    (for ``this.file.*`` properties), and a frozen timestamp so all formulas
    in a single load share the same ``now``.

    Parameters
    ----------
    record : Record
        The record being evaluated against.
    vault : Vault
        The loaded vault, used for cross-record lookups.
    base_path : Path
        Absolute path to the ``.base`` file for the record's type. Used by
        the ``_ThisProxy`` to expose ``this.file.name`` etc.
    now : datetime.datetime
        Timestamp frozen at load time. Passed to time-sensitive functions
        (``now()``, ``today()``, ``.relative()``) to ensure consistency.
    """

    record: Record
    vault: Vault
    base_path: Path
    now: datetime.datetime
