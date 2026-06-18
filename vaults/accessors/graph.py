"""Graph accessor — exports the vault as a property graph (NetworkX, Kuzu, RDF)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..schema import FieldSchema, FieldType
from ..links import iter_link_names, LINK_TYPES

if TYPE_CHECKING:
    import networkx as nx
    from ..vault import Vault

_FIELD_TO_KUZU: dict[FieldType, str] = {
    FieldType.STRING:       "STRING",
    FieldType.INTEGER:      "INT64",
    FieldType.NUMBER:       "DOUBLE",
    FieldType.BOOLEAN:      "BOOL",
    FieldType.DATE:         "DATE",
    FieldType.DATETIME:     "TIMESTAMP",
    FieldType.LIST_STRINGS:  "STRING[]",
    FieldType.LIST_INTEGERS: "INT64[]",
    FieldType.LIST_NUMBERS:  "DOUBLE[]",
    FieldType.LIST_MIXED:    "STRING[]",
    FieldType.UNKNOWN:      "STRING",
}


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
            if f.effective_type in LINK_TYPES and f.link_target:
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


_GRAPHML_PRIMITIVES = (bool, int, float, str)


def _flatten_value(v: Any) -> Any:
    """Coerce a value to a GraphML/GEXF-safe primitive.

    GraphML supports only bool, int, float, and str. Lists are joined;
    date/datetime and any other type is cast to str.
    """
    if isinstance(v, list):
        return ", ".join(str(i) for i in v)
    if isinstance(v, _GRAPHML_PRIMITIVES):
        return v
    return str(v)


def _flatten_graph_attrs(G: "nx.DiGraph") -> "nx.DiGraph":
    """Return a copy of G with all node/edge attrs coerced to GraphML-safe primitives."""
    H = G.copy()
    for _, data in H.nodes(data=True):
        for k, v in list(data.items()):
            data[k] = _flatten_value(v)
    for _, _, data in H.edges(data=True):
        for k, v in list(data.items()):
            data[k] = _flatten_value(v)
    return H


def _build_digraph(vault: "Vault") -> "nx.DiGraph":
    """
    Build a directed NetworkX graph from all vault records and their link fields.

    Each record becomes a node with id ``"{record_name}|{type_name}"``. Scalar
    field values are stored as node attributes; link fields are omitted from node
    attrs and instead become directed edges. Each edge carries ``field_name``,
    ``source_type``, and ``target_type`` attributes.

    Parameters
    ----------
    vault : Vault
        The vault to build the graph from.

    Returns
    -------
    nx.DiGraph
        A directed graph with one node per record and one edge per link target.
    """
    import networkx as nx

    G = nx.DiGraph()

    for type_name, type_schema in vault.schema.types.items():
        for rec in vault.records.get(type_name, []):
            node_id = f"{rec.name}|{type_name}"
            attrs: dict[str, Any] = {"type": type_name}
            for f in type_schema.fields:
                if f.effective_type in LINK_TYPES:
                    continue
                val = rec.fields.get(f.name)
                if val is not None:
                    attrs[f.name] = val
            G.add_node(node_id, **attrs)

    for type_name, type_schema in vault.schema.types.items():
        for f in type_schema.fields:
            if f.effective_type not in LINK_TYPES:
                continue
            target_type = f.link_target or ""
            for rec in vault.records.get(type_name, []):
                source_id = f"{rec.name}|{type_name}"
                for target_name in iter_link_names(rec.fields.get(f.name)):
                    target_id = f"{target_name}|{target_type}" if target_type else target_name
                    G.add_edge(
                        source_id, target_id,
                        field_name=f.name,
                        source_type=type_name,
                        target_type=target_type,
                    )

    return G


class GraphAccessor:
    """
    Exports the vault as a property graph in multiple formats.

    Supports NetworkX (in-memory), Kuzu (Cypher / property graph DB), RDF
    (Turtle, N-Triples, etc. via RDFLib), GraphML, and GEXF. The underlying
    directed graph is built once and cached; all export methods derive from it.

    Accessed via ``vault.graph``, which lazily constructs and caches this accessor.
    """

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
        """
        Kuzu name mapping per type, split into ``properties`` and ``relationships``.

        Use this to translate original field names to Kuzu identifiers when
        writing Cypher queries against the graph returned by ``to_kuzu()``.

        Returns
        -------
        dict[str, dict[str, dict[str, str]]]
            Nested dict keyed by type name. Each value has two sub-dicts:

            ``properties`` : dict[str, str]
                Maps original scalar field name → Kuzu node property name
                (used in ``RETURN n.X``).
            ``relationships`` : dict[str, str]
                Maps original link field name → Kuzu relationship table name
                (used in ``MATCH ()-[:X]->()``). Short names are used by
                default; ``{Type}__{field}`` prefix is added only when two
                fields would otherwise collide on the same normalised name.
        """
        rel_names = _build_rel_names(self._vault)
        result: dict[str, dict[str, dict[str, str]]] = {}
        for type_name, type_schema in self._vault.schema.types.items():
            props: dict[str, str] = {}
            rels: dict[str, str] = {}
            for f in type_schema.fields:
                if f.effective_type in LINK_TYPES:
                    if f.link_target:
                        rels[f.name] = rel_names[(type_name, f.name)]
                else:
                    props[f.name] = _kuzu_name(f.name)
            result[type_name] = {"properties": props, "relationships": rels}
        return result

    @property
    def rdf_names(self) -> dict[str, dict[str, dict[str, str]]]:
        """
        RDF name mapping per type, split into ``properties`` and ``relationships``.

        Use this to translate original field names to URI local names when
        writing SPARQL queries against the graph returned by ``to_rdf()``.

        Returns
        -------
        dict[str, dict[str, dict[str, str]]]
            Nested dict keyed by type name. Each value has two sub-dicts:

            ``properties`` : dict[str, str]
                Maps original scalar field name → URI local name used in
                ``<{base_uri}ontology/{local_name}>``.
            ``relationships`` : dict[str, str]
                Maps original link field name → URI local name.
        """
        result: dict[str, dict[str, dict[str, str]]] = {}
        for type_name, type_schema in self._vault.schema.types.items():
            props: dict[str, str] = {}
            rels: dict[str, str] = {}
            for f in type_schema.fields:
                if f.effective_type in LINK_TYPES:
                    if f.link_target:
                        rels[f.name] = _uri_safe(f.name)
                else:
                    props[f.name] = _uri_safe(f.name)
            result[type_name] = {"properties": props, "relationships": rels}
        return result

    # ── NetworkX ──────────────────────────────────────────────────────────────

    def to_networkx(self, directed: bool = True) -> "nx.DiGraph | nx.Graph":
        """
        Return the vault as a NetworkX graph.

        Parameters
        ----------
        directed : bool
            When True (default), returns a ``DiGraph`` where edges follow
            declared link directions. When False, returns an undirected ``Graph``
            useful for reachability and path queries that ignore direction —
            trading away edge semantics for traversal freedom.

        Returns
        -------
        nx.DiGraph or nx.Graph
            Nodes carry ``type`` plus all scalar field values as attributes.
            Edges carry ``field_name``, ``source_type``, and ``target_type``.
            Node identity is ``"{record_name}|{type_name}"``.
        """
        G = self._digraph()
        return G if directed else G.to_undirected()

    # ── Kuzu ─────────────────────────────────────────────────────────────────

    def to_kuzu(self, path: Optional[str | Path] = None):
        """
        Load the vault into a Kuzu property graph database and return a live connection.

        Creates one node table per type and one relationship table per link
        field. Relationship table names follow the same collision-avoidance
        scheme described in ``kuzu_names``.

        Parameters
        ----------
        path : str, Path, or None
            Filesystem path to persist the Kuzu database. When None, an
            in-memory database is used.

        Returns
        -------
        kuzu.Connection
            An open connection to the populated Kuzu database.

        Raises
        ------
        ImportError
            When the ``kuzu`` package is not installed.
        """
        try:
            import kuzu
        except ImportError as e:
            raise ImportError(
                "Kuzu is required for `.graph.to_kuzu()`. Install with: pip install vaults[kuzu]"
            ) from e

        db = kuzu.Database(str(path)) if path else kuzu.Database()
        conn = kuzu.Connection(db)
        vault = self._vault
        rel_names = _build_rel_names(vault)

        for type_name, type_schema in vault.schema.types.items():
            scalar_fields = [
                f for f in type_schema.fields
                if f.effective_type not in LINK_TYPES
            ]
            col_defs = ["record STRING"]
            for f in scalar_fields:
                kuzu_type = _FIELD_TO_KUZU.get(f.effective_type, "STRING")
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
                    params[pname] = _coerce_kuzu(val, f.effective_type)
                    prop_parts.append(f"{_kuzu_name(f.name)}: ${pname}")
                conn.execute(
                    f"CREATE (:`{type_name}` {{{', '.join(prop_parts)}}})",
                    params,
                )

        for type_name, type_schema in vault.schema.types.items():
            for f in type_schema.fields:
                if f.effective_type not in LINK_TYPES:
                    continue
                target_type = f.link_target
                if not target_type:
                    continue
                rel_name = rel_names[(type_name, f.name)]
                conn.execute(
                    f"CREATE REL TABLE `{rel_name}` (FROM `{type_name}` TO `{target_type}`)"
                )
                for rec in vault.records.get(type_name, []):
                    for target_name in iter_link_names(rec.fields.get(f.name)):
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
        """
        Map the vault to RDF triples and return an RDFLib Graph.

        OWL mapping:

        - Records → named individuals at ``{base_uri}{Type}/{filename}``
        - Types → OWL classes at ``{base_uri}ontology/{TypeName}``
        - Scalar fields → OWL datatype properties
        - Link fields → OWL object properties
        - Declared pairs → ``owl:inverseOf`` assertions

        Parameters
        ----------
        path : str, Path, or None
            When provided, the graph is serialised to this file path using
            ``format``. The RDFLib Graph is always returned regardless.
        base_uri : str
            Base URI for all generated resource and ontology URIs. A trailing
            slash is appended automatically if absent.
        format : str
            RDFLib serialisation format (e.g. ``"turtle"``, ``"n3"``,
            ``"nt"``). Only used when ``path`` is provided.

        Returns
        -------
        rdflib.Graph
            The fully populated RDF graph.

        Raises
        ------
        ImportError
            When the ``rdflib`` package is not installed.
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
                eff = f.effective_type
                prop_uri = onto[_uri_safe(f.name)]
                if eff in LINK_TYPES:
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
                    eff = f.effective_type
                    prop_uri = onto[_uri_safe(f.name)]
                    val = rec.fields.get(f.name)
                    if val is None:
                        continue

                    if eff in LINK_TYPES:
                        target_type = f.link_target or type_name
                        for target_name in iter_link_names(val):
                            target_uri = URIRef(
                                base_uri + f"{_uri_safe(target_type)}/{_uri_safe(target_name)}"
                            )
                            g.add((ind_uri, prop_uri, target_uri))

                    elif eff == FieldType.LIST_STRINGS:
                        items = val if isinstance(val, list) else [val]
                        for item in items:
                            g.add((ind_uri, prop_uri, Literal(str(item), datatype=XSD.string)))

                    elif eff == FieldType.LIST_INTEGERS:
                        items = val if isinstance(val, list) else [val]
                        for item in items:
                            g.add((ind_uri, prop_uri, Literal(item, datatype=XSD.integer)))

                    elif eff == FieldType.LIST_NUMBERS:
                        items = val if isinstance(val, list) else [val]
                        for item in items:
                            g.add((ind_uri, prop_uri, Literal(item, datatype=XSD.double)))

                    else:
                        xsd_type = _XSD_MAP.get(eff)
                        if xsd_type:
                            g.add((ind_uri, prop_uri, Literal(val, datatype=xsd_type)))
                        else:
                            g.add((ind_uri, prop_uri, Literal(str(val))))

        if path is not None:
            g.serialize(destination=str(path), format=format)

        return g

    # ── GraphML / GEXF export ─────────────────────────────────────────────────

    def to_graphml(self, path: str | Path, directed: bool = True) -> Path:
        """
        Write the vault graph to a GraphML file and return the path.

        GraphML is an XML-based format readable by Gephi, yEd, and other tools.
        List-typed field values are joined to comma-separated strings because
        GraphML does not support array attributes.

        Parameters
        ----------
        path : str or Path
            Destination file path.
        directed : bool
            When False, the graph is converted to undirected before export.

        Returns
        -------
        Path
            The resolved path to the written file.
        """
        import networkx as nx

        G = self._digraph() if directed else self._digraph().to_undirected()
        H = _flatten_graph_attrs(G)
        path = Path(path)
        nx.write_graphml(H, str(path))
        return path

    def to_gephi(self, path: str | Path, directed: bool = True) -> Path:
        """
        Write the vault graph to a GEXF file (Gephi's native format) and return the path.

        GEXF (Graph Exchange XML Format) is the native import format for Gephi.
        List-typed field values are joined to comma-separated strings because
        GEXF does not support array attributes.

        Parameters
        ----------
        path : str or Path
            Destination file path.
        directed : bool
            When False, the graph is converted to undirected before export.

        Returns
        -------
        Path
            The resolved path to the written file.
        """
        import networkx as nx

        G = self._digraph() if directed else self._digraph().to_undirected()
        H = _flatten_graph_attrs(G)
        path = Path(path)
        nx.write_gexf(H, str(path))
        return path

    def __repr__(self) -> str:
        n_recs = sum(len(recs) for recs in self._vault.records.values())
        types = list(self._vault.schema.types)
        return f"GraphAccessor({types}, {n_recs} records)"
