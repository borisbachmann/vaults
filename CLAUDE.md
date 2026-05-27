# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: Build the `vaults` Python package

A Python package that acts as an interface for data analytics on **human-curated structured and relational data in Markdown format**, centering on **Obsidian**. Files on the disk are the source of truth. The package provides to the following capabilities:

- **Parsing:** Parses `.md` and Obsidian `.bases` content to python objects of interlinked `Records` in `Vault` container.
- **Linting:** When parsing automatically detects inconsistencies in relational data, providing the user with information to ensure type safety etc. in original data.
- **Access:** Convert `Vault` contents to DataFrames (pandas and polars), Knowledge Graph (Kuzu and triplets in Turtle format) as well as in memory Database with SQL capabilities (DuckDB)
- **Expander:** Limited capabilities to expand the vault by writing to files: (1) Writing analytic results back to new properties, inserting between but not touching original MD content, (2) Writing new records to existing types, not touching existing files, (3) Creating new types (folder & connected `.base` file). Tightly scoped to ensure original data integrity. Use on backed-up files.
- **Janitor:** Capabilities to correct inconsistencies in relational data and interlinkages, as a bonus to Obsidian's `Linter` plugin which focusess on individual notes.

The goal of the project is to build that package and ensure clean architecture and code. Far goal is eventual deployment as a pip-installabale package.

## Directory layout

- `vaults/` — the Python package source (`Schema` and `Vault` classes)
- `data/` — real-world data used for developing and testing (gitignored)
- `docs/` — public documentation (data contract, code map)
- `archive/` — legacy scripts, config, and build instructions (gitignored)
- `tests/` — tests

## Package structure

- `vault.py` – `Vault` class as main interface to hold `Records` and provides interface to access, expander and janitor capabilities.
- `expander` - Write layer for expanding the vault: add records, columns, or entire types. `operations.py` holds the `Expander` class, `io.py` handles file I/O, `adapters.py` normalizes inputs.
- `record.py` – Basic `Record` class
- `schema.py`– `Schema` class that describes data structure and related classes and helpers
- `linter.py` – Functionality to test and report data integrity
- `links.py`- Handling of Wikilinks in MD files as well as backlinks and link-driven lookups
- `syntax`- Parsing of Obsidian `.bases` syntax for filtering, grouping, sorting and formula fields. `compiler.py` holds a self-contained `BaseCompiler` class that is reusable outside this package. `grammar.py` contains a Lark grammar constructed with the help of Claude Opus.
- `accessors` - Provides access via pandas and polars DataFrame output, DuckDB connection or KGs (Kuzu for Cypher and export to Turtle files for SPARQL)

See `docs/code-map.md` for detailed per-module breakdown: classes, functions, data flow, cross-module dependencies, dataclass fields, and test coverage.

## Development process

The user drives the build process step by step and orchestrates what gets built. Implement only what is explicitly asked for in the current step. Do not propose full plans, suggest next steps, or implement anything beyond the current request. The user tests each piece in Jupyter before moving on.

Always create and run tests. Vault fixtures in `tests/fixtures`.

Always create docstrings according to the below `Docstrings` rules.

## Git hygiene

If not prompted explicitly, the standard is: the user handles git. If you are tasked, the following 
rules apply:

Never commit files that may contain personal information or local environment details. This includes (but is not limited to):
- `.claude/settings.local.json` — may contain absolute paths
- Any file with hardcoded absolute paths, usernames, or machine-specific config
- API credentials or tokens of any kind

When in doubt, add the file to `.gitignore` rather than committing it.

## Docstrings
Follow NumPy docstring style for all Python documentation.

### How to add docstrings to an existing codebase
Work module by module, not package-wide. Process one file at a time, review the output before moving on, and only proceed once the current module looks correct.

### When to write docstrings
- All public functions, methods, and classes
- Private functions that contain non-obvious logic or assumptions
- Skip trivial property accessors and __repr__/__str__ unless behaviour is non-standard

### What to document
Focus on information that is not apparent from the code itself:
- The purpose and intent — not a restatement of the function name
- Non-obvious parameter constraints (e.g. expected shape, dtype, value range)
- Assumptions and preconditions
- Known edge cases or failure modes
- References to papers, algorithms, or external resources where applicable

Do not write docstrings that merely restate what the signature already communicates.

### Format
```python
def function(param1: np.ndarray, param2: float) -> np.ndarray:
    """
    Short one-line summary (imperative mood, no period).

    Extended description if needed. Explain intent, assumptions,
    or relevant theoretical background here.

    Parameters
    ----------
    param1 : np.ndarray of shape (n_samples, n_features)
        Description including expected dtype or value range if relevant.
    param2 : float
        Description. Note valid range or constraints if non-obvious.

    Returns
    -------
    np.ndarray of shape (n_samples,)
        Description of what is returned and what it represents.

    Raises
    ------
    ValueError
        When and why this is raised.

    Notes
    -----
    Relevant implementation notes, algorithm references, or caveats.
    For research code, cite papers here:
    "Based on the method described in Smith et al. (2021) [1]_."

    References
    ----------
    .. [1] Smith, J. et al. (2021). "Paper Title". Journal, vol(issue), pp.

    Examples
    --------
    >>> result = function(X, 0.5)
    """
```

## Data contract

The Vault operates on markdown files organized along type-exclusive directories within a common folder in the file system's vault root (`/data/` by default). Corresponding Obsidian `.base` files have the same name as the typed folders and also sit in a common folder (`/bases/` by default). See `docs/contract/contract.md` for details.
