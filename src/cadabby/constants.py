"""System constants, defaults, enums, and schema order for Cadabby."""

SCHEMA_VERSION = 2

# Note frontmatter enums
NOTE_TYPES = ("entity", "concept", "synthesis", "comparison", "guide")
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

# Validation patterns
ACTOR_PATTERN = r"^(human|agent|process):[A-Za-z0-9._\-/]+$"
RFC3339_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
BODY_HASH_PREFIX = "sha256:"
