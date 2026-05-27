from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .schema import TypeSchema


@dataclass
class Record:
    """
    A single parsed entry from the vault, corresponding to one Markdown file.

    Parameters
    ----------
    name : str
        The record's identifier — the stem of the source Markdown filename.
    type_schema : TypeSchema
        The schema of the type this record belongs to, describing its expected fields.
    fields : dict[str, Any]
        Parsed frontmatter properties. Values may be scalars, lists, or wikilink strings.
    path : Path or None
        Absolute path to the source Markdown file. None for synthetically created records.
    """

    name: str
    type_schema: TypeSchema
    fields: dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    @property
    def type(self) -> str:
        """
        The name of the type this record belongs to, derived from its schema.

        Returns
        -------
        str
            Value of ``type_schema.name``.
        """
        return self.type_schema.name
