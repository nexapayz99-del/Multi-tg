import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
assert BOT_TOKEN and ":" in BOT_TOKEN,
(
    f"BOT_TOKEN missing or invalid: {BOT_TOKEN!r}\n"
    f"Check .env path: {ENV_PATH}"
)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SESSION_KEY = os.getenv("SESSION_KEY", "").encode()

FSUB_CHANNEL_ID = int(os.getenv("FSUB_CHANNEL_ID", "0"))
FSUB_CHANNEL_LINK = os.getenv("FSUB_CHANNEL_LINK", "")
USE_FSUB = FSUB_CHANNEL_ID != 0 and FSUB_CHANNEL_LINK != ""

MAX_AUTO_CHANNELS = 15
VC_JOIN_DELAY = 7                # seconds between account joins
AUTO_POLL_INTERVAL = 30          # seconds — checks new posts & VC state
SILENT_WAV = "silent.wav"
POSITIVE_REACTIONS = ["👍","❤️","🔥","🥰","👏","😁","🎉","🤩","🙏","💯","😍","👌"]
