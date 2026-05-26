"""Tests for vault.graph — NetworkX, Kuzu, and RDF export."""
from pathlib import Path

import pytest

from vaults import FieldSchema, FieldType, Record, Schema, TypeSchema, Vault

FIXTURE = Path(__file__).parent / "fixtures" / "sample_vault"

# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def vault():
    return Vault.from_vault(FIXTURE, dangling_refs="stub")


@pytest.fixture(scope="module")
def vault_pairs():
    return Vault.from_vault(
        FIXTURE,
        dangling_refs="stub",
        relationship_pairs=[("Projekte.Traeger", "Personen.Projekte")],
    )


# ── kuzu_names property ───────────────────────────────────────────────────────

def test_kuzu_names_covers_all_types(vault):
    names = vault.graph.kuzu_names
    assert "Projekte" in names
    assert "Personen" in names


def test_kuzu_names_has_properties_and_relationships_keys(vault):
    entry = vault.graph.kuzu_names["Projekte"]
    assert "properties" in entry
    assert "relationships" in entry


def test_kuzu_names_scalar_in_properties(vault):
    assert vault.graph.kuzu_names["Projekte"]["properties"]["Titel"] == "Titel"


def test_kuzu_names_link_in_relationships_short_form(vault):
    # Only one field named Traeger in the vault → short form, no prefix
    assert vault.graph.kuzu_names["Projekte"]["relationships"]["Traeger"] == "Traeger"


def test_kuzu_names_normalizes_umlaut():
    schema = Schema(types={"Typen": TypeSchema(name="Typen", fields=[
        FieldSchema(name="Träger", type=FieldType.STRING),
        FieldSchema(name="Teil der NSP", type=FieldType.STRING),
    ])})
    vault = Vault(schema=schema, records={"Typen": []})
    props = vault.graph.kuzu_names["Typen"]["properties"]
    assert props["Träger"] == "Traeger"
    assert props["Teil der NSP"] == "Teil_der_NSP"


def test_kuzu_names_collision_falls_back_to_prefixed():
    # Two types both have a link field named "ref" — collision → prefixed
    schema_a = TypeSchema(name="A", fields=[FieldSchema(name="ref", type=FieldType.LINK, link_target="B")])
    schema_b = TypeSchema(name="B", fields=[FieldSchema(name="ref", type=FieldType.LINK, link_target="A")])
    vault = Vault(schema=Schema(types={"A": schema_a, "B": schema_b}), records={})
    assert vault.graph.kuzu_names["A"]["relationships"]["ref"] == "A__ref"
    assert vault.graph.kuzu_names["B"]["relationships"]["ref"] == "B__ref"


# ── NetworkX ──────────────────────────────────────────────────────────────────

def test_to_networkx_returns_digraph(vault):
    import networkx as nx
    G = vault.graph.to_networkx()
    assert isinstance(G, nx.DiGraph)


def test_nodes_use_pipe_identity(vault):
    G = vault.graph.to_networkx()
    assert "Alpha|Projekte" in G.nodes
    assert "Anna|Personen" in G.nodes


def test_node_has_type_attribute(vault):
    G = vault.graph.to_networkx()
    assert G.nodes["Alpha|Projekte"]["type"] == "Projekte"


def test_node_has_scalar_field(vault):
    G = vault.graph.to_networkx()
    assert G.nodes["Alpha|Projekte"]["Titel"] == "Alpha-Projekt"


def test_link_field_not_in_node_attrs(vault):
    G = vault.graph.to_networkx()
    assert "Traeger" not in G.nodes["Alpha|Projekte"]


def test_edges_created_for_links(vault):
    G = vault.graph.to_networkx()
    assert G.has_edge("Alpha|Projekte", "Anna|Personen")
    assert G.has_edge("Alpha|Projekte", "Ben|Personen")


def test_edge_carries_field_name(vault):
    G = vault.graph.to_networkx()
    data = G.edges["Alpha|Projekte", "Anna|Personen"]
    assert data["field_name"] == "Traeger"
    assert data["source_type"] == "Projekte"
    assert data["target_type"] == "Personen"


def test_stub_node_present(vault):
    G = vault.graph.to_networkx()
    assert "Clara|Personen" in G.nodes


def test_pairs_produce_bidirectional_edges(vault_pairs):
    G = vault_pairs.graph.to_networkx()
    assert G.has_edge("Alpha|Projekte", "Anna|Personen")
    assert G.has_edge("Anna|Personen", "Alpha|Projekte")


def test_to_networkx_directed_false_returns_graph(vault):
    import networkx as nx
    G = vault.graph.to_networkx(directed=False)
    assert isinstance(G, nx.Graph)
    assert not isinstance(G, nx.DiGraph)


def test_undirected_finds_path_against_edge_direction(vault):
    import networkx as nx
    # Beta→Clara (directed). Clara→Beta does not exist in the DiGraph.
    G_dir = vault.graph.to_networkx(directed=True)
    assert not nx.has_path(G_dir, "Clara|Personen", "Beta|Projekte")
    G_und = vault.graph.to_networkx(directed=False)
    assert nx.has_path(G_und, "Clara|Personen", "Beta|Projekte")


def test_digraph_cached_within_accessor(vault):
    ga = vault.graph
    g1 = ga.to_networkx()
    g2 = ga.to_networkx()
    assert g1 is g2


# ── Kuzu ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def kuzu_conn(vault, tmp_path_factory):
    return vault.graph.to_kuzu(tmp_path_factory.mktemp("kuzu") / "db")


@pytest.fixture(scope="module")
def kuzu_conn_pairs(vault_pairs, tmp_path_factory):
    return vault_pairs.graph.to_kuzu(tmp_path_factory.mktemp("kuzu_pairs") / "db")


def test_kuzu_returns_connection(kuzu_conn):
    import kuzu
    assert isinstance(kuzu_conn, kuzu.Connection)


def test_kuzu_node_tables_created(kuzu_conn):
    res = kuzu_conn.execute(
        "CALL show_tables() RETURN name, type"
    ).get_as_df()
    node_tables = set(res[res["type"] == "NODE"]["name"])
    assert "Projekte" in node_tables
    assert "Personen" in node_tables


def test_kuzu_records_loaded(kuzu_conn):
    res = kuzu_conn.execute("MATCH (n:Projekte) RETURN n.record").get_as_df()
    assert set(res["n.record"]) == {"Alpha", "Beta"}


def test_kuzu_scalar_field_value(kuzu_conn):
    res = kuzu_conn.execute(
        "MATCH (n:Projekte {record: 'Alpha'}) RETURN n.Titel"
    ).get_as_df()
    assert res.iloc[0, 0] == "Alpha-Projekt"


def test_kuzu_rel_table_created(kuzu_conn):
    res = kuzu_conn.execute(
        "CALL show_tables() RETURN name, type"
    ).get_as_df()
    rel_tables = set(res[res["type"] == "REL"]["name"])
    assert "Traeger" in rel_tables


def test_kuzu_edges_loaded(kuzu_conn):
    res = kuzu_conn.execute(
        "MATCH (a:Projekte)-[:Traeger]->(b:Personen) "
        "RETURN a.record, b.record"
    ).get_as_df()
    pairs = set(zip(res["a.record"], res["b.record"]))
    assert ("Alpha", "Anna") in pairs
    assert ("Alpha", "Ben") in pairs


def test_kuzu_pair_both_rel_tables(kuzu_conn_pairs):
    res = kuzu_conn_pairs.execute(
        "CALL show_tables() RETURN name, type"
    ).get_as_df()
    rel_tables = set(res[res["type"] == "REL"]["name"])
    # "Traeger" has no collision → short form; "Projekte" would clash with the
    # Projekte node table → falls back to prefixed "Personen__Projekte"
    assert "Traeger" in rel_tables
    assert "Personen__Projekte" in rel_tables


def test_kuzu_no_missing_dep_error():
    """GraphAccessor raises ImportError with install hint when kuzu is absent."""
    import sys
    import importlib
    kuzu_real = sys.modules.get("kuzu")
    sys.modules["kuzu"] = None  # type: ignore[assignment]
    try:
        from vaults.accessors.graph import GraphAccessor
        import vaults
        v = vaults.Vault.from_vault(FIXTURE, dangling_refs="stub")
        ga = GraphAccessor(v)
        with pytest.raises(ImportError, match="pip install kuzu"):
            ga.to_kuzu()
    finally:
        if kuzu_real is not None:
            sys.modules["kuzu"] = kuzu_real
        else:
            del sys.modules["kuzu"]


# ── rdf_names property ───────────────────────────────────────────────────────

def test_rdf_names_scalar_in_properties(vault):
    props = vault.graph.rdf_names["Projekte"]["properties"]
    assert props["Titel"] == "Titel"


def test_rdf_names_link_in_relationships(vault):
    rels = vault.graph.rdf_names["Projekte"]["relationships"]
    assert rels["Traeger"] == "Traeger"


def test_rdf_names_normalizes_umlaut():
    schema = Schema(types={"Typen": TypeSchema(name="Typen", fields=[
        FieldSchema(name="Träger", type=FieldType.STRING),
        FieldSchema(name="Übergeordnete Akteure", type=FieldType.STRING),
    ])})
    vault = Vault(schema=schema, records={"Typen": []})
    props = vault.graph.rdf_names["Typen"]["properties"]
    assert props["Träger"] == "Traeger"
    assert props["Übergeordnete Akteure"] == "Uebergeordnete_Akteure"


# ── RDF ───────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rdf_graph(vault):
    return vault.graph.to_rdf()


@pytest.fixture(scope="module")
def rdf_graph_pairs(vault_pairs):
    return vault_pairs.graph.to_rdf()


def test_rdf_returns_graph(rdf_graph):
    from rdflib import Graph
    assert isinstance(rdf_graph, Graph)


def test_rdf_type_declared_as_owl_class(rdf_graph):
    from rdflib import URIRef
    from rdflib.namespace import OWL, RDF
    cls = URIRef("http://vault/ontology/Projekte")
    assert (cls, RDF.type, OWL.Class) in rdf_graph


def test_rdf_record_typed(rdf_graph):
    from rdflib import URIRef
    from rdflib.namespace import RDF
    ind = URIRef("http://vault/Projekte/Alpha")
    cls = URIRef("http://vault/ontology/Projekte")
    assert (ind, RDF.type, cls) in rdf_graph


def test_rdf_scalar_literal(rdf_graph):
    from rdflib import Literal, URIRef
    from rdflib.namespace import XSD
    ind  = URIRef("http://vault/Projekte/Alpha")
    prop = URIRef("http://vault/ontology/Titel")
    assert (ind, prop, Literal("Alpha-Projekt", datatype=XSD.string)) in rdf_graph


def test_rdf_link_as_object_property(rdf_graph):
    from rdflib import URIRef
    ind  = URIRef("http://vault/Projekte/Alpha")
    prop = URIRef("http://vault/ontology/Traeger")
    obj  = URIRef("http://vault/Personen/Anna")
    assert (ind, prop, obj) in rdf_graph


def test_rdf_link_property_declared_as_object_property(rdf_graph):
    from rdflib import URIRef
    from rdflib.namespace import OWL, RDF
    prop = URIRef("http://vault/ontology/Traeger")
    assert (prop, RDF.type, OWL.ObjectProperty) in rdf_graph


def test_rdf_scalar_property_declared_as_datatype_property(rdf_graph):
    from rdflib import URIRef
    from rdflib.namespace import OWL, RDF
    prop = URIRef("http://vault/ontology/Titel")
    assert (prop, RDF.type, OWL.DatatypeProperty) in rdf_graph


def test_rdf_inverse_of_for_pair(rdf_graph_pairs):
    from rdflib import URIRef
    from rdflib.namespace import OWL
    field_a = URIRef("http://vault/ontology/Traeger")
    field_b = URIRef("http://vault/ontology/Projekte")
    assert (field_a, OWL.inverseOf, field_b) in rdf_graph_pairs
    assert (field_b, OWL.inverseOf, field_a) in rdf_graph_pairs


def test_rdf_record_name_with_space_normalized():
    from rdflib import URIRef
    from rdflib.namespace import RDF
    from vaults import Record
    type_schema = TypeSchema(name="Orte", fields=[FieldSchema(name="Name", type=FieldType.STRING)])
    schema = Schema(types={"Orte": type_schema})
    rec = Record(name="DEIN Park", type_schema=type_schema, fields={"Name": "DEIN Park"})
    vault = Vault(schema=schema, records={"Orte": [rec]})
    g = vault.graph.to_rdf()
    ind = URIRef("http://vault/Orte/DEIN_Park")
    cls = URIRef("http://vault/ontology/Orte")
    assert (ind, RDF.type, cls) in g


def test_rdf_serializes_to_file(rdf_graph, tmp_path):
    out = tmp_path / "vault.ttl"
    rdf_graph.serialize(destination=str(out), format="turtle")
    assert out.exists()
    assert out.stat().st_size > 0


def test_rdf_to_rdf_with_path(vault, tmp_path):
    out = tmp_path / "vault.ttl"
    g = vault.graph.to_rdf(path=out)
    from rdflib import Graph
    assert isinstance(g, Graph)
    assert out.exists()


# ── GraphML ───────────────────────────────────────────────────────────────────

def test_to_graphml_creates_file(vault, tmp_path):
    out = tmp_path / "vault.graphml"
    result = vault.graph.to_graphml(out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_to_graphml_readable(vault, tmp_path):
    import networkx as nx
    out = tmp_path / "vault.graphml"
    vault.graph.to_graphml(out)
    H = nx.read_graphml(str(out))
    assert "Alpha|Projekte" in H.nodes
    assert "Anna|Personen" in H.nodes


def test_to_graphml_edges_present(vault, tmp_path):
    import networkx as nx
    out = tmp_path / "vault.graphml"
    vault.graph.to_graphml(out)
    H = nx.read_graphml(str(out))
    assert H.has_edge("Alpha|Projekte", "Anna|Personen")


def test_to_graphml_directed_false(vault, tmp_path):
    import networkx as nx
    out = tmp_path / "vault_und.graphml"
    vault.graph.to_graphml(out, directed=False)
    H = nx.read_graphml(str(out))
    assert not isinstance(H, nx.DiGraph)


# ── GEXF / Gephi ──────────────────────────────────────────────────────────────

def test_to_gephi_creates_file(vault, tmp_path):
    out = tmp_path / "vault.gexf"
    result = vault.graph.to_gephi(out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_to_gephi_readable(vault, tmp_path):
    import networkx as nx
    out = tmp_path / "vault.gexf"
    vault.graph.to_gephi(out)
    H = nx.read_gexf(str(out))
    node_labels = {data.get("label", nid) for nid, data in H.nodes(data=True)}
    assert "Alpha|Projekte" in node_labels or any("Alpha" in n for n in H.nodes)


def test_to_gephi_directed_false(vault, tmp_path):
    import networkx as nx
    out = tmp_path / "vault_und.gexf"
    vault.graph.to_gephi(out, directed=False)
    H = nx.read_gexf(str(out))
    assert not isinstance(H, nx.DiGraph)
