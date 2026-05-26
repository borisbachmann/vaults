# vaults

A Python package for data analytics on human-curated structured and relational data in Markdown format, built around [Obsidian](https://obsidian.md).

Files on disk are the source of truth. `vaults` parses `.md` records and Obsidian `.base` files into an interlinked in-memory snapshot, then exposes that data through multiple backends.

## Features

- **Parsing** — Reads Markdown frontmatter and `.base` files into typed, interlinked `Record` objects inside a `Vault` container. Formulas defined in `.base` files are compiled and evaluated at load time.
- **Linting** — Automatically detects inconsistencies in relational data (mixed link targets, dangling references, schema inhomogeneity) and reports them as navigable violations.
- **DataFrames** — Export per-type tables as pandas or polars DataFrames, including Obsidian Bases views with column selection and ordering.
- **SQL** — Query the vault via DuckDB with auto-generated join tables for link fields.
- **Knowledge Graphs** — Export as NetworkX graphs, Kuzu (Cypher), or RDF/Turtle (SPARQL).
- **Expander** — Write layer for adding records, columns, or entire types back to disk while preserving existing file content.
- **Janitor** *(planned)* — Corrections for relational inconsistencies and interlinkages, complementing Obsidian's Linter plugin.

## Install

```bash
git clone <repo-url>
cd vaults
pip install .
```

Optional extras for accessor backends:

```bash
pip install ".[db]"      # DuckDB
pip install ".[graph]"   # Kuzu
pip install ".[polars]"  # Polars DataFrames
pip install ".[all]"     # all of the above
```

## Quick start

```python
from vaults import Vault

vault = Vault.from_vault("/path/to/obsidian/vault")

# Inspect lint violations
for v in vault.violations:
    print(v)

# DataFrames (pandas)
df = vault.dfs["Personen"].to_pandas()

# SQL via DuckDB (requires duckdb)
con = vault.db.to_duckdb()
con.sql("SELECT * FROM Personen").show()

# Knowledge graph (requires kuzu)
db = vault.graph.to_kuzu()

# Add records to disk
vault.expand.add_records("Personen", [{"name": "New Person"}])
```

## Data contract

The vault must follow a specific folder structure and format. See [docs/contract/contract.MD](docs/contract/contract.MD) for the full specification.

## Code structure

See [docs/code-map.md](docs/code-map.md) for a detailed module breakdown, data flows, and cross-module dependencies.

## License

[MIT](LICENSE)
