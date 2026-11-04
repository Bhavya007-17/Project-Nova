"""Where Nova keeps things (SPEC §17). Everything lives in the repo checkout."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
SETTINGS = CONFIG / "settings.yaml"
VOCAB = CONFIG / "vocab.yaml"
ALIASES = CONFIG / "aliases.yaml"
DATA = ROOT / "data"
CATALOG_CACHE = DATA / "catalog.json"
LOGS = ROOT / "logs"
MODELS = ROOT / "models"
RECORDINGS = ROOT / "fixtures" / "voicebar-recordings"
