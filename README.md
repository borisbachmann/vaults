# vaults

A Python package for data analytics on human-curated structured and relational data in Markdown format, built around [Obsidian](https://obsidian.md). The core idea: You build a future-proof knowledge base in Markdown files on your disk using established and well-maintained tools. When you need more complex analytics, `vaults` provides an interface to established data formats used in the Python ecosystem.

Files on disk stay the source of truth. `vaults` treats your markdown files as relational data. It assumes a corresponding folder structure (see the [data contract](docs/contract/CONTRACT.md)) and offers linting to help you ensure relational consistency.

Closing the analytical loop, `vaults` offers to-disk-writing capabilities for tasks like bulk-insertion of analytical results without touching existing data.

## Features

- **Parsing** — Reads Markdown frontmatter and `.base` files into typed, interlinked `Record` objects inside a `Vault` container. Formulas defined in `.base` files are compiled and evaluated at load time.
- **Linting** — Automatically detects inconsistencies in relational data (mixed link targets, dangling references, schema inhomogeneity) and reports them as navigable violations.
- **DataFrames** — Export per-type tables as pandas or polars DataFrames, Apache Arrow tables, Parquet files, or CSV, including Obsidian Bases views with column selection and ordering.
- **SQL** — Query the vault via DuckDB with auto-generated join tables for link fields.
- **Knowledge Graphs** — Export as NetworkX graphs, Kuzu (Cypher), RDF/Turtle (SPARQL), GraphML, or GEXF (Gephi).

The above don't touch files on disk and are safe to use on original data. Use the following with care:

- **Expander** — Write layer for adding records, columns, or entire types back to disk while preserving existing file content.
- **Janitor** *(planned)* — Corrections for relational inconsistencies and interlinkages, complementing Obsidian's Linter plugin.

## Install

```bash
pip install git+https://github.com/borisbachmann/vaults.git
```

Optional extras for accessor backends:

```bash
pip install "vaults[db] @ git+https://github.com/borisbachmann/vaults.git"      # DuckDB
pip install "vaults[graph] @ git+https://github.com/borisbachmann/vaults.git"   # Kuzu
pip install "vaults[polars] @ git+https://github.com/borisbachmann/vaults.git"  # Polars DataFrames
pip install "vaults[all] @ git+https://github.com/borisbachmann/vaults.git"     # all of the above
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

# Arrow / Parquet / CSV
tbl = vault.dfs["Personen"].to_arrow()          # pa.Table
vault.dfs["Personen"].to_parquet("out.parquet") # single type
vault.dfs.to_parquet("exports/")               # all types → exports/{Type}.parquet
vault.dfs["Personen"].to_csv("out.csv")

# SQL via DuckDB (requires duckdb)
con = vault.db.to_duckdb()
con.sql("SELECT * FROM Personen").show()

# Knowledge graph (requires kuzu)
db = vault.graph.to_kuzu()

# Graph export (NetworkX required; no extra deps for file formats)
vault.graph.to_graphml("vault.graphml")
vault.graph.to_gephi("vault.gexf")

# Add records to disk
vault.expand.add_records("Personen", [{"name": "New Person"}])
```

## Data contract

The vault must follow a specific folder structure and format. See [docs/contract/contract.MD](docs/contract/contract.MD) for the full specification.

## Code structure

See [docs/code-map.md](docs/code-map.md) for a detailed module breakdown, data flows, and cross-module dependencies.

## License

[MIT](LICENSE)
