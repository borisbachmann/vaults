# Obsidian Bases — Functions & Formulas Reference

*Source: DeepWiki / obsidianmd/obsidian-help, sourced from commit 5fb785ac*

---

## Formula Properties

### Property Reference Syntax

| Property Type | Syntax | Example |
|---|---|---|
| Note properties | `note.property` or `property` | `note.price` or `price` |
| File properties | `file.property` | `file.name`, `file.mtime` |
| Formula properties | `formula.property_name` | `formula.price_per_unit` |

Note properties can omit the `note.` prefix. Formula properties must use the `formula.` prefix.

### Storage Format

Formulas stored in `.base` YAML under `formulas:`. Values always stored as strings; output type determined by expression return value.

---

## Global Functions

### Type Conversion

| Function | Signature | Description |
|---|---|---|
| `date()` | `date(date: string): date` | Parse string to date. Format: `YYYY-MM-DD HH:mm:ss` |
| `number()` | `number(input: any): number` | Convert to number. Dates→ms, booleans→1/0 |
| `list()` | `list(element: any): List` | Wrap element in list if not already a list |
| `file()` | `file(path: string | file | url): file` | Return file object for path |
| `link()` | `link(path: string | file, display?: value): Link` | Create Link object |

### Conditional and Logic

| Function | Signature | Description |
|---|---|---|
| `if()` | `if(condition: any, trueResult: any, falseResult?: any): any` | Conditional |
| `max()` | `max(value1: number, value2: number...): number` | Largest value |
| `min()` | `min(value1: number, value2: number...): number` | Smallest value |

### Date and Time

| Function | Signature | Description |
|---|---|---|
| `now()` | `now(): date` | Current date+time |
| `today()` | `today(): date` | Current date, time zeroed |
| `duration()` | `duration(value: string): duration` | Parse string as duration |

Duration strings: `"1d"`, `"2w"`, `"3M"`, `"4h"`. Explicit parsing required for arithmetic: `duration('5h') * 2`.

### Display Functions (no-op in Python context)

| Function | Description |
|---|---|
| `image()` | Render image in view |
| `icon()` | Lucide icon |
| `html()` | HTML snippet |
| `escapeHTML()` | Escape HTML characters |

---

## Type-Specific Functions (dot notation)

### Any

| Function | Signature | Description |
|---|---|---|
| `isTruthy()` | `any.isTruthy(): boolean` | Coerce to boolean |
| `isType()` | `any.isType(type: string): boolean` | Type check |
| `toString()` | `any.toString(): string` | String representation |

### Date

**Fields:** `date.year`, `date.month`, `date.day`, `date.hour`, `date.minute`, `date.second`, `date.millisecond`

**Methods:**

| Function | Signature | Description |
|---|---|---|
| `format()` | `date.format(format: string): string` | Moment.js format string |
| `relative()` | `date.relative(): string` | Human-readable (e.g. "3 days ago") |
| `date()` | `date.date(): date` | Date with time removed |
| `time()` | `date.time(): string` | Time as string |

### String

**Field:** `string.length`

| Function | Signature | Description |
|---|---|---|
| `contains()` | `string.contains(value: string): boolean` | Substring check |
| `replace()` | `string.replace(pattern: string \| Regexp, replacement: string): string` | Replace |
| `split()` | `string.split(separator: string \| Regexp, n?: number): list` | Split to list |
| `slice()` | `string.slice(start: number, end?: number): string` | Substring |
| `lower()` | `string.lower(): string` | Lowercase |
| `title()` | `string.title(): string` | Title case |
| `trim()` | `string.trim(): string` | Strip whitespace |
| `startsWith()` | `string.startsWith(value: string): boolean` | Prefix check |
| `endsWith()` | `string.endsWith(value: string): boolean` | Suffix check |

### Number

| Function | Signature | Description |
|---|---|---|
| `abs()` | `number.abs(): number` | Absolute value |
| `round()` | `number.round(digits?: number): number` | Round |
| `ceil()` | `number.ceil(): number` | Round up |
| `floor()` | `number.floor(): number` | Round down |
| `toFixed()` | `number.toFixed(precision: number): string` | Fixed-point string |

### List

**Field:** `list.length`

| Function | Signature | Description |
|---|---|---|
| `filter()` | `list.filter(value: Boolean): list` | Filter using implicit `value`, `index` |
| `map()` | `list.map(value: Any): list` | Transform using implicit `value`, `index` |
| `reduce()` | `list.reduce(expression: Any, acc: Any): Any` | Reduce using implicit `value`, `index`, `acc` |
| `sort()` | `list.sort(): list` | Sort ascending |
| `unique()` | `list.unique(): list` | Remove duplicates |
| `join()` | `list.join(separator: string): string` | Join to string |
| `flat()` | `list.flat(): list` | Flatten nested lists |
| `slice()` | `list.slice(start: number, end?: number): list` | Sublist |
| `contains()` | `list.contains(value: any): boolean` | Membership check |

**Examples:**
- `[1,2,3,4].filter(value > 2)` → `[3,4]`
- `[1,2,3,4].map(value + 1)` → `[2,3,4,5]`
- `[1,2,3].reduce(acc + value, 0)` → `6`

### File

**Fields:**

| Field | Type | Description |
|---|---|---|
| `file.name` | string | File name with extension |
| `file.basename` | string | File name without extension |
| `file.path` | string | Full path from vault root |
| `file.folder` | string | Parent folder path |
| `file.ext` | string | Extension |
| `file.size` | number | Bytes |
| `file.properties` | object | All frontmatter properties (does not auto-refresh) |
| `file.tags` | list | All tags including inline |
| `file.links` | list | All internal links including frontmatter |
| `file.embeds` | list | All embeds |
| `file.backlinks` | list | Backlink files (performance heavy; prefer `file.links`) |
| `file.ctime` | date | Creation timestamp |
| `file.mtime` | date | Last modified timestamp |
| `file.file` | file | File object for use in functions |

**Methods:**

| Function | Signature | Description |
|---|---|---|
| `asLink()` | `file.asLink(display?: string): Link` | Link object for this file |
| `hasLink()` | `file.hasLink(otherFile: file \| string): boolean` | Does file link to other? |
| `hasProperty()` | `file.hasProperty(name: string): boolean` | Has frontmatter property? |
| `hasTag()` | `file.hasTag(...values: string): boolean` | Has any of the tags? |
| `inFolder()` | `file.inFolder(folder: string): boolean` | In folder or subfolder? |

### Link

| Function | Signature | Description |
|---|---|---|
| `asFile()` | `link.asFile(): file` | File object if link resolves locally |
| `linksTo()` | `link.linksTo(file): boolean` | Does linked file link to given file? |

### Object

| Function | Signature | Description |
|---|---|---|
| `isEmpty()` | `object.isEmpty(): boolean` | No own properties? |
| `keys()` | `object.keys(): list` | List of keys |
| `values()` | `object.values(): list` | List of values |

### Regex

| Function | Signature | Description |
|---|---|---|
| `matches()` | `regexp.matches(value: string): boolean` | Does regex match string? |

**Example:** `/abc/.matches("abcde")` → `true`

---

## Operators

### Arithmetic
`+`, `-`, `*`, `/`, `%`, `( )`

### Comparison
`==`, `!=`, `>`, `<`, `>=`, `<=`

### Boolean
`!`, `&&`, `||`

### Date Arithmetic

Dates modified with `+` and `-`:

| Unit | Formats |
|---|---|
| Year | `y`, `year`, `years` |
| Month | `M`, `month`, `months` |
| Week | `w`, `week`, `weeks` |
| Day | `d`, `day`, `days` |
| Hour | `h`, `hour`, `hours` |
| Minute | `m`, `minute`, `minutes` |
| Second | `s`, `second`, `seconds` |

**Examples:**
- `now() + "1 day"`
- `file.mtime > now() - "1 week"`
- `date("2024-12-01") + "1M" + "4h"`
- `now() - file.ctime` → milliseconds

Duration arithmetic: duration must be on left: `duration('5h') * 2` not `2 * duration('5h')`.

---

## Data Types

### Primitives
- **String:** `"hello"` or `'world'`
- **Number:** `42`, `3.14`
- **Boolean:** `true`, `false`

### Date / Duration
- `date("2025-01-01 12:00:00")` — fields: `.year`, `.month`, `.day`, `.hour`, `.minute`, `.second`, `.millisecond`
- Duration string format: `"1d"`, `"2w"`, `"3M"` — use `duration()` for arithmetic

### Collections
- **List:** `[1, 2, 3]` or `list(x)` — zero-based index access: `property[0]`
- **Object:** `{"a": 1, "b": 2}` — access: `property.subprop` or `property["subprop"]`

### File and Link
- **File:** `file("path/to/note.md")` or via `file` keyword
- **Link:** wikilinks in frontmatter auto-recognized; `link("filename", "display text")`; comparable with `==` and `!=`; links comparable to files: `author == this`

---

## `this` Context

| Context | `this` refers to |
|---|---|
| Main content area | The base file itself |
| Embedded in note | The embedding file |
| Sidebar | Active file in main content area |

For vaults where bases always live in `bases/` folder (never embedded), `this` is always the base file itself — statically resolvable from `base_path`.

---

## Filter Conjunction Operators

Used in `.base` YAML filter blocks:
- `and` — all conditions true
- `or` — any condition true
- `not` — no conditions true

---

## Summary Formulas

Operate on `values` variable — a list of all property values across the result set. Defined in `.base` under `summaries:`.

Built-in summary functions:
- **Numbers:** Average, Min, Max, Sum, Range, Median, Stddev
- **Dates:** Earliest, Latest, Range
- **Booleans:** Checked, Unchecked
- **All types:** Empty, Filled, Unique

---

## Syntax Notes (post v1.9.2 breaking change)

- Functions are object-oriented: `file.name.contains("Books")` not `contains(file.name, "Books")`
- Functions can be chained: `property.split(' ').sort()[0].lower()`
- Property names with spaces: `note["Property Name"]` not backtick syntax
- Date arithmetic simplified: `date("01/01/2025") + "1 year"` not `dateModify()`
- Comparison operators replace date functions: `date1 < date2` not `dateBefore(date1, date2)`
