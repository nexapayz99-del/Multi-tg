from motor.motor_asyncio import AsyncIOMotorClient
from cryptography.fernet import Fernet
from config import MONGO_URI, SESSION_KEY
import logging

logger = logging.getLogger(__name__)
_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = _client["tg_manager"]

accounts       = db["accounts"]
users          = db["users"]
admins         = db["admins"]
pseudo_owners  = db["pseudo_owners"]
auto_channels  = db["auto_channels"]
seen_posts     = db["seen_posts"]
vc_state       = db["vc_state"]
plans          = db["plans"]
sessions_seen  = db["sessions_seen"]
settings       = db["settings"]
banned_users   = db["banned_users"]

_fernet = Fernet(SESSION_KEY) if SESSION_KEY else None


def encrypt_session(s: str) -> str:
    if not _fernet:
        return s
    return _fernet.encrypt(s.encode()).decode()


def decrypt_session(s: str) -> str:
    if not _fernet:
        return s
    try:
        return _fernet.decrypt(s.encode()).decode()
    except Exception:
        return s


async def get_account(phone: str):
    doc = await accounts.find_one({"phone": phone})
    if doc and doc.get("session"):
        doc["session"] = decrypt_session(doc["session"])
    return doc


async def save_account(phone, session, name, uid, extra=None):
    """Save session to MongoDB only — never to disk."""
    payload = {
        "phone": phone,
        "session": encrypt_session(session),
        "name": name,
        "added_by": uid,
    }
    if extra:
        payload.update(extra)
    await accounts.update_one({"phone": phone}, {"$set": payload}, upsert=True)
    logger.info(f"💾 Session saved to MongoDB: {phone} ({name})")


async def all_accounts(limit=5000):
    """Return all sessions from MongoDB."""
    docs = await accounts.find({}).to_list(length=limit)
    for d in docs:
        if d.get("session"):
            d["session"] = decrypt_session(d["session"])
    return docs


# ═══════════════════════════════════════════════════════════════
# PRIVATE-CHANNEL JOIN TRACKING
# ═══════════════════════════════════════════════════════════════
async def mark_joined(link: str, phone: str):
    """Remember that `phone` successfully joined the channel at `link`."""
    await auto_channels.update_one(
        {"link": link},
        {"$addToSet": {"joined_accounts": phone}},
        upsert=False,
    )


async def has_joined(link: str, phone: str) -> bool:
    doc = await auto_channels.find_one(
        {"link": link, "joined_accounts": phone},
        {"_id": 1},
    )
    return bool(doc)


async def get_joined_set(link: str) -> set:
    doc = await auto_channels.find_one({"link": link},
                                       {"joined_accounts": 1})
    return set(doc.get("joined_accounts", [])) if doc else set()


async def get_cached_chat_id(link: str):
    doc = await auto_channels.find_one({"link": link}, {"chat_id": 1, "title": 1})
    if doc and doc.get("chat_id"):
        return doc["chat_id"], doc.get("title")
    return None, None


async def cache_chat_id(link: str, chat_id: int, title: str = ""):
    await auto_channels.update_one(
        {"link": link},
        {"$set": {"chat_id": chat_id, "title": title or ""}},
        upsert=False,
    )
    
    # ═══════════════════════════════════════════════════════════════
# FLOOD TRACKING
# ═══════════════════════════════════════════════════════════════
flood_state = db["flood_state"]  # add near the other collection defs


async def set_flood_until(phone: str, until_ts: float):
    await flood_state.update_one(
        {"phone": phone},
        {"$set": {"until": until_ts}},
        upsert=True,
    )


async def get_flood_until(phone: str) -> float:
    doc = await flood_state.find_one({"phone": phone})
    if doc:
        until = doc.get("until", 0)
        # auto-clean past entries
        if until < __import__("time").time():
            return 0
        return until
    return 0