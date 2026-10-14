"""
session_manager.py — bulletproof session importer (no proxy).
Uses Pyrogram's own session-string parser wherever possible.
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, re, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# LOGIN HELPER — logs EVERY outcome at INFO
# ═══════════════════════════════════════════════════════════════
async def _try_login(ss: str, uid: int, use_api_id: int,
                     use_api_hash: str, tag: str) -> bool:
    logger.info(f"   ▶ attempting {tag} (api_id={use_api_id})")
    try:
        async with Client(
            f"{tag}_{uuid.uuid4().hex[:6]}",
            api_id=use_api_id, api_hash=use_api_hash,
            session_string=ss,
            in_memory=True, no_updates=True,
        ) as app:
            me = await app.get_me()
            if me is None:
                logger.warning(f"   ❌ {tag}: get_me() returned None")
                return False
            ss2 = await app.export_session_string()
            phone = getattr(me, "phone_number", None) or f"id_{me.id}"
            name = getattr(me, "first_name", "?")
            await save_account(phone, ss2, name, uid, {"id": me.id})
            logger.info(f"   ✅ {tag} OK: {name} ({phone})")
            return True
    except Exception as e:
        logger.warning(f"   ❌ {tag} EXC {type(e).__name__}: {str(e)[:160]}")
        return False


# ═══════════════════════════════════════════════════════════════
# STRING FORMAT DETECTION
# ═══════════════════════════════════════════════════════════════
def _looks_like_pyrogram(s: str) -> bool:
    return bool(s) and s.startswith("BQ") and 300 < len(s) < 5000


def _looks_like_telethon(s: str) -> bool:
    return bool(s) and s.startswith("1") and 100 < len(s) < 2000


def _parse_dc_hex(s: str):
    """Return (dc_id, key_bytes) or None. dc_id=0 → try all."""
    if not s:
        return None
    s = s.strip()

    m = re.match(r"^(\d{1,2}):([0-9a-fA-F]+)", s)
    if m:
        hexkey = m.group(2)
        if len(hexkey) >= 512:
            hexkey = hexkey[:512]
        if len(hexkey) == 512:
            try:
                return int(m.group(1)), bytes.fromhex(hexkey)
            except Exception:
                return None
        return None

    if len(s) == 512 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return 0, bytes.fromhex(s)
        except Exception:
            return None
    return None


def hex_to_pyrogram_string(dc_id: int, key_bytes: bytes, api_id: int):
    packed = struct.pack(">BI?256sq?", dc_id, api_id, False, key_bytes, 0, False)
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


# ═══════════════════════════════════════════════════════════════
# UNIVERSAL IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_any_string(s: str, uid: int,
                            api_id_hint: int = None,
                            api_hash_hint: str = None) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 20:
        return False

    # Order of preference: hint > .env
    hint_id = api_id_hint or API_ID
    hint_hash = api_hash_hint or API_HASH

    # 1. Hex / DC:HEX
    dc_hex = _parse_dc_hex(s)
    if dc_hex:
        dc_id, key_bytes = dc_hex
        dcs = list(range(1, 6)) if dc_id == 0 else [dc_id]
        for try_dc in dcs:
            try:
                pyro_str = hex_to_pyrogram_string(try_dc, key_bytes, hint_id)
            except Exception as e:
                logger.info(f"   hex→str dc={try_dc}: {e}")
                continue
            if await _try_login(pyro_str, uid, hint_id, hint_hash,
                                f"hex{try_dc}"):
                return True
        return False

    # 2. Pyrogram string
    if _looks_like_pyrogram(s):
        # Try with hint first, then fallback to .env
        if await _try_login(s, uid, hint_id, hint_hash, "pyrogram"):
            return True
        if hint_id != API_ID:
            logger.info(f"   retry pyrogram with .env api_id={API_ID}")
            if await _try_login(s, uid, API_ID, API_HASH, "pyrogram_env"):
                return True
        return False

    # 3. Telethon string
    if _looks_like_telethon(s):
        if await _try_login(s, uid, hint_id, hint_hash, "telethon"):
            return True
        if hint_id != API_ID:
            if await _try_login(s, uid, API_ID, API_HASH, "telethon_env"):
                return True
        return False

    logger.info(f"   unrecognized string (len={len(s)}, prefix={s[:20]!r})")
    return False


# ═══════════════════════════════════════════════════════════════
# SQLITE IMPORT — extract api_id from file first
# ═══════════════════════════════════════════════════════════════
def extract_from_sqlite(db_path):
    """
    Return (pyrogram_string, api_id) or (None, None).
    Reads api_id from the SQLite sessions table itself.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(sessions)")
            cols = {r[1] for r in c.fetchall()}

            if "auth_key" not in cols or "dc_id" not in cols:
                logger.info(f"   sqlite: missing auth_key/dc_id cols={cols}")
                return None, None

            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row:
                logger.info("   sqlite: empty sessions table")
                return None, None

            dc_id, auth_key = row
            if not auth_key or len(auth_key) != 256:
                logger.info(f"   sqlite: bad key len {len(auth_key) if auth_key else 0}")
                return None, None

            stored_api_id = None
            if "api_id" in cols:
                c.execute("SELECT api_id FROM sessions LIMIT 1")
                srow = c.fetchone()
                if srow and srow[0]:
                    stored_api_id = int(srow[0])

            use_id = stored_api_id or API_ID
            packed = struct.pack(">BI?256sq?", int(dc_id), use_id,
                                 False, auth_key, 0, False)
            ss = base64.urlsafe_b64encode(packed).decode().rstrip("=")
            logger.info(f"   sqlite extracted: dc={dc_id} api_id={use_id}")
            return ss, use_id
    except Exception as e:
        logger.info(f"   sqlite EXC: {e}")
        return None, None


# ═══════════════════════════════════════════════════════════════
# JSON IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_json_file(jf: str, uid: int) -> bool:
    logger.info(f"📄 JSON: {os.path.basename(jf)}")
    try:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"   ❌ read: {e}")
        return False

    if not isinstance(data, dict):
        return False

    json_api_id = data.get("api_id") or data.get("app_id")
    json_api_hash = data.get("api_hash") or data.get("app_hash")
    try:
        json_api_id = int(json_api_id) if json_api_id else None
    except Exception:
        json_api_id = None

    candidates = []
    for key in ("pyrogram_string", "session_string", "session",
                "string_session", "StringSession",
                "telethon_string", "hex_auth_key"):
        v = data.get(key)
        if v and isinstance(v, str) and v not in ("null", "None", ""):
            candidates.append((key, v.strip()))

    if not candidates:
        logger.warning(f"   ❌ no session key. keys={list(data.keys())}")
        return False

    logger.info(f"   api_id in JSON: {json_api_id}")

    for name, val in candidates:
        logger.info(f"   ── {name} (len={len(val)})")
        try:
            if await import_any_string(val, uid,
                                       api_id_hint=json_api_id,
                                       api_hash_hint=json_api_hash):
                return True
        except Exception as e:
            logger.warning(f"   {name} raised: {e}")
    return False


# ═══════════════════════════════════════════════════════════════
# WALK EXTRACTED DIR
# ═══════════════════════════════════════════════════════════════
async def import_file(path: str, uid: int, extract_dir: str) -> tuple:
    ok = fail = 0
    all_files = []
    for root, _, files in os.walk(extract_dir):
        for f in files:
            all_files.append(os.path.join(root, f))

    logger.info(f"📦 Archive: {len(all_files)} file(s)")

    # JSON
    for jf in glob.glob(f"{extract_dir}/**/*.json", recursive=True):
        if await import_json_file(jf, uid):
            ok += 1
        else:
            fail += 1

    # SQLite
    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        base = os.path.basename(sf)
        ss, used_api_id = extract_from_sqlite(sf)
        if not ss:
            continue
        logger.info(f"🗄️  SQLite: {base} (api_id={used_api_id})")
        if await _try_login(ss, uid, used_api_id or API_ID, API_HASH, "sqlite"):
            ok += 1
        else:
            fail += 1

    # TXT
    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        try:
            with open(tf, "r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if not (re.match(r"^\d{1,2}:[0-9a-fA-F]", line) or
                            line.startswith("BQ") or
                            (line.startswith("1") and len(line) > 100) or
                            len(line) == 512):
                        continue
                    if await import_any_string(line, uid):
                        ok += 1
                    else:
                        logger.warning(f"   ❌ line {ln} failed")
                        fail += 1
        except Exception as e:
            logger.warning(f"   TXT read: {e}")

    logger.info(f"🏁 Import: ✅ {ok} | ❌ {fail}")
    return ok, fail


async def import_zip(zip_path: str, uid: int) -> tuple:
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        for inner_zip in glob.glob(f"{tmp}/**/*.zip", recursive=True):
            inner_dir = os.path.splitext(inner_zip)[0]
            os.makedirs(inner_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(inner_zip) as z:
                    z.extractall(inner_dir)
            except Exception:
                pass
        return await import_file(zip_path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def import_any_file(path: str, uid: int) -> tuple:
    if path.lower().endswith(".zip"):
        return await import_zip(path, uid)
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        dest = os.path.join(tmp, os.path.basename(path))
        shutil.copy2(path, dest)
        return await import_file(path, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)