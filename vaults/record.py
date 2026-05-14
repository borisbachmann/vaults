from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .schema import TypeSchema


@dataclass
class Record:
    name: str
    type_schema: TypeSchema
    fields: dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    @property
    def type(self) -> str:
        return self.type_schema.name
