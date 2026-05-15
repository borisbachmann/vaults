# Vaults Data Contract

For the construction of a Vault, the actual MD and .bases files in the original vault folder are the source of truth. However, for the Vault to function properly, certain constraints must be met. These are identified in the data contract.

Vault construction just recognizes the structures laid down in this document. Linting checks if these structures are met.

Violation of rules is handled differently per rule, as specified below. Each lintable constraint has an ID (e.g. `S-1`) that appears in the `rule` field of `LintViolation`. Where violations reference specific records, the `details` field carries structured data keyed to help the user navigate to the affected records in Obsidian.


## Path and File names

All filenames are internally normalized to NFC unicode and can exist as the filesystem handles them. As the vault is created on a specific OS, all filenames are by definition compatible. However, the system internally checks for cross-platform compatibility.

### `S-5` Cross-platform filename compatibility [warning]
Record names and type folder names must not contain characters incompatible across platforms (`<>:"/\|?*`) and must be NFC-normalized.

### Handling of violations
- If a path contains non-compatible characters: flag in linter


## Folder structure

Two folders, flat in the vault folder's root. By default called `bases` and `data`.
- `data`: Holds individual MD files -- the records, each in an individual type sub-folder, named after the type (e.g. `persons`).
- `bases`: Holds Obsidian .bases files. One file for each type folder, each named exactly as the folder (plus extension, e.g. `persons.base`). These hold different views on the data as well as formulas for computed fields not found in the records themselves.

Additional data can be kept in the vault folder at leisure, but these two folders are important for integrity.

### `S-2` Bases folder present [warning]
The `bases` folder must exist in the vault root.

### `S-1` Base file per type [error]
Each type folder in `data` must have a corresponding `.base` file in `bases`.

### `S-3` Base file without type folder [warning]
A `.base` file in `bases` must have a matching type folder in `data`.

### `S-4` Clean type folders [warning]
Type folders must contain only `.md` record files. Subdirectories and non-`.md` files are flagged.

### Handling of violations
- If the `data` folder is not found: hard error.
- If the `bases` folder is not found: flag in linter (`S-2`)
- If no type sub-folders are found in data: hard error
- If any sub-folder contains other files and folders: let fall through, flag in linter (`S-4`)
- If a base exists without a folder of the same name: let fall through, flag in linter (`S-3`)
- If a folder exists without a base of the same name: create records anyway, flag in linter (`S-1`)


## Record format

Each record is a Markdown file. The file's name becomes the record's name and identifier in the Vault. Individual properties live in the YAML front matter. Additionally, the Markdown body is read to a field called `full_text`. If at least one of the files has a non-empty Markdown body, that field is created for all records of this type.

### `R-1` Field homogeneity [warning]
All records of a type should carry the same set of fields. The linter reports any field not present across all records, with the proportion of records carrying it. A `homogeneity_threshold` parameter (default `0.0`) controls sensitivity: only fields whose minority proportion exceeds the threshold are flagged.

### Handling of violations
- If any YAML property has the name `full_text`: hard error
- If there is a property not shared across all records of a type: create property anyway, flag in linter with proportion of files carrying that property specified (`R-1`)


## Wikilinks

YAML properties may contain wikilinks. Wikilinks in properties are treated as record links between homogeneous types. That means: Each link in one property across all records of one type has to link to records of the same type, identifiable by the folder in which the target is found. Links in one property are never allowed to point to different folders.

Obsidian may disambiguate wikilinks by prepending path segments (e.g. `[[data/Personen/Anna]]` instead of `[[Personen/Anna]]`). The system resolves these by using the last two path segments (folder and record name), making it agnostic to the folder configuration.

### `R-3` Consistent link targets [warning]
All links in a single field across all records of a type must point to the same target folder. Details are structured as `{folder: [record_names]}` so the user can see which records link to each folder and navigate to the outliers in Obsidian.

### `R-4` Links within known types [warning]
Links must point to folders that exist as type folders within the canonical `data` directory. Details are structured as `{type: {record: {field: [links]}}}` to identify the specific records and links to inspect.

### Handling of violations
- If two or more links in one property point at different folders: let individual links fall through, drop them, and flag individually in linter (`R-3`)
- If a link points to a folder outside of the canonical data folders: let individual links fall through, drop them, and flag individually in linter (`R-4`)

### Handling of dangling links
If a link points to a non-existent file potentially in a flat type directory within the `data` directory, handle according to the `dangling_links` parameter specified upon Vault creation. A dangling link can point to an existing type directory, with a file missing, or to a type directory not existing at all. If `dangling_links` is:
- `drop`: let individual links fall through, drop them, and flag individually in linter.
- `stub`: create a stub record carrying the specified name and all fields, without values populated. If the type itself (i.e. the folder) does not exist, create that type, too, with a stub entry and NO fields. Flag all in linter.
- `error`: Throw a hard error.


## Formulas

Formula fields are computed fields defined in `.base` files, not stored in record frontmatter. They are translated from Obsidian Bases expression syntax and evaluated at vault load time.

### `F-1` Translatable expression [error]
Every formula field must have an expression that can be translated to a Python callable.

### `F-2` Valid field references [warning]
Formula expressions using `note.X` must reference fields that exist as non-formula fields on the same type.

### `F-3` Acyclic dependency graph [error]
Formula fields may reference other formulas via `formula.X`. The resulting dependency graph must be acyclic. Cyclic formulas cannot be evaluated and are set to `None`.

### `F-4` Consistent output type [warning]
After evaluation, a formula's output values across all records should resolve to a single consistent `FieldType`. If the inferred output type is `UNKNOWN` but non-null values exist, the formula produces inconsistent types.

### Handling of violations
- If a formula cannot be translated: flag in linter (`F-1`), formula evaluates to `None`
- If a `note.X` reference is unknown: flag in linter (`F-2`)
- If formulas form a cycle: flag in linter (`F-3`), cyclic formulas evaluate to `None`
- If output type is inconsistent: flag in linter (`F-4`)
