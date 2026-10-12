import os
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)


def _get(key, default=None, required=False):
    v = os.getenv(key, default)
    if v is None or v == "":
        if required:
            raise RuntimeError(f"Missing required env: {key} (check {ENV_PATH})")
        return default
    return v.strip()


API_ID = int(_get("API_ID", "0", required=True))
API_HASH = _get("API_HASH", required=True)
BOT_TOKEN = _get("BOT_TOKEN", required=True)
MONGO_URI = _get("MONGO_URI", "mongodb://localhost:27017")
OWNER_ID = int(_get("OWNER_ID", "0", required=True))
SESSION_KEY = _get("SESSION_KEY", "").encode()

MAX_AUTO_CHANNELS = 15
VC_JOIN_DELAY = 3
AUTO_POLL_INTERVAL = 20
VC_POLL_INTERVAL = 15
SILENT_WAV = "silent.wav"
POSITIVE_REACTIONS = ["👍","❤️","🔥","🥰","👏","😁","🎉","🤩","🙏","💯","😍","👌"]

# ═══════════════════════════════════════════════════════
# FSub is PERMANENTLY DISABLED — no channels, no checks
# ═══════════════════════════════════════════════════════
USE_FSUB = False
FSUB_CHANNEL_ID = 0
FSUB_CHANNEL_LINK = ""

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError(f"BOT_TOKEN missing/invalid: {BOT_TOKEN!r}")