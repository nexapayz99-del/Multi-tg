"""
session_manager.py — imports sessions from:
  • .txt files with session strings (pyrogram/telethon/hex)
  • .json files (Telethon / Pyrogram / custom)
  • .session SQLite files (Telethon)
  • .zip archives containing any of the above
  • Raw hex 256-byte auth keys
  • Raw pyrogram string sessions
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
# VALIDATORS
# ═══════════════════════════════════════════════════
def is_valid_pyro_string(s: str) -> bool:
    """Check if a string is a valid Pyrogram v2 session string."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not (300 <= len(s) <= 5000):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    if not all(c in allowed for c in s):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        # Pyrogram v2 session: min 200 bytes
        return len(raw) >= 200
    except Exception:
        return False


def extract_hex(s: str):
    """Convert 512-char hex auth key to pyrogram string session."""
    s = s.strip()
    if len(s) == 512 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            key = bytes.fromhex(s)
            # Pyrogram v2 format: >BI?256sq?
            packed = struct.pack(">BI?256sq?", 2, API_ID, False, key, 0, False)
            return base64.urlsafe_b64encode(packed).decode().rstrip("=")
        except Exception as e:
            logger.debug(f"hex pack failed: {e}")
            return None
    return None


def extract_from_sqlite(db_path, api_id=API_ID):
    """Extract pyrogram string from a Telethon .session SQLite file."""
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()

            # Try sessions table
            c.execute("PRAGMA table_info(sessions)")
            cols = [r[1] for r in c.fetchall()]
            if "auth_key" not in cols or "dc_id" not in cols:
                return None

            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row:
                return None

            dc_id, auth_key = row
            if not auth_key or len(auth_key) != 256:
                logger.debug(f"bad auth_key len: {len(auth_key) if auth_key else 0}")
                return None

            # Pyrogram v2 packed format: >BI?256sq?
            packed = struct.pack(">BI?256sq?", int(dc_id), int(api_id),
                                 False, auth_key, 0, False)
            ss = base64.urlsafe_b64encode(packed).decode().rstrip("=")
            return ss
    except Exception as e:
        logger.debug(f"sqlite extract failed: {e}")
        return None


# ═══════════════════════════════════════════════════
# SINGLE STRING IMPORT
# ═══════════════════════════════════════════════════
async def import_any_string(s: str, uid: int) -> bool:
    """Try hex → telethon sqlite → pyrogram string."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()

    # 1. Try hex
    hexed = extract_hex(s)
    if hexed:
        s = hexed

    # 2. Try pyrogram string
    if is_valid_pyro_string(s):
        try:
            async with Client(
                f"imp_{uuid.uuid4().hex[:6]}",
                api_id=API_ID, api_hash=API_HASH,
                session_string=s,
                in_memory=True, no_updates=True,
            ) as app:
                me = await app.get_me()
                ss = await app.export_session_string()
                await save_account(me.phone_number, ss, me.first_name, uid,
                                   {"id": me.id})
                logger.info(f"✅ Imported {me.first_name} ({me.phone_number})")
                return True
        except Exception as e:
            logger.debug(f"pyro import failed: {e}")
            return False

    # 3. Try telethon-style (starts with '1' and different length)
    if len(s) > 350 and s[0] == "1":
        try:
            async with Client(
                f"tl_{uuid.uuid4().hex[:6]}",
                api_id=API_ID, api_hash=API_HASH,
                session_string=s,
                in_memory=True, no_updates=True,
            ) as app:
                me = await app.get_me()
                ss = await app.export_session_string()
                await save_account(me.phone_number, ss, me.first_name, uid,
                                   {"id": me.id})
                logger.info(f"✅ Imported (telethon style) {me.phone_number}")
                return True
        except Exception as e:
            logger.debug(f"telethon string import failed: {e}")

    return False


# ═══════════════════════════════════════════════════
# FILE IMPORT (any directory)
# ═══════════════════════════════════════════════════
async def import_file(path: str, uid: int, extract_dir: str) -> tuple:
    """Walk a directory and import every session found. Returns (ok, fail)."""
    ok = fail = 0

    # ─── JSON files ───
    for jf in glob.glob(f"{extract_dir}/**/*.json", recursive=True):
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug(f"json read fail {jf}: {e}")
            fail += 1
            continue

        # Extract possible session keys
        keys_to_try = [
            "session_file", "session_string", "session",
            "string_session", "StringSession",
        ]
        found = None
        for k in keys_to_try:
            if isinstance(data, dict) and data.get(k):
                found = data[k]
                break
        # Nested: {"data": {"session": "..."}}
        if not found and isinstance(data, dict):
            for v in data.values():
                if isinstance(v, dict):
                    for k in keys_to_try:
                        if v.get(k):
                            found = v[k]
                            break
                    if found:
                        break

        if not found:
            # Try companion .session file
            base = os.path.splitext(jf)[0]
            sql_path = base + ".session"
            if os.path.exists(sql_path):
                found = extract_from_sqlite(sql_path, API_ID)

        if found and await import_any_string(found, uid):
            ok += 1
        else:
            logger.debug(f"json no session: {jf}")
            fail += 1

    # ─── .session (Telethon SQLite) ───
    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        try:
            ss = extract_from_sqlite(sf, API_ID)
            if ss and await import_any_string(ss, uid):
                ok += 1
            else:
                logger.debug(f"session no extract: {sf}")
                fail += 1
        except Exception as e:
            logger.debug(f"session fail {sf}: {e}")
            fail += 1

    # ─── .txt (one string per line) ───
    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        try:
            with open(tf, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if await import_any_string(line, uid):
                            ok += 1
                        else:
                            fail += 1
        except Exception as e:
            logger.debug(f"txt fail {tf}: {e}")

    # ─── .hex files (raw hex) ───
    for hf in glob.glob(f"{extract_dir}/**/*.hex", recursive=True):
        try:
            with open(hf, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and await import_any_string(line, uid):
                        ok += 1
                    else:
                        fail += 1
        except Exception as e:
            logger.debug(f"hex fail {hf}: {e}")

    # ─── Any other extension with a valid string inside ───
    extensions_done = (".json", ".session", ".txt", ".hex")
    for f in glob.glob(f"{extract_dir}/**/*", recursive=True):
        if not os.path.isfile(f):
            continue
        if any(f.lower().endswith(ext) for ext in extensions_done):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                content = fp.read().strip()
            if content and await import_any_string(content, uid):
                ok += 1
        except Exception:
            pass

    return ok, fail


# ═══════════════════════════════════════════════════
# ZIP IMPORT
# ═══════════════════════════════════════════════════
async def import_zip(zip_path: str, uid: int) -> tuple:
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        # Look for nested zips and extract
        for inner_zip in glob.glob(f"{tmp}/**/*.zip", recursive=True):
            inner_dir = os.path.splitext(inner_zip)[0]
            os.makedirs(inner_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(inner_zip) as z:
                    z.extractall(inner_dir)
            except Exception as e:
                logger.debug(f"inner zip {inner_zip}: {e}")

        return await import_file(zip_path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════
# ZIP IMPORT (any file type)
# ═══════════════════════════════════════════════════
async def import_any_file(path: str, uid: int) -> tuple:
    """Entry point — handles zip, session, json, txt, or raw string file."""
    low = path.lower()

    if low.endswith(".zip"):
        return await import_zip(path, uid)

    # Otherwise: single file in its own temp dir
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        dest = os.path.join(tmp, os.path.basename(path))
        shutil.copy2(path, dest)
        return await import_file(path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)