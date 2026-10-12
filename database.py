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
    payload = {
        "phone": phone,
        "session": encrypt_session(session),
        "name": name,
        "added_by": uid,
    }
    if extra:
        payload.update(extra)
    await accounts.update_one({"phone": phone}, {"$set": payload}, upsert=True)


async def all_accounts(limit=5000):
    docs = await accounts.find({}).to_list(length=limit)
    for d in docs:
        if d.get("session"):
            d["session"] = decrypt_session(d["session"])
    return docs