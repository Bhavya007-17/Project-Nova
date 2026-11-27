"""Where Nova keeps things (SPEC §17). Everything lives in the repo checkout."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
SETTINGS = CONFIG / "settings.yaml"
VOCAB = CONFIG / "vocab.yaml"
EXAMPLES = CONFIG / "examples.yaml"  # what people say -> what Nova does (recall matches these by meaning)
ALIASES = CONFIG / "aliases.yaml"
DATA = ROOT / "data"
CATALOG_CACHE = DATA / "catalog.json"
CHROME_BRIDGE = DATA / "chrome-bridge.json"  # the extension's pipe name + key (nova chrome --install)
DICTATION_HISTORY = DATA / "dictation_history.jsonl"  # the last 50 dictations (SPEC §11)
SKILLS = DATA / "skills"  # learned skills, one JSON file each (SPEC §12)
LOGS = ROOT / "logs"
MODELS = ROOT / "models"
EMBED = MODELS / "embed"  # the sentence model recall uses
RECALL_CACHE = DATA / "recall.npz"  # the example sentences as vectors (rebuilt when examples.yaml changes)
RECORDINGS = ROOT / "fixtures" / "voicebar-recordings"
