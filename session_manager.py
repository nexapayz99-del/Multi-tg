"""
session_manager.py — imports from every session format:
  • Pyrogram string sessions
  • Telethon string sessions
  • Raw hex 256-byte auth keys
  • Telethon .session SQLite files
  • JSON files (Telethon / Pyrogram / custom)
  • ZIP archives (with nested zips)
  • TXT files (one string per line)
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# VALIDATORS
# ═══════════════════════════════════════════════════════════════
def is_valid_pyrogram_string(s: str) -> bool:
    """Pyrogram v2 string session check."""
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
        return len(raw) >= 200
    except Exception:
        return False


def is_valid_telethon_string(s: str) -> bool:
    """
    Telethon StringSession check.
    Format: base64 of a struct with dc_id + auth_key + more.
    Typically starts with '1' and is 200-500 chars.
    """
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not (100 <= len(s) <= 1000):
        return False
    if not s.startswith("1"):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        # Telethon stores: version + dc_id + api_id + auth_key (+ optional)
        # Minimal length is 1 + 1 + 4 + 256 = 262 or similar
        return 200 <= len(raw) <= 600
    except Exception:
        return False


def extract_hex(s: str):
    """Convert 512-char hex auth key → pyrogram string session."""
    s = s.strip()
    if len(s) != 512:
        return None
    if not all(c in "0123456789abcdefABCDEF" for c in s):
        return None
    try:
        key = bytes.fromhex(s)
        packed = struct.pack(">BI?256sq?", 2, API_ID, False, key, 0, False)
        return base64.urlsafe_b64encode(packed).decode().rstrip("=")
    except Exception as e:
        logger.debug(f"hex pack failed: {e}")
        return None


def extract_from_sqlite(db_path, api_id=API_ID):
    """Extract pyrogram string session from a Telethon .session SQLite file."""
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(sessions)")
            cols = [r[1] for r in c.fetchall()]
            if "auth_key" not in cols or "dc_id" not in cols:
                logger.debug(f"  sqlite: missing columns (have: {cols})")
                return None

            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row:
                logger.debug(f"  sqlite: sessions table empty")
                return None

            dc_id, auth_key = row
            if not auth_key or len(auth_key) != 256:
                logger.debug(f"  sqlite: bad auth_key length {len(auth_key) if auth_key else 0}")
                return None

            packed = struct.pack(">BI?256sq?", int(dc_id), int(api_id),
                                 False, auth_key, 0, False)
            return base64.urlsafe_b64encode(packed).decode().rstrip("=")
    except Exception as e:
        logger.debug(f"  sqlite: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# SINGLE STRING IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_any_string(s: str, uid: int) -> bool:
    """Try every known string format against a single string."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 50:
        return False

    # 1. Raw hex
    hexed = extract_hex(s)
    if hexed:
        s = hexed
        logger.info("  🔑 detected hex auth key")

    # 2. Pyrogram string
    if is_valid_pyrogram_string(s):
        try:
            async with Client(
                f"py_imp_{uuid.uuid4().hex[:6]}",
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
            logger.warning(f"  ❌ pyrogram failed: {str(e)[:100]}")

    # 3. Telethon string
    if is_valid_telethon_string(s):
        try:
            async with Client(
                f"tl_imp_{uuid.uuid4().hex[:6]}",
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
            logger.warning(f"  ❌ telethon failed: {str(e)[:100]}")

    logger.debug(f"  string not recognized (len={len(s)}, prefix={s[:15]!r})")
    return False


# ═══════════════════════════════════════════════════════════════
# FILE IMPORT (walk a directory)
# ═══════════════════════════════════════════════════════════════
async def import_file(path: str, uid: int, extract_dir: str) -> tuple:
    ok = fail = 0

    # Log every file we found
    all_files = []
    for root, _, files in os.walk(extract_dir):
        for f in files:
            all_files.append(os.path.join(root, f))
    logger.info(f"📦 Archive contains {len(all_files)} file(s):")
    for f in all_files[:30]:
        try:
            sz = os.path.getsize(f)
        except Exception:
            sz = "?"
        logger.info(f"   • {os.path.relpath(f, extract_dir)} ({sz} bytes)")

    # ─── JSON ───
    for jf in glob.glob(f"{extract_dir}/**/*.json", recursive=True):
        logger.info(f"📄 JSON: {os.path.basename(jf)}")
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"   ❌ read fail: {e}")
            fail += 1
            continue

        # Common key names
        keys = ["session_file", "session_string", "session",
                "string_session", "StringSession"]
        found = None
        if isinstance(data, dict):
            for k in keys:
                if data.get(k):
                    found = data[k]
                    break
            if not found:
                for v in data.values():
                    if isinstance(v, dict):
                        for k in keys:
                            if v.get(k):
                                found = v[k]
                                break
                        if found:
                            break

        # Companion .session file
        if not found:
            sql = os.path.splitext(jf)[0] + ".session"
            if os.path.exists(sql):
                found = extract_from_sqlite(sql, API_ID)
                if found:
                    logger.info(f"   📎 extracted from companion {sql}")

        if found and await import_any_string(found, uid):
            ok += 1
        else:
            logger.warning(f"   ❌ no session in JSON")
            fail += 1

    # ─── .session SQLite ───
    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        logger.info(f"🗄️  SQLite: {os.path.basename(sf)}")
        ss = extract_from_sqlite(sf, API_ID)
        if ss and await import_any_string(ss, uid):
            ok += 1
        else:
            logger.warning(f"   ❌ sqlite extract failed")
            fail += 1

    # ─── .txt ───
    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        logger.info(f"📝 TXT: {os.path.basename(tf)}")
        try:
            with open(tf, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            for ln, line in enumerate(lines, 1):
                if await import_any_string(line, uid):
                    ok += 1
                else:
                    logger.warning(f"   ❌ line {ln} invalid")
                    fail += 1
        except Exception as e:
            logger.warning(f"   ❌ read fail: {e}")

    # ─── .hex ───
    for hf in glob.glob(f"{extract_dir}/**/*.hex", recursive=True):
        logger.info(f"🔑 HEX: {os.path.basename(hf)}")
        try:
            with open(hf, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        if await import_any_string(line, uid):
                            ok += 1
                        else:
                            fail += 1
        except Exception as e:
            logger.warning(f"   ❌ read fail: {e}")

    # ─── Any other file with a session string inside ───
    known_exts = (".json", ".session", ".txt", ".hex")
    for f in all_files:
        if any(f.lower().endswith(ext) for ext in known_exts):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                content = fp.read().strip()
            if content and await import_any_string(content, uid):
                ok += 1
        except Exception:
            pass

    logger.info(f"🏁 Import result: ✅ {ok} | ❌ {fail}")
    return ok, fail


# ═══════════════════════════════════════════════════════════════
# ZIP IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_zip(zip_path: str, uid: int) -> tuple:
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            logger.info(f"🗜️  ZIP: {len(names)} entries")
            for n in names[:20]:
                logger.info(f"   - {n}")
            z.extractall(tmp)

        # Recursive nested zips
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


# ═══════════════════════════════════════════════════════════════
# UNIVERSAL ENTRY POINT
# ═══════════════════════════════════════════════════════════════
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