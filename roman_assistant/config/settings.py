"""Application constants for Roman."""

from pathlib import Path

APP_NAME = "Roman"
APP_SUBTITLE = "PERSONAL AI ASSISTANT"
WINDOW_SIZE = "1180x760"
MIN_WINDOW_SIZE = (860, 600)

BASE_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = BASE_DIR / "assets"

# The JSON backend remains the active default.  The SQLite path is only a
# configuration target for explicit prototype/testing selection; merely
# importing settings or constructing the default adapter must not create it.
MEMORY_BACKEND = "json"
SQLITE_MEMORY_PATH = BASE_DIR / "memory" / "roman_memory.sqlite3"

# Retrieval limits are deliberately character-based until the local model's
# tokenizer is available here.  They bound prompt size without changing the
# existing Ollama request format.
MEMORY_MAX_FACTS = 8
MEMORY_MAX_MESSAGES = 6
MEMORY_MAX_RECENT_MESSAGES = 12
MEMORY_MAX_FACT_CHARS = 500
MEMORY_MAX_MESSAGE_CHARS = 1000
MEMORY_MAX_CONTEXT_CHARS = 6000
MEMORY_MAX_PROMPT_CHARS = 16000

WHISPER_MODEL = "base"
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "float16"
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_REQUEST_TIMEOUT_SECONDS = 300
MIC_DEVICE_INDEX = 1
MIC_RATE = 16_000
MIC_CHANNELS = 1
MIC_FRAMES_PER_BUFFER = 4_000

BG = "#090a0c"
PANEL = "#15171b"
PANEL_ALT = "#30343b"
LINE = "#626a74"
CYAN = "#80b9ff"
BLUE = "#80b9ff"
TEXT = "#eceef2"
MUTED = "#a4abb6"
GREEN = "#75d6a1"
GOLD = "#e3bd79"
RED = "#f08a8a"
FONT = "Segoe UI"
CODE_FONT = "Cascadia Code"
SIDEBAR = "#101114"
CODE_INLINE = "#c5d9f2"
CODE_KEYWORD = "#8cbcff"
CODE_STRING = "#9bd49a"
CODE_COMMENT = "#a4abb6"
CODE_NUMBER = "#d6aaff"
CODE_BUILTIN = "#f2c879"
CODE_FUNCTION = "#7fd4da"
STATUS_THINKING = "#4f86ca"
STATUS_DIM = "#48505a"
