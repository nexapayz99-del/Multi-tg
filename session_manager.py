"""
session_manager.py — bulletproof session importer (with proxy support)

Supported:
  • Pyrogram v2 strings (BQ..., Ag...)
  • Telethon strings (1...)
  • DC:HEX format (5:68610c3f...)
  • Plain hex (512 or 514 chars)
  • JSON files (pyrogram_string / telethon_string / hex_auth_key)
  • SQLite .session (Pyrogram AND Telethon schemas)
  • TXT files
  • ZIP archives
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, re, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account
from utils.proxy import get_proxy

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# VALIDATORS
# ═══════════════════════════════════════════════════════════════
def is_valid_pyrogram_string(s: str) -> bool:
    """Accept BQ... AND Ag... pyrogram strings."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not (s.startswith("BQ") or s.startswith("Ag")):
        return False
    if not (200 <= len(s) <= 5000):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    if not all(c in allowed for c in s):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        return 200 <= len(raw) <= 500
    except Exception:
        return False


def is_valid_telethon_string(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not s.startswith("1"):
        return False
    if not (100 <= len(s) <= 2000):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        return 200 <= len(raw) <= 600
    except Exception:
        return False


def _parse_dc_hex(s: str):
    """Parse DC:HEX → (dc_id, key_bytes). Accepts 512/514-char hex."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()

    # Format "5:68610c3f..."
    m = re.match(r"^(\d{1,2}):([0-9a-fA-F]+)", s)
    if m:
        try:
            dc_id = int(m.group(1))
            hexkey = m.group(2)
            if len(hexkey) >= 512:
                hexkey = hexkey[:512]
            if len(hexkey) == 512:
                return dc_id, bytes.fromhex(hexkey)
        except Exception:
            return None

    # Plain hex — accept 512 or 514 chars (some exporters add 2 trailing bytes)
    if len(s) in (512, 514) and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return 2, bytes.fromhex(s[:512])
        except Exception:
            return None

    return None


def hex_to_pyrogram_string(dc_id: int, key_bytes: bytes, api_id: int = None):
    api_id = api_id or API_ID
    packed = struct.pack(">BI?256sq?", dc_id, api_id, False, key_bytes, 0, False)
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def extract_from_sqlite(db_path, api_id=None):
    """Return (session_string, api_id) or (None, None)."""
    api_id = api_id or API_ID
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(sessions)")
            cols = [r[1] for r in c.fetchall()]

            if "auth_key" not in cols or "dc_id" not in cols:
                return None, None

            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row:
                return None, None

            dc_id, auth_key = row
            if not auth_key or len(auth_key) != 256:
                return None, None

            stored_api_id = api_id
            if "api_id" in cols:
                c.execute("SELECT api_id FROM sessions LIMIT 1")
                srow = c.fetchone()
                if srow and srow[0]:
                    stored_api_id = int(srow[0])

            packed = struct.pack(">BI?256sq?", int(dc_id), stored_api_id,
                                 False, auth_key, 0, False)
            ss = base64.urlsafe_b64encode(packed).decode().rstrip("=")
            return ss, stored_api_id
    except Exception as e:
        logger.debug(f"  sqlite: {e}")
        return None, None


# ═══════════════════════════════════════════════════════════════
# CORE IMPORT
# ═══════════════════════════════════════════════════════════════
async def _try_login_pyrogram(session_string: str, uid: int,
                              api_id: int, api_hash: str,
                              tag: str = "?") -> bool:
    """Common login path — returns True on success."""
    try:
        async with Client(
            f"im_{uuid.uuid4().hex[:6]}",
            api_id=api_id, api_hash=api_hash,
            session_string=session_string,
            in_memory=True, no_updates=True,
            proxy=get_proxy(),
        ) as app:
            me = await app.get_me()
            ss = await app.export_session_string()
            await save_account(me.phone_number, ss, me.first_name, uid,
                               {"id": me.id})
            logger.info(f"  ✅ {tag} OK: {me.first_name} ({me.phone_number})")
            return True
    except Exception as e:
        logger.warning(f"  ❌ {tag} failed: {str(e)[:160]}")
        return False


async def import_any_string(s: str, uid: int,
                            api_id_hint: int = None,
                            api_hash_hint: str = None,
                            label: str = "") -> bool:
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 20:
        return False

    use_api_id = api_id_hint or API_ID
    use_api_hash = api_hash_hint or API_HASH

    # ─── 1. DC:HEX or plain hex ───
    dc_hex = _parse_dc_hex(s)
    if dc_hex:
        dc_id, key_bytes = dc_hex
        try:
            pyro_str = hex_to_pyrogram_string(dc_id, key_bytes, use_api_id)
        except Exception as e:
            logger.warning(f"  ❌ hex→string fail: {e}")
            return False
        return await _try_login_pyrogram(pyro_str, uid,
                                         use_api_id, use_api_hash,
                                         tag="HEX")

    # ─── 2. Pyrogram string (BQ... / Ag...) ───
    if is_valid_pyrogram_string(s):
        return await _try_login_pyrogram(s, uid,
                                         use_api_id, use_api_hash,
                                         tag="PYROGRAM")

    # ─── 3. Telethon string (1...) ───
    if is_valid_telethon_string(s):
        return await _try_login_pyrogram(s, uid,
                                         use_api_id, use_api_hash,
                                         tag="TELETHON")

    logger.debug(f"  not recognized (len={len(s)}, prefix={s[:20]!r})")
    return False


# ═══════════════════════════════════════════════════════════════
# JSON IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_json_file(jf: str, uid: int) -> bool:
    logger.info(f"📄 JSON: {os.path.basename(jf)}")
    try:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"   ❌ read fail: {e}")
        return False

    if not isinstance(data, dict):
        return False

    json_api_id = data.get("api_id") or data.get("app_id")
    json_api_hash = data.get("api_hash") or data.get("app_hash")
    if json_api_id:
        try:
            json_api_id = int(json_api_id)
        except Exception:
            json_api_id = None

    tried_any = False
    for key in ("pyrogram_string", "session_string", "session",
                "string_session", "StringSession",
                "hex_auth_key", "telethon_string",
                "auth_key", "key"):
        v = data.get(key)
        if not v or not isinstance(v, str) or v in ("null", "None"):
            continue
        tried_any = True
        logger.info(f"   trying {key} (len={len(v)})")
        if await import_any_string(v, uid,
                                   api_id_hint=json_api_id,
                                   api_hash_hint=json_api_hash,
                                   label=key):
            return True

    if not tried_any:
        logger.warning(f"   ❌ no session key. Keys: {list(data.keys())}")
    else:
        logger.warning(f"   ❌ all keys failed in {os.path.basename(jf)}")
    return False


# ═══════════════════════════════════════════════════════════════
# FILE IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_file(path: str, uid: int, extract_dir: str) -> tuple:
    ok = fail = 0

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

    # ─── JSON ───
    for jf in glob.glob(f"{extract_dir}/**/*.json", recursive=True):
        if await import_json_file(jf, uid):
            ok += 1
        else:
            fail += 1

    # ─── .session SQLite ───
    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        base = os.path.basename(sf)
        ss, used_api_id = extract_from_sqlite(sf, API_ID)
        if not ss:
            logger.debug(f"   skip {base}: no valid schema")
            continue
        logger.info(f"🗄️  SQLite: {base} (api_id={used_api_id})")
        if await _try_login_pyrogram(ss, uid, used_api_id, API_HASH,
                                     tag="SQLITE"):
            ok += 1
        else:
            fail += 1

    # ─── .txt ───
    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        base = os.path.basename(tf)
        logger.info(f"📝 TXT: {base}")
        try:
            with open(tf, "r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if not (re.match(r"^\d{1,2}:[0-9a-fA-F]", line) or
                            line.startswith("BQ") or
                            line.startswith("Ag") or
                            line.startswith("1") or
                            len(line) in (512, 514)):
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
# ZIP / FILE ENTRY
# ═══════════════════════════════════════════════════════════════
async def import_zip(zip_path: str, uid: int) -> tuple:
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            logger.info(f"🗜️  ZIP: {len(z.namelist())} entries")
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