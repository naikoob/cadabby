# Vault Style & Voice Guidelines

* **Voice**: Authoritative, analytical, objective, and dense. Avoid conversational filler and throat-clearing.
* **Epistemic Honesty Over Completeness**: Write only what cited `raw/` sources and linked vault notes establish. Never extrapolate benchmarks, SLAs, guarantees, or dates from model knowledge; omit unattested details or record them as open questions. User-requested background explanations must be fenced inside a `> [!WARNING] Unverified Supplement (not attested in raw/)` callout (see `AGENTS.md` §3 and `.agents/skills/librarian/SKILL.md`).
* **File Naming & Titles**:
  - **Lowercase `kebab-case` Stems**: Use singular, lowercase noun phrases separated by hyphens (`flash-attention.md`, `sqlite-vs-duckdb.md`), keeping the full display title in frontmatter `title`.
  - **MOC & Hub Suffixes**: Suffix Maps of Content (`type: moc`) in `wiki/` with `-moc.md` (`storage-moc.md`). In cognitive domains (`customers/`, `projects/`), name folder hubs `README.md` or `{topic}-moc.md` — never `index.md` (reserved for the root gap report). `README.md` is the one exception to lowercase stems, kept uppercase for Git and Markdown renderers.
  - **No Metadata Prefixes**: Never encode `type`, `status`, or dates in `wiki/` filenames (avoid `concept-sqlite.md`, `2026-10-09-sqlite.md`); those belong in frontmatter.
  - **Semantic Disambiguation**: Disambiguate colliding `wiki/` stems with a domain qualifier (`sqlite-wal.md` vs. `postgresql-wal.md`), never sequence numbers or hashes. Notes sharing a stem across domains (`wiki/architecture.md` and `projects/apollo/architecture.md`) disambiguate at the link site (`[[apollo/architecture]]`).
* **Cross-Linking**: Link concepts with `[[wikilinks]]` or `[[wikilinks|Aliases]]` (prefer wikilinks over relative Markdown links). Inside Markdown tables, escape alias pipes as `\|` (`[[wikilinks\|Aliases]]`).
* **OKF Frontmatter & Tags**: Every note across all domains requires YAML frontmatter with `type`, `title`, `description`, and `status`. Tags use lowercase kebab-case with optional `/` hierarchy (`trust/human-reviewed`) and must never contain whitespace.
* **Governance**: Consult `AGENTS.md` for cognitive domain manifests, trust tiers, and operational rules.
