"""Application constants for ROMAN Ⅰ."""

from pathlib import Path

APP_NAME = "ROMAN Ⅰ"
APP_SUBTITLE = "PERSONAL AI ASSISTANT"
WINDOW_SIZE = "1180x760"
MIN_WINDOW_SIZE = (860, 600)

BASE_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = BASE_DIR / "assets"

WHISPER_MODEL = "base"
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "float16"
OLLAMA_MODEL = "llama3.2:3b"
MIC_DEVICE_INDEX = 1
MIC_RATE = 16_000
MIC_CHANNELS = 1
MIC_FRAMES_PER_BUFFER = 4_000

BG = "#07111d"
PANEL = "#0b1928"
PANEL_ALT = "#0e2032"
LINE = "#1b3a55"
CYAN = "#38c9f2"
BLUE = "#638dff"
TEXT = "#edf5ff"
MUTED = "#94a9bd"
GREEN = "#32d9a0"
GOLD = "#ffd078"
FONT = "Segoe UI"
