"""
session_manager.py — supports:
  • Pyrogram strings (BQ...)
  • Telethon strings (1...)
  • DC:HEX format (5:68610c...)    ← your format
  • Plain hex (512 chars)
  • JSON with telegram/pyrogram/telethon strings
  • SQLite .session (Telethon)
  • ZIP archives
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, re, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# VALIDATORS
# ═══════════════════════════════════════════════════════════════
def is_valid_pyrogram_string(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    # Pyrogram v2 strings start with BQ, 300-500 chars
    if not (s.startswith("BQ") and 300 <= len(s) <= 5000):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    if not all(c in allowed for c in s):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        return len(raw) >= 200
    except Exception:
        return False


def is_valid_telethon_string(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not (s.startswith("1") and 100 <= len(s) <= 2000):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    if not all(c in allowed for c in s):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        return 200 <= len(raw) <= 600
    except Exception:
        return False


def _parse_dc_hex(s: str):
    """
    Parse '5:68610c3f...' format → (dc_id, key_bytes).
    Handles both plain '5:hex' and '5:hexp' (trailing garbage char).
    """
    s = s.strip()

    # Format: DC:HEX
    m = re.match(r"^(\d{1,2}):([0-9a-fA-F]+)", s)
    if m:
        try:
            dc_id = int(m.group(1))
            hexkey = m.group(2)
            # Trim to 512 hex chars (256 bytes)
            if len(hexkey) >= 512:
                hexkey = hexkey[:512]
            if len(hexkey) == 512:
                return dc_id, bytes.fromhex(hexkey)
        except Exception:
            return None

    # Plain hex 512 chars
    if len(s) == 512 and all(c in "0123456789abcdefABCDEF" for c in s):
        return 2, bytes.fromhex(s)

    return None


def hex_to_pyrogram_string(dc_id: int, key_bytes: bytes):
    """Convert DC + 256-byte auth key → Pyrogram v2 session string."""
    packed = struct.pack(">BI?256sq?", dc_id, API_ID, False, key_bytes, 0, False)
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def extract_from_sqlite(db_path, api_id=API_ID):
    """Telethon .session → pyrogram string."""
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(sessions)")
            cols = [r[1] for r in c.fetchall()]
            if "auth_key" not in cols or "dc_id" not in cols:
                logger.debug(f"  sqlite: missing columns {cols}")
                return None
            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row:
                logger.debug("  sqlite: sessions table empty")
                return None
            dc_id, auth_key = row
            if not auth_key or len(auth_key) != 256:
                logger.debug(f"  sqlite: bad key length {len(auth_key) if auth_key else 0}")
                return None
            return hex_to_pyrogram_string(int(dc_id), auth_key)
    except Exception as e:
        logger.debug(f"  sqlite: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# SINGLE STRING IMPORT (the core)
# ═══════════════════════════════════════════════════════════════
async def import_any_string(s: str, uid: int) -> bool:
    """Try every known string format."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 20:
        return False

    # ─── 1. DC:HEX format (your main format) ───
    dc_hex = _parse_dc_hex(s)
    if dc_hex:
        dc_id, key_bytes = dc_hex
        try:
            pyro_str = hex_to_pyrogram_string(dc_id, key_bytes)
            async with Client(
                f"hex_{uuid.uuid4().hex[:6]}",
                api_id=API_ID, api_hash=API_HASH,
                session_string=pyro_str,
                in_memory=True, no_updates=True,
            ) as app:
                me = await app.get_me()
                ss = await app.export_session_string()
                await save_account(me.phone_number, ss, me.first_name, uid,
                                   {"id": me.id})
                logger.info(f"  ✅ HEX OK: {me.first_name} ({me.phone_number})")
                return True
        except Exception as e:
            logger.warning(f"  ❌ hex login failed: {str(e)[:120]}")

    # ─── 2. Pyrogram string ───
    if is_valid_pyrogram_string(s):
        try:
            async with Client(
                f"py_{uuid.uuid4().hex[:6]}",
                api_id=API_ID, api_hash=API_HASH,
                session_string=s,
                in_memory=True, no_updates=True,
            ) as app:
                me = await app.get_me()
                ss = await app.export_session_string()
                await save_account(me.phone_number, ss, me.first_name, uid,
                                   {"id": me.id})
                logger.info(f"  ✅ PYROGRAM OK: {me.first_name} ({me.phone_number})")
                return True
        except Exception as e:
            logger.warning(f"  ❌ pyrogram failed: {str(e)[:120]}")

    # ─── 3. Telethon string ───
    if is_valid_telethon_string(s):
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
                logger.info(f"  ✅ TELETHON OK: {me.first_name} ({me.phone_number})")
                return True
        except Exception as e:
            logger.warning(f"  ❌ telethon failed: {str(e)[:120]}")

    logger.debug(f"  not recognized (len={len(s)}, prefix={s[:20]!r})")
    return False


# ═══════════════════════════════════════════════════════════════
# SMART JSON IMPORT — knows your key names
# ═══════════════════════════════════════════════════════════════
async def import_json_file(jf: str, uid: int) -> bool:
    """Handle your JSON format: telethon_string / pyrogram_string / hex_auth_key."""
    logger.info(f"📄 JSON: {os.path.basename(jf)}")
    try:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"   ❌ read fail: {e}")
        return False

    if not isinstance(data, dict):
        return False

    # Priority: hex_auth_key > telethon_string > pyrogram_string > anything else
    candidates = []

    hex_key = data.get("hex_auth_key")
    if hex_key:
        candidates.append(("hex_auth_key", hex_key))

    tl_str = data.get("telethon_string")
    if tl_str and tl_str not in (None, "", "null"):
        candidates.append(("telethon_string", tl_str))

    py_str = data.get("pyrogram_string")
    if py_str and py_str not in (None, "", "null"):
        candidates.append(("pyrogram_string", py_str))

    # Other common key names
    for k in ["session_string", "session", "string_session", "StringSession"]:
        if data.get(k):
            candidates.append((k, data[k]))

    if not candidates:
        logger.warning(f"   ❌ no session key found. Keys: {list(data.keys())}")
        return False

    for name, val in candidates:
        logger.info(f"   trying {name} (len={len(str(val))})")
        if await import_any_string(str(val), uid):
            return True

    logger.warning(f"   ❌ all keys failed in {os.path.basename(jf)}")
    return False


# ═══════════════════════════════════════════════════════════════
# FILE IMPORT — walk directory
# ═══════════════════════════════════════════════════════════════
async def import_file(path: str, uid: int, extract_dir: str) -> tuple:
    ok = fail = 0

    # Log what we found
    all_files = []
    for root, _, files in os.walk(extract_dir):
        for f in files:
            all_files.append(os.path.join(root, f))

    logger.info(f"📦 Archive: {len(all_files)} file(s)")
    for f in all_files[:25]:
        try:
            sz = os.path.getsize(f)
        except Exception:
            sz = "?"
        logger.info(f"   • {os.path.relpath(f, extract_dir)} ({sz} bytes)")

    # ─── JSON files (your format) ───
    json_files = glob.glob(f"{extract_dir}/**/*.json", recursive=True)
    for jf in json_files:
        if await import_json_file(jf, uid):
            ok += 1
        else:
            fail += 1

    # ─── .session SQLite ───
    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        base = os.path.basename(sf)
        logger.info(f"🗄️  SQLite: {base}")

        # Check if there's a companion .json we already imported
        # (avoid counting twice)
        ss = extract_from_sqlite(sf, API_ID)
        if ss and await import_any_string(ss, uid):
            ok += 1
        else:
            logger.warning(f"   ❌ sqlite skipped (dead or invalid)")
            # Don't count as fail if json succeeded for same phone
            # Just skip silently
            # fail += 1

    # ─── .txt with DC:HEX lines ───
    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        base = os.path.basename(tf)
        logger.info(f"📝 TXT: {base}")
        try:
            with open(tf, "r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Skip README-like lines
                    if not re.match(r"^\d{1,2}:[0-9a-fA-F]", line) and \
                       not line.startswith("BQ") and \
                       not line.startswith("1") and \
                       len(line) != 512:
                        continue
                    if await import_any_string(line, uid):
                        ok += 1
                    else:
                        logger.warning(f"   ❌ line {ln} failed")
                        fail += 1
        except Exception as e:
            logger.warning(f"   ❌ read fail: {e}")

    logger.info(f"🏁 Import: ✅ {ok} | ❌ {fail}")
    return ok, fail


# ═══════════════════════════════════════════════════════════════
# ZIP
# ═══════════════════════════════════════════════════════════════
async def import_zip(zip_path: str, uid: int) -> tuple:
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            logger.info(f"🗜️  ZIP: {len(names)} entries")
            z.extractall(tmp)

        for inner_zip in glob.glob(f"{tmp}/**/*.zip", recursive=True):
            inner_dir = os.path.splitext(inner_zip)[0]
            os.makedirs(inner_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(inner_zip) as z:
                    z.extractall(inner_dir)
            except Exception as e:
                logger.debug(f"inner zip: {e}")

        return await import_file(zip_path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def import_any_file(path: str, uid: int) -> tuple:
    low = path.lower()
    if low.endswith(".zip"):
        return await import_zip(path, uid)

    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        dest = os.path.join(tmp, os.path.basename(path))
        shutil.copy2(path, dest)
        return await import_file(path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)