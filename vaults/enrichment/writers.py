from __future__ import annotations

import io
import logging
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..vault import Vault

logger = logging.getLogger(__name__)

_OBSIDIAN_INVALID = re.compile(r'[\[\]:\\/^|#]')


def sanitize_filename(name: str) -> tuple[str, bool]:
    """Return (sanitized_name, changed).

    Replaces Obsidian-invalid characters ([]:\\/^|#) with '_', NFC-normalizes,
    and strips leading/trailing dots and spaces."""
    normalized = unicodedata.normalize("NFC", name)
    replaced = _OBSIDIAN_INVALID.sub("_", normalized)
    result = replaced.strip(". ") or "_"
    return result, result != name


def _resolve_suffix(base: str, existing: set[str]) -> str:
    """Append _2, _3, ... to base until the result is not in existing."""
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _write_md_file(path: Path, fields: dict[str, Any], body: str = "") -> None:
    """Write a .md file with YAML frontmatter.

    Null contract: None and [] both render as a bare key (field:).
    False renders as `false` (a real value). Key order is preserved."""
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    yaml = YAML()
    yaml.representer.add_representer(
        type(None),
        lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:null", ""),
    )

    data = CommentedMap()
    for k, v in fields.items():
        data[k] = None if (v is None or v == []) else v

    if data:
        stream = io.StringIO()
        yaml.dump(data, stream)
        yaml_str = stream.getvalue()
    else:
        yaml_str = ""

    content = f"---\n{yaml_str}---\n"
    if body:
        content += f"\n{body}\n"

    path.write_text(content, encoding="utf-8")


def _read_md_file(path: Path) -> tuple[dict, str]:
    """Read frontmatter and body from an existing .md file."""
    import frontmatter
    post = frontmatter.load(path)
    return dict(post.metadata), post.content


def _write_base_file(path: Path, type_name: str, props: list[str], data_folder: str) -> None:
    """Write a minimal .base file for a newly created type."""
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    yaml = YAML()

    order = CommentedSeq(["file.name"] + props)
    view = CommentedMap()
    view["type"] = "table"
    view["name"] = type_name
    view["order"] = order

    views = CommentedSeq([view])

    and_clause = CommentedSeq([f'file.inFolder("data/{type_name}")'])
    filters_and = CommentedMap()
    filters_and["and"] = and_clause

    doc = CommentedMap()
    doc["filters"] = filters_and
    doc["views"] = views

    path.parent.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO()
    yaml.dump(doc, stream)
    path.write_text(stream.getvalue(), encoding="utf-8")


def _check_stale(vault: "Vault") -> None:
    """Raise RuntimeError if the vault's disk state has changed since it was loaded."""
    if vault.is_stale:
        raise RuntimeError(
            "Vault is stale — disk contents have changed since the last load. "
            "Call vault.reload() before writing."
        )
