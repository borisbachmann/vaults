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
    record: Record
    vault: Vault
    base_path: Path
    now: datetime.datetime
