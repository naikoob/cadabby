"""System constants, defaults, enums, and schema order for Cadabby."""

# Bumped to 3 when raw sources began being written to the FTS index (§2.4).
# A cache built before that holds no raw rows and no amount of incremental
# scanning adds them, since the files are unchanged; the version mismatch is
# what forces the one-time rebuild that picks them up.
SCHEMA_VERSION = 3

# Note frontmatter enums and constants
TYPE_MOC = "moc"
TYPE_CONCEPT = "concept"
TYPE_ENTITY = "entity"
TYPE_SYNTHESIS = "synthesis"
TYPE_COMPARISON = "comparison"
TYPE_GUIDE = "guide"

NOTE_TYPES = (
    TYPE_MOC,
    TYPE_CONCEPT,
    TYPE_ENTITY,
    TYPE_SYNTHESIS,
    TYPE_COMPARISON,
    TYPE_GUIDE,
)
NOTE_STATUSES = ("active", "evergreen", "draft", "completed", "deprecated", "abandoned")
TRUST_TIERS = ("human-reviewed", "machine-confirmed", "stale-verified", "unverified")

# Required OKF frontmatter fields
REQUIRED_FRONTMATTER_FIELDS = ("type", "title", "description", "status")

# Canonical frontmatter key serialization order
SCHEMA_KEY_ORDER = (
    "type",
    "title",
    "description",
    "status",
    "tags",
    "sources",
    "verified",
    "generated",
)

# Ranking weights & default multipliers
FTS_COLUMN_WEIGHTS = (4.0, 2.0, 1.0, 1.5)  # (title, description, body, tags)

DEFAULT_TRUST_MULTIPLIERS = {
    "human-reviewed": 2.0,
    "machine-confirmed": 1.2,
    "stale-verified": 1.0,
    "unverified": 1.0,
}

DEFAULT_STATUS_MULTIPLIERS = {
    "evergreen": 1.0,
    "active": 1.0,
    "draft": 0.9,
    "completed": 0.85,
    "deprecated": 0.4,
    "abandoned": 0.4,
}

# Vault defaults
DEFAULT_RAW_TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".rst", ".csv")
DEFAULT_INTEGRITY = "mtime_size"
DEFAULT_LOG_ROTATE_BYTES = 262144  # 256 KB

# Directory & file conventions
DIR_RAW = "raw"
DIR_WIKI = "wiki"
DIR_LOG = "log"
DIR_DOT_CADABBY = ".cadabby"
DIR_AGENTS = ".agents"
DIR_CLAUDE = ".claude"
DIR_OBSIDIAN = ".obsidian"

DEFAULT_IGNORED_DIRS = frozenset({
    ".git",
    ".cadabby",
    ".obsidian",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "target",
    "dist",
    "build",
    ".pytest_cache",
})

FILE_CONFIG = ".cadabby.json"
FILE_INDEX = "index.md"
FILE_LOG = "log.md"
FILE_AGENTS = "AGENTS.md"
FILE_CLAUDE = "CLAUDE.md"
FILE_GEMINI = "GEMINI.md"
FILE_STYLE = "STYLE.md"
FILE_MCP = ".mcp.json"
FILE_CACHE_DB = "cache.db"
FILE_LOCK = "vault.lock"

# Settings file of Obsidian's core Templates plugin. The engine only ever reads
# it (§7.5); `init --obsidian-templates` is the single exception, and it writes
# the file once, as part of creating the folder the file names.
FILE_OBSIDIAN_TEMPLATES = "templates.json"
TEMPLATE_TITLE_PLACEHOLDER = "{{title}}"

# Folder `init --obsidian-templates` creates and declares when the vault has no
# template folder of its own. An existing declaration always wins.
DIR_TEMPLATES = "templates"

# Validation patterns
ACTOR_PATTERN = r"^(human|agent|process):[A-Za-z0-9._\-/]+$"
RFC3339_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
BODY_HASH_PREFIX = "sha256:"
