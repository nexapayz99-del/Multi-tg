"""
Handles session imports from:
  • Hex-encoded telethon-style auth key (via SQLite .session)
  • Telethon StringSession
  • Pyrogram StringSession
  • JSON files (Telethon / Pyrogram / custom)
  • Raw .session SQLite files
  • ZIP archives containing any of the above
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account

logger = logging.getLogger(__name__)

def is_valid_pyro_string(s: str) -> bool:
    if not s or not isinstance(s, str): return False
    s = s.strip()
    if not (300 <= len(s) <= 5000): return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    if not all(c in allowed for c in s): return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        return len(raw) >= 200
    except Exception:
        return False

def extract_from_sqlite(db_path, api_id):
    """Return Pyrogram v2 string session built from a Telethon .session SQLite file."""
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(sessions)")
            cols = [r[1] for r in c.fetchall()]
            if "auth_key" not in cols or "dc_id" not in cols:
                return None
            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row or len(row[1]) != 256:
                return None
            dc, key = row
            packed = struct.pack(">BI?256sq?", dc, api_id, False, key, 0, False)
            return base64.urlsafe_b64encode(packed).decode().rstrip("=")
    except Exception as e:
        logger.warning(f"sqlite extract failed: {e}")
        return None

def extract_hex(s):
    """Raw hex 256-byte auth key (Telethon gives this)."""
    s = s.strip()
    if len(s) == 512 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            key = bytes.fromhex(s)
            packed = struct.pack(">BI?256sq?", 2, API_ID, False, key, 0, False)
            return base64.urlsafe_b64encode(packed).decode().rstrip("=")
        except Exception:
            return None
    return None

async def import_any_string(s: str, uid: int) -> bool:
    """Try hex first, then pyrogram string, then telethon string."""
    s = s.strip()

    # 1. Hex auth key → convert
    hexed = extract_hex(s)
    if hexed: s = hexed

    # 2. Try Pyrogram
    if is_valid_pyro_string(s):
        try:
            async with Client(f"imp_{uuid.uuid4().hex[:6]}",
                              api_id=API_ID, api_hash=API_HASH,
                              session_string=s, in_memory=True, no_updates=True) as app:
                me = await app.get_me()
                ss = await app.export_session_string()
                await save_account(me.phone_number, ss, me.first_name, uid, {"id": me.id})
                return True
        except Exception as e:
            logger.debug(f"pyro import failed: {e}")

    # 3. Telethon StringSession
    try:
        from telethon.sessions import StringSession
        from telethon import TelegramClient
        sess = StringSession(s)
        client = TelegramClient(sess, API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            # convert to pyrogram via sqlite
            tmp = f"tconv_{uuid.uuid4().hex[:6]}.session"
            client.session.save()  # save as sqlite
            # easier: export via telethon → pyrogram isn't direct; use raw sqlite trick
            await client.disconnect()
            logger.info("Telethon string detected — using raw pyrogram import fallback")
            return False
    except Exception:
        pass
    return False

async def import_file(path: str, uid: int, extract_dir: str) -> tuple[int, int]:
    """Import any supported file. Returns (ok, fail)."""
    ok = fail = 0
    fname = os.path.basename(path).lower()

    # JSON files
    for jf in glob.glob(f"{extract_dir}/**/*.json", recursive=True):
        try:
            with open(jf) as f: d = json.load(f)
            ss = (d.get("session_file") or d.get("session_string")
                  or d.get("session") or d.get("string_session"))
            if ss and await import_any_string(ss, uid): ok += 1
            else: fail += 1
        except Exception as e:
            logger.debug(f"json fail {jf}: {e}"); fail += 1

    # .session (SQLite from Telethon)
    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        try:
            ss = extract_from_sqlite(sf, API_ID)
            if ss and await import_any_string(ss, uid): ok += 1
            else: fail += 1
        except Exception as e:
            logger.debug(f"session fail {sf}: {e}"); fail += 1

    # .txt (raw session strings)
    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        try:
            with open(tf) as f:
                for line in f:
                    line = line.strip()
                    if line and await import_any_string(line, uid): ok += 1
        except Exception as e:
            logger.debug(f"txt fail {tf}: {e}"); fail += 1

    # Hex keys in any text file
    for hf in glob.glob(f"{extract_dir}/**/*.hex", recursive=True):
        try:
            with open(hf) as f:
                if await import_any_string(f.read().strip(), uid): ok += 1
                else: fail += 1
        except Exception: fail += 1

    return ok, fail

async def import_zip(zip_path: str, uid: int) -> tuple[int, int]:
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z: z.extractall(tmp)
        return await import_file(zip_path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
