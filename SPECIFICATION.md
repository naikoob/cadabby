# Cadabby: Technical Specification

**Version:** 0.5.0  
**Status:** Approved Architecture (Multi-Domain Extended)  
**Author:** Pair programmed with Antigravity  
**Target Runtime:** CPython >= 3.11, standard library only

---

## 0. Reading This Document

This specification is ~27k tokens. Read the section you need, not the file.

Every `§` citation in this document resolves to a real heading — `test_every_section_reference_resolves`
fails the suite otherwise — so a section number found in code comments, `AGENTS.md`, or a test docstring is
a safe address. Extract one with:

```bash
awk '/^## 4\./,/^## 5\./' SPECIFICATION.md     # one top-level section
grep -n '^#\{2,4\} ' SPECIFICATION.md          # all headings with line numbers
```

| § | Section | Answers |
| :--- | :--- | :--- |
| 1 | Executive Summary & System Vision | What Cadabby is for and what it refuses to be |
| 2 | Vault Architecture & Directory Conventions | Layer layout, flat `wiki/`, cognitive domains, `index.md` as gap report, vault resolution |
| 3 | Epistemic Trust Tiers & OKF Frontmatter | The restricted YAML subset, body hashing, the four tiers, git provenance audit |
| 4 | SQLite Ephemeral Cache & Search Engine | Schema, dual contentless-external FTS5 tables, retraction invariant, BM25 epistemic ranking |
| 5 | Cadabby Tool Surface (CLI & MCP) | The 8-tool invariant, command surface, error taxonomy and exit codes |
| 6 | Core Operational Workflows | Ingestion, synthesis, verification, and the six lint gates end to end |
| 7 | Multi-Harness Consumption Architecture | Single-definition personas, plugin assembly, shims, templates, `init`/`install` ownership |
| 8 | Durability & Concurrency | Atomic replace, advisory locking, append-only ledger writes |
| 9 | Codebase Packaging & Repository Structure | The authoritative file tree and the ports/adapters boundary |
| 10 | Acceptance Criteria | Falsifiable observations, each traced to the test that checks it |

---

## 1. Executive Summary & System Vision

**Cadabby** is a zero-dependency, pure Python standard library engine (CLI + Model Context Protocol server) designed to power personal and shared **LLM Wiki vaults** optimized for seamless co-working between humans and autonomous AI agents.

It unifies four foundational paradigms:

1. **Andrej Karpathy's LLM Wiki Pattern**: A persistent, compounding knowledge base where the human curates raw, immutable sources and directs queries, while the AI agent acts as the primary "programmer" who compiles, updates, cross-references, and maintains the wiki markdown codebase.
2. **Open Knowledge Format (OKF v0.2)**: A rigorous epistemic trust hierarchy (`human-reviewed` > `machine-confirmed` > `stale-verified` > `unverified`) that resists hallucination compounding, enforces provenance, and keeps attribution auditable without suffocating agent autonomy.
3. **Self-Describing, Self-Governing Cognitive Domains**: Beyond the canonical `wiki/`, vaults can host arbitrary human-oriented and cognitive domains (e.g. `customers/`, `projects/`, `research/`), each governed by a local `{domain}/AGENTS.md` manifest defining note types, layout rules, source requirements, and prompt-injected agent directives.
4. **Multi-Harness Consumption**: First-class support for **Google Antigravity** (via native MCP tool provider, MCP resources, skills, and slash commands) and **Claude Code** (via `.mcp.json`, `CLAUDE.md`, and `.claude/commands/`), anchored by a single canonical constitution in `AGENTS.md` and localized domain directives.

### Key Tenets

* **Strict Zero Dependencies**: Requires only Python >= 3.11 and the standard library (`sqlite3`, `json`, `hashlib`, `re`, `argparse`, `pathlib`, `subprocess`). Eliminates virtualenv conflicts across heterogeneous agent subprocess environments.
* **Decoupled Engine**: Operates against any compliant Markdown vault directory; not bound to a single vault repository.
* **Disposable SQLite Cache**: Markdown files on disk are the single source of truth. The SQLite database (`.cadabby/cache.db`) is an ephemeral, disposable cache for incremental synchronization, multi-domain FTS5 BM25 search with epistemic boosting, and wikilink graph traversal. Deleting it is always safe.
* **Content-Bound Verification**: A verification record attests to *specific content*, not to a filename. Editing a note's body invalidates prior verifications automatically (§3.4). This is the mechanism that makes the trust hierarchy load-bearing rather than decorative.
* **Cognitive Domain Boundaries & Agent Autonomy**: Autonomous agents are provided both global vault principles (in `AGENTS.md`) and scoped behavioral constraints (in `{domain}/AGENTS.md`) injected during note grounding and surfaced via standard MCP Resources.
* **Auditability over Enforcement**: Cadabby does not claim to make forged human endorsements *impossible* — any agent with filesystem access can write arbitrary bytes. It makes them **unlikely by accident and detectable after the fact**: the engine refuses to stamp `human:*` over MCP, every write is atomic and Git-reversible, and `cadabby audit` cross-checks claimed human endorsements against Git authorship (§3.5).
* **Strict 8-Tool MCP Invariant**: Exactly 8 core tools are exposed to models, preventing cognitive tool degradation (§5.1). Domain policies are exposed via native MCP Resources (`resources/list`, `resources/read`), never by adding bloat tools.
* **Universal Portability**: A vault initialized with `cadabby init` works out of the box in Antigravity, Claude Code, Cursor, Zed, Codex, or any MCP-compatible environment. Portability is delivered by two files (`.mcp.json` and `AGENTS.md`); everything else in §7 is convenience on top.

```mermaid
graph TD
    subgraph Human Interface
        H[Human User] -->|Browses & Reads| OBS[Obsidian / Markdown Editor]
        H -->|Curates & Drops| RAW[raw/ Sources]
        H -->|Directs / CLI| CAD_CLI[cadabby CLI]
    end

    subgraph Multi-Harness Layer
        AGY[Google Antigravity<br>plugin: cadabby] -->|stdio JSON-RPC| CAD_MCP[cadabby MCP Server]
        CC[Claude Code<br>.mcp.json + CLAUDE.md] -->|stdio JSON-RPC| CAD_MCP
        CUR[Cursor / Zed / Codex<br>.mcp.json + AGENTS.md] -->|stdio JSON-RPC| CAD_MCP
    end

    subgraph Engine Core [Cadabby Engine - Pure Python Stdlib]
        CAD_CLI --- CAD_CORE[Cadabby Core Engine]
        CAD_MCP --- CAD_CORE
        CAD_CORE -->|implicit incremental scan| CACHE[(Ephemeral SQLite Cache<br>Multi-Domain FTS5 + Link Graph)]
        CAD_CORE -->|blame / log| GIT[(Git History<br>provenance audit)]
    end

    subgraph The Vault Filesystem
        CAD_CORE -->|Reads Immutable| RAW
        CAD_CORE -->|Scaffolds & Updates| WIKI[wiki/ Canonical Knowledge]
        CAD_CORE -->|Scaffolds & Updates| DOMAINS[customers/, projects/, ...<br>Cognitive Domains]
        CAD_CORE -->|Auto-generates| IDX[index.md Gap Report]
        CAD_CORE -->|Appends| LOG[log.md Audit Trail]
        WIKI -->|Renders In| OBS
        DOMAINS -->|Renders In| OBS
        IDX -->|Navigates In| OBS
    end
```

---

## 2. Vault Architecture & Directory Conventions

A Cadabby vault adheres to a multi-domain architecture combining immutable evidence, canonical synthesis, and domain-scoped cognitive folders:

```text
my-vault/
├── .cadabby.json             # Vault configuration & root marker
├── .gitignore                # Ignores .cadabby/ and .obsidian/workspace*.json
├── .mcp.json                 # Standard MCP registration (auto-detected by harnesses)
├── AGENTS.md                 # Single canonical constitution & global agent directives
├── CLAUDE.md                 # Non-normative pointer delegating to AGENTS.md
├── GEMINI.md                 # Non-normative pointer delegating to AGENTS.md
├── STYLE.md                  # Voice, formatting, and prose standards
├── index.md                  # Auto-generated gap report: entry points, unfiled notes, raw queue
├── log.md                    # Append-only chronological activity ledger (active year)
│
├── log/                      # Rotated ledgers
│   └── 2025.md
│
├── .cadabby/                 # Disposable cache; gitignored (4)
│   └── cache.db
│
├── .agents/
│   ├── skills/               # CANONICAL persona runbooks (7.1)
│   │   ├── librarian/SKILL.md    # Ingestion, synthesis, cross-referencing
│   │   └── technician/SKILL.md   # Cache health, linting, pre-commit maintenance
│   └── plugins/cadabby/      # Antigravity plugin, copied in by init (7.2)
│       ├── plugin.json
│       ├── mcp_config.json
│       ├── skills/cadabby-wiki/SKILL.md
│       └── agents/           # Thin manifests -> .agents/skills/<persona>/
│           ├── librarian/agent.md
│           └── technician/agent.md
│
├── .claude/
│   └── commands/             # Thin slash-command shims (7.3)
│       ├── ingest.md
│       ├── vault-status.md
│       └── vault-lint.md
│
├── .obsidian/                # Optional: written only by init --obsidian (7.5)
│   ├── app.json              # Routes pasted attachments to raw/attachments/
│   └── templates.json        # Only with --obsidian-templates; names templates/ (7.5)
│
├── templates/                # Optional: pre-notes, excluded from discovery (7.5)
│   ├── Concept.md
│   └── MOC.md
│
├── raw/                      # LAYER 1: Immutable Ground Truth
│   ├── attachments/          # Obsidian paste/drop target, so media lands in Layer 1 (7.5)
│   ├── 2026-paper-kv.pdf
│   ├── web-clip-article.md
│   └── audio-transcript.txt
│
├── wiki/                     # LAYER 2: Canonical Flat Wiki & Maps of Content (LLM Maintained)
│   ├── storage-moc.md        # Maps of Content (MOC) hub notes
│   ├── ai-systems-moc.md
│   ├── epistemic-trust-tiers.md
│   ├── flash-attention.md
│   ├── sqlite.md
│   ├── anthropic.md
│   └── sqlite-vs-duckdb.md
│
├── customers/                # COGNITIVE DOMAIN: Self-governing customer accounts
│   ├── AGENTS.md             # Domain manifest & localized AI agent directives
│   └── acme-corp/
│       ├── README.md         # Folder hub by domain convention — not engine-generated (§2.5)
│       └── q3-review.md
│
├── projects/                 # COGNITIVE DOMAIN: Self-governing engineering projects
│   ├── AGENTS.md             # Domain manifest & localized AI agent directives
│   └── apollo/
│       └── README.md
│
└── archive/                  # COGNITIVE DOMAIN: Conventional retirement domain (0.5x rank boost)
    └── cs244b/
        └── README.md
```

### 2.1. Layer & Domain Responsibilities

| Layer / File | Primary Owner | Mutability | Description |
| :--- | :--- | :--- | :--- |
| **`raw/`** | Human | **Immutable** | Raw articles, web clips, PDFs, interview transcripts. Agents read but never modify. |
| **`wiki/`** | AI Agent | **Mutable** | Canonical flat knowledge synthesis: flat wiki notes (`wiki/*.md`) whose topology is governed by Maps of Content (`type: moc`) rather than folders (§2.5), with open note typing. Compiled and maintained by LLM. |
| **`{domain}/`** (e.g. `customers/`, `projects/`) | Human / Agent | **Mutable** | Self-governing cognitive domains hosting human-oriented or operational notes. Supports arbitrary nested subdirectories. |
| **`archive/`** | Human / Agent | **Mutable** | Conventional retirement domain for completed courses, churned accounts, superseded projects, or retired notes. Preserves frontmatter and attestations intact, compounds a `0.5×` domain rank multiplier (§4.4), yields bare-stem link resolution to active domains (§4.4), and is excluded from `index.md` worklists (§2.5). |
| **`index.md`** | Engine | **Generated** | Vault-wide *gap report*: MOC entry points, wiki notes filed under no MOC, the raw source processing queue, and verification debt (§2.5). Not a catalog of every note. Never hand-edited. |
| **`log.md`** | Engine / Agent | **Append-Only** | Chronological record of ingests, queries, lint runs, and major syntheses. |
| **`AGENTS.md`** | Human / Agent | **Collaborative** | The global normative constitution for all AI agents working on this vault. |
| **`{domain}/AGENTS.md`** | Human / Domain Lead | **Normative** | Domain manifest (YAML schema constraints) and localized markdown agent directives injected during grounding (§2.2). |
| **`.agents/skills/`** | Human / Agent | **Collaborative** | Canonical persona runbooks. The only place persona behavior is defined (§7.1). |
| **`CLAUDE.md`**, **`GEMINI.md`** | Human | **Write-once** | Thin non-normative pointers to `AGENTS.md`. Seeded by `init` and thereafter user-owned (§7.4): no command rewrites them, because users customize them heavily. `init --force` is the sole exception and announces itself (§7.3). |

In `wiki/`, all notes reside directly in `wiki/*.md` with zero subdirectories. Knowledge topology and thematic clusters are governed by Maps of Content (`type: moc`) rather than physical folder hierarchies. Subdirectories within `wiki/` are strictly disallowed (`WIKI_NESTING_DISALLOWED`). In custom cognitive domains (`customers/`, `projects/`, `archive/`), nested subdirectories are fully supported (§2.2).

### 2.2. Self-Describing Cognitive Domains (`{domain}/AGENTS.md`)

Cadabby treats any non-reserved top-level directory in the vault as an independent **Cognitive Domain**. Reserved directories (`raw/`, `log/`, `.cadabby/`, `.obsidian/`, `.git/`, `.agents/`, `.claude/`) and standard tool ignore folders (`DEFAULT_IGNORED_DIRS`: `.venv/`, `node_modules/`, `target/`, `dist/`, etc.) are excluded automatically.

One further directory is excluded, and only when the user has declared it: the folder named by Obsidian's core Templates plugin, which holds pre-notes rather than notes (§7.5). The exclusion applies wherever that folder sits — a top-level one never becomes a domain, and a nested one such as `meta/templates/` is pruned from its parent domain's walk while the parent remains a domain in full.

A domain defines its governance rules and AI operational constraints through a local `{domain}/AGENTS.md` file:

```yaml
---
domain: customers
description: "Enterprise customer accounts, contacts, and contract histories"
allowed_types:
  - account
  - contact
  - meeting
require_sources: true
---
# Customer Domain Agent Instructions

All customer profiles must maintain enterprise confidentiality.
- Ground every account fact against primary signed contracts in `raw/`.
- Architectural design decisions must wikilink to canonical concepts in `[[wiki/...]]`.
```

#### Governance Schema Fields
* **`domain`** *(string, optional)*: Explicit domain identifier (defaults to directory name).
* **`description`** *(string, optional)*: High-level purpose of the cognitive domain.
* **`allowed_types`** *(list of strings, optional)*: If specified, `lint` gate 1 and `scaffold` strictly validate that notes in this domain use one of these types. If omitted (`None`), the domain is permissive and accepts any valid string type.
* **`require_sources`** *(boolean, default: `false`)*: If `true`, `lint` gate 4 treats missing or empty `sources:` as a validation error.

#### Fallback Behavior
* **Unconfigured Domains**: A top-level directory lacking `AGENTS.md` is discovered as a permissive cognitive domain (`allowed_types: None`, `require_sources: false`, empty directives). Custom domains support arbitrary nested subdirectories.
* **Canonical `wiki/` Domain**: If `wiki/AGENTS.md` is not present, `wiki/` automatically defaults to canonical governance: flat layout governed by Maps of Content (`type: moc`), open typing (`allowed_types: None`), `require_sources: false`.

#### Search Inclusion Is Structural, Not Configurable

**Every discovered domain is searchable, and there is no per-domain opt-out.** Exclusion from the index happens before a directory ever becomes a domain: reserved names (`raw/`, `log/`), dot-prefixed directories, and `DEFAULT_IGNORED_DIRS`. The scan walks only `raw/` and discovered domain paths, so root-level files (`index.md`, `log.md`, `AGENTS.md`, `STYLE.md`) are never indexed either. By the time a directory is a domain, it is markdown knowledge the user deliberately placed in the vault.

A per-domain `searchable: false` switch was specified in earlier drafts and deliberately removed, because a boolean is the wrong instrument twice over:

* **It makes search lie by omission.** An agent querying a term held only in a hidden domain gets zero results, concludes the knowledge does not exist, and scaffolds a duplicate note. Converting "I don't know" into "it isn't there" is strictly worse than returning a low-ranked match — the same failure mode as a stale ingestion queue (§4.3).
* **It is the wrong granularity.** Noise is a property of a note or lifecycle stage, not a binary visibility switch: `deprecated`/`abandoned` status carries a `0.4×` multiplier, and `ranking.domain` (defaulting to `{"archive": 0.5}`) weights retired domains alongside trust and status (§4.4), deprioritizing without hiding.

Callers that want a narrower result set pass `domain` to `vault_search` (§5.1), which puts the exclusion at the call site where the caller can see what they are leaving out. It is never a hidden property of the vault.

### 2.3. Generalized Content Identifiers (CIDs)

Cadabby uses canonical CIDs to uniquely reference notes across all cognitive domains:

* **Cognitive Domain Notes**: `rel_path` without file extension $\rightarrow$ `{domain}/{relative_stem}`.
  - `wiki/epistemic-trust-tiers.md` $\rightarrow$ `wiki/epistemic-trust-tiers`
  - `customers/acme-corp/README.md` $\rightarrow$ `customers/acme-corp/README`
  - `projects/apollo/rfc-001.md` $\rightarrow$ `projects/apollo/rfc-001`
* **Raw Sources**: Retain their filename and extension verbatim.
  - `raw/2026-paper-kv.pdf` $\rightarrow$ `raw/2026-paper-kv.pdf`
  - `raw/karpathy-llm-wiki-gist.md` $\rightarrow$ `raw/karpathy-llm-wiki-gist.md`

### 2.4. Raw Source Semantics

A file in `raw/` is **processed** if and only if at least one note in any cognitive domain lists it in `sources:`. This is derived, never stored — there is no ingestion state to drift out of sync, and `lint` gate 4 (§6.3) is its exact inverse.

**Cadabby does not extract text from binary sources.** A zero-dependency engine cannot read PDFs, audio, or images. Cadabby indexes the full text of every file whose extension appears in `raw_text_extensions` (§2.6); for every other file it indexes the filename and records existence, size, and provenance edges only. Extracting meaning from `raw/paper.pdf` is the **agent's** responsibility using its own file-reading tools — Cadabby tracks that the edge exists and that the file has not changed.

`raw_text_extensions` **replaces** the default list rather than extending it, which is what lets a vault exclude an extension it does not want indexed — a directory of large generated `.csv` files, for instance. The shipped `.cadabby.json` therefore lists all five defaults explicitly, so that editing the key shows what is being replaced.

**Raw sources are searchable, but not from the default corpus.** `vault_search` omits the raw layer unless `domain: "raw"` is passed. Two distinct reasons, and the second is the binding one:

* Curated notes are the answer to most questions; unprocessed sources would otherwise crowd them out.
* **BM25 scores a document against its corpus.** If raw sources shared the curated index, their term statistics would enter the scoring of every wiki query — thirty transcripts repeating a term drive its inverse document frequency to zero, and a note that scored 1.29 on that term scores 0.00 instead. Because the trust and status multipliers (§4.4) are applied *to* the BM25 score, a flattened score silently disables epistemic ranking: a `human-reviewed` note's 2.0× boost multiplies approximately nothing. A `WHERE` filter does not prevent this, because filtering happens after scoring. The two corpora are therefore held in two FTS5 tables (§4.2), which is the only arrangement under which each set of statistics describes the documents it actually ranks.

### 2.5. Navigation Topology: Maps of Content & `index.md`

Three mechanisms could organize a vault — physical folders, curated hub notes, and generated catalogs. Cadabby gives each exactly one job so that none of them duplicates another.

**`wiki/` topology is editorial, and lives in MOCs.** Because `wiki/` is flat by mandate (§2.1), thematic structure has to live in content rather than in paths. A Map of Content (`type: moc`) is a curated hub asserting a *judgment* — these notes belong together, in this order, for this reason. Nothing derives a MOC; that is the point of one. MOCs are ordinary notes, created and maintained with the same tools as any other note.

**`index.md` is a gap report, not a catalog.** An auto-generated table of every wiki note duplicates three things at once: what MOCs curate deliberately, what a file explorer already shows for a flat folder, and what `vault_search` ranks better — while churning the Git diff on every note added. `index.md` therefore enumerates only what nothing else surfaces:

1. **Entry points** — every `type: moc` note, linked. The human's starting page.
2. **Unfiled notes** — wiki notes reachable from no MOC by forward link at any depth. This is the one failure mode flat layout introduces: a note is born un-MOC'd and is thereafter findable only by search. `lint` gate 5 does *not* catch it — a note that links outward has outbound edges and so is not an orphan (§6.3).
3. **Raw sources** — each `raw/` file that no note cites yet. "Processed" is derived and never stored (§2.4), so this is the only place a human browsing the vault sees the ingestion queue. Processed sources are omitted rather than listed with a status, for the same reason the whole report omits filed notes: a source that has been ingested is not work.
4. **Verification debt** — notes whose verification entries have all gone stale (§3.4).

Notes are linked as `[[stem]]`, except a stem a wikilink cannot carry (`#`, `|`, `]`, `^`, `[` — possible in a hand-named file), which is linked by Markdown path (`[C#](wiki/C%23.md)`) so the row still opens the note.

Each section is omitted when empty. Section 1 is navigation and persists in a healthy vault — losing your starting page precisely when the vault is in good order would be perverse. Sections 2 through 4 are the gap report proper, and every row in them is work someone still has to do; shrinking them to nothing is the objective, and a vault that gets there renders entry points and a line saying so.

Two scoping rules follow from that split. Entry points and unfiled notes are **`wiki/`-only**, because MOCs are a flat-namespace construct and domains carry their topology in folders. The raw queue and verification debt are **vault-wide**, because any note in any domain may cite `raw/` (§2.4) or carry verification entries (§3.4). Reachability for section 2 is transitive and may travel through any layer — a wiki note pulled in via a domain note is filed — but only `wiki/` notes are reported. Notes in `archive/` (`layer = 'archive'`) and notes with `status: deprecated` or `abandoned` are excluded from both section 2 and section 4: filing or re-verifying a retired note is not active work, and counting it would give the report a floor it could never reach zero from.

**Cognitive domains generate nothing.** The engine writes no `{domain}/index.md`, and requires no MOCs there. Two reasons, in order of weight:

* Domains are self-governing (§2.2). Depositing an engine-owned, never-hand-edited file into `customers/` contradicts the promise that the domain's own `AGENTS.md` governs its layout.
* Domains have folders, so they already have topology. `customers/acme-corp/` *is* the grouping, and `customers/acme-corp/README.md` is conventionally that folder's hub — the role a MOC plays in a flat namespace. Mandating `type: moc` on top of that would additionally force every domain declaring `allowed_types` to whitelist `moc` or reject its own index note.

A domain that wants a maintained index declares it in its `{domain}/AGENTS.md` directives; the agent then maintains that note as ordinary domain content under the domain's own rules. The root `index.md` still covers the whole vault's raw-source queue, because a note in any domain may cite `raw/` (§2.4).

### 2.6. Vault Configuration (`.cadabby.json`)

The config file doubles as the vault root marker. Root discovery walks up from the working directory, exactly like `.git` — the full resolution order is in §2.7. All fields are optional; defaults shown.

```json
{
  "schema": 3,
  "vault_name": "my-vault",
  "raw_text_extensions": [".md", ".markdown", ".txt", ".rst", ".csv"],
  "integrity": "mtime_size",
  "ranking": {
    "trust": {
      "human-reviewed": 2.0,
      "machine-confirmed": 1.2,
      "stale-verified": 1.0,
      "unverified": 1.0
    },
    "status": {
      "evergreen": 1.0, "active": 1.0, "draft": 0.9,
      "completed": 0.85, "deprecated": 0.4, "abandoned": 0.4
    },
    "domain": {
      "archive": 0.5
    }
  },
  "identities": {
    "human:owner": ["owner@example.com"]
  },
  "log_rotate_bytes": 262144
}
```

`integrity` selects cache invalidation strategy: `"mtime_size"` (default, fast) or `"hash"` (stat plus SHA-256 of file bytes; immune to clock skew and same-second edits, costs a full read per file per scan).

`identities` maps OKF actor strings to Git author emails and is consumed only by `cadabby audit` (§3.5).

Known keys are checked at load, by JSON type and then by value: `integrity` must be one of the two modes above, every ranking multiplier a finite number (`NaN` and `Infinity` are rejected, though `json` would accept them), `raw_text_extensions` an array of strings, and each `identities` value an array of strings. A violation is `CONFIG_INVALID` naming the key path (`ranking.trust.human-reviewed`). Nothing downstream repairs a bad value: a silently neutralized multiplier is a setting the user believes is in force.

Unrecognized keys are carried through the merge untouched rather than rejected, so a config written for a newer or older Cadabby still loads. The cost is that a misspelled key is silently inert, and the engine must therefore never ship a key it does not read — see §7.5 for the one time it did.

### 2.7. Vault Resolution (normative)

Every command resolves exactly one vault root before doing anything else. The order is fixed:

1. **`--vault <path>`** — names the root *exactly*. Never walked up from.
2. **`$CADABBY_VAULT`** — names the root *exactly*, with the same no-walk-up rule. If it is set but does not name a vault, commands fail with that message rather than falling through to the working directory: a misconfigured variable that silently resolves somewhere else is how an agent writes into the wrong vault.
3. **The working directory**, walked upward to the first root marker.

A **root marker** is `.cadabby.json` or an `.obsidian/` directory. The second exists so that `cadabby init` can adopt an existing Obsidian vault in place rather than demanding a fresh directory; it means a plain Obsidian vault with no `.cadabby.json` resolves as a Cadabby root, which is intended but easy to be surprised by.

Only exact forms (1) and (2) are permitted to designate a root without a search, and this asymmetry is the point. Walking up is a convenience for a human standing in a subdirectory; it is a hazard for anything that names a vault deliberately, because the nearest marker above a wrong path is still a valid vault. §7.6 depends on this order: a bare `--global` install registers an **unbound** server precisely so that resolution happens per launch through (2) and (3) rather than being frozen into one absolute path at install time.

---

## 3. Epistemic Trust Tiers & OKF Frontmatter Specification

Every Markdown note across `wiki/` and all discovered cognitive domains must contain valid frontmatter conforming to Open Knowledge Format (OKF v0.2), expressed in the **Restricted YAML Subset** defined in §3.2. (Domain manifests `{domain}/AGENTS.md` use this same subset).

### 3.1. Frontmatter Schema

```yaml
---
type: moc | entity | concept | synthesis | comparison | guide | <domain-specific>
title: "Unique Human-Readable Title"
description: "One-line high-signal abstract or summary"
status: active | evergreen | draft | completed | deprecated | abandoned
tags:
  - knowledge-base
  - sqlite
sources:
  - raw/2026-paper-kv.pdf
  - raw/karpathy-llm-wiki-gist.md
verified:
  - by: human:owner
    at: '2026-10-03T12:00:00Z'
    method: manual-review
    of: 'sha256:9f2b7c1e4a8d05f3b6c29e7d1a4f80b35c6e9d2a7f41b8c035e6d9a2f74b1c80'
generated:
  by: agent:claude-opus-5
  at: '2026-10-03T11:45:00Z'
---
```

**Required**: `type`, `title`, `description`, `status`. **Optional**: `tags`, `sources`, `verified`, `generated`. Timestamps are RFC 3339 UTC with a `Z` suffix and must be single-quoted (an unquoted YAML timestamp is a typed scalar in full YAML, and the subset avoids implicit typing entirely).

* **Type Validation**: Cadabby recognizes six canonical note types (`moc`, `concept`, `entity`, `comparison`, `synthesis`, `guide`). In `wiki/`, open typing is supported by default (`allowed_types: None`), allowing dynamic note types. `moc` is distinguished only in that `index.md` lists Maps of Content as the vault's entry points and measures coverage against them (§2.5). In custom cognitive domains, `type` must match one of the entries in the domain's declared `allowed_types` (§2.2); if the domain defines no `allowed_types`, any non-empty string scalar is accepted.
* **Sources Requirement**: `sources` is optional by default, but is **required** whenever the note's enclosing cognitive domain specifies `require_sources: true` in `{domain}/AGENTS.md`.
* **Tag Grammar**: a tag is one or more segments joined by `/`; each segment is one or more runs of letters or digits joined by `-`. In short: lowercase kebab-case, with `/` reserved for hierarchy (`knowledge-base`, `trust/human-reviewed`). Segments match unicode letters and digits rather than ASCII, so non-English tags are conformant. **Whitespace is forbidden**, and this is a storage contract rather than a style preference: the cache stores a note's tags space-joined (§4.2), so a tag containing a space makes the `--tag` facet match notes that do not carry it. Lint therefore reports whitespace as a `TAG_MALFORMED` *error*, and a tag that is merely non-canonical (uppercase, underscores) as a `TAG_MALFORMED` *warning* — the canonicalizing writer (§3.2) repairs the latter on the next write, and the former it cannot.

**Deliberately omitted: `updated: {by, at}`.** The only epistemic question modification tracking needs to answer is "has this drifted since it was reviewed?", and `body_hash` vs. `of:` (§3.4) answers it from content rather than from a clock — immune to skew, out-of-order writes, and an agent forgetting to bump a field. Last-writer attribution is recorded losslessly by `log.md` and by Git, whereas a single overwritten slot would be lossy and self-reported. Note that `git clone` resets mtime to checkout time while preserving `git log`; conversely zip, Dropbox, Syncthing, and rsync preserve mtime but carry no history — so every realistic distribution path retains at least one of the two. Revisit only if a recency multiplier is added to ranking (§4.4), and even then prefer backfilling the timestamp from `git log` into the disposable cache over putting it in-band.

### 3.2. Restricted YAML Subset (normative)

A hand-rolled "robust YAML parser" is an unbounded project, and a lossy round-trip corrupts a human's hand-edited file. Cadabby therefore accepts a deliberately small, fully specified subset and **fails loudly** on anything outside it.

**Accepted grammar.** The frontmatter block is delimited by `---` on its own line at byte 0 and a closing `---`. A leading UTF-8 byte-order mark is ignored when reading and not written back, since editors on Windows add one, and treating `\ufeff---` as "no frontmatter" once led an update to write a second block above the original. Lines end in `\n` or `\r\n`; a carriage return *not* followed by a line feed anywhere in the block is rejected, because Git counts `\n` only: a `\r`-joined `verified:` entry sat on one Git line and borrowed the blame of an older, genuine endorsement (§3.5). Bodies keep §3.3's line-ending rules. Within it:

* **Block mappings** at indent 0: `key: value`. A colon is a mapping indicator only when followed by a space or the end of the line, so `title:Foo` is rejected rather than read as a key, and a sequence item `- https://example.com` stays one string.
* **Block mappings of scalars nested one level**: a key whose value is itself a mapping, its members indented 2 spaces — the shape `generated:` uses (§3.1). Members must be scalars; nesting stops here.
* **Block sequences** of scalars: `- value`, indented 2 spaces under their key. The dash must be followed by a space or the end of the line; `-foo` is not an item and is rejected.
* **Block sequences of flat mappings**: `- key: value` followed by sibling `key: value` lines at the dash's content indent. Nesting depth stops here.
* **Scalars**: bare (`active`), single-quoted, or double-quoted. Escapes recognized inside double quotes: `\\ \" \n \t`. Bare scalars are never implicitly typed — everything is a string except the literals `true`, `false`, and `null`. `~` is a string, not YAML's alternative spelling of null.
* **Keys** are unique. A key written twice is rejected, even if its first occurrence was an empty `key:`.
* **Comments**: a `#` beginning a line, or preceded by whitespace outside a quoted scalar. A quote opens a quoted scalar only where the value begins (after `key: ` or `- `); an apostrophe inside a bare value (`it's # note`) is literal and does not hide the comment. Comments and blank lines inside the frontmatter are accepted on read and **discarded by any write**: the canonicalizing writer below emits one form, and a comment has no key to travel with once keys are reordered. Frontmatter is engine-owned; prose belongs in the body, which every write preserves byte for byte outside the edited section.

**Explicitly rejected**: flow style (`[a, b]`, `{k: v}`), anchors and aliases (`&`, `*`), tags (`!!str`), multi-line scalars (`|`, `>`), multi-document markers, mappings nested more than two levels, and sequences of sequences.

**Failure behavior (read side).** A file whose frontmatter does not parse is **never written to**. It is recorded in the cache with `parse_error` set, excluded from search and graph results, and reported by `lint` as a `FRONTMATTER_UNPARSEABLE` error naming the offending line. Best-effort partial parsing is forbidden: a parser that guesses is a parser that silently destroys user data.

**Failure behavior (write side).** The same prohibition binds the writer, and binds it harder. Given a value the subset cannot express — a mapping nested two levels deep, a sequence inside a mapping — the writer **raises and emits nothing**; it must never coerce the value to text. The temptation is a language-level stringification, and the consequence is specific and severe: the structured value is destroyed, the emitted file is rejected by this very parser, and the note is therefore recorded with `parse_error` and **disappears from search** while the write reports success. That is the engine inflicting silent incompleteness on its own retrieval surface, which §1 ranks as the worst available outcome. The error names the offending key path (`provenance.import`), because an agent that cannot see which field it got wrong will retry the same call. Note that the write path is reachable from `vault_update_note`'s `patch_frontmatter`, which accepts an arbitrary JSON object — so this is an input-validation boundary, not a theoretical one.

**Canonicalizing writer.** Every Cadabby write emits the canonical form: keys in schema order (`type`, `title`, `description`, `status`, `tags`, `sources`, `verified`, `generated`, then unrecognized keys alphabetically, preserved verbatim), two-space sequence indent, single-quoted timestamps, bare scalars where unambiguous. A string that another YAML reader (1.1 or 1.2 core schema, in any casing) would type as a boolean, null, number or date — `3`, `1e3`, `.inf`, `TRUE`, `Null`, `yes`, `2024-01-31` — is double-quoted, so Obsidian and other tools in the vault read the same string Cadabby does. The body is written as read, leading blank lines included, and a note read with CRLF line endings is written back with CRLF throughout, so a frontmatter edit changes no body line. The writer additionally lowercases and de-duplicates `tags`, preserving first-seen order, and **refuses the write** if any tag contains whitespace — lowercasing cannot repair that, and emitting it would corrupt the `--tag` facet (§3.1). Humans may author anything inside the subset; the first agent write normalizes the file. This bounds the round-trip problem to "read subset, write one canonical form," which is testable to exhaustion.

### 3.3. Body Hash (normative)

The **body** is everything after the closing `---` of the frontmatter block. Its hash is computed over the normalized body:

1. Decode UTF-8.
2. Normalize line endings to `\n`.
3. Strip trailing whitespace from each line.
4. Strip leading and trailing blank lines.
5. Encode UTF-8, `sha256`, lowercase hex, prefixed `sha256:`.

The hash deliberately **excludes frontmatter**, so that retagging, restatusing, or adding a source does not invalidate a human's review of the prose. It is recomputed on every cache scan and stored as `notes.body_hash`.

### 3.4. Trust Tier Derivation

A verification entry is **valid** when its `of:` field is present and equals the note's current `body_hash`. The tier is derived from valid entries only:

1. **`human-reviewed`**: at least one *valid* entry with `by: human:*`. This represents ground truth for AI agents.
2. **`machine-confirmed`**: no valid human entry, but at least one *valid* entry with `by: agent:*` or `by: process:*`.
3. **`stale-verified`**: `verified` is non-empty, but no entry is valid — every attestation refers to content that has since changed, or predates the `of:` requirement. The records are preserved; the trust is not.
4. **`unverified`**: `verified` is absent or empty. Default state for newly scaffolded pages.

Actor strings match `^(human|agent|process):[A-Za-z0-9._\-/]+$`. A version suffix after `/` (`agent:gemini-flash/1.5`) is permitted and carries no special meaning.

**Why this matters.** Without content binding, a human verifies a note, an agent rewrites half of it, and the note reads `human-reviewed` forever — reintroducing the exact hallucination compounding the trust hierarchy exists to prevent. Drift is surfaced, not hidden: `status` reports a **verification debt** count, and `lint` gate 6 lists every stale note.

### 3.5. Identity, Attribution & Provenance Audit

Cadabby's identity model is a **convention with an audit trail**, not a security boundary. Stating the limits precisely:

* **What the engine guarantees.** The MCP server refuses to write `by: human:*` under any circumstances, and stamps `by: agent:<client_id>` taken from the JSON-RPC `initialize` handshake's `clientInfo.name`, with every character outside the §3.4 actor grammar's ASCII set replaced by `-` (`Café Agent` → `agent:Caf--Agent`), so the actor it stamps is always one verify and lint accept. `cadabby verify --human` requires an interactive TTY (`sys.stdin.isatty()`), shows the note's title, CID, and body-hash prefix, and writes only after the user types back a one-time random token it has just printed; it then stamps `by: human:<os-username>`. A wrong or empty answer aborts with nothing written. `cadabby verify` without `--human` never stamps `human:*`, terminal or not. Every write is atomic and leaves the vault in a Git-revertible state.

  The TTY check alone stopped nothing that mattered: agents run shell commands inside pseudo-terminals, so a flagless `cadabby verify` issued by an agent used to stamp a human endorsement silently, and `printf 'y\n' | script -qec 'cadabby verify <cid> --human' /dev/null` did the same in one line. The token turns an accidental endorsement into one that has to be engineered on purpose, which is the line this section draws.
* **What it does not guarantee.** Agents that speak MCP in practice also hold filesystem and shell access; such an agent can edit the markdown directly or invoke the CLI. `clientInfo` is self-reported and unauthenticated, so `agent:<client_id>` is a claim, not an identity. The mechanism prevents *accidental* forgery — the overwhelmingly common case — and nothing more.
* **What is actually verifiable.** Git already records authenticated-ish authorship. `cadabby audit` locates each `human:*` verification entry, runs `git blame --porcelain` on **every line of it** — `by:`, `at:`, `method:` and `of:` alike — and compares each line's commit author email against the `identities` map in `.cadabby.json`. Blaming only the `by:` line let an agent edit a body and re-point the person's `of:` to the new hash with the audit staying clean; any line of a human entry committed by someone else is a mismatch naming that line. A mismatch — most importantly a human endorsement introduced by a commit authored by an agent — is reported as `PROVENANCE_MISMATCH`. With `--require-signed`, every commit among the entry's lines is also flagged as `UNSIGNED_ENDORSEMENT` unless Git reports a good signature (`git log --format=%G?` is `G`, or `U` for a good signature from an untrusted key). An endorsement not yet committed has no author to check: it is a `PROVENANCE_MISMATCH` saying so, and no signature is sought for a commit that does not exist. An empty author email never matches an identity. The audit fails closed: a note whose frontmatter does not parse but mentions `human:` is a `PROVENANCE_MISMATCH` (its endorsements cannot be audited), and a signature check that cannot run counts as unsigned. Git runs with `core.fsmonitor` and hooks disabled and `GIT_CONFIG_NOSYSTEM=1`, so a `.git/config` shipped inside an archived or synced vault cannot make the audit execute a program. This turns an unenforceable claim into a detectable one at the cost of one `subprocess` call per note.

`audit` is a no-op with a clear notice when the vault is not a Git repository. `--json` always prints `{"notice": …, "findings": […]}`, `notice` being `null` when the audit ran. Each git call has a 30-second timeout: a hung `rev-parse` reads as no repository, and a hung `blame` or `log` fails the audit as `IO_ERROR` rather than letting the endorsement it was checking pass unchecked.

---

## 4. SQLite Ephemeral Cache & Search Engine

The SQLite cache lives at `.cadabby/cache.db` and is 100% disposable. If deleted, any command reconstructs it on the next scan.

### 4.1. Runtime Capability Check

FTS5 ships with most but not all CPython SQLite builds. On first connection Cadabby probes for it and, if absent, aborts with an actionable message naming the Python interpreter and its `sqlite3.sqlite_version` rather than failing mid-query. Connections open with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`.

### 4.2. Database Schema

`files` and `notes` are deliberately **one table** — they were 1:1 on `rel_path` and nothing was gained by splitting them.

```sql
-- One row per tracked file. Covers wiki/ notes and raw/ sources alike.
CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY,          -- FTS5 external-content rowid
    cid          TEXT NOT NULL UNIQUE,         -- "wiki/epistemic-trust-tiers"
    rel_path     TEXT NOT NULL UNIQUE,         -- "wiki/epistemic-trust-tiers.md"
    stem         TEXT NOT NULL,                -- "epistemic-trust-tiers"
    layer        TEXT NOT NULL,                -- cognitive domain ("wiki", "customers", ...) | "raw"

    -- Invalidation
    mtime        REAL NOT NULL,
    file_size    INTEGER NOT NULL,
    file_hash    TEXT,                         -- populated only when integrity="hash"
    indexed_at   TEXT NOT NULL,

    -- Parsed OKF metadata (NULL for raw/ files)
    title        TEXT,
    description  TEXT,
    type         TEXT,
    status       TEXT,
    trust_tier   TEXT,                         -- derived, see 3.4
    body_hash    TEXT,                         -- see 3.3
    frontmatter_json TEXT,                     -- verbatim parsed frontmatter
    parse_error  TEXT,                         -- non-NULL => excluded from search

    -- Space-joined tags, denormalized from frontmatter_json. Required, see below.
    tags         TEXT NOT NULL DEFAULT '',

    -- Full text, stored once. Empty for binary raw/ sources.
    body         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notes_layer_type ON notes(layer, type);
CREATE INDEX IF NOT EXISTS idx_notes_trust ON notes(trust_tier);

-- Link graph: wikilinks and relative Markdown note links (§4.4). target_cid is NULL for
-- dead links, which is what lint reads.
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY,
    source_cid  TEXT NOT NULL,
    target_raw  TEXT NOT NULL,                 -- "flash-attention#Tiling" exactly as written
    target_cid  TEXT,                          -- resolved; NULL = broken link
    alias       TEXT,                          -- "[[Note|alias]]" -> "alias"
    anchor      TEXT,                          -- "[[Note#heading]]" -> "heading"
    occurrences INTEGER NOT NULL DEFAULT 1,
    kind        TEXT NOT NULL DEFAULT 'wiki'   -- 'wiki' for [[...]], 'markdown' for [label](path.md)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_edge
    ON links(source_cid, kind, target_raw);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_cid);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_cid);

-- Provenance edges: wiki note -> raw source. Drives "processed" derivation (2.4).
CREATE TABLE IF NOT EXISTS sources (
    source_cid  TEXT NOT NULL,                 -- the wiki note
    raw_path    TEXT NOT NULL,                 -- as written in frontmatter
    resolved    INTEGER NOT NULL,              -- 1 if raw_path names an indexed raw/ file
    resolved_path TEXT,                        -- that file's rel_path once normalized ('./raw/x' -> 'raw/x'), else NULL
    PRIMARY KEY (source_cid, raw_path)
);
CREATE INDEX IF NOT EXISTS idx_sources_raw ON sources(raw_path);
CREATE INDEX IF NOT EXISTS idx_sources_resolved ON sources(resolved_path);

-- Contentless-external FTS5: the index is built, the text is not duplicated.
-- Two tables, one per corpus: curated notes in notes_fts, raw sources in
-- raw_fts. A row belongs to exactly one of them, chosen by its layer. See
-- below for why this is not one table with a filter. Each table's content is
-- a view holding exactly the rows it indexes, so integrity-check can compare
-- the index with its content (see below).
CREATE VIEW IF NOT EXISTS notes_fts_content AS
    SELECT id, title, description, body, tags FROM notes
    WHERE layer != 'raw' AND parse_error IS NULL;
CREATE VIEW IF NOT EXISTS raw_fts_content AS
    SELECT id, title, description, body, tags FROM notes
    WHERE layer = 'raw' AND parse_error IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, description, body, tags,
    content = 'notes_fts_content',
    content_rowid = 'id',
    tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
    title, description, body, tags,
    content = 'raw_fts_content',
    content_rowid = 'id',
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS cache_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

**The `tags` column is load-bearing, not a convenience.** External-content FTS5 keeps only the inverted index and reads column values back from the content table, so every column declared in `notes_fts` must also exist in its content view, and so in `notes`. Drop `tags` from `notes` and the failure is immediate and total rather than subtle: loading a row fails, so `snippet()` raises `SQL logic error` on *every* search — the query at §4.4 calls it unconditionally — and `INSERT INTO notes_fts(notes_fts) VALUES('rebuild')` fails with `no such column: T.tags`. The column earns its place twice over: holding tags space-joined also lets the `--tag` filter run as a SQL predicate, `instr(' ' || n.tags || ' ', ' ' || :tag || ' ') > 0`, instead of re-parsing `frontmatter_json` for every candidate row. That predicate is exact only because the tag grammar forbids whitespace (§3.1); without that rule `--tag machine` would match a note tagged solely `machine learning`. `instr` is case-sensitive, so the probe is lowercased at the call site, and `upsert_note` stores tags lowercased whatever casing the file uses. Both halves are needed: §3.1 makes a non-canonical case only a warning, so a hand-written `ML` stays `ML` on disk until an agent rewrites the file, and up to and including 0.4.2 such a tag could not be found by `--tag` in any spelling.

Because the index lives outside the content table, it must be maintained explicitly alongside every `notes` mutation:

```sql
-- Before updating or deleting a row, retract the old index entry.
-- The values passed here MUST be the row's OLD column values, read back
-- from `notes` first. Passing the new values silently corrupts the index:
-- the old terms are never retracted and stale matches persist forever.
INSERT INTO notes_fts(notes_fts, rowid, title, description, body, tags)
    VALUES('delete', :id, :old_title, :old_description, :old_body, :old_tags);
```

This is the single sharpest edge in the cache layer, so it is confined to one `cache.upsert_note()` / `cache.delete_note()` pair; **no other code path may write `notes` directly**. `test_cache.py` asserts the retraction by rewriting a note's body and confirming the old term no longer matches, then running `INSERT INTO notes_fts(notes_fts, rank) VALUES('integrity-check', 1)` on both tables.

With two indexes the retraction has a second way to go wrong: it must target **the table the row was written to**. Retracting against the wrong table leaves the real entry standing and corrupts the other, and like a retraction with the wrong values it fails silently. A single `_fts_table(layer)` mapping therefore serves every read and write, so no call site re-derives it. Note that a file never changes layer in place — the layer is a pure function of `rel_path`, so moving a file between `raw/` and a cognitive domain reaches the cache as a delete followed by an insert, not an update.

The integrity check runs against **both** tables. A mismatched retraction damages one index while leaving the other clean, so checking only the curated one would report health while raw search returned wrong rows.

**The check must compare the index with its content, and that is why each table's content is a view.** FTS5's plain `integrity-check` verifies only the index's internal structure; it passes on an index still holding a note's old terms, which is exactly the corruption a missed retraction leaves. With `rank = 1` FTS5 also re-tokenizes the content and compares, and reports a mismatch as `SQLITE_CORRUPT`. That form cannot run against a shared `content = 'notes'`: each index holds only some of the rows, so every raw row reads as missing from `notes_fts`, and every curated row as missing from `raw_fts`. The two views state each index's row set once, in the same terms as the insert gate in `upsert_note()` — the layer picks the table, and only rows that parsed are indexed — so the comparison is exact. Up to and including 0.4.2 the content was `notes` and the check was the structural one, so every retraction assertion built on it passed whatever the index held.

`cache_meta` holds `schema_version` and the `raw_text_extensions` list the raw index was built with (§4.3). **There are no migrations**: a version mismatch deletes `cache.db` and rebuilds, and so does a `cache.db` SQLite cannot open or read as a database. This is a disposable cache, and that freedom is worth more than incremental upgrade paths.

### 4.3. Synchronization

Sync is **implicit**. Every read command (`search`, `ground`, `status`, `graph`, `lint`) performs an incremental scan first, and so does every write; `cadabby sync` merely exposes the scan for debugging and forces a full pass. An agent cannot forget to sync, because it cannot sync.

The scan walks `raw/` and all discovered cognitive domains (`wiki/`, `customers/`, `projects/`, etc.), skipping `DEFAULT_IGNORED_DIRS` and domain manifest files (`{domain}/AGENTS.md`). It `stat`s every file, and:

1. **Inserts** paths absent from `notes`.
2. **Reparses** paths whose `(mtime, file_size)` pair differs — or whose SHA-256 differs, under `integrity: "hash"` — and every `raw/` path, whatever its stat, when `raw_text_extensions` differs from the list recorded in `cache_meta`. Which raw files have indexed text is a function of the config as well as the file, so changing the list takes effect on the next scan with no file touched and no `--rebuild`.
3. **Deletes** rows whose `rel_path` no longer exists on disk, including their `links`, `sources`, and FTS rows — retracted from whichever of the two indexes the row's layer selects, never from both and never from the wrong one (§4.2). *(Deletion reconciliation is not optional; without it, a renamed note haunts search results indefinitely.)*
4. Re-resolves `links.target_cid` and `sources.resolved` / `sources.resolved_path` against the files the cache now holds, whenever any file changed. Citations resolve against the indexed raw set rather than a separate disk probe, so a raw file arriving after the note that cites it clears `SOURCE_MISSING` without the note being edited. A raw file is *unprocessed* exactly when no `sources.resolved_path` names it; `index.md` and `vault_status` share that one definition.

`(mtime, file_size)` misses the pathological case of a same-second, same-length edit; `integrity: "hash"` exists for vaults where that matters. A full stat sweep of a few thousand notes costs single-digit milliseconds, so there is no staleness heuristic to get wrong.

**Index regeneration rides the scan.** Any scan reporting a non-zero change count regenerates `index.md` before returning, whichever entry point triggered it — CLI command, MCP tool, or `sync`. A scan that found no changes skips regeneration entirely; `cadabby sync` regenerates unconditionally.

This means an MCP *read* tool can write one file, which is deliberate. `index.md` is a gap report (§2.5), and a stale queue listing an already-ingested source as unprocessed will send an agent to redo work that is already done — a failure strictly worse than having no queue at all. Binding regeneration to the scan rather than to the write path also covers the common case the write path misses: a human dropping a PDF into `raw/` changes the vault without any Cadabby write. The cost is bounded by §8's byte-comparison guard, so regeneration that produces identical bytes writes nothing and leaves no Git diff.

### 4.4. Epistemic BM25 Rank Boosting & Domain Filtering

SQLite's `bm25()` returns **negative** values, more negative meaning a better match, so that `ORDER BY rank` ascending yields best-first. Multiplying a negative score is sign-sensitive and inverts silently if anyone later sorts the other way, so the sign is normalized once, at the query:

```sql
-- {fts} is notes_fts, or raw_fts when :domain is exactly 'raw' (§4.2).
-- It is selected by the engine from the layer, never interpolated from
-- caller input, so the table name cannot carry an injection.
SELECT n.cid,
       ((-bm25({fts}, 4.0, 2.0, 1.0, 1.5))
        + 0.05 * ((CASE WHEN instr(highlight({fts}, 0, char(1), char(2)), char(1)) > 0 THEN 4.0 ELSE 0.0 END)
                + (CASE WHEN instr(highlight({fts}, 1, char(1), char(2)), char(1)) > 0 THEN 2.0 ELSE 0.0 END)
                + (CASE WHEN instr(highlight({fts}, 2, char(1), char(2)), char(1)) > 0 THEN 1.0 ELSE 0.0 END)
                + (CASE WHEN instr(highlight({fts}, 3, char(1), char(2)), char(1)) > 0 THEN 1.5 ELSE 0.0 END)))
         * :trust_mult
         * :status_mult
         * :domain_mult AS score
FROM {fts}
JOIN notes n ON n.id = {fts}.rowid
WHERE {fts} MATCH :query
  AND n.parse_error IS NULL
  AND (n.layer = :domain OR (:domain IS NULL AND n.layer != 'raw'))
ORDER BY score DESC, n.cid ASC
LIMIT :limit;
```

Higher is better, unconditionally.

* **Domain-Filtered Search**: If `:domain` is provided (e.g. `domain="projects"`), search queries are constrained strictly to that cognitive domain. If omitted (`:domain IS NULL`), search spans all non-raw cognitive domains (`layer != 'raw'`). `domain="raw"` is accepted and is the only way to reach unprocessed sources (§2.4); it switches the query to `raw_fts`, so raw results are ranked against other raw sources rather than against curated notes.
* **Filter-Only Search**: When `query` is omitted or empty (`""`) and at least one filter (`domain`, `type`, `status`, `trust`, `tag`) is supplied, `search` queries `notes` directly (without `MATCH`), scoring each matching note by `:trust_mult * :status_mult * :domain_mult` (`ORDER BY score DESC, n.cid ASC LIMIT :limit`) and setting `snippet` to `COALESCE(NULLIF(n.description, ''), substr(n.body, 1, 120), '')`. Calling `search` with an empty or whitespace-only `query` and no filters is refused as `INVALID_ARGUMENT` (§5.4). A non-empty `query` containing only punctuation sanitizes to no FTS tokens and returns `[]`.
* **Multi-Term `OR` Fill**: A sanitized query is evaluated first under FTS5's default implicit `AND`, so documents containing every term always take the top slots. When an implicit-`AND` query of two or more distinct terms (with no explicit `AND`, `OR`, or `NOT` operators) returns fewer than `limit` rows, `search` re-runs it with the sanitized terms joined by `OR` and appends the new matches, deduplicated, until `limit` is reached. The result is therefore two tiers, each ordered by the same epistemic score: all-terms matches, then partial ones. FTS5's `bm25()` sums across matched terms, so a partial match covering more terms tends to score higher, but trust and status multipliers still apply, and a client must not re-sort the merged list by `score` or it loses the all-terms-first guarantee. Filling rather than only falling back on zero hits matters because one broad note that happens to mention every term (an account brief, a hub) would otherwise hide the focused notes that each explain one of them. `limit` is a count of at least 1; anything else is `INVALID_ARGUMENT`.
* **Canonical Stem Resolution Priority**: In the link graph (`LinkTargetIndex`), known note CIDs are registered in three priority tiers: `wiki/` stems first, then active cognitive domains alphabetically, then `archive/` stems last. A bare wikilink (`[[Architecture]]`) always resolves to the canonical note in `wiki/Architecture` rather than an arbitrary domain note, and an active domain note (`projects/apollo/Architecture`) is never shadowed by an archived note (`archive/old/Architecture`) even though `archive/` sorts earlier alphabetically. Notes in custom or archived domains can be linked unambiguously via qualified CID (e.g. `[[customers/acme/Account]]`, `[[archive/cs244b/Syllabus]]`) or scoped stem (`[[acme/Account]]`).
* **Qualified Targets Never Fall Back to a Stem**: A wikilink target containing `/` (`[[apollo/Architecture]]`) resolves by exact CID or path suffix only. A typo in the path (`[[customers/acmee/README]]`) is `LINK_DEAD`, never silently resolved to some other note sharing the final stem.
* **A Broken Note Keeps Its Name**: Links resolve against every note on disk, including one whose frontmatter does not parse. A link that resolves to such a note stays unresolved (`target_cid` NULL), and gate 3's `LINK_DEAD` names the note whose frontmatter is at fault (§6.3); it never falls through to another note sharing the stem. Up to and including 0.4.2 a broken `wiki/Architecture` silently sent every `[[Architecture]]` to `projects/apollo/Architecture`, with nothing reported, until the YAML was fixed.
* **Markdown Note Links**: A relative inline Markdown link whose path ends in `.md` (`[Arch](../../wiki/Architecture.md#Storage)`, percent-encoded or `<angle-bracketed>` paths included) is a graph edge with `kind = 'markdown'`, carrying backlinks, gate 3 and gate 5 checks, and `index.md` reachability exactly as a wikilink does. It resolves by **exact path from the citing note's folder only** — never by stem, because a stem fallback would silently absorb the one-`../`-too-many mistakes the dead-link check exists to catch. Images, URLs, vault-absolute paths, same-note `#heading` jumps, code spans, paths with a `raw` segment (gate 4's citations), and targets that cannot be notes (root files such as `STYLE.md`, reserved or dot folders, `AGENTS.md` manifests, the declared template folder) are not edges. `[[Wikilinks]]` remain the form agents write, since they need no `../` depth; Markdown note links exist so vaults authored with Obsidian's Markdown link format, or published to renderers that do not understand wikilinks, stay checked.
* **Note Transclusions**: `![[Note]]` and `![[Note#Heading]]` embed one note's content in another, so they are wikilink edges (`kind = 'wiki'`) carrying backlinks, anchor checks, and `index.md` reachability: a note an MOC only transcludes is not unfiled. An embed whose final path segment names a non-`.md` extension (`![[diagram.png]]`, `![[flow.canvas]]`, `![[paper.pdf#page=3]]`) is an attachment and is never an edge.

Two properties of FTS5's `bm25()` make this multiplication safe, both verified against SQLite 3.53.2 and both easy to "fix" into a bug later:

* **It never crosses zero.** A term occurring in every indexed document would drive a textbook IDF negative, flipping `-bm25()` negative and inverting every multiplier precisely when the boost matters most. FTS5 clamps instead, bottoming out around `-1e-06`, so `-bm25()` is always ≥ 0 and multiplication is monotonic. Do not substitute a hand-rolled BM25 without re-establishing this.
* **It saturates for common terms, and the field-presence floor keeps column weights alive.** When a query term appears in at least half of the indexed corpus — which is the normal case in a small vault or a focused customer engagement — FTS5 clamps IDF to `~1e-06`. Because `bm25()` multiplies every column weight by that single corpus-wide IDF, a clamped `-bm25()` alone would flatten a `title` + `description` + `tags` match down to the same `~2e-06` noise floor as a passing mention in `body`. Adding `0.05 * w_c` per matched column via `highlight()` preserves the `4.0 : 2.0 : 1.0 : 1.5` field hierarchy (`title` `+0.200`, `description` `+0.100`, `tags` `+0.075`, `body` `+0.050`) before multiplying by `:trust_mult * :status_mult * :domain_mult`, while leaving discriminating terms (`-bm25() ≈ 1–10`) dominated by BM25.

The column weights (`title` 4.0, `description` 2.0, `body` 1.0, `tags` 1.5) and field-presence floor (`0.05`) are fixed; the multipliers come from `.cadabby.json` (§2.6) and default to:

* **Trust:** `human-reviewed` 2.0× · `machine-confirmed` 1.2× · `stale-verified` 1.0× · `unverified` 1.0×
* **Status:** `evergreen`/`active` 1.0× · `draft` 0.9× · `completed` 0.85× · `deprecated`/`abandoned` 0.4×
* **Domain:** `archive` 0.5× · all other domains 1.0× (compounds multiplicatively with trust and status, so a `completed` note moved into `archive/` scores `0.85 × 0.5 = 0.425×` without rewriting its frontmatter or invalidating its `verified:` hashes)

### 4.5. Epistemic Health & Domain Status Reporting

`VaultCache.get_status()` aggregates vault health metrics across all domains:
* `total_notes`: Count of all tracked notes across all cognitive domains.
* `total_wiki`: Count of notes in canonical `wiki/`.
* `domains`: Per-domain breakdown mapping `{domain_name: count}` (e.g. `{"wiki": 4, "customers": 1, "projects": 1}`).
* `total_raw`, `unprocessed_raw`, and `filename_only_raw`: raw files, those no citation resolves to (§4.3), and a `{extension: count}` breakdown of raw files whose extension is outside `raw_text_extensions` and that are therefore indexed by filename stem only (§2.4).
* `verification_debt`, `trust_tiers`, `types`, `statuses`: epistemic counts.
* `broken_links`: link rows whose target does not resolve, the same set gate 3 reports as `LINK_DEAD` (§6.3).
* `vault_name`, `root`, `integrity`: identification and the active integrity mode.

`vault_status` and `cadabby status` both serve this through one builder that adds **`finding_counts`** — `{"error": n, "warning": m, "by_code": {...}}` from the six gates of §6.3 — on every call, and the `findings` list itself with `lint: true`. Every check therefore shows red or green without a second call, while the full list, which runs to hundreds on a neglected vault, is fetched only to repair. `broken_links` and `verification_debt` stay distinct from those counts on purpose: they count link rows and `stale-verified` notes, which need not match gate 6's warnings in either direction. Gate 6 also warns on a human attestation superseded only by an agent's, whose note is `machine-confirmed` and so is not debt; and a `stale-verified` note whose entries cannot bind at all (a malformed actor, a missing `of:`, an entry that is not a mapping) is debt reported as `ACTOR_MALFORMED` or `VERIFICATION_UNBOUND` rather than `VERIFICATION_STALE`. Every unit of debt has at least one gate 6 finding.

The same builder adds **`queues`**, the `index.md` worklists themselves (§2.5), so an agent never reads that file off disk to learn what is owed:

```json
"queues": {
  "unprocessed_raw": {"total": 3, "items": ["raw/a.pdf", "raw/b.md", "raw/c.txt"]},
  "unfiled":         {"total": 1, "items": ["wiki/flash-attention"]},
  "verification_debt": {
    "agent_payable": {"total": 2, "items": ["wiki/raft", "customers/acme/deal"]},
    "human_owed":    {"total": 1, "items": ["wiki/architecture"]}
  }
}
```

* **One definition per queue.** `index.md` and `queues` read the same functions in `cache.py` (`unprocessed_raw_paths`, `unfiled_note_rows`, `verification_debt_rows`), in the same order, so the two cannot disagree.
* **Capped at 25 items each**, with `total` uncapped. `vault_status` runs at the start of nearly every task, and a neglected vault's hundreds of sources would cost every turn that reads them. The cap is a constant, not a parameter.
* **The debt queue is the actionable subset of `verification_debt`.** The count is every `stale-verified` note; the queue, like `index.md`, leaves out retired notes (`archive/`, `deprecated`, `abandoned`), which nobody maintains. The difference is deliberate, not drift.
* **Who owes the debt** is decided by the note's latest well-formed stale attestation — the last one in `verified:` list order, which is append-only (§3.4), not the one with the greatest `at:`: `human_owed` when it is `human:*`, `agent_payable` otherwise, including when no entry binds at all. This is the vault constitution's rule that an agent never verifies over a human's stale sign-off, made machine-readable. Gate 6's `VERIFICATION_STALE` message names the latest *human* entry instead, since it reports whose endorsement drifted; the queue answers who must act now, so a human sign-off later re-checked by an agent is the agent's to renew.

---

## 5. Cadabby Tool Surface (CLI & MCP)

The CLI and the MCP server expose the same engine, but **not the same surface**. Agents degrade as tool count rises, and several commands exist for the operator, not the model.

### 5.1. MCP Tools (Strict 8-Tool Invariant)

To prevent cognitive tool dilution and LLM routing degradation, Cadabby preserves a strict **8-tool invariant**. Cognitive domain operations enhance existing tool schemas rather than proliferating specialized domain tools:

| MCP Name | Multi-Domain Adaptations | Description |
| :--- | :--- | :--- |
| **`vault_search`** | Optional `domain` filter & filter-only browsing | FTS5 BM25 search across all cognitive domains (or filtered by `domain`), with trust boosting and filters (`type`, `status`, `trust`, `tag`, `domain`, `limit`). `query` may be omitted or `""` when at least one filter is supplied (§4.4); a query with nothing searchable left after sanitizing (`---`) is treated the same way. Quoted phrases may take a prefix `*` (`"flash atten"*`), and `a AND NOT b` excludes `b` (FTS5's `NOT` is binary, so the `AND` is dropped). Returns cid, domain, title, description, trust tier, score, and snippet. |
| **`vault_ground`** | Directives, `#section` scoping, 1-hop neighbor metadata & `raw/` sources | Retrieves full, `#section`-scoped, or budget-truncated markdown for `targets`, **plus each note's domain, localized agent directives (`directives`), forward links, backlinks, and sources**. Names resolve exactly as for the write tools (below). Returns one entry per target, in order, each echoing its `target`; a target that cannot be grounded (not found, ambiguous, unparseable) is `{"target", "error": {"code", "message", "retryable"}}` in its place, classified as in §5.4, and the call fails only if no target grounds (an empty `targets` is `INVALID_ARGUMENT`). Appending `#heading` (`wiki/sqlite#Concurrency`, `[[Note#Heading]]`, or a GitHub-style slug) extracts only that ATX heading section and its nested subsections (`"section": matched_heading`, or `null` for a full note); a `#heading` the note no longer has returns the whole note with `section_error` listing its headings, since the note itself resolved (§6.3 treats `ANCHOR_MISSING` as a warning for the same reason). Each 1-hop `links` and `backlinks` entry carries the resolved neighbor's `title`, `type`, `status`, `trust_tier`, and `description` (`null` when unresolved). Each entry has `kind`: `note`, or `raw` for a source (below). |
| **`vault_status`** | `lint` parameter & `queues` | The vault health check (§4.5): note counts by tier, type, and domain, verification debt, unprocessed and filename-only `raw/` files, broken links, `finding_counts` from the six gates of §6.3, and the `queues` themselves (§4.5). `lint: true` adds every finding. Findings never set `isError` (§5.4). |
| **`vault_graph`** | MOCs and neighborhoods in any domain | Link structure without note text. `view: "neighborhood"` (default) resolves `target` exactly as the write tools name notes (below) and returns its 1- and 2-hop links and backlinks, co-citations and bibliographic coupling, every entry carrying its neighbor's `title`, `type`, `status` and `trust_tier`; each list is capped at `limit` (default 20) with `<list>_total`. `view: "mocs"` returns each wiki MOC's distinct outgoing and incoming link counts (self-links excluded), sorted by outgoing count, and the overlaps between MOCs, `ratio` being shared targets over the smaller MOC's count, rounded to three decimals and sorted by ratio then shared count; both lists are capped at `limit` with `mocs_total` and `overlaps_total`. `target` is required for `neighborhood` and refused for `mocs`; a `raw/` target is refused, since sources are not graph nodes. |
| **`vault_scaffold_note`** | `domain` and `path` parameters | Creates a note in `wiki/` or within a target cognitive domain (`domain`, optional `path`), with valid OKF frontmatter and `generated:` attribution. Without `path` (an empty one counts as none), the file stem is the title in lowercase kebab-case; a `path` ending in `/` names a folder, and the stem again comes from the title. Validates against domain `allowed_types` if defined. Refuses to overwrite, including a note whose path differs only by letter case or that is reached through a symlinked folder, and refuses names links could not carry (below). |
| **`vault_update_note`** | `edits` parameter | Non-destructive frontmatter patches and body edits across any domain note. `edits` is the only body-edit parameter: an ordered list of `replace_text` (exact, unique match anywhere in the body, including the H1 and preamble above the first `##`; never the frontmatter), `replace_section` and `append_section` (both addressing `##` headings) operations applied atomically. A `##` heading is recognized by the same ATX rule `vault_ground` uses — a closing `#` sequence, a tab after `##` and inline code are all normalized — and `replace_section` matches the requested heading exactly after that cleaning (never by slug or case), keeps the existing heading line byte for byte, and replaces only the text under it. Edits are applied in one write with one ledger entry. If any operation fails, nothing is written. A call that would change nothing — no operations, or values the note already has — is `INVALID_ARGUMENT` and writes neither the note nor the ledger. Never truncates unparseable files. `patch_frontmatter` may not touch the engine-owned `verified` and `generated` (a `human:*` entry is refused as `HUMAN_ATTESTATION_REFUSED`), nor give `title`, `description`, `type`, or `status` a non-string value. |
| **`vault_verify_note`** | Unchanged | Appends a verification entry stamped `by: agent:<client_id>`, `at:` now, and `of:` the current body hash. Writing `human:*` over MCP is refused unconditionally. |
| **`vault_move_note`** | Any domain or folder subtree | Moves or renames a note or an entire folder subtree (`customers/northwind/` → `customers/apollo/northwind/`, `cs244b/` → `archive/cs244b/`) and repoints every link across the vault in one logged operation. A dry-run unless `apply: true`; both return the same plan, with a `plan_token` that an apply may pass back. `to` ending in `/` moves a single note into that folder; a bare name renames a single note in place. Refuses what scaffold refuses at the destination. A folder move lists each note under the folder whose frontmatter does not parse as a blocker, since such a note is never written (§3.2). |

**Naming an existing note.** `vault_ground`, `vault_graph`, `vault_update_note`, `vault_verify_note`, `vault_move_note` (and its `approve_stale` entries) and `cadabby verify --human` accept a CID, a vault-relative path, a bare stem, or a wikilink, and always act on a note the storage lists under its canonical CID. For `vault_move_note` and `cadabby move`, `cid` may also name a folder prefix (e.g. `customers/northwind/` or `cs244b`), moving every note under that prefix to the corresponding relative path under `to`. The exact path is tried first (so a filename containing `#` still resolves), then a unique letter-case-insensitive path, then the name read as a wikilink, and finally a folder prefix matching one or more notes; a name ending in `/` always names a folder. A leading `./` is dropped and a `.md` suffix in any letter case is the extension, here and in scaffold's `path` and move's `to`, while `.` or `..` segments anywhere else are refused. So with both `wiki/projects.md` and a `projects/` domain, `projects` is the note and `projects/` the folder. A wiki-relative name is canonical as in §4.4; any other name matching more than one note — two domain `README` hubs — is refused as `INVALID_ARGUMENT` listing the candidates, never guessed. On a case-insensitive filesystem the storage would open either spelling, so acting on the caller's spelling instead of the listed one could, for a move, plan nothing and delete the real file.

**Names links can carry.** Scaffold and move refuse a destination whose names contain `#`, `|`, `[`, `]`, `^`, `` ` ``, `<` or `>`, a folder or file name with leading or trailing whitespace, or a `raw` folder below a domain (Markdown links into it would read as gate 4 citations, §4.4). Each of these would leave the note unreachable by at least one link form.

**Why status absorbed lint, and move took the slot.** Across the simulated user journeys, agents called `vault_status` and `vault_lint` back to back as one "check" step, a third of all tool calls between them, and the two read the same freshly scanned rows. A move, by contrast, was the one request every journey raised that no tool could carry out: agents either refused it or renamed files from the shell, which repoints no links and logs nothing.

**Grounding a source.** A `vault_ground` target whose path starts with `raw/` names a source, never a note, since no note can live there (§2.1). It resolves against the files storage lists as evidence (the scan's own walk, §2.4): the whole name first, so a filename containing `#` still resolves, then the text before the last `#` as `path#heading`, each exactly and then uniquely ignoring letter case. Nothing outside that list can be read, whatever `..` or symlink the name carries. The entry is `{"target", "kind": "raw", "path", "section", "content", "filename_only", "truncated", "cited_by"}`, with `section_error` as for notes. `cited_by` lists the notes whose `sources:` resolve to the file, with `title`, `type` and `trust_tier`. There is no `trust_tier` of its own: a source is evidence, not a claim. A file outside `raw_text_extensions` returns `content: null` and `filename_only: true`, because the engine has no reader for it and decoding it would hand back bytes dressed as source text. Budgeting truncates at section boundaries exactly as for notes.

**Why graph took the eighth slot.** Two gaps forced agents off the MCP surface, and only one needed a tool. Reading a source and seeing the worklists did not: `vault_ground` and `vault_status` absorbed them. Link structure did: the technician runbook splits MOCs past ~25 links, merges MOCs whose links overlap by more than half, and files notes by co-citation, and before `vault_graph` the only way to see that structure was to ground every MOC and neighbor in full. Unlike the status/lint and move decisions above, this one rests on what the runbooks require, not on measured journeys; if journeys show agents not calling it, it is the first candidate to fold back into `vault_ground`.

**Move semantics.** The plan is computed from every note as it is now, not from the cache, so it sees Obsidian edits made since the last scan:

* **Minimal rewrites.** A link that opened a moved note is rewritten only if its text would no longer open it — to the shortest wikilink text that does, or the relative path from the citing note — because each rewrite changes a body and so unbinds that note's attestations (§3.4). A bare `[[stem]]` that still resolves after a move to another folder is left byte-identical, as is a relative Markdown link between two co-moving notes in a folder subtree move whose relative geometry is unchanged. Only the target text changes; embed markers, anchors, aliases and escaped table pipes are preserved. Links in code are never touched. Every rewritten link is parsed back and must still be one link of its kind naming the same file (an embed of a dotted stem gains `.md`, so `![[v1.5]]` does not turn into an attachment); one that would not is a blocker.
* **The moved note keeps its tier** when its body does not change: attestations bind to content, not to the path, and a pure rename writes its bytes unchanged. Its own relative paths — note links, `raw/` citations, image embeds and reference definitions — are recomputed from the new folder so they name the same files (pointing to a co-moving note's new path when moving a folder subtree); reference definitions elsewhere that name a moved note follow it too.
* **Refusals.** A plan is blocked while any other link would open a different note after the move (a note moved into `wiki/` taking over a bare `[[architecture]]` that opens `projects/apollo/architecture` today), while it would rewrite a `human-reviewed` note the caller has not named in `approve_stale` — the agent must ask the user first — or when a single-note move's `to` names an existing folder without a trailing `/`. A note whose frontmatter does not parse is checked from its raw text: it blocks only if it links to a moved note, and is otherwise reported under `warnings`. A dead link the move would bring to life is reported, not blocked.
* **Apply.** If the dry run's `plan_token` is passed and the recomputed plan differs, nothing is written (`VAULT_CONFLICT`), so what the user approved is what is written. Destinations are written first, so a crash leaves a duplicate rather than a lost note. Where a destination is still another moving note's source — an overlapping subtree shift such as `a/` → `a/b/` — that note is copied to its own destination and its old path retired first, so each collision chain runs from its tail and no note is ever absent from disk. The one retire-first step is a case-only rename, since on a case-insensitive filesystem both spellings are one file. Vacated source directories are pruned after deleting the old paths. Every rewrite carries the concurrency token of what was read, and on a conflict every file already written is restored, byte for byte, from its own token, and folders the move created are removed, so a concurrent edit is never overwritten. One ledger entry records the move.

**Arguments.** The MCP server checks every argument against the tool's `inputSchema` before running it — types, enums, `minimum`, non-empty strings, `minItems`, and array items — so a mistyped argument is `INVALID_ARGUMENT` and never reaches the vault; a JSON `null` means the argument was not given, so a `null` required argument is reported missing rather than failing deeper as `INTERNAL`, and a `null` optional one takes its default (`limit: null` is 20). Every `inputSchema` declares `additionalProperties: false`, and a key a tool does not declare is `INVALID_ARGUMENT` naming it: a misspelt `qurey` silently ignored would turn a typo into a call that succeeds doing something else. A `human:*` `actor` or `by` is still refused as `HUMAN_ATTESTATION_REFUSED` (§3.5), ahead of that check. `vault_scaffold_note` additionally refuses an empty or blank `title` or `description` — the note would fail lint's `FIELD_MISSING` the moment it existed — and an actor outside the §3.4 grammar, since `generated.by` is attribution just as `verified[].by` is.

**Deliberately not exposed to agents:** `sync` (implicit — see §4.3), `lint` (reached through `vault_status` with `lint: true`), `audit`, `init`, and `install` (operator actions). The ledger has no free-text write on either surface: `log.md` records the write tools, and nothing else.

**Retired arguments.** An argument removed from a tool is refused as `INVALID_ARGUMENT` naming its replacement — `vault_ground`'s `cids` (now `targets`), `vault_update_note`'s `append_section` and `replace_section` (now `edits` entries) — before the required-argument check, so a caller following an older vault's habits is told the new name rather than that `targets` is missing. These are messages, never aliases.

### 5.2. MCP Resources (Domain Directives & Configuration)

Standard MCP clients (including Antigravity, Claude Code, and Inspector) query MCP resources to inspect background context without consuming tool execution steps. Cadabby advertises `"resources": {}` capability in `initialize` and exposes two standard URI patterns:

| Resource URI | MIME Type | Description |
| :--- | :--- | :--- |
| **`vault://domains`** | `application/json` | Inventory of all discovered cognitive domains, their descriptions, `allowed_types`, and `require_sources` flags. |
| **`domain://{domain}/directives`** | `text/markdown` | The localized agent instructions extracted from `{domain}/AGENTS.md` (e.g. `domain://customers/directives`). A domain with no directives reads as a placeholder; a domain that does not exist is `-32602`. |

**Transport.** Newline-delimited JSON-RPC — one message per line, no embedded newlines — is the normative MCP stdio framing, and the one every mainstream harness speaks. The server also accepts LSP-style `Content-Length` framing, and **every reply mirrors the framing of the message it answers**. Replying in a framing the client did not send is not a cosmetic defect: up to and including 0.3.1 the server answered every request with `Content-Length` headers, so a standards-conforming client failed to parse the very first `initialize` reply and the vault mounted with no tools at all. One message is at most 16 MiB in either framing. A larger one is answered `-32600` and read away in bounded chunks, and the server keeps serving; up to and including 0.4.2 a `Content-Length` claiming more memory than existed raised `MemoryError` and ended the session without a reply. `Content-Length` may appear anywhere in the header block, case-insensitively. A request whose `method` is not a string is `-32600`; a `tools/call` without a string `name`, or a `resources/read` without a string `uri`, is `-32602`.

### 5.3. CLI Commands

The first eight commands mirror the eight MCP tools one-to-one: `cadabby <verb>` takes the arguments `vault_<verb>` (or `vault_<verb>_note`) does and prints the same result with `--json`. The rest are operator commands no agent needs over MCP (§5.1).

| Command | Description |
| :--- | :--- |
| `cadabby search [query]` | As `vault_search` (`query` optional when at least one filter is given); `--domain`, `--type`, `--trust`, `--status`, `--tag`, `--limit` (at least 1), `--json`. |
| `cadabby ground <targets...>` | As `vault_ground` (notes and `raw/` paths, including `<target>#<heading>` section grounding); `--budget <tokens>`, `--json`. |
| `cadabby status` | As `vault_status`, queues included; `--lint` adds every finding and still exits 0, since findings never fail a status call; `--json`. |
| `cadabby graph <target>` / `cadabby graph --mocs` | As `vault_graph`, `neighborhood` and `mocs` views; exactly one of `<target>` or `--mocs` (exit 2 otherwise); `--limit`, `--json`. |
| `cadabby scaffold <title>` | As `vault_scaffold_note`; `--domain <domain>`, `--path <path>`, `--type <type>`, `--desc <desc>`, `--template <name>`. |
| `cadabby update <path>` | As `vault_update_note`; `--patch-frontmatter`, `--edits`, `--expected-hash`. Like `scaffold` and `move`, records `--actor` if given and `process:cli` otherwise; a terminal is never taken as evidence of a person, and `--actor` must match §3.4 and may not be `human:*` (exit 2), since only `verify --human` records a person. |
| `cadabby move <cid> <to>` | As `vault_move_note`; prints the plan (and its `plan_token`) unless `--apply`; `--approve-stale <cid>` once per note, `--plan-token`, `--actor`, `--json`. A blocked `--apply` exits 3. |
| `cadabby verify <path>` | Appends a verification entry. `--human` requires a TTY and stamps `human:<username>`; otherwise stamps `process:cli`. |
| `cadabby init [path]` | Scaffolds a vault at `path` (default: current directory). Creates `.cadabby.json`, `.gitignore`, `.mcp.json`, `AGENTS.md`, pointer files, `.agents/skills/`, directory tree, seed `index.md`; `--obsidian`, `--obsidian-templates`. Refuses to overwrite existing files without `--force`. |
| `cadabby install` | Harness registration (§7.6). `--antigravity`, `--claude`, `--all` select targets; `--global` installs to the user-level configs instead of the vault workspace; `--vault <path>` binds a global install to one vault; `--path` overrides a single target's destination; `--force` overwrites a conflicting entry Cadabby did not write; `--uninstall`, `--dry-run`. |
| `cadabby mcp` | Runs the JSON-RPC 2.0 stdio MCP server exposing the 8 tools and domain resources. With no `--vault`, resolves the vault per launch (§2.7), which is what makes an unbound global install work. |
| `cadabby lint` | Dynamic six gates across all domains; exit 1 on error-severity findings, for CI and pre-commit hooks. The MCP surface reaches the same findings through `vault_status` with `lint: true`. |
| `cadabby sync` | Forces an incremental scan across all domains and unconditionally regenerates `index.md` (§4.3). `--rebuild` discards `cache.db` first. |
| `cadabby audit` | Git provenance audit of `human:*` endorsements (§3.5). `--require-signed`. |

### 5.4. Error Surface (normative)

A failure is useless to a caller that cannot tell what kind of failure it was. Every error crossing the CLI or MCP boundary therefore carries a **stable code**, a human-readable message, and a **`retryable`** flag meaning an identical request could plausibly succeed later without the caller changing anything. The codes are a public contract; the messages are not, and must never be parsed.

| Code | Retryable | Raised when | Exit |
| :--- | :--- | :--- | :--- |
| `VAULT_CONFLICT` | yes | A read-modify-write found the file had changed since the operation began (§8). | 4 |
| `LOCK_TIMEOUT` | yes | The advisory lock could not be acquired within the timeout. | 4 |
| `CONFIG_INVALID` | no | `.cadabby.json` is malformed or structurally wrong (§2.6). | 3 |
| `FRONTMATTER_UNPARSEABLE` | no | A note's frontmatter falls outside the restricted subset (§3.2). | 3 |
| `FRONTMATTER_UNSERIALIZABLE` | no | A patch value cannot be written inside the restricted subset. | 3 |
| `NOT_FOUND` | no | A vault, note, or source that was addressed does not exist. | 3 |
| `ALREADY_EXISTS` | no | A scaffold or move would overwrite an existing note. | 3 |
| `INVALID_ARGUMENT` | no | An argument is absent, mistyped, outside its enum, or names more than one note. | 3 |
| `HUMAN_ATTESTATION_REFUSED` | no | `by: human:*` was requested over MCP (§3.4). | 3 |
| `PERMISSION_DENIED` | no | The filesystem refused the operation. | 3 |
| `UNKNOWN_TOOL` | no | A `tools/call` named a tool outside the eight (§5.1). | 2 |
| `IO_ERROR` | no | Any other `OSError` — a full disk, a vanished mount. | 5 |
| `INTERNAL` | no | Anything unrecognized. A bug. | 5 |

**`retryable` is the field that matters.** It is the one an agent branches on, and the one prose structurally cannot carry. Exactly two codes set it, and both mean the request was well-formed and lost a race. `HUMAN_ATTESTATION_REFUSED` is deliberately distinct from `INVALID_ARGUMENT` for the same reason: an agent reading "invalid argument" may reasonably try another spelling of the actor, and this refusal is categorical.

**MCP.** Tool failures return `isError: true` with the content being a JSON object `{"error": {"code", "message", "retryable", "tool"}}`. The payload rides in the text content because MCP's tool-result channel has no typed error field — `isError` is a single boolean — so the structure has to go somewhere the caller can still parse. This is separate from the JSON-RPC protocol errors (`-32700` through `-32603`), which govern malformed envelopes rather than failed operations and are unchanged.

`isError` means the call failed, nothing else. Findings describe the vault, not the call, so `vault_status` never sets it however red the vault; an agent branches on `finding_counts.error`. The CLI deliberately differs: `cadabby lint` keeps exit 1 for error findings, because pre-commit hooks depend on it.

**CLI.** Six exit statuses, because each calls for a different reaction from a script: `0` success, `1` the command ran and the vault has findings, `2` the command was invoked wrongly, `3` fix the input or the environment, `4` transient, retry, `5` unexpected. `1` keeps its existing meaning because pre-commit hooks already depend on it, and `2` is not reused for anything else because argparse owns it. A rule between arguments that argparse cannot express — `search` with neither a query nor a filter, `install` with no target — is `INVALID_ARGUMENT`, so `3`.

**Classification is total.** Anything unrecognized becomes `INTERNAL` rather than propagating: an MCP server that dies on a surprise is worse than one that reports the surprise badly, and the caller still learns that the request failed and that retrying it unchanged will not help.

`FRONTMATTER_UNPARSEABLE` is intentionally the same string as the lint code in §6.3. It is one condition observed from two surfaces, and giving it two names would be the defect.

---

**Token budgeting** (`--budget`, `vault_ground`) uses a `len(text) / 4` character heuristic. A zero-dependency engine has no tokenizer; the estimate is documented as approximate, deliberately conservative, and truncates at section boundaries with an explicit `[truncated: N of M sections]` marker rather than mid-prose.

---

## 6. Core Operational Workflows

### 6.1. Ingestion Workflow (Compounding Synthesis)

```mermaid
sequenceDiagram
    autonumber
    actor H as Human
    participant V as Vault (raw/)
    participant A as AI Agent (MCP)
    participant C as Cadabby Engine
    participant W as Vault (wiki/)

    H->>V: Drops source (e.g. raw/paper.pdf)
    H->>A: "Ingest paper.pdf"
    A->>C: vault_status()
    C-->>A: Unprocessed: raw/paper.pdf
    Note over A,V: Agent reads the PDF with its own tools.<br/>Cadabby does not parse binaries (2.4).
    A->>C: vault_search(keywords from paper)
    C-->>A: Existing related entities/concepts
    A->>C: vault_ground(top matches)
    C-->>A: Full text + links + backlinks
    A->>C: vault_scaffold_note("Topic Synthesis", type="synthesis", sources=["raw/paper.pdf"])
    C-->>W: Writes wiki/Topic-Synthesis.md (atomic), logs it
    A->>C: vault_update_note("Existing Entity", edits)
    C-->>W: Updates non-human-reviewed entities with new citations, logs it
    Note over A,V: Agent re-reads raw/paper.pdf and audits every<br/>number, quote, date, and causal claim before attesting.
    A->>C: vault_verify_note("Topic Synthesis", method="cross-source")
    C-->>W: Stamps by: agent:<client_id>, of: sha256:<body hash>
    Note over C,W: The scan behind each tool call regenerates index.md<br/>when it detects changes. No vault_sync call needed (4.3).
```

Ingestion enforces closed-world source grounding: every factual claim in a note citing `sources: ["raw/..."]` must trace directly to those files or explicitly linked vault notes, never to ungrounded parametric model memory. Before calling `vault_update_note` on related notes, the agent checks `trust_tier` and never modifies the body of a `human-reviewed` note without explicit user confirmation (as any body edit mutates `body_hash` and demotes the human attestation to `stale-verified`). When a new `raw/` source contradicts an existing note, the agent surfaces both claims with source attribution rather than silently overwriting the prior text. Finally, `vault_verify_note` is called only after an explicit claim-by-claim verification pass against primary `raw/` sources or `human-reviewed` notes — never in the blind on conversational drafts or notes synthesized solely from `unverified` or `stale-verified` notes.

### 6.2. Query & Answer Stash Workflow

1. Human asks an exploratory or comparative question.
2. Agent calls `vault_search`, then `vault_ground` on the top results to retrieve high-trust concepts with their graph neighborhood.
3. Agent synthesizes a response with `[[wikilink]]` citations, surfacing the `trust_tier` of cited notes (explicitly flagging `unverified` or `stale-verified` notes and stating clearly when the vault has no coverage rather than filling gaps from model memory without disclosure).
4. **Compounding step**: if the synthesis represents enduring, non-trivial knowledge (e.g. a trade-off comparison) and is grounded in vault `raw/` sources or verified notes, the agent calls `vault_scaffold_note(..., type="comparison")` to stash it permanently in `wiki/`, and links it from the relevant MOC so it is not born unfiled (§2.5). Stashed notes remain `unverified` unless verified directly against primary `raw/` sources or `human-reviewed` notes. The log entry is appended automatically, and the scan behind the next call refreshes `index.md` (§4.3).

### 6.3. Epistemic Linting Workflow

`cadabby lint` / `vault_status(lint: true)` executes six deterministic gates across all discovered cognitive domains, skipping `{domain}/AGENTS.md` manifests. Each finding carries a stable code, a severity (`error` | `warning`), a `rel_path`, and a line number where applicable:

1. **Schema Integrity** — frontmatter parses within the §3.2 subset; required fields present (`title`, `description`, `status`, `type`); `status` within enum; timestamps RFC 3339; `tags` within the §3.1 grammar.
   - For `wiki/`, `type` supports open dynamic typing (`allowed_types: None` by default).
   - For custom cognitive domains, `type` is validated against `domain_def.allowed_types` if configured in `{domain}/AGENTS.md` (§2.2). If unconfigured (`allowed_types: None`), any non-empty string type is permitted.
   - Codes: `FRONTMATTER_UNPARSEABLE`, `FIELD_MISSING`, `ENUM_INVALID`, `TIMESTAMP_INVALID`, `TAG_MALFORMED` (error for whitespace, warning for non-canonical case).
   - A `{domain}/AGENTS.md` whose frontmatter does not parse, whose `allowed_types` is not a list of strings, whose `require_sources` is not a boolean, or whose `domain` differs from its folder name is `DOMAIN_MANIFEST_INVALID` (error), reported at the manifest. The domain stays searchable but open, and `scaffold_note` into it is refused until the manifest is fixed.
2. **Flat Wiki & Layout Consistency** — enforces structural discipline across domains.
   - In `wiki/`, all notes must reside directly under `wiki/` with no subdirectories (`WIKI_NESTING_DISALLOWED`).
   - In custom cognitive domains (e.g. `customers/`, `projects/`), arbitrary nested subdirectories (e.g. `customers/acme-corp/README.md`) are permitted and encouraged.
3. **Link Consistency** — every `[[Wikilink]]` and every relative Markdown note link (§4.4) resolves across all domains (`links.target_cid IS NOT NULL`). Wikilink alias and anchor forms are resolved against note CIDs and stems; Markdown note links by exact path, and a dead one names the relative path that would resolve when its stem identifies a note. Anchors of both kinds are additionally checked against target markdown headings, matching the heading text, its space/hyphen-normalized form, or its GitHub-style slug (`#b-tree-indexes-v2` for `## B-Tree Indexes (v2)`). Codes: `LINK_DEAD` (error), `ANCHOR_MISSING` (warning — the target resolves, so the note is reachable and only the jump is wrong).
4. **Provenance Check** — every `sources:` entry must exist under `raw/` (`sources.resolved = 1`). Furthermore, if the note's domain manifest specifies `require_sources: true`, an empty or omitted `sources:` list is flagged as an error. Code: `SOURCE_MISSING`. In-body citations get the same check: a relative Markdown link outside a code span whose path contains a `raw` segment must resolve, from the citing note's folder, to a file under `raw/`. Wikilinks never reach it, URLs are skipped, and a `raw/` link is never a graph edge. When the last `raw` segment onward names a real file, the message suggests the correct relative path, since the usual fault is one `../` too many or too few. Code: `RAW_LINK_BROKEN` (error — the citation a reader follows is dead).
5. **Graph Connectivity** — flags orphan notes (zero inbound and zero outbound links). Evaluated for canonical `wiki/` notes (`WHERE n.layer = 'wiki'`), recognizing cross-domain links from external domains (e.g. a customer note linking to a wiki concept prevents that concept from being flagged as an orphan). Severity `warning`. Code: `NOTE_ORPHAN`. MOC coverage is a distinct concern and is deliberately not a gate: a note that links outward but appears in no Map of Content has edges and is therefore not an orphan, so it is surfaced by `index.md` instead (§2.5).
6. **Verification Integrity** — actor strings match the §3.4 pattern; `of:` present on every entry; stale verifications enumerated as verification debt. Because `verified:` is an append-only audit trail (§3.4), an entry whose `of:` differs from `body_hash` is silent when superseded by a valid current entry of equal or higher authority (`human-reviewed` supersedes all prior entries; `machine-confirmed` supersedes prior machine entries while still warning if a prior `human:*` entry drifted). A note warns at most once, naming its highest-authority stale entry (the latest human one, else the latest) and counting the others: the debt is one re-review however long the history, and a count that grew with the history would reward pruning it. Codes: `ACTOR_MALFORMED` (error), `VERIFICATION_UNBOUND` (error — missing `of:`, or an entry that is not a `by`/`at`/`of` mapping, so the attestation binds to nothing), `VERIFICATION_STALE` (warning).

   Severity across all six gates divides what is broken from what is owed. Exactly four codes are warnings — `NOTE_ORPHAN`, `ANCHOR_MISSING`, `VERIFICATION_STALE`, and the case arm of `TAG_MALFORMED` — and the division is a contract, not a convention. Promoting any of them to an error makes `lint` fail on a vault that is merely incomplete, which is the normal state of one being written to; demoting an error hides corruption. `TAG_MALFORMED` is the only code that is both, because whitespace in a tag is unrepairable while casing is fixed on the next agent write (§3.2).

`cadabby audit` (§3.5) is deliberately a separate command: it shells out to Git, is orders of magnitude slower than the six gates, and should not sit in an agent's inner loop.

---

## 7. Multi-Harness Consumption Architecture

Cadabby operates across heterogeneous agent harnesses without configuration divergence. The governing principle is that **every instruction has exactly one home**, and harness-specific files are shims that point at it.

### 7.1. Single-Definition Invariant

Rule drift across harnesses is the failure mode this section exists to prevent, so the rule applies to personas as strictly as it applies to the constitution:

* **`AGENTS.md` is the sole normative contract.** Practitioner voice, epistemic trust tiers, scaffolding rules, and tool preferences are defined there and nowhere else.
* **`.agents/skills/<persona>/SKILL.md` is the sole definition of each persona.** There are exactly two, named identically in every context: **`librarian`** (ingestion, synthesis, cross-referencing) and **`technician`** (cache health, linting, pre-commit maintenance).
* **Every harness-specific file is non-normative and thin.** `CLAUDE.md`, `GEMINI.md`, Antigravity subagent manifests, and slash commands contain pointers and invocations, never behavior. If a harness file grows prose that could contradict `AGENTS.md`, that is a defect.

A consequence worth stating: a persona must never be defined once in the vault and again in an installed plugin. The Antigravity subagent manifests (§7.2) carry a name, a model preference, and an instruction to load `.agents/skills/<persona>/SKILL.md` from the active vault. They carry no runbook text of their own.

### 7.2. Google Antigravity Plugin

Cadabby bundles a plugin inside the installed package, at
`cadabby/assets/plugins/cadabby/`, so it ships in the wheel and resolves
identically from an installed copy and a source checkout:

```text
cadabby/assets/plugins/cadabby/
├── plugin.json               # Antigravity plugin manifest
├── mcp_config.json           # MCP tool provider configuration
├── skills/
│   └── cadabby-wiki/
│       └── SKILL.md          # Entry-point skill: progressive disclosure, points to AGENTS.md
└── agents/
    ├── librarian/agent.md    # Thin manifest -> .agents/skills/librarian/SKILL.md
    └── technician/agent.md   # Thin manifest -> .agents/skills/technician/SKILL.md
```

**Capabilities.** The `cadabby-wiki` skill injects high-level wiki operations into context without consuming prompt tokens up front. The root agent delegates reading, drafting, and cross-linking to **librarian**, and cache maintenance, diagnostics, and linting to **technician**. Slash commands `/ingest`, `/vault-status`, and `/vault-lint` are available in the chat UI.

> **Verified Architecture (v0.5.0).** The manifest layout conforms to the single-definition invariant (§7.1): entry-point progressive disclosure skill at `skills/cadabby-wiki/SKILL.md` and thin subagent manifests in `agents/` delegating runbook execution directly to `.agents/skills/<persona>/SKILL.md` in the active vault. `cadabby init` scaffolds this plugin directly into `.agents/plugins/cadabby/` to guarantee zero-configuration, vault-scoped MCP auto-mount for Antigravity, symmetric with `.mcp.json` for Claude Code.

### 7.3. Claude Code Integration

1. **Auto-mount via `.mcp.json`** (written by `cadabby init`):

   ```json
   {
     "mcpServers": {
       "cadabby": {
         "command": "/usr/bin/python3",
         "args": ["-m", "cadabby", "mcp"]
       }
     }
   }
   ```

   `init` and `install` prefer the installed `cadabby` console script (`sys.executable` sibling) when present and runnable, falling back to `[sys.executable, "-m", "cadabby", "mcp"]` when running from an editable checkout or when no sibling binary exists. Recording the absolute path ensures agent subprocesses that inherit a minimal `PATH` still resolve the exact environment.

2. **Harness delegation via `CLAUDE.md`.** A ~15-line non-normative pointer instructing Claude Code to read `AGENTS.md` as the single constitution, to prefer native MCP tools over subshell commands, and never to override actor identity when stamping `verified:`. **`init` writes it once and no command ever rewrites it** — users customize `CLAUDE.md` heavily, and an engine that regenerates it will eventually destroy their work. `init --force` is the only path that overwrites, and it says so.

3. **Slash commands in `.claude/commands/`.** `/ingest`, `/vault-status`, `/vault-lint` — the same high-level verbs Antigravity users get.

### 7.4. Keeping Harness Shims From Going Stale

Every harness surface Cadabby writes is a **copy frozen at the version that wrote it**. `init` copies the slash commands into `.claude/commands/`, the personas into `.agents/skills/`, and the Antigravity plugin into `.agents/plugins/cadabby/`; `cadabby install` with no flags does the same, because the default target is the active vault (§7.6). Only one path escapes this — a **bare `--global`** install, which is unbound and therefore symlinks the plugin to the installed package — and it is the exception, not the baseline. The condition is the absence of a vault binding, not the directory the command was run from. Left alone, a fix to `/ingest` would never reach a vault that already exists.

The resolution is to make the shims too thin to need fixing. A command file invokes the CLI or names a persona; it carries no logic:

```markdown
---
description: Ingest an unprocessed source from raw/ into the wiki
---
Follow the librarian runbook at `.agents/skills/librarian/SKILL.md`,
then run the ingestion workflow for: $ARGUMENTS
```

Behavior lives in `.agents/skills/` and `AGENTS.md`, both of which are vault content the user can update by pulling.

**How to call a tool lives in the tool's MCP description, never in `AGENTS.md`.** `AGENTS.md` is user-owned (table below), so whatever it says about the tool surface is frozen at the version that wrote it: every 0.3.x vault still tells its agents to call `vault_lint`, and none of them mention `vault_move_note`. Tool descriptions are the one surface every harness shows the model and the running engine always sends, so per-tool guidance — parameters, refusals, the `raw/` search fallback, what `budget_tokens` caps, the move-consent step — goes there. `AGENTS.md` keeps only rules that span tools, and names tools without showing how to call them. MCP's optional `instructions` field in the `initialize` reply was evaluated for this role and rejected: Antigravity accepts it and does not surface it to the model.

**`AGENTS.md` carries only rules every task needs; task procedures live in the persona runbooks.** Harnesses load `AGENTS.md` into every session, so each line in it is paid for on every turn, while a runbook is read only when its task comes up. The constitution therefore keeps the trust rules, the consent and write-path rules, and the offline fallback, and routes by task to `.agents/skills/librarian/SKILL.md` and `.agents/skills/technician/SKILL.md` for everything else: raw-citation syntax, supplements, domain manifests, debt explanations, lint repairs. The routing must name the runbook paths, because not every harness discovers `.agents/skills/` on its own. The split also moves the procedures from a user-owned file to engine-owned ones, so `install` delivers improvements to them in existing vaults.

**The thin-shim rule earns a refresh command.** Because a shim carries no user content, the engine may rewrite one at any time without asking, and `cadabby install` does exactly that: it re-syncs every engine-owned file in the vault — the slash commands, the persona runbooks, and the plugin tree — writing only the files whose bytes differ (§7.6). This divides vault files into three categories, and the division is normative:

| | files | written by |
|---|---|---|
| **Engine-owned** | `.mcp.json`, `.claude/commands/*`, `.agents/skills/*`, `.agents/plugins/cadabby/*` | `init`, and `install` on every run |
| **User-owned** | `.cadabby.json`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `STYLE.md`, `.obsidian/*`, `templates/*` | `init` once; never by `install` |
| **Generated** | `index.md`, `log.md`, `log/*.md`, `.cadabby/*` | `init` seeds; thereafter any scan or write that changes the vault (§2.1, §4.3) |

The third row is not a kind of ownership but its absence: these files have no authored content to protect, so hand-edits to them are not preserved and `--force` has nothing to do with them. The distinction that matters for `install` is the first two rows, since generated files are regenerated regardless of what `install` does.

`cadabby init --force` is **not** the way to refresh shims, and must not be described as one. It crosses the line in the table: it rewrites the user-owned documents along with the engine-owned ones, resetting the vault's domain definitions, style rules, and constitution to the stock templates. Notes and the ledger survive; nothing else that distinguishes the vault does. It remains available as a deliberate reset, and §7.3's guarantee that `CLAUDE.md` is never regenerated is scoped to exactly that exception.

**A shim is safe because it names behavior instead of containing it, not because it is short.** The distinction matters, because one shim bends the rule: the plugin's `skills/cadabby-wiki/SKILL.md` enumerates the eight MCP tool names rather than pointing at them. That enumeration is a frozen copy of the tool surface, and it is tolerable only because §5.1 fixes that surface at exactly eight tools. The two sections are therefore load-bearing for each other: **changing the 8-Tool invariant silently invalidates the tool list in every vault initialized before the change**, with no error anywhere to connect the two. Any future addition to §5.1 must update this shim in the same change. For the same reason `plugin.json`'s version string is pinned to the package version and checked by test, rather than trusted to be kept in step by hand.

### 7.5. Obsidian Configuration (optional)

`init` can write a minimal `.obsidian/app.json`. Five key behaviors and constraints:

* **Attachment routing defaults to `raw/attachments/`.** Sets `"attachmentFolderPath": "raw/attachments"` and `"newLinkFormat": "relative"`. Any screenshots, PDFs, or media pasted or dropped into Obsidian automatically land in Layer 1 (immutable raw evidence) rather than polluting vault root or cognitive domains.
* **`.gitignore` must exclude `.obsidian/workspace*.json`.** Obsidian rewrites workspace state constantly, and committing it produces a diff on every session.
* **The core Templates plugin's folder is excluded from note discovery, and its bodies are available to `scaffold_note`.** A template is a *pre-note*: valid only once its placeholders are substituted. `title: {{title}}` is a flow mapping and `tags: []` a flow sequence, both outside the restricted subset (§3.2), so a template folder walked as a cognitive domain reports `FRONTMATTER_UNPARSEABLE` against every template it holds and `lint` exits non-zero for as long as the user keeps using the plugin. The folder is therefore excluded (§2.2). Foam needs no equivalent rule — its `.foam/templates/` is dot-prefixed and already invisible to domain discovery.

  Which folder it is comes from reading `.obsidian/templates.json`, never from mirroring the setting into `.cadabby.json`. A duplicated setting goes stale the moment the folder is renamed in Obsidian, and the symptom — lint turning red again — points nowhere near the cause. The file is read defensively: an absent, malformed, empty, absolute, or vault-escaping value excludes nothing. Failing safe costs a lint finding that names the offending template, which is diagnosable; failing loudly would let a third-party config brick the vault. Exactly one command writes this file, under the conditions in the next bullet; every other code path only reads it.

  `vault_scaffold_note` and `cadabby scaffold` accept a `template` naming a file in that folder, substitute `{{title}}`, and use the result as the note body. This is what keeps the two authoring paths convergent: a human pressing *Insert template* in Obsidian and an agent calling the tool produce the same note shape. **Only the body is taken.** A template's own frontmatter is discarded, because OKF frontmatter is engine-owned — `generated`, `verified`, and the body hash they bind to are epistemic infrastructure (§3.4), and a template able to set them could mint notes whose trust tier asserts more than the vault can back, by typo rather than by intent. `{{date}}` and `{{time}}` are deliberately not substituted: the plugin stores their formats as moment.js tokens, converting those to `strftime` is a parser nobody needs, and Obsidian already renders them correctly on its own side.

* **`init --obsidian-templates` writes starter templates, and is opt-in because it reserves a folder.** The flag implies `--obsidian`, creates `templates/`, populates it with a `concept` and a `moc` starter, and declares it in `.obsidian/templates.json` — the one write described above. Doing this by default would reserve `templates/` in every Obsidian vault, and §2.2's rule that any non-reserved top-level directory is a cognitive domain means a user who wanted a real `templates/` domain would lose it with no error at all. That silent loss is the whole reason the folder is read from the user's setting rather than defaulted, so the engine must not re-introduce it from the other side.

  Three rules keep the two halves consistent. **An existing declaration always wins**: a vault already pointing Obsidian at `meta/templates` gets its starters there and keeps its settings file byte-for-byte, under `--force` as well — resetting the setting while the starters went elsewhere would leave the declared folder empty and the populated one a live domain full of pre-notes. **The folder is never declared empty**: the setting is written only together with the files it names, so the reservation never outlives its purpose. And **a `templates/` that already holds notes is refused** with a message naming the collision, because declaring it is precisely the silent domain loss above.

  The starters are **user-owned** (§7.4), written once and never refreshed by `install`. A template carries content rather than naming behavior, so unlike a shim, regenerating one destroys an edit.

  They set `type`, `title` via `{{title}}`, and `status`, and deliberately **leave `description` empty**. The core plugin has no input prompt — that is Templater and Foam territory — so the field cannot be templated, and a note inserted from a starter therefore fails `lint` with `FIELD_MISSING` naming the note until a human writes one. That is the intended outcome, not a rough edge: a placeholder such as `TODO` would lint clean and quietly seed a required epistemic field, one that feeds BM25 ranking and `index.md`, with filler that nothing would ever flag. A loud error is the honest report of an undescribed note. For the same reason no starter carries `verified` or `generated`, which would let every hand-created note claim an attestation nobody made (§3.4).

* **Trust-tier graph coloring is deliberately not offered.** Obsidian colors graph nodes from stored properties, but `trust_tier` is *derived* from the `verified` array and the current body hash (§3.4) and never written to the file, so there is nothing for Obsidian to query. Coloring the graph by tier would require the engine to maintain a real `trust/<tier>` tag in every note's frontmatter.

  A materialized tier is correct only until the next body edit. The hash moves, the true tier drops to `stale-verified`, and the frontmatter goes on asserting `trust/human-reviewed` until something runs a sync — claiming human review of text no human has read, which is the single thing §3.4 exists to prevent, committed by the engine rather than by an agent. `index.md` is derived state that Cadabby *does* materialize (§2.5), and the contrast is the rule: a stale gap report under-states outstanding work in an engine-owned file that announces itself as generated, while a stale trust tag over-states confidence inside user-owned frontmatter. Materializing derived state is acceptable when going stale fails safe.

  No query is a substitute either. Graph color groups take search queries, so a group can match notes carrying a `verified` block at all, but no query can distinguish a current attestation from a broken one — that comparison *is* the hash check. The available choices are a two-way attested/unattested split that writes nothing, or no coloring. A `materialize_trust_tags` flag sat in the `obsidian` section of `.cadabby.json` up to and including 0.3.1, type-checked on load and read by no code path; it was removed, and §2.6's permissive merge means vaults that still carry it load without complaint.

### 7.6. Harness Registration (`cadabby install`)

By default, `cadabby install` configures the **active vault workspace** (`.mcp.json` for Claude Code and `.agents/plugins/cadabby` for Antigravity), ensuring harness integrations are bound and refreshed for the current vault without polluting the user's global environment. For multi-project universal access, `--global` targets the user-level configuration directories (`~/.claude.json` and `~/.gemini/antigravity/plugins/cadabby`); because that configures the user environment rather than a vault, it writes no vault files.

A global install comes in two modes, and the difference is whether a vault is **named**, never where the command was run from:

| | `cadabby install --global` | `cadabby install --global --vault X` |
|---|---|---|
| MCP entry | unbound (`cadabby mcp`) | bound (`cadabby mcp --vault X`) |
| plugin | **symlink** to the installed package | copy |
| vault resolved | per launch, via `$CADABBY_VAULT` or the working directory (§2.7) | always X |
| stays current | yes, the symlink tracks upgrades | only via `install` re-runs (§7.4) |

Bare `--global` must **not** adopt a vault discovered from the working directory. Doing so bakes one absolute path into a configuration that every workspace on the machine reads, so opening a second vault would silently operate on the first — the exact opposite of the universal access `--global` exists to provide. Standing inside a vault is not a request to bind to it; `--vault` is.

The two modes are exclusive by construction rather than by policy: `mcp_config.json` lives *inside* the plugin directory, so a bound install has nowhere to write it unless that directory is a real copy. **Binding costs the symlink**, which is why it is opt-in and why the bare form is the default.

On a freshly scaffolded vault `install` is a no-op — `init` already registered both harnesses. **`install` is the re-run verb**, and it exists because two things go stale that `init` will not repair: the recorded interpreter path, which is absolute (§7.3) and therefore dies when a venv is rebuilt or Python is upgraded, and the engine-owned shims, which were copied at the version that scaffolded the vault. In the default vault-bound mode it re-syncs every engine-owned file in the table in §7.4.

```bash
cadabby install --all              # configure local vault workspace for all harnesses (default)
cadabby install --claude           # register Cadabby in vault workspace .mcp.json
cadabby install --antigravity      # configure vault workspace .agents/plugins/cadabby
cadabby install --all --global     # register globally across user home configs
cadabby install --claude --uninstall
cadabby install --all --dry-run    # print every file that would change, write nothing
```

Registration edits configuration files the user owns, so the merge semantics are the feature, not an afterthought:

* **Idempotent.** Running `install` twice is a no-op. The implementation compares desired state to actual and writes only on difference.
* **Non-destructive merge.** Target configs are parsed, the `cadabby` key is updated in place, and everything else is preserved byte-for-byte where the format allows: the file's own indent (or compact one-line form), raw non-ASCII, and presence or absence of a trailing newline are kept, so a file written by JavaScript's `JSON.stringify` — how Claude Code writes `~/.claude.json` — differs only in the `cadabby` entry. A config rewritten by another program between Cadabby's read and its write is refused with the retryable `VAULT_CONFLICT` rather than overwritten (§8). Cadabby never rewrites a config it failed to parse, and never truncates one it cannot write atomically (§8).
* **Conflict-aware, but not about its own output.** An existing `mcpServers.cadabby` entry pointing somewhere else is reported and left alone unless `--force` is passed. An entry that Cadabby itself wrote **for the same vault** is not a conflict, even when it differs: its only defect is naming an interpreter that no longer exists, and refreshing it is the command's primary purpose. Treating that as a conflict would gate the repair behind `--force` and label Cadabby's own dead entry as somebody else's property. An entry bound to a *different* vault, or one Cadabby did not write, still requires `--force`.
* **Path-overridable, for one target at a time.** `--path` overrides the install destination. Another product's config directory is not a stable interface, so the default is a detection attempt with a clear error — never a silently hardcoded path. The two harnesses take destinations of **different kinds** — Antigravity a plugin directory, Claude a JSON config file — so no single value of `--path` is correct for both: combining it with `--all` is rejected before anything is written, and the user runs the command once per harness. Each target additionally checks that its destination is of the right kind before writing, so a wrong `--path` produces a named conflict rather than an errno from inside a copy or a parse. These checks run under `--dry-run` too; a preview that reports success for an install that cannot succeed is worse than no preview. Neither check is overridable by `--force`, which means "overwrite the Cadabby thing you found," not "delete whatever unrelated file happens to sit here."
* **No hard CLI dependency.** `--claude` writes the config file directly rather than requiring the `claude` binary. If a harness CLI is present it may be used; if absent, `install` still succeeds.
* **Uninstallable.** `--uninstall` removes exactly what `install` added, leaving user keys intact. An `mcpServers.cadabby` entry Cadabby did not write — one `install` would have refused as a conflict — is left in place unless `--force` is passed.

---

## 8. Durability & Concurrency

A vault is a directory in Git that a human in Obsidian and one or more agents may touch at once. The invariant is that **no interleaving leaves a file half-written**.

* **Atomic writes.** Every file mutation writes to a temporary file in the *same directory*, `fsync`s it, `os.replace()`s it over the target, and `fsync`s the directory so the rename itself survives a power cut. The temporary name is truncated to fit `NAME_MAX`, so a note whose own name is near the limit can still be edited. A crashed or killed Cadabby never produces a truncated note. This applies to harness config files written by `install` as much as to vault notes. The temporary file is created `0600` and given the target's mode (or the ordinary `0644` less umask for a new file) only just before the replace, so a private file such as `~/.claude.json` is never world-readable mid-write.
* **Write containment.** A vault is a folder anyone can hand you — a clone, a template, a synced share — so every write is judged by where it would land, symlinks followed: a note, `log.md`, `log/`, `index.md`, `.cadabby/` and every file `init`/`install` writes inside the vault must resolve inside the vault root, or the write is refused as `PERMISSION_DENIED` and nothing is written. A symlink that stays inside the vault is written through, but a note path that is a link (even a dangling one) must land where a note may live, so `wiki/x.md -> ../raw/p.md` is refused. Reads are deliberately not contained: a folder you symlinked in from elsewhere (`docs -> ~/Documents/notes`) is indexed and searched under its in-vault name. Up to and including 0.4.2, `log.md -> ~/.bashrc` turned every ledger append into a line of the user's shell startup file.
* **Read-modify-write safety.** `update`, `move`, and `verify` re-read the file and recompute its hash immediately before replacing it; if the on-disk content changed since the operation began — or the file was deleted — the write aborts with `VAULT_CONFLICT` rather than clobbering a concurrent edit or resurrecting a deleted note.
* **Ledger appends.** `log.md` is appended with a single `write()` to a descriptor opened `O_APPEND` (and `O_NOFOLLOW` where the platform has it), which is the one case where no locking is required. Every entry is exactly one line, its actor matching §3.4: a note name containing a newline is flattened rather than allowed to forge a second entry. (Atomicity holds for appends below the platform's pipe-buffer size; entries are single lines, far below it.)
* **Index regeneration.** `index.md` is regenerated under an advisory lock (`.cadabby/vault.lock`, created `O_CREAT | O_EXCL`, holding pid and start time, treated as stale after 60 seconds, once its pid is gone, or if its timestamp is more than 5 seconds in the future, which no live holder's clock wrote). A lock whose content cannot be read — empty because its creator has not written it yet, or garbage — is stale only once its mtime is older than that, and an owner `os.kill` answers with `EPERM` is alive, merely another user's. Breaking a stale lock, and releasing one's own, first moves the file aside and removes it only if it still holds the exact bytes judged, putting it back otherwise, so a lock re-acquired in between is never deleted. Rows are emitted in a **deterministic sort order** and the file is **written only if its bytes changed** — otherwise every sync churns the Git diff for nothing.
* **Cache concurrency.** WAL mode plus a 5-second busy timeout. Two processes scanning simultaneously is correct but wasteful; it is not an error.
* **Log rotation.** When `log.md` exceeds `log_rotate_bytes` (default 256 KB) or crosses a calendar year, `sync` moves it to `log/<year>.md` and starts a fresh ledger. An unbounded ledger eventually becomes a file agents waste their whole context window reading. Because appends take no lock, the live ledger is renamed aside *before* it is archived: an append racing the rotation lands in a fresh `log.md` (or, through a descriptor opened before the rename, in the file being archived, which is re-read once), and nothing overwrites it. If such an append creates the new ledger first, it starts without the year heading rather than lose the entry.

---

## 9. Codebase Packaging & Repository Structure

```text
cadabby/
├── pyproject.toml              # Build config (flit), zero runtime deps, requires-python = ">=3.11"
├── uv.lock                     # Dev environment only; the distributed package has no dependencies
├── README.md                   # Quickstart, installation, and harness usage
├── LICENSE                     # MIT
├── SPECIFICATION.md            # This document
├── .github/workflows/tests.yml    # unittest on Python 3.11 / 3.12 / 3.13; gates publish
├── .github/workflows/publish.yml  # Tag push (v*) -> tests -> PyPI trusted publishing
├── .agents/skills/release/        # Maintainer release runbook (SKILL.md, scripts/gh_run_status.py)
│
├── src/
│   └── cadabby/
│       ├── __init__.py         # Package exports & version
│       ├── __main__.py         # Entry point for python3 -m cadabby
│       ├── cli.py              # argparse surface and terminal formatting
│       ├── constants.py        # Enums, default config, schema constants
│       ├── domain.py           # Note entity, cognitive domain definitions, AGENTS.md parsing (2.2)
│       ├── errors.py           # Error taxonomy, classify(), exit-code map (5.4)
│       ├── vault.py            # Root discovery, config loading, path/cid mapping
│       ├── frontmatter.py      # Restricted-subset parser + canonicalizing writer (3.2)
│       ├── okf.py              # Schema validation, body hashing, trust tier derivation (3.3-3.4)
│       ├── fsutil.py           # Atomic replace, advisory lock, O_APPEND ledger writes
│       ├── cache.py            # SQLite connection, FTS5 probe, scan/upsert/delete, search
│       ├── graph.py            # Wikilink and Markdown note-link extraction, resolution, backlink queries
│       ├── indexer.py          # index.md gap report (2.5) and log.md append/rotate
│       ├── lint.py             # The dynamic six gates across all domains (6.3)
│       ├── audit.py            # Git blame provenance audit (3.5)
│       ├── ops.py              # scaffold, update, move, verify, ground — use cases, written to ports (9.1)
│       ├── installer.py        # cadabby init / install, idempotent config merge (7.6)
│       ├── mcp.py              # JSON-RPC 2.0 stdio handler, 8 tools + domain resources
│       │
│       ├── ports.py            # Driven-port Protocols: NoteStorage, IndexCache, Ledger
│       ├── adapters/
│       │   ├── disk_storage.py   # DiskNoteStorage, FileLedger — production adapters
│       │   └── memory_storage.py # InMemoryNoteStorage, InMemoryLedger — hermetic doubles
│       │
│       └── assets/             # Inside the package, so it ships in the wheel
│           ├── vault/          # Vault starter template
│           │   ├── .cadabby.json
│           │   ├── .gitignore
│           │   ├── .mcp.json
│           │   ├── AGENTS.md
│           │   ├── CLAUDE.md
│           │   ├── GEMINI.md
│           │   ├── STYLE.md
│           │   ├── index.md
│           │   ├── obsidian/
│           │   │   ├── app.json
│           │   │   └── templates.json
│           │   └── templates/
│           │       ├── Concept.md
│           │       └── MOC.md
│           ├── skills/         # CANONICAL persona runbooks (7.1)
│           │   ├── librarian/SKILL.md
│           │   └── technician/SKILL.md
│           ├── commands/       # Thin slash-command shims, harness-agnostic (7.4)
│           │   ├── ingest.md
│           │   ├── vault-status.md
│           │   └── vault-lint.md
│           └── plugins/cadabby/  # Antigravity plugin, assembled from the above (7.2)
│               ├── plugin.json
│               ├── mcp_config.json
│               ├── skills/cadabby-wiki/SKILL.md
│               └── agents/
│                   ├── librarian/agent.md
│                   └── technician/agent.md
│
├── examples/
│   └── demo-vault/             # Golden fixture: used by tests and the README
│
└── tests/
    ├── helpers.py              # Shared test fixtures, mock helpers, and demo-vault copy
    ├── run_parallel.py         # Zero-dependency parallel test runner (process pool)
    ├── test_fsutil.py          # Atomic durability (tempfile + fsync), locks, O_APPEND ledger
    ├── test_vault.py           # Vault root resolution (2.7), config deep merge, path/CID mapping
    ├── test_frontmatter.py     # Subset parse/write boundary, round-trip fidelity (3.2)
    ├── test_okf.py             # Body hashing, actor/timestamp validation, tier derivation (3.1, 3.3-3.4)
    ├── test_cache.py           # Scan, deletion reconciliation, FTS5 ranking sign & integrity
    ├── test_graph.py           # Alias/anchor/path wikilink + Markdown note-link resolution, target index
    ├── test_indexer.py         # index.md gap report, MOC reachability, log rotation
    ├── test_lint.py            # Each gate's codes and severities
    ├── test_errors.py          # Code taxonomy, retryability, MCP payload, exit codes (5.4)
    ├── test_ops.py             # Scaffolding, attribution, conflict detection, atomicity
    ├── test_move.py            # vault_move_note: link rewriting, trust impact, blockers, rollback
    ├── test_audit.py           # Git blame provenance audit for human:* attestations (3.5)
    ├── test_adapters.py        # Driven secondary storage adapters (DiskNoteStorage, FileLedger) (9.1)
    ├── test_domain_model.py    # Note entity and use cases driven through in-memory adapters
    ├── test_domains.py         # Multi-domain discovery, cache, linting, ops, MCP resources
    ├── test_templates.py       # Template folder exclusion, scaffold from template, starters (7.5)
    ├── test_mcp.py             # Handshake, tool execution, human:* refusal
    ├── test_cli.py             # Command surface, flags, JSON output
    ├── test_installer.py       # init scaffolding, idempotency, merge, uninstall, dry-run
    ├── test_run_parallel.py    # Parallel test runner target discovery, worker dispatch, and CLI
    └── test_acceptance.py      # The §10 criteria and TestAcceptanceTraceability
```

**Assets live inside the package, not beside it.** `src/cadabby/assets/` is the single on-disk home for the vault template, the canonical persona runbooks, the command shims, and the assembled Antigravity plugin (§7.1); `cadabby init` copies from it and the plugin resolves out of it, so neither keeps a second copy. Its position *inside* `src/cadabby/` is a packaging requirement rather than a filing preference: anything outside the package directory is absent from the built wheel, which would leave `init` and the plugin working from a source checkout and broken from `pip install`. The same reasoning places the plugin under `assets/plugins/` instead of at the repository root (§7.2).

### 9.1. Ports and Adapters

`ops.py` — the use-case layer behind both the CLI and the MCP server — is written against three `typing.Protocol` interfaces in `ports.py` rather than against the filesystem directly:

| Port | Production adapter | Test adapter |
|---|---|---|
| `NoteStoragePort` | `DiskNoteStorage` | `InMemoryNoteStorage` |
| `LedgerPort` | `FileLedger` | `InMemoryLedger` |
| `IndexCachePort` | `VaultCache` (§4) | — |

Callers may inject adapters; omitting them falls back to the disk pair, so the ordinary code path is unchanged and nothing is wired by configuration. The payoff is that scaffolding, update, move, verification, and attribution logic can be exercised with no vault on disk, no SQLite file, and no temporary directory — the behavior under test is the use case itself rather than the use case plus the filesystem. `Protocol` is used instead of `abc.ABC` because it imposes no runtime base-class requirement and costs nothing at import time, which matters for a CLI that an agent may invoke many times in a session.

The boundary is deliberately partial. It covers note persistence and the ledger, which are the operations that need substitution for testing and are plausible extension points. It does not cover the cache: `VaultCache` satisfies `IndexCachePort` structurally but has no second implementation, because a cache that is explicitly disposable (§4.2) gains nothing from being swappable. Ports are justified where a second implementation already exists or is clearly coming; elsewhere they are indirection with no reader.

---

## 10. Acceptance Criteria

Everything above states what is true of a conforming Cadabby. This section states **how you would find out it was not** — one falsifiable observation per criterion, phrased as something you can go and do to a vault.

That phrasing is a rule, not a style. A criterion that restates its requirement becomes a second normative copy, and two copies drift: C17 once promised an `index.md` with no tables while §2.5 required the entry-point table to always render, both marked normative, and the contradiction surfaced only when a test failed. The **Defined in** column carries the pointer so the prose does not have to repeat the rule. If a criterion below reads like a requirement rather than an experiment, that is the defect to fix.

A note on scope: the five-phase build order that used to open this section was removed at 0.3.1. It described the order in which already-shipped code was written, nothing checked it, and it was the one part of this document that could rot undetectably. Git history has it.

### Criteria for 0.5.0

* **C1.** Deleting `.cadabby/` and re-running any command reproduces byte-identical search results across all cognitive domains.
* **C2.** Editing a verified note's body downgrades it to `stale-verified` on the next scan, and `status` counts it as verification debt.
* **C3.** Point `vault_update_note` at a note whose frontmatter uses flow style: the call fails, the file's bytes are unchanged, and `lint` names it `FRONTMATTER_UNPARSEABLE` with the offending line.
* **C4.** `lint` exits non-zero on a demo vault seeded with one instance of each error code, and zero on the clean demo vault.
* **C5.** Create `customers/` with a note in it and nothing else: it is searchable on the next command, with no registration step. Add `customers/AGENTS.md` declaring `allowed_types` and `require_sources: true` and both begin to bind in that domain and in no other.
* **C6.** Scaffold `type: account` into a domain whose manifest allows it and the note appears at the requested path; scaffold the same type into one that does not and the call is refused, naming the permitted set. Scaffold with a `path` under `raw/`, a dot-folder, or a nested `wiki/` folder and the call is refused with nothing written.
* **C7.** Ground one `customers/` note and one `wiki/` note in a single call: the payload carries the customers directives verbatim against the first entry and not against the second.
* **C8.** A term appearing only in an uncited `raw/` file returns nothing until `domain: "raw"` is passed. A term appearing in two cognitive domains returns both when `domain` is omitted, and only one when it is given.
* **C9.** With `wiki/Architecture` and `projects/apollo/Architecture` both present, `[[Architecture]]` resolves to the wiki note, and `[[apollo/Architecture]]` is the form that reaches the other. Neither is reported as a dead link. A qualified target with a typo (`[[apollo/Architcture]]`) is `LINK_DEAD`, not resolved to the wiki note by its stem.
* **C10.** Count the entries `tools/list` returns: eight, and the same eight the plugin's frozen `cadabby-wiki/SKILL.md` names; every tool count stated in README, AGENTS.md, this spec or a shipped asset is that same number. `initialize` advertises `"resources": {}`, and `resources/list` then `resources/read` serve `vault://domains` and a `domain://{domain}/directives` for each discovered domain.
* **C11.** `wiki/sub/Note.md` raises `WIKI_NESTING_DISALLOWED`; `customers/acme/Q3/Note.md` at the same depth raises nothing. A customer note linking to an otherwise-isolated wiki concept keeps that concept off gate 5's orphan list. Both hold when the nested note's frontmatter does not parse: it reports `FRONTMATTER_UNPARSEABLE` and `WIKI_NESTING_DISALLOWED` together.
* **C12.** Call `vault_verify_note` with any argument shaped to produce `by: human:*`: it fails with `HUMAN_ATTESTATION_REFUSED` and the note's bytes are unchanged. No MCP argument reaches the human tier. Run `cadabby verify` on a TTY with no flag: the entry is `process:cli`. Add `--human` and answer the prompt with anything but the printed token: the command exits 3 (`INVALID_ARGUMENT`) and the bytes are unchanged. No surface, MCP or use case, reports the refusal as `PERMISSION_DENIED`.
* **C13.** `cadabby audit` detects a `human:owner` entry introduced by a commit authored by an unmapped email.
* **C14.** A note renamed on disk leaves no stale row in `notes`, `links`, `sources`, or either FTS index, and `INSERT INTO <index>(<index>, rank) VALUES('integrity-check', 1)` passes afterward for both `notes_fts` and `raw_fts` (§4.2). Change a row's body with a raw `UPDATE notes` that skips retraction and the same check fails.
* **C15.** A `human-reviewed` note outranks an identically-matching `unverified` note, and a `deprecated` one is pushed below both.
* **C16.** `cadabby sync` run twice in a row produces no Git diff on the second run.
* **C17.** On the clean demo vault, `index.md` names every MOC and no filed note. Add one unfiled note, one uncited `raw/` file, and one note edited since its attestation: exactly three sections appear. Resolve all three and the file returns to entry points alone. A vault with MOCs and no gaps renders its entry points followed by the "Nothing outstanding" line.
* **C18.** Link a note listed under Unfiled from any MOC: the next scan drops it, including when the link runs through two intermediate notes. A note that links outward but sits under no MOC stays listed while `lint` reports no `NOTE_ORPHAN` for it.
* **C19.** After `init`, a full ingestion cycle, and `sync`, list every file under `customers/` and `projects/`: each one was written by an agent or a human. No `{domain}/index.md`, and nothing else engine-owned.
* **C20.** Drop a file into `raw/` and run any command that scans — CLI, MCP tool, or `sync`: `index.md` lists it afterward, with no explicit sync call. Run the same command again with nothing changed and `index.md`'s bytes and mtime are untouched, the report never having been built.
* **C21.** `cadabby init` into a directory with an existing `CLAUDE.md` leaves that file untouched and says so.
* **C22.** `cadabby install --all` run twice changes no bytes on the second run; `--uninstall` restores the pre-install config exactly. Corrupt an installed `mcp_config.json` and re-run with `--force`: the install is refused and the file's bytes are unchanged. Point `--antigravity --path` at a directory that is not a Cadabby plugin and run a `--force` install or `--uninstall`: both are refused and the directory is untouched. Run `--claude --uninstall` over a `cadabby` entry Cadabby did not write: refused without `--force`, bytes unchanged. Install into a config indented with four spaces, a tab, or nothing, carrying non-ASCII keys: only the `cadabby` entry changes. Rewrite the config between install's read and its write: `VAULT_CONFLICT`, and the rewrite survives.
* **C23.** Grep any distinctive sentence from a persona runbook across the repository: it occurs once. The plugin's subagent manifests name `.agents/skills/<persona>/SKILL.md` instead of repeating it.
* **C24.** Call an MCP tool with a required argument omitted, then with a stale `expected_hash`: the first reports `INVALID_ARGUMENT` naming the field, the second `VAULT_CONFLICT` with `retryable: true`. No failure in either surface produces a code absent from §5.4's table, and only the two race codes say retry.
* **C25.** Point `.obsidian/templates.json` at a folder and put a stock Obsidian template in it: `lint` stays clean and no domain is reported for that folder. Delete the settings file and the same folder becomes an ordinary cognitive domain again. Nest it one level down and its parent keeps every note but the templates. Scaffold with `template:` and the note carries the template's headings with `{{title}}` substituted, engine-generated frontmatter, and nothing from the template's own.
* **C26.** Run `init --obsidian`: no `templates/` and no `templates.json` appear. Run `init --obsidian-templates` instead and both do, the folder it declares is the folder discovery skips, and `lint` is clean. Copy a starter into `wiki/` with `{{title}}` substituted and the only error is `FIELD_MISSING` for `description`. Re-run the flag over an edited starter and the edit survives; re-run it against a vault that already declares another folder, with or without `--force`, and the starters land there while the settings file is untouched; run it where `templates/` already holds a note and the command refuses, writing no settings file.
* **C27.** Send `initialize` to `cadabby mcp` as one JSON line: the reply is exactly one JSON line with no header. Send it `Content-Length`-framed: the reply is framed the same way.
* **C28.** Call `vault_update_note` with three `edits` whose third `replace_text` matches nothing: the file's bytes and `log.md` are unchanged. Fix the third and repeat: one write, one ledger line. Aim a `replace_text` at a string that occurs only in the frontmatter and it is refused.
* **C29.** In `customers/acme/Deal.md` write `[Arch](../../wiki/Architecture.md)`: grounding `wiki/Architecture` lists the deal as a backlink of kind `markdown`, and `wiki/Architecture` is not `NOTE_ORPHAN`. Change the link to `../wiki/Architecture.md` and `lint` reports `LINK_DEAD` suggesting `../../wiki/Architecture.md`; a link to `../STYLE.md` or a domain's `AGENTS.md` is reported as nothing.
* **C30.** Write a note whose `title` and `sources` contain `[`, `{`, `: `, ` #`, quotes, U+2028 or a form feed: it parses back to the same values. A bare `- https://example.com` sequence item stays a string, and a top-level `title:Foo` is `FRONTMATTER_UNPARSEABLE`.
* **C31.** Save a note with CRLF line endings: `verify` and `update` both succeed without `VAULT_CONFLICT`. Patch its frontmatter: every line still ends in CRLF and the blank line after the closing fence remains. Save one with a leading UTF-8 BOM: it parses, and an `update` leaves exactly one frontmatter block with its fields intact. Delete a note between `update`'s read and its write: the write fails with `VAULT_CONFLICT` and the note stays deleted.
* **C32.** Leave a stale lock that the engine cannot remove: the next write raises `LOCK_TIMEOUT` within its timeout rather than waiting forever.
* **C33.** Cite `raw/later.md` before the file exists: `lint` reports `SOURCE_MISSING`. Drop the file in and, with the citing note untouched, the finding clears. A note citing `./raw/x.md` counts `raw/x.md` as processed in both `index.md` and `vault_status`.
* **C34.** `vault_status.broken_links` equals the number of `LINK_DEAD` findings `lint` reports for the same vault.
* **C35.** Write `customers/AGENTS.md` with a flow-style `allowed_types`: `lint` reports `DOMAIN_MANIFEST_INVALID` at that file, and scaffolding into `customers` is refused until it is fixed.
* **C36.** Set a ranking multiplier to `NaN`, `Infinity`, `"2.0"`, or `true` in `.cadabby.json`: every command fails with `CONFIG_INVALID` naming that multiplier. A `search` whose database is locked fails as a lock, never as zero results.
* **C37.** On a TTY, run `scaffold` and `update` without `--actor`: the note's `generated.by` and each ledger entry record `process:cli`, never `human:*`; `update --actor agent:me` records `agent:me`.
* **C38.** Run `install` with no target, or with `--path` and two targets: it exits 3 and writes nothing. Point `--claude --path` at an unparseable config: it exits 3 with the bytes unchanged. `verify --human --agent x` is rejected by the parser (exit 2); `verify --human` with no TTY exits 3. `search` with neither a query nor a filter exits 3.
* **C39.** Audit a `human:*` endorsement that is not yet committed: one `PROVENANCE_MISMATCH` saying so, and no `UNSIGNED_ENDORSEMENT` under `--require-signed`. An endorsement in a file Git does not track is reported the same way.
* **C40.** Ground a note whose frontmatter omits `type` and `status`: both are reported empty, never as invented defaults.
* **C41.** Put `raw/run.log` in a vault and run any command: `status` reports `{".log": 1}` under `filename_only_raw` and a term from its body is not found with `domain: "raw"`. Add `.log` to `raw_text_extensions` and, with the file untouched, `filename_only_raw` is empty and the same search finds it; remove it again and the hit is gone.
* **C42.** Scaffold `"Flash Attention"` with no `path` in an empty vault: the file is `wiki/flash-attention.md`. In a vault that already holds `wiki/Flash-Attention.md`, the same call is refused and writes nothing.
* **C43.** With one note containing both `zorblat` and `quaffle` and two notes containing one each, `search("zorblat quaffle")` returns the first note at the top and both others below it; `limit: 1` returns only the first, and `zorblat AND quaffle` never returns the partial matches.
* **C44.** Move a note that a neighbour links to as `[[x]]`, `[[x|a]]`, `[[x#h]]`, `![[x#h]]`, a table-escaped `[[x\|a]]` and two relative Markdown links: a dry-run lists that neighbour and writes nothing; applying rewrites exactly those seven targets and no link inside code. Move a note to another folder under the same stem instead: a bare `[[x]]` elsewhere is left byte-identical, and a `human-reviewed` moved note whose body did not change is still `human-reviewed` at its new path.
* **C45.** Move a note into `wiki/` under a stem that a bare wikilink already opens in a domain: the move is refused and nothing is written. Move one that a `human-reviewed` note links to: refused until that note is named in `approve_stale`, after which it reads `stale-verified`. Edit a linking note between plan and apply: the apply fails with `VAULT_CONFLICT`, every file it had written is restored, and the edit survives.
* **C46.** In a vault with one dead link, `vault_status` reports `finding_counts.error` of 1 with no `findings` and `isError: false`; with `lint: true` it adds the `LINK_DEAD` finding, still with `isError: false`. `cadabby status --json` reports the same counts, and calling `vault_lint` fails with `UNKNOWN_TOOL` whose message names `vault_status` with `lint: true`.
* **C47.** Call `vault_update_note` with a `patch_frontmatter` that sets `verified` to a `human:*` entry: it fails with `HUMAN_ATTESTATION_REFUSED` and the file is unchanged. Patch `verified` to an agent entry, to `null`, or patch `generated`: each fails with `INVALID_ARGUMENT` and writes nothing.
* **C48.** Give a note a list-valued `title`: `cadabby status` and every MCP tool still work, and lint reports `ENUM_INVALID` for it. `vault_scaffold_note` with a list `description` fails with `INVALID_ARGUMENT` and writes no file.
* **C49.** With `customers/acme/README.md` and `projects/apollo/README.md` present, `vault_update_note`, `vault_verify_note` and `vault_move_note` on `README` each fail with `INVALID_ARGUMENT` naming both and change nothing, while each full path works. Name `wiki/flash-attention.md` as `wiki/Flash-Attention`: the tool acts on, and reports, `wiki/flash-attention`.
* **C50.** Move a note while a referrer is edited between plan and apply, the referrer having frontmatter comments and CRLF line endings, into a folder that did not exist: every file the move wrote is restored byte for byte and the new folder is gone. Take a dry run's `plan_token`, add a link to the note, and apply with that token: it fails with `VAULT_CONFLICT` and writes nothing.
* **C51.** Grep the `AGENTS.md` that `init` writes for a `vault_*` tool followed by `(`: there is none. Read the `tools/list` descriptions instead: `vault_search` says matching is lexical and that `raw/` needs `domain='raw'`, and `vault_move_note` says to show the user the plan before applying.
* **C52.** Open the `AGENTS.md` that `init` writes: it names `.agents/skills/librarian/SKILL.md` and `.agents/skills/technician/SKILL.md`, and contains no domain-manifest example and no raw-citation `../` recipe. Open the librarian runbook: both are there.
* **C53.** Place two notes with identical text and `status: completed` in `cs244b/` and `archive/cs244b/`: the active domain note outranks the archived one by `2×` (`0.85` vs `0.85 × 0.5 = 0.425`), a bare wikilink resolves to an active domain before `archive/`, and `index.md` excludes `archive/` and `abandoned` notes from both Unfiled Notes and Verification Debt.
* **C54.** Call `vault_move_note` with a folder prefix (`customers/northwind/` → `customers/apollo/northwind/`, or `cs244b/` → `archive/cs244b/`): every note under the prefix moves atomically, relative links between co-moving `human-reviewed` notes stay byte-identical and keep their tier, depth-changed links to `raw/` or external notes are rewritten, and the vacated source folder is pruned.
* **C55.** Call `vault_search` (or `cadabby search`) with no `query` and a filter (`trust: "human-reviewed"`, `domain: "wiki"`, or `tag: "attention"`): matching notes return ordered by `trust_mult * status_mult * domain_mult` then `cid ASC` with a non-empty snippet. Call `vault_search` or `cadabby search` with an empty `query` and no filters: it fails with `INVALID_ARGUMENT`.
* **C56.** Call `vault_ground` with `"wiki/storage-engine#B-Tree Indexes (v2)"` or `"[[storage-engine#b-tree-indexes-v2]]"`: `content` contains that ATX heading and its nested `###` subsections (ignoring fenced `##` lines) and stops before the next `##`, with `"section": "B-Tree Indexes (v2)"`; a missing `#heading` returns the whole note with `"section": null` and a `section_error` naming the note's available headings; and each resolved 1-hop entry in `links` and `backlinks` carries the neighbor's `title`, `type`, `status`, `trust_tier`, and `description`.
* **C57.** Patch any frontmatter field through `vault_update_note` with a value that starts like a body hash or a timestamp and carries a quote, newlines and a `verified:` block: the value reads back as one string, no key is added, and the note's trust tier is unchanged. A writer that would emit frontmatter not reading back as the same values refuses with `FRONTMATTER_UNSERIALIZABLE` and writes nothing.
* **C58.** With two `README` hubs present, ground `["sqlite", "no-such-note", "README"]`: three entries in that order — the note, a `NOT_FOUND` error, and an `INVALID_ARGUMENT` error naming both hubs. Ground only `no-such-note`: the call itself fails with `NOT_FOUND`. Ground a note whose frontmatter does not parse alongside a good one: the good one grounds and the other's entry is `FRONTMATTER_UNPARSEABLE` naming its file. `./wiki/sqlite` and `wiki/SQLite` ground `wiki/sqlite`, as they name it to `update` and `verify`.
* **C59.** With `wiki/projects.md` and a `projects/` domain, `move projects` plans the note and `move projects/` the folder. Shift `a/` → `a/b/` (and `a/sub/` → `a/`) while recording storage calls: no source is deleted before its content has been saved at its destination.
* **C60.** Tag a note `ML` by hand: `--tag ML`, `--tag ml` and `--tag Ml` all find it. Break the frontmatter of `wiki/Architecture` while `projects/apollo/Architecture` exists: `[[Architecture]]` is unresolved, not redirected, and lint's `LINK_DEAD` names `wiki/Architecture`. Fix the frontmatter and the link resolves to it again.
* **C61.** Append to `log.md` while rotation archives it: the entry is in the archive or the new ledger, and no pending file is left behind. Leave an empty `vault.lock` younger than `stale_age`, or one whose owner answers `EPERM`: neither is broken. Judge one lock stale and find another in its place when breaking it: the other survives. A holder releases only the exact lock it wrote.
* **C62.** Send `cadabby mcp` a frame larger than the message cap, in either framing, followed by a `ping`: the first is answered `-32600` and the `ping` normally; a `Content-Length` far beyond what ever arrives is answered at once rather than allocated. Call any tool with a required argument set to `null`: `INVALID_ARGUMENT` naming it as missing. Initialize as `Café Agent`: the actor is `agent:Caf--Agent` and `vault_verify_note` succeeds. Scaffold with an empty `title`, `type` or `description`, or with an actor outside §3.4's grammar: refused, nothing written.
* **C63.** Overwrite `cache.db` with garbage and run any command: it succeeds and the cache is rebuilt. Delete a note after a scan has listed it but before it reads it: the scan finishes and the note leaves the cache. Save `.cadabby.json` with a BOM: it loads; with bytes that are not UTF-8: `CONFIG_INVALID`. Edit a note whose name is 250 bytes long: it succeeds. A bare `#` line never makes the next line a heading.
* **C64.** Write `title:` followed later by `title: x`, or a sequence item `-foo`: rejected as unparseable. Read `title: it's # note`: the title is `it's` and the comment is dropped. `human:bob\n` is not a valid actor. Write a title of `3`, `1e3`, `.inf`, `TRUE`, `Null`, `yes` or `2024-01-31`: it is emitted double-quoted and reads back as the same string.
* **C65.** Search `sqlite AND NOT duckdb`: no result contains `duckdb`. Search `---` with `trust: human-reviewed`: the same notes as the filter alone. Name a hand-written note `C#.md`: `index.md` links it as `[C#](wiki/C%23.md)`. Update a note with a value it already has, or with nothing: `INVALID_ARGUMENT`, and neither the note nor `log.md` changes. Scaffold with `domain: ""` or a path ending in `.MD`: the note lands in `wiki/` with a `.md` suffix. Dry-run a folder move over a note with broken frontmatter: the plan lists it under `blocked`, and the apply is refused.
* **C66.** Send `cadabby mcp` a request with an `id` and no `method`: `-32600`. Send `tools/call` with a non-string `name`, or read `domain://nope/directives`: `-32602`. Call `vault_search` with `limit: null`: it returns results; with `qurey`: `INVALID_ARGUMENT` naming `qurey`. Frame a request with `Content-Type` before `Content-Length`: it is answered. Run `cadabby audit --json`: one object shape whether or not a notice is set; a git call that hangs past 30 s fails the audit.
* **C67.** `vault_ground` on `raw/<file>.md#Heading` returns that section with `kind: raw`, no `trust_tier`, and `cited_by` naming each note whose `sources:` cite it. A PDF in `raw/` returns `filename_only: true` and `content: null`. `raw/../.cadabby.json`, a dotfile, a symlink out of `raw/`, and an unlisted path are `NOT_FOUND`.
* **C68.** Add an unfiled wiki note and two stale notes, one whose latest attestation is `human:*` and one where an agent attested after the human: `vault_status` `queues` list the unfiled note and every unprocessed source `index.md` lists, the first stale note under `human_owed` only, and the second under `agent_payable`. Thirty unprocessed sources return 25 `items` and the full `total`.
* **C69.** `vault_graph` on a note returns its backlinks with `title` and `trust_tier`, a `*_total` per list, and no note text anywhere. Two MOCs linking four notes each, three in common, report `shared: 3, ratio: 0.75` under `view: mocs`. `view: mocs` with a `target`, `neighborhood` without one, and a `raw/` target are `INVALID_ARGUMENT`.
* **C70.** `vault_ground` with `cids` and `vault_update_note` with `append_section` are `INVALID_ARGUMENT` naming `targets` and `edits`; neither old name appears in any `inputSchema`.
* **C71.** The first eight `cadabby` subcommands are exactly the MCP tool names without `vault_` and `_note`, and `cadabby log` is a usage error. `cadabby graph` needs exactly one of a target or `--mocs`, and `cadabby status --lint --json` lists findings and exits 0 on a vault with a dead link.
* **C72.** Symlink `log.md`, `index.md` or `.cadabby/` to a file or folder outside the vault, then update a note: the call fails with `PERMISSION_DENIED` and the outside file is unchanged. Scaffold through a domain folder symlinked outside, or through a dangling `wiki/x.md -> ../raw/p.md`: refused, nothing written. Replace an installed shim or the vault's `.mcp.json` with a link outside and run `install`: refused, target unchanged. A folder symlinked *in* from outside is still indexed under its in-vault name. `--actor human:alice` or an actor containing a newline exits 2, and no ledger entry spans two lines. A lock dated in the future is broken at once. A `0600` file being rewritten never has a world-readable temporary copy.
* **C73.** In a real Git repository, let an agent commit an edit that re-points only the `of:` line of a person's existing entry: `audit` reports `PROVENANCE_MISMATCH` on that line. An untouched endorsement stays clean. Join a forged `human:*` entry with lone carriage returns: the note is `FRONTMATTER_UNPARSEABLE` everywhere (no trust tier in the cache), and `audit` reports it. A `core.fsmonitor` program in the repository's own config is not executed by `audit`.
* **C74.** Parse 100 KB of adversarial text — a run of backticks, repeated `[[`, `[[a#`, `[](`, `[](<`, `[`, or a heading padded with spaces — through the link, heading, section and frontmatter-comment parsers: each finishes in well under a second. `replace_section` on `## Intro ##`, `##\tSetup` or ``## `cfg` file`` replaces the content under that heading, keeps the heading line, and adds no second section.

### Traceability

A criterion nobody can point to a test for is a wish. This table is the map, and `TestAcceptanceTraceability` keeps it honest: every test named here must exist, and every criterion must have a row. The **Defined in** column is the other half of the rule above — it is where a criterion's requirement actually lives, so the criterion itself never has to restate it. Note that this column is the weakest link in the table: a test name either exists or does not, but a section reference can point at a real section that is simply the wrong one, and for several releases five rows did exactly that. `test_every_section_reference_resolves` catches only the dangling case; keeping criteria short enough that the right section is obvious is the rest of the defense. An earlier generation of this list was tracked by test methods numbered `ac1`–`ac19`; the list was rewritten, the numbers were not, and the suite spent several releases asserting criteria that had been deleted while four new ones went unverified. Names of tests are cited rather than numbers for that reason.

| # | Defined in | Verified by |
| :--- | :--- | :--- |
| C1 | §4.1, §4.4 | `test_ac2_disposable_cache_reproducibility`, `test_disposable_cache_reproducibility`, `test_c1_multi_domain_tiebreak_reproducibility` |
| C2 | §3.3, §4.3 | `test_ac4_body_edit_downgrades_to_stale_verified`, `test_drift_downgrades_to_stale_verified`, `test_status_counts_verification_debt`, `test_status_debt_agrees_with_the_index_report` |
| C3 | §3.2, §6.3 | `test_ac3_restricted_yaml_grammar`, `test_gate1_schema_integrity_errors`, `test_parser_rejects_out_of_subset_shapes`, `test_update_note_rejects_out_of_subset_patch_without_touching_the_file` |
| C4 | §6.3 | `test_clean_demo_vault_has_zero_errors` and the per-gate cases, extended by `test_gate1_invalid_timestamps_in_both_blocks`, `test_gate3_anchor_missing_only_when_the_target_resolves`, `test_gate3_anchor_that_exists_is_silent`, `test_gate6_stale_verification_is_debt_not_an_error`, `test_gate6_superseded_history_is_silent_when_reverified`, `test_gate6_one_stale_warning_per_note`, `test_raw_link_wrong_depth_is_reported_with_suggestion`, `test_correct_raw_links_code_spans_and_urls_are_ignored`; the taxonomy itself is held by `test_the_fixture_seeds_one_of_every_code`, `test_the_spec_names_exactly_the_codes_lint_emits` and `test_only_connectivity_and_debt_are_warnings`, which keep this document's list, `lint.py`'s emissions, and one all-codes fixture at the same set |
| C5 | §2.2 | `test_discover_domains_ignores_reserved_and_ignored_directories`, `test_domain_with_agents_md`, `test_lint_require_sources_enforcement`, `test_lint_custom_types_and_allowed_types` |
| C6 | §2.2, §5.1 | `test_scaffold_note_in_custom_domain`, `test_scaffold_note_enforces_domain_allowed_types`, `test_scaffold_path_outside_note_locations_is_refused` |
| C7 | §5.1 | `test_ground_notes_enriches_domain_and_directives` |
| C8 | §2.4, §4.4 | `test_multi_domain_cache_scan_and_fts_search`, `test_raw_full_text_is_reachable_only_through_the_raw_domain` |
| C9 | §4.2 | `test_cross_domain_links_and_stem_priority`, `test_link_target_index`, `test_qualified_link_never_falls_back_to_stem`, `test_qualified_target_has_no_stem_fallback` |
| C10 | §5.1, §5.2 | `test_ac10_mcp_capabilities_and_human_refusal`, `test_initialize_and_tools_list`, `test_mcp_server_resources_list_and_read`, `test_plugin_skill_enumerates_exactly_the_tools_the_server_exposes`, `test_every_stated_tool_count_matches_the_server` |
| C11 | §2.2, §6.3 | `test_lint_layout_bypass_for_flexible_domains`, `test_gate2_layout_consistency_error`, `test_lint_cross_domain_orphan_prevention`, `test_gate2_reported_alongside_unparseable_frontmatter` |
| C12 | §3.5, §5.1, §5.3 | `test_verify_note_human_refusal_and_machine_attestation`, `test_ac10_mcp_capabilities_and_human_refusal`, `test_flagless_verify_in_a_tty_stamps_process_cli`, `test_human_verify_requires_typed_token`, `test_human_verify_wrong_token_writes_nothing`, `test_use_case_human_refusal_code` |
| C13 | §3.5 | `test_ac11_audit_provenance_mismatch` |
| C14 | §4.2, §4.3 | `test_ac6_rename_leaves_no_stale_records`, `test_deletion_reconciliation_and_fts_retraction`, `test_moving_a_file_between_layers_retracts_from_the_right_index`, `test_integrity_check_detects_an_unretracted_update`, `test_integrity_check_is_clean_with_raw_and_unparseable_rows` |
| C15 | §4.4 | `test_ac7_epistemic_ranking_ordering`, `test_epistemic_ranking_boosts_and_status_penalty`, `test_small_vault_saturated_term_preserves_field_weight_hierarchy`, `test_multi_term_query_falls_back_to_or_when_implicit_and_returns_nothing` |
| C16 | §4.3, §8 | `test_ac16_sync_zero_git_diff`, `test_sync_vault_index_stays_byte_idempotent` |
| C17 | §2.5 | `test_entry_points_lists_every_moc`, `test_filed_notes_are_not_enumerated`, `test_unfiled_note_is_reported`, `test_reachability_is_transitive`, `test_deprecated_notes_are_not_unfiled_work`, `test_raw_queue_lists_only_unprocessed_sources`, `test_verification_debt_section_lists_stale_notes`, `test_a_healthy_vault_has_entry_points_and_no_gap_sections` |
| C18 | §2.5, §6.3 | `test_linking_from_a_moc_clears_the_note`, `test_outward_linking_note_is_unfiled_but_not_an_orphan`, `test_a_domain_note_is_never_unfiled`, `test_gate5_orphan_detection_single_query` |
| C19 | §2.5 | `test_no_index_is_written_into_a_cognitive_domain` |
| C20 | §4.3, §8 | `test_a_changed_scan_regenerates_without_an_explicit_sync`, `test_an_unchanged_scan_leaves_bytes_and_mtime_untouched`, `test_an_unchanged_scan_does_not_even_build_the_report`, `test_a_raw_file_a_human_dropped_in_reaches_the_queue` |
| C21 | §5.3, §7.4 | `test_ac17_init_untouched_claude_notification`, `test_init_without_force_does_not_rewrite_existing_mcp_configs` |
| C22 | §7.6 | `test_ac18_installer_idempotency_and_uninstall`, `test_run_install_all_and_uninstall`, `test_install_on_a_fresh_vault_is_a_no_op`, `test_antigravity_refuses_unparseable_mcp_config_bytes_unchanged`, `test_force_and_uninstall_never_delete_a_non_cadabby_directory`, `test_claude_uninstall_leaves_a_foreign_entry_without_force`, `test_claude_install_preserves_style_and_non_ascii`, `test_claude_install_refuses_a_concurrent_rewrite` |
| C23 | §7.4, §9 | `test_ac19_single_definition_invariant`, `test_shims_point_at_vault_content_rather_than_restating_it`, `test_epistemic_anti_hallucination_guardrails_in_shipped_assets` |
| C24 | §5.4 | `test_a_missing_required_argument_blames_the_caller`, `test_a_conflict_tells_the_caller_to_retry`, `test_a_failure_is_json_not_prose`, `test_only_races_are_retryable`, `test_spec_table_matches_the_module`, `test_spec_table_agrees_with_the_exit_map`, `test_spec_table_agrees_on_which_codes_retry` |
| C25 | §2.2, §7.5 | `test_a_template_folder_does_not_turn_lint_red`, `test_a_declared_template_folder_is_not_a_cognitive_domain`, `test_without_the_setting_the_folder_is_an_ordinary_domain`, `test_a_nested_template_folder_is_pruned_but_its_parent_survives`, `test_the_template_body_seeds_the_note_with_the_title_substituted`, `test_the_templates_own_frontmatter_is_discarded`, `test_an_escaping_or_absolute_folder_is_refused` |
| C26 | §7.4, §7.5 | `test_plain_obsidian_init_writes_no_templates`, `test_the_flag_writes_the_folder_and_declares_it_together`, `test_the_declared_folder_is_the_one_the_engine_skips`, `test_inserting_a_starter_leaves_exactly_the_field_it_cannot_fill`, `test_re_running_leaves_an_edited_starter_untouched`, `test_an_existing_declaration_wins_and_is_left_alone`, `test_force_does_not_redirect_an_existing_declaration`, `test_a_templates_folder_already_holding_notes_is_refused`, `test_shipped_starters_never_carry_epistemic_frontmatter` |
| C27 | §5.2 | `test_newline_delimited_request_gets_newline_delimited_reply`, `test_content_length_request_keeps_content_length_framing`, `test_mixed_framing_stream_answers_each_message_in_kind` |
| C28 | §5.1, §8 | `test_batched_edits_are_atomic_and_logged_once`, `test_replace_text_zero_or_multiple_matches_writes_nothing`, `test_replace_text_cannot_reach_frontmatter`, `test_replace_text_edits_the_preamble_above_first_section`, `test_update_note_schema_exposes_edits` |
| C29 | §4.4, §6.3 | `test_markdown_link_is_a_graph_edge_with_backlink`, `test_markdown_link_prevents_orphan`, `test_dead_markdown_link_suggests_relative_path`, `test_markdown_link_to_non_note_file_is_not_an_edge`, `test_anchor_matches_github_slug`, `test_ground_payload_reports_link_kind`, `test_markdown_link_from_moc_files_the_note`, `test_markdown_note_links_extracted`, `test_resolve_markdown_target_depth_and_escape` |
| C30 | §3.2 | `test_writer_output_always_reparses`, `test_colon_in_bare_sequence_item_stays_scalar`, `test_top_level_key_without_space_is_rejected`, `test_tilde_is_a_string`, `test_writer_refuses_non_string_scalars`, `test_writer_preserves_top_level_null` |
| C31 | §3.2, §3.3, §8 | `test_crlf_note_verifies_and_updates`, `test_bom_prefixed_note_parses_and_updates_cleanly`, `test_leading_bom_is_ignored`, `test_concurrent_delete_is_a_conflict`, `test_an_update_keeps_crlf_and_the_blank_line_after_the_fence`, `test_crlf_newline_applies_to_the_whole_file` |
| C32 | §8 | `test_unbreakable_stale_lock_times_out` |
| C33 | §4.3 | `test_source_resolution_tracks_raw_arrivals`, `test_dot_slash_citation_counts_as_processed` |
| C34 | §4.5, §6.3 | `test_status_broken_links_matches_lint` |
| C35 | §2.2, §6.3 | `test_invalid_manifest_is_reported_and_blocks_scaffold`, `test_manifest_field_shapes_are_checked`, `test_valid_manifest_has_no_error` |
| C36 | §2.6, §5.4 | `test_vault_cache_search_malformed_ranking_config`, `test_search_propagates_non_syntax_sqlite_errors`, `test_config_values_are_validated` |
| C37 | §5.3, §3.5 | `test_cli_writes_default_to_process_cli_even_on_a_tty` |
| C38 | §5.4 | `test_install_usage_errors_and_refusals_never_exit_findings`, `test_verify_mutually_exclusive_flags`, `test_human_verify_without_tty_is_invalid_argument`, `test_search_without_query_or_filter_is_invalid_argument` |
| C39 | §3.5 | `test_uncommitted_endorsement_is_a_provenance_failure`, `test_empty_author_never_matches`, `test_untracked_endorsement_is_a_provenance_failure`, `test_untracked_endorsement_real_git` |
| C40 | §3.1 | `test_missing_type_and_status_read_as_missing` |
| C41 | §2.4, §4.3, §4.5 | `test_extension_list_change_reaches_untouched_raw_files`, `test_status_filename_only_raw_distinguishes_skipped_extensions_from_empty_text_files`, `test_unchanged_extension_list_rescans_nothing` |
| C42 | §5.1 | `test_default_stem_is_lowercase_kebab_case`, `test_scaffold_refuses_case_only_collision` |
| C43 | §4.4 | `test_or_fill_surfaces_partial_matches_after_all_terms_hits` |
| C44 | §5.1 | `test_dry_run_writes_nothing`, `test_rewrites_every_link_form_and_nothing_else`, `test_links_that_still_resolve_are_left_alone`, `test_moved_note_keeps_its_tier_when_its_body_is_unchanged`, `test_span_slices_are_exactly_the_target_text`, `test_moved_note_images_and_reference_definitions_follow_the_new_folder`, `test_rewritten_links_stay_edges_or_the_destination_is_refused` |
| C45 | §5.1 | `test_retargeting_someone_elses_link_is_refused`, `test_human_reviewed_referrer_needs_explicit_approval`, `test_conflict_mid_apply_restores_every_file`, `test_source_edited_between_plan_and_apply_is_a_conflict`, `test_in_memory_conflict_restores_every_note_and_logs_nothing` |
| C46 | §4.5, §5.1, §5.4 | `test_status_reports_finding_counts_and_findings_only_on_request`, `test_status_json_carries_the_same_finding_counts_as_mcp` |
| C47 | §3.4, §5.1 | `test_patch_frontmatter_never_writes_engine_owned_fields` |
| C48 | §3.1, §5.1 | `test_list_valued_core_fields_never_break_the_scan` |
| C49 | §5.1, §5.4 | `test_names_resolve_to_one_canonical_note_or_are_refused`, `test_ambiguous_names_and_unknown_approvals_are_refused`, `test_wrong_case_name_resolves_to_the_real_note` |
| C50 | §5.1, §8 | `test_rollback_restores_exact_bytes_and_removes_new_folders`, `test_stale_plan_token_refuses_apply_and_writes_nothing` |
| C51 | §7.4, §5.1 | `test_vault_agents_md_names_tools_without_call_syntax`, `test_tool_descriptions_carry_per_tool_guidance` |
| C52 | §7.4, §7.1 | `test_vault_agents_md_routes_task_procedures_to_the_runbooks` |
| C53 | §2.1, §2.5, §4.4 | `test_archive_domain_ranking_multiplier_compounds_with_trust_and_status`, `test_stem_resolution_prefers_active_domains_over_archive`, `test_index_excludes_archive_and_abandoned_from_worklists` |
| C54 | §5.1 | `test_folder_move_preserves_comoving_links_and_prunes_empty_dirs`, `test_folder_move_into_archive_retires_subtree_atomically` |
| C55 | §4.4, §5.1, §5.3 | `test_filter_only_search_orders_by_epistemic_multipliers_and_rejects_unfiltered_empty_query`, `test_mcp_filter_only_search_and_section_grounding`, `test_cli_filter_only_search_and_section_grounding` |
| C56 | §5.1, §5.3 | `test_ground_notes_section_anchor_extracts_target_section_and_subsections`, `test_ground_notes_enriches_one_hop_links_and_backlinks_with_neighbor_metadata`, `test_mcp_filter_only_search_and_section_grounding`, `test_cli_filter_only_search_and_section_grounding` |
| C57 | §3.2, §3.5 | `test_values_that_look_like_hashes_or_timestamps_cannot_inject_keys`, `test_real_hashes_and_timestamps_stay_single_quoted`, `test_writer_refuses_output_that_does_not_reparse`, `test_patch_frontmatter_cannot_forge_a_human_attestation` |
| C58 | §5.1, §5.3, §5.4 | `test_ground_reports_each_failed_target_in_place`, `test_ground_resolves_names_like_the_write_tools`, `test_ground_unparseable_target_is_named_not_fatal`, `test_mcp_ground_partial_results`, `test_cli_ground_reports_failed_targets_and_keeps_the_rest` |
| C59 | §5.1 | `test_move_prefers_the_note_reading_over_a_folder`, `test_overlapping_folder_shift_never_retires_an_unwritten_source`, `test_write_steps_case_only_rename_still_retires_first` |
| C60 | §4.2, §4.4, §6.3 | `test_uppercase_tag_in_file_matches_any_case_filter`, `test_unparseable_note_keeps_its_name_and_links_stay_unresolved`, `test_dead_link_to_unparseable_note_names_it`, `test_scan_reresolves_stem_priority_when_wiki_note_repaired` |
| C61 | §8 | `test_rotation_never_loses_a_concurrent_append`, `test_fresh_empty_lock_is_not_broken`, `test_old_empty_lock_is_broken`, `test_eperm_owner_counts_as_alive`, `test_remove_if_unchanged_restores_a_swapped_lock`, `test_release_only_removes_the_exact_lock_written` |
| C62 | §3.5, §5.1, §5.2 | `test_oversized_frame_is_refused_and_server_keeps_serving`, `test_null_required_argument_is_missing`, `test_non_ascii_client_name_yields_a_valid_actor`, `test_scaffold_refuses_empty_core_fields_over_mcp`, `test_scaffold_refuses_empty_core_fields_and_invalid_actor` |
| C63 | §2.6, §4.2, §4.3, §8 | `test_a_corrupt_cache_file_is_rebuilt`, `test_a_file_deleted_mid_scan_is_dropped_not_fatal`, `test_a_byte_order_mark_is_tolerated`, `test_invalid_utf8_is_config_invalid`, `test_a_name_near_name_max_can_still_be_rewritten`, `test_the_parent_directory_is_fsynced_after_the_rename`, `test_bare_hash_line_does_not_promote_the_next_line` |
| C64 | §3.2, §3.4 | `test_an_empty_key_still_counts_for_duplicate_detection`, `test_dash_without_space_is_not_a_sequence_item`, `test_an_apostrophe_inside_a_bare_scalar_does_not_hide_the_comment`, `test_actor_and_timestamp_reject_a_trailing_newline`, `test_core_schema_lookalikes_are_quoted_and_round_trip` |
| C65 | §2.5, §4.4, §5.1 | `test_and_not_excludes_matching_notes`, `test_phrase_prefix_is_kept`, `test_an_unsearchable_query_with_a_filter_falls_back_to_the_filter`, `test_a_stem_with_hash_is_linked_by_markdown_path`, `test_an_update_that_changes_nothing_writes_nothing`, `test_empty_domain_means_wiki`, `test_uppercase_md_suffix_is_the_extension`, `test_folder_move_lists_an_unparseable_note_as_a_blocker`, `test_memory_twin_honours_the_template_folder`, `test_dotted_stem_embed_is_an_attachment_and_md_suffix_is_a_note` |
| C66 | §3.5, §5.1, §5.2 | `test_request_without_a_method_is_invalid_request`, `test_tools_call_with_a_non_string_name_is_invalid_params`, `test_reading_directives_of_an_unknown_domain_is_invalid_params`, `test_null_limit_means_the_default`, `test_unknown_argument_is_refused_by_name`, `test_content_length_need_not_be_the_first_header`, `test_audit_json_has_one_shape_with_or_without_a_notice`, `test_a_hung_blame_fails_the_audit`, `test_a_hung_rev_parse_reads_as_no_repository` |
| C67 | §5.1, §2.4 | `test_ground_reads_a_raw_source_section_with_its_citers`, `test_ground_a_pdf_source_is_filename_only`, `test_ground_refuses_anything_that_is_not_listed_raw_evidence`, `test_ground_mixes_notes_and_sources_and_budgets_both`, `test_disk_lists_and_reads_only_raw_evidence`, `test_disk_listing_matches_the_cache_raw_rows`, `test_memory_twin_reads_what_was_placed_and_refuses_the_rest` |
| C68 | §4.5, §2.5 | `test_status_queues_match_index_and_split_debt`, `test_status_queues_are_capped_with_totals`, `test_human_last_is_human_owed`, `test_agent_after_human_is_the_agents`, `test_unbound_and_malformed_entries_name_nobody`, `test_an_entry_that_still_binds_is_not_stale` |
| C69 | §5.1 | `test_graph_neighborhood_carries_metadata_and_no_text`, `test_graph_mocs_reports_overlap_ratio`, `test_graph_refuses_bad_view_target_combinations` |
| C70 | §5.1, §5.4 | `test_retired_arguments_name_their_replacement` |
| C71 | §5.3 | `test_every_mcp_tool_has_a_cli_verb_listed_first`, `test_log_is_gone`, `test_graph_needs_exactly_one_of_target_or_mocs`, `test_status_lint_lists_findings_and_still_exits_zero`, `test_graph_mocs_json_and_raw_ground_text` |
| C72 | §5.3, §8 | `test_ledger_symlinked_outside_is_refused`, `test_index_and_cache_symlinked_outside_are_refused`, `test_note_write_through_a_symlinked_domain_is_refused`, `test_dangling_link_into_raw_is_refused`, `test_symlinks_inside_the_vault_still_work`, `test_a_folder_symlinked_in_from_outside_is_read_under_its_vault_name`, `test_install_refuses_a_shim_symlinked_outside`, `test_newlines_in_a_message_cannot_forge_entries`, `test_both_ledgers_refuse_an_invalid_actor`, `test_parser_refuses_human_and_malformed_actors`, `test_the_temp_file_is_never_more_permissive_than_the_target`, `test_a_future_dated_lock_is_stale`, `test_contained_refuses_links_out_and_allows_links_in` |
| C73 | §3.2, §3.5 | `test_rewriting_only_the_of_line_is_caught`, `test_an_untouched_endorsement_stays_clean`, `test_a_carriage_return_forgery_is_unparseable_and_reported`, `test_a_lone_cr_attestation_gets_no_trust_tier`, `test_repository_config_cannot_run_code_during_an_audit` |
| C74 | §4.3, §5.1 | `test_link_and_heading_parsers_stay_linear`, `test_frontmatter_comment_split_stays_linear`, `test_links_still_parse_as_before`, `test_closing_hashes_and_tabs_are_replaced_not_duplicated`, `test_matching_is_exact_after_cleaning` |

C17-C20 were unmet together for several releases -- all four were the §2.5 gap report, specified and never built, while an older catalog occupied the file. They were closed as a group, since each depended on the same two queries. C2 and C4 followed: C2 needed the `status` payload asserted rather than only the tier behind it, and C4 needed a fixture seeding one instance of every lint code, which turned the taxonomy itself into something a test can hold. C1 was closed by adding `n.cid ASC` as the secondary sort key in §4.4, making equal-scoring search rows across domains deterministic regardless of insertion order. C24 arrived last and in the opposite order from the rest: §5.4 was written because the surface it describes was already shipped and undocumented, with one code living inside a message string and every other failure flattened to prose at the boundary. The criterion exists to keep that from recurring, which is why it checks the shape of a failure rather than any particular one.

Several tests retain `acN` prefixes from the superseded numbering. Their names are left alone — renaming costs churn and buys nothing the table does not already give — but each docstring now cites the criterion it actually satisfies, and the five that correspond to no current criterion (`test_ac1`, `ac5`, `ac8`, `ac9`, `ac12`) say so and cite the section they really pin.
