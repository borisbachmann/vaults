# Graph Namespace — Design & Implementation Instructions

## Overview

The `.graph` namespace sits alongside `.dfs` and `.db` on the `Vault` object. It exposes the vault's records and relationships as a property graph, with three export targets covering different use cases: algorithmic analysis (NetworkX), embedded Cypher querying (Kuzu), and semantic web interoperability (RDF/RDFLib).

All three targets are built from a shared internal representation produced by `_resolve_pairs()`.

---

## `_resolve_pairs()` — Universal Pair Handler

Implement as an internal `Vault` method. Called before `.graph`, `.dfs`, and `.db` output. **Retrofit into existing DF and DB functionality.**

**For each declared pair** `(TypeA.field_x, TypeB.field_y)`:

1. Collect all `(source, target)` tuples from `TypeA.field_x` across all records of `TypeA`.
2. Collect all `(target, source)` tuples from `TypeB.field_y` across all records of `TypeB` (inverted, since field_y points B→A).
3. Take the **outer union** of both sets (deduplicated).
4. For every unique `(a, b)` tuple in that union, emit:
   - `(a, b, field_x, TypeA, TypeB)`
   - `(b, a, field_y, TypeB, TypeA)`

**For unpaired link fields**, emit one directed edge per link as-is.

**Output**: a flat list of resolved edge tuples:
```
(source_id, target_id, field_name, source_type, target_type)
```
No further pair logic is needed downstream. All three export targets consume this list directly.

**Node identity format**: `filename|type` throughout.

---

## `.graph` Namespace

### Internal shared graph object

Build a `networkx.DiGraph` as the internal representation from the resolved edge tuples:

**Nodes**: one per record.
- `type`: type name
- all scalar fields as attributes

**Edges**: one per resolved edge tuple.
- `field_name`
- `source_type`
- `target_type`

This DiGraph is the shared base. All three export methods derive from it.

---

### `.graph.to_networkx()`

Returns the `networkx.DiGraph` directly. Intended for algorithmic use: traversal, centrality, clustering, community detection, etc.

No transformation needed beyond the base construction.

---

### `.graph.to_kuzu(path=None)`

Returns a live Kuzu connection with the vault loaded as a property graph. Intended for interactive Cypher querying without a server.

**Kuzu is an optional dependency.** If not installed, raise a clear error:
```
KuzuNotInstalledError: "Kuzu is required for `.graph.to_kuzu()`. Install it with `pip install kuzu`."
```

**Schema construction**:
- One node table per type, with columns derived from scalar fields.
- One relationship table per unique `field_name` in the resolved edge list, typed from `source_type` to `target_type`.

**Loading**:
- Insert all nodes per type.
- Insert all edges per relationship table.

**Return value**: a Kuzu `Connection` object, ready for `.execute("MATCH ...")` calls.

If `path` is provided, persist the Kuzu database to disk. If `None`, use an in-memory database.

---

### `.graph.to_rdf(path=None, base_uri="http://vault/")`

Returns an RDFLib `Graph` (or serializes to a file if `path` is provided). Intended for interoperability with SPARQL tooling and triple stores.

**Mapping**:
- Records → named individuals: `{base_uri}{type}/{filename}`
- Types → OWL classes: `{base_uri}ontology/{TypeName}`
- `rdf:type` assertions for every record
- Scalar fields → datatype properties: `{base_uri}ontology/{field_name}`
- Link fields → object properties: `{base_uri}ontology/{field_name}`
- Relationship pairs → `owl:inverseOf` declarations between the two field URIs

**Serialization**: if `path` is provided, serialize as Turtle (`.ttl`) by default. Format can be overridden (`format="json-ld"` etc.) via a `format` parameter.

**Return value**: the RDFLib `Graph` object regardless of whether a path is given.

---

## Dependencies

| Target | Dependency | Required |
|---|---|---|
| `.to_networkx()` | `networkx` | Hard |
| `.to_kuzu()` | `kuzu` | Optional |
| `.to_rdf()` | `rdflib` | Hard |

---

## Reminders for later

- **Pairs in DF/DB functionality**: `_resolve_pairs()` must be retrofitted into the existing DataFrame and SQLite output. Paired fields should not produce two independent join tables — revisit `.to_df()` and `.to_db()` join table logic once the pair handler exists.
- **Self-linking loops**: no policy has been defined yet for records that link to themselves. Needs a decision before `.graph`, `.dfs`, or `.db` output can be considered complete.
