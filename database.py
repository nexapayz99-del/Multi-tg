from motor.motor_asyncio import AsyncIOMotorClient
from cryptography.fernet import Fernet
from config import MONGO_URI, SESSION_KEY
import logging, time
from datetime import datetime

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
flood_state    = db["flood_state"]     # per-phone flood-until
backups        = db["backups"]         # permanent session backup

# Fernet for session encryption
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


# ═══════════════════════════════════════════════════════════════
# ACCOUNTS
# ═══════════════════════════════════════════════════════════════
async def get_account(phone: str):
    doc = await accounts.find_one({"phone": phone})
    if doc and doc.get("session"):
        doc["session"] = decrypt_session(doc["session"])
    return doc


async def save_account(phone, session, name, uid, extra=None):
    """
    Save session to MongoDB AND keep a permanent backup.
    Never writes to disk.
    """
    encrypted = encrypt_session(session)
    payload = {
        "phone": phone,
        "session": encrypted,
        "name": name,
        "added_by": uid,
        "updated_at": datetime.utcnow(),
    }
    if extra:
        payload.update(extra)
    await accounts.update_one({"phone": phone}, {"$set": payload}, upsert=True)
    logger.info(f"💾 Session saved: {phone} ({name})")

    # ── ALSO save into permanent backup collection ──
    await backups.update_one(
        {"phone": phone},
        {"$set": {
            "phone": phone,
            "session": encrypted,
            "name": name,
            "user_id": extra.get("id") if extra else None,
            "added_by": uid,
            "last_seen": datetime.utcnow(),
        },
        "$setOnInsert": {"first_saved": datetime.utcnow()}},
        upsert=True,
    )


async def all_accounts(limit=5000):
    docs = await accounts.find({}).to_list(length=limit)
    for d in docs:
        if d.get("session"):
            d["session"] = decrypt_session(d["session"])
    return docs


async def restore_from_backups() -> int:
    """
    Re-import every backup row into `accounts` (if missing or empty).
    Useful if the accounts table is wiped. Returns count restored.
    """
    restored = 0
    async for b in backups.find({}):
        phone = b.get("phone")
        session = b.get("session")
        if not phone or not session:
            continue
        existing = await accounts.find_one({"phone": phone})
        if existing and existing.get("session"):
            continue  # already present
        await accounts.update_one(
            {"phone": phone},
            {"$set": {
                "phone": phone,
                "session": session,      # already encrypted
                "name": b.get("name", "?"),
                "added_by": b.get("added_by", 0),
                "id": b.get("user_id"),
                "restored_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        restored += 1
    logger.info(f"♻️ Restored {restored} account(s) from backup")
    return restored


async def export_all_backups() -> list:
    """
    Return list of dicts with DECRYPTED session strings.
    Owner can use this to re-import via bot.
    """
    out = []
    async for b in backups.find({}):
        s = decrypt_session(b.get("session", ""))
        if not s:
            continue
        out.append({
            "phone": b.get("phone"),
            "name": b.get("name", "?"),
            "session": s,
        })
    return out


async def backup_count() -> int:
    return await backups.count_documents({})


# ═══════════════════════════════════════════════════════════════
# FLOOD TRACKING
# ═══════════════════════════════════════════════════════════════
async def set_flood_until(phone: str, until_ts: float):
    await flood_state.update_one(
        {"phone": phone},
        {"$set": {"phone": phone, "until": until_ts}},
        upsert=True,
    )


async def get_flood_until(phone: str) -> float:
    doc = await flood_state.find_one({"phone": phone})
    if not doc:
        return 0
    until = doc.get("until", 0)
    if until < time.time():
        # expired — clean up
        try:
            await flood_state.delete_one({"phone": phone})
        except Exception:
            pass
        return 0
    return until


# ═══════════════════════════════════════════════════════════════
# PRIVATE-CHANNEL JOIN TRACKING
# ═══════════════════════════════════════════════════════════════
async def mark_joined(link: str, phone: str):
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
    doc = await auto_channels.find_one({"link": link},
                                       {"chat_id": 1, "title": 1})
    if doc and doc.get("chat_id"):
        return doc["chat_id"], doc.get("title")
    return None, None


async def cache_chat_id(link: str, chat_id: int, title: str = ""):
    await auto_channels.update_one(
        {"link": link},
        {"$set": {"chat_id": chat_id, "title": title or ""}},
        upsert=False,
    )