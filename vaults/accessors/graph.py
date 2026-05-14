"""Graph accessor — exports the vault as a property graph (NetworkX, Kuzu, RDF)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..schema import FieldSchema, FieldType

if TYPE_CHECKING:
    import networkx as nx
    from ..vault import Vault

_WIKILINK_RE = re.compile(r"^\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]$")
_LINK_TYPES = (FieldType.LINK, FieldType.LIST_LINKS, FieldType.LIST_MIXED)

_FIELD_TO_KUZU: dict[FieldType, str] = {
    FieldType.STRING:       "STRING",
    FieldType.INTEGER:      "INT64",
    FieldType.NUMBER:       "DOUBLE",
    FieldType.BOOLEAN:      "BOOL",
    FieldType.DATE:         "DATE",
    FieldType.DATETIME:     "TIMESTAMP",
    FieldType.LIST_STRINGS: "STRING[]",
    FieldType.LIST_MIXED:   "STRING[]",
    FieldType.UNKNOWN:      "STRING",
}


def _effective_type(f: FieldSchema) -> FieldType:
    if f.type == FieldType.FORMULA:
        return f.output_type or FieldType.UNKNOWN
    return f.type


def _iter_link_names(value: Any) -> list[str]:
    """Extract bare record name stems from a link field value."""
    if value is None:
        return []
    if isinstance(value, str):
        m = _WIKILINK_RE.match(value.strip())
        if not m:
            return []
        parts = m.group(1).split("/")
        return [parts[-1]]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                m = _WIKILINK_RE.match(item.strip())
                if m:
                    result.append(m.group(1).split("/")[-1])
        return result
    return []


def _coerce_kuzu(value: Any, field_type: FieldType) -> Any:
    """Coerce a value to a Kuzu-safe Python type."""
    if value is None:
        return None
    if field_type in (FieldType.LIST_STRINGS, FieldType.LIST_MIXED):
        if isinstance(value, list):
            return [str(item) if item is not None else "" for item in value]
        return [str(value)]
    if field_type == FieldType.UNKNOWN:
        return str(value)
    return value


def _ascii_slug(s: str) -> str:
    """Expand German umlauts then replace every remaining non-alphanumeric char with ``_``."""
    for ch, rep in ("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"):
        s = s.replace(ch, rep)
    return re.sub(r"[^A-Za-z0-9_]", "_", s.encode("ascii", "ignore").decode())


def _kuzu_name(s: str) -> str:
    """Normalize to a Kuzu-safe ASCII identifier (no leading digit, non-empty)."""
    s = _ascii_slug(s)
    if s and s[0].isdigit():
        s = "_" + s
    return s or "_field"


def _build_rel_names(vault: "Vault") -> dict[tuple[str, str], str]:
    """Return the Kuzu relationship table name for every (type_name, field_name) link field.

    Short form (just the normalized field name) is used unless two different
    link fields normalize to the same name, or the short name would clash with
    a node table (type) name — in both cases all colliding entries fall back to
    ``{type_name}__{short}``.
    """
    from collections import Counter
    # Node table names are reserved; rel tables can't share a name with them.
    reserved = {_kuzu_name(t) for t in vault.schema.types}
    short_counts: Counter = Counter()
    link_fields: list[tuple[str, str]] = []

    for type_name, type_schema in vault.schema.types.items():
        for f in type_schema.fields:
            if _effective_type(f) in _LINK_TYPES and f.link_target:
                short = _kuzu_name(f.name)
                short_counts[short] += 1
                link_fields.append((type_name, f.name))

    return {
        (type_name, field_name): (
            _kuzu_name(field_name)
            if short_counts[_kuzu_name(field_name)] == 1
            and _kuzu_name(field_name) not in reserved
            else f"{type_name}__{_kuzu_name(field_name)}"
        )
        for type_name, field_name in link_fields
    }


def _uri_safe(s: str) -> str:
    """Normalize to a clean URI path segment (non-empty)."""
    return _ascii_slug(s) or "_"


def _build_digraph(vault: "Vault") -> "nx.DiGraph":
    import networkx as nx

    G = nx.DiGraph()

    for type_name, type_schema in vault.schema.types.items():
        for rec in vault.records.get(type_name, []):
            node_id = f"{rec.name}|{type_name}"
            attrs: dict[str, Any] = {"type": type_name}
            for f in type_schema.fields:
                if _effective_type(f) in _LINK_TYPES:
                    continue
                val = rec.fields.get(f.name)
                if val is not None:
                    attrs[f.name] = val
            G.add_node(node_id, **attrs)

    for type_name, type_schema in vault.schema.types.items():
        for f in type_schema.fields:
            if _effective_type(f) not in _LINK_TYPES:
                continue
            target_type = f.link_target or ""
            for rec in vault.records.get(type_name, []):
                source_id = f"{rec.name}|{type_name}"
                for target_name in _iter_link_names(rec.fields.get(f.name)):
                    target_id = f"{target_name}|{target_type}" if target_type else target_name
                    G.add_edge(
                        source_id, target_id,
                        field_name=f.name,
                        source_type=type_name,
                        target_type=target_type,
                    )

    return G


class GraphAccessor:
    def __init__(self, vault: "Vault") -> None:
        self._vault = vault
        self._digraph_cache: Optional["nx.DiGraph"] = None

    def _digraph(self) -> "nx.DiGraph":
        if self._digraph_cache is None:
            self._digraph_cache = _build_digraph(self._vault)
        return self._digraph_cache

    # ── Name mapping ─────────────────────────────────────────────────────────

    @property
    def kuzu_names(self) -> dict[str, dict[str, dict[str, str]]]:
        """Kuzu name mapping per type, split into ``properties`` and ``relationships``.

        ``properties`` maps original scalar field names to their Kuzu node
        property names (used in ``RETURN n.X``).

        ``relationships`` maps original link field names to their Kuzu
        relationship table names (used in ``MATCH ()-[:X]->()``) .

        Short relationship table names are used by default; the
        ``{Type}__{field}`` prefix is added only when two fields in the vault
        would otherwise collide on the same normalized name.
        """
        rel_names = _build_rel_names(self._vault)
        result: dict[str, dict[str, dict[str, str]]] = {}
        for type_name, type_schema in self._vault.schema.types.items():
            props: dict[str, str] = {}
            rels: dict[str, str] = {}
            for f in type_schema.fields:
                if _effective_type(f) in _LINK_TYPES:
                    if f.link_target:
                        rels[f.name] = rel_names[(type_name, f.name)]
                else:
                    props[f.name] = _kuzu_name(f.name)
            result[type_name] = {"properties": props, "relationships": rels}
        return result

    @property
    def rdf_names(self) -> dict[str, dict[str, dict[str, str]]]:
        """RDF name mapping per type, split into ``properties`` and ``relationships``.

        ``properties`` maps original scalar field names to the URI local name
        used in ``<{base_uri}ontology/{local_name}>``.

        ``relationships`` maps original link field names to their URI local name.

        Use these when writing SPARQL queries against the graph returned by
        ``to_rdf()``.
        """
        result: dict[str, dict[str, dict[str, str]]] = {}
        for type_name, type_schema in self._vault.schema.types.items():
            props: dict[str, str] = {}
            rels: dict[str, str] = {}
            for f in type_schema.fields:
                if _effective_type(f) in _LINK_TYPES:
                    if f.link_target:
                        rels[f.name] = _uri_safe(f.name)
                else:
                    props[f.name] = _uri_safe(f.name)
            result[type_name] = {"properties": props, "relationships": rels}
        return result

    # ── NetworkX ──────────────────────────────────────────────────────────────

    def to_networkx(self, directed: bool = True) -> "nx.DiGraph | nx.Graph":
        """Return the vault as a NetworkX graph.

        ``directed=True`` (default) returns a ``DiGraph`` where edges follow
        declared link directions.  ``directed=False`` returns an undirected
        ``Graph`` useful for reachability and path queries that should ignore
        direction — explicitly trading away edge semantics for traversal freedom.

        Nodes carry ``type`` plus all scalar field values as attributes.
        Edges carry ``field_name``, ``source_type``, and ``target_type``.
        Node identity is ``filename|type``.
        """
        G = self._digraph()
        return G if directed else G.to_undirected()

    # ── Kuzu ─────────────────────────────────────────────────────────────────

    def to_kuzu(self, path: Optional[str | Path] = None):
        """Return a live Kuzu connection with the vault loaded as a property graph.

        One node table per type; one relationship table per link field, named
        ``{SourceType}__{field_name}``.  Pass ``path`` to persist the database;
        omit for an in-memory database.

        Raises ``ImportError`` if Kuzu is not installed.
        """
        try:
            import kuzu
        except ImportError as e:
            raise ImportError(
                "Kuzu is required for `.graph.to_kuzu()`. Install it with `pip install kuzu`."
            ) from e

        db = kuzu.Database(str(path)) if path else kuzu.Database()
        conn = kuzu.Connection(db)
        vault = self._vault
        rel_names = _build_rel_names(vault)

        for type_name, type_schema in vault.schema.types.items():
            scalar_fields = [
                f for f in type_schema.fields
                if _effective_type(f) not in _LINK_TYPES
            ]
            col_defs = ["record STRING"]
            for f in scalar_fields:
                kuzu_type = _FIELD_TO_KUZU.get(_effective_type(f), "STRING")
                col_defs.append(f"{_kuzu_name(f.name)} {kuzu_type}")
            col_defs.append("PRIMARY KEY (record)")
            conn.execute(f"CREATE NODE TABLE `{type_name}` ({', '.join(col_defs)})")

            for rec in vault.records.get(type_name, []):
                params: dict[str, Any] = {"_r": rec.name}
                prop_parts = ["record: $_r"]
                for i, f in enumerate(scalar_fields):
                    val = rec.fields.get(f.name)
                    if val is None:
                        continue
                    pname = f"_f{i}"
                    params[pname] = _coerce_kuzu(val, _effective_type(f))
                    prop_parts.append(f"{_kuzu_name(f.name)}: ${pname}")
                conn.execute(
                    f"CREATE (:`{type_name}` {{{', '.join(prop_parts)}}})",
                    params,
                )

        for type_name, type_schema in vault.schema.types.items():
            for f in type_schema.fields:
                if _effective_type(f) not in _LINK_TYPES:
                    continue
                target_type = f.link_target
                if not target_type:
                    continue
                rel_name = rel_names[(type_name, f.name)]
                conn.execute(
                    f"CREATE REL TABLE `{rel_name}` (FROM `{type_name}` TO `{target_type}`)"
                )
                for rec in vault.records.get(type_name, []):
                    for target_name in _iter_link_names(rec.fields.get(f.name)):
                        conn.execute(
                            f"MATCH (a:`{type_name}` {{record: $src}}), "
                            f"(b:`{target_type}` {{record: $tgt}}) "
                            f"CREATE (a)-[:`{rel_name}`]->(b)",
                            {"src": rec.name, "tgt": target_name},
                        )

        return conn

    # ── RDF ───────────────────────────────────────────────────────────────────

    def to_rdf(
        self,
        path: Optional[str | Path] = None,
        base_uri: str = "http://vault/",
        format: str = "turtle",
    ):
        """Return an RDFLib Graph with the vault mapped to RDF triples.

        Mapping:
        - Records → named individuals at ``{base_uri}{Type}/{filename}``
        - Types → OWL classes at ``{base_uri}ontology/{TypeName}``
        - Scalar fields → OWL datatype properties
        - Link fields → OWL object properties
        - Declared pairs → ``owl:inverseOf`` assertions

        Pass ``path`` to serialise to disk (default format: Turtle).
        The RDFLib Graph is always returned regardless of ``path``.

        Raises ``ImportError`` if RDFLib is not installed.
        """
        try:
            from rdflib import Graph, Literal, Namespace, URIRef
            from rdflib.namespace import OWL, RDF, XSD
        except ImportError as e:
            raise ImportError(
                "RDFLib is required for `.graph.to_rdf()`. Install it with `pip install rdflib`."
            ) from e

        if not base_uri.endswith("/"):
            base_uri += "/"

        vault = self._vault
        g = Graph()
        onto = Namespace(base_uri + "ontology/")
        g.bind("onto", onto)
        g.bind("owl", OWL)

        _XSD_MAP = {
            FieldType.STRING:   XSD.string,
            FieldType.INTEGER:  XSD.integer,
            FieldType.NUMBER:   XSD.double,
            FieldType.BOOLEAN:  XSD.boolean,
            FieldType.DATE:     XSD.date,
            FieldType.DATETIME: XSD.dateTime,
        }

        # Declare OWL classes and properties
        for type_name, type_schema in vault.schema.types.items():
            g.add((onto[_uri_safe(type_name)], RDF.type, OWL.Class))
            for f in type_schema.fields:
                eff = _effective_type(f)
                prop_uri = onto[_uri_safe(f.name)]
                if eff in _LINK_TYPES:
                    g.add((prop_uri, RDF.type, OWL.ObjectProperty))
                else:
                    g.add((prop_uri, RDF.type, OWL.DatatypeProperty))

        # owl:inverseOf for declared pairs
        for first, second in vault.relationship_pairs:
            _, field_a = first.split(".", 1)
            _, field_b = second.split(".", 1)
            g.add((onto[_uri_safe(field_a)], OWL.inverseOf, onto[_uri_safe(field_b)]))
            g.add((onto[_uri_safe(field_b)], OWL.inverseOf, onto[_uri_safe(field_a)]))

        # Records as named individuals with property assertions
        for type_name, type_schema in vault.schema.types.items():
            cls_uri = onto[_uri_safe(type_name)]
            for rec in vault.records.get(type_name, []):
                ind_uri = URIRef(base_uri + f"{_uri_safe(type_name)}/{_uri_safe(rec.name)}")
                g.add((ind_uri, RDF.type, cls_uri))

                for f in type_schema.fields:
                    eff = _effective_type(f)
                    prop_uri = onto[_uri_safe(f.name)]
                    val = rec.fields.get(f.name)
                    if val is None:
                        continue

                    if eff in _LINK_TYPES:
                        target_type = f.link_target or type_name
                        for target_name in _iter_link_names(val):
                            target_uri = URIRef(
                                base_uri + f"{_uri_safe(target_type)}/{_uri_safe(target_name)}"
                            )
                            g.add((ind_uri, prop_uri, target_uri))

                    elif eff == FieldType.LIST_STRINGS:
                        items = val if isinstance(val, list) else [val]
                        for item in items:
                            g.add((ind_uri, prop_uri, Literal(str(item), datatype=XSD.string)))

                    else:
                        xsd_type = _XSD_MAP.get(eff)
                        if xsd_type:
                            g.add((ind_uri, prop_uri, Literal(val, datatype=xsd_type)))
                        else:
                            g.add((ind_uri, prop_uri, Literal(str(val))))

        if path is not None:
            g.serialize(destination=str(path), format=format)

        return g

    def __repr__(self) -> str:
        n_recs = sum(len(recs) for recs in self._vault.records.values())
        types = list(self._vault.schema.types)
        return f"GraphAccessor({types}, {n_recs} records)"
