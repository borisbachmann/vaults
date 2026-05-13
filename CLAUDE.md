# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: Build the `vaults` Python package

A Python package centered on two classes — `Schema` and `Vault` — that treat an Obsidian vault as a serialization format for structured, relationally linked research data. See `docs/vault-class-instructions.md` for the full design spec.

## Directory layout

- `vaults/` — the Python package source (`Schema` and `Vault` classes)
- `data/nsp_vault/` — the test vault (primary test fixture; gitignored)
- `data/` — API credentials for Baserow (gitignored)
- `docs/` — design documentation
- `archive/` — legacy scripts and config

## Development process

The user drives the build process step by step and orchestrates what gets built. Implement only what is explicitly asked for in the current step. Do not propose full plans, suggest next steps, or implement anything beyond the current request. The user tests each piece in Jupyter before moving on.

## Git hygiene

Never commit files that may contain personal information or local environment details. This includes (but is not limited to):
- `.claude/settings.local.json` — may contain absolute paths
- Any file with hardcoded absolute paths, usernames, or machine-specific config
- API credentials or tokens of any kind

When in doubt, add the file to `.gitignore` rather than committing it.
