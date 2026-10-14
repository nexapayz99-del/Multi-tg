"""
session_manager.py — diagnostic session importer (no proxy).
Tells you WHY a session fails, not just that it did.
"""
import os, json, glob, sqlite3, struct, base64, zipfile, shutil, uuid, re, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import save_account

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# DEEP PARSER — inspect what's inside the string
# ═══════════════════════════════════════════════════════════════
def inspect_pyrogram_string(s: str):
    """
    Pyrogram v2 format: base64( >BI?256sQ? )
      B  = dc_id (1 byte)
      I  = api_id (4 bytes)
      ?  = test_mode (1 byte)
      256s = auth_key (256 bytes)
      Q  = user_id (8 bytes)
      ?  = is_bot (1 byte)
    Total = 1+4+1+256+8+1 = 271 bytes → base64 ≈ 362 chars
    """
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
    except Exception as e:
        return f"base64 fail: {e}"

    if len(raw) != 271:
        return f"WRONG LENGTH {len(raw)} (expected 271) — string is malformed"

    try:
        dc_id, api_id, test, auth_key, user_id, is_bot = struct.unpack(
            ">BI?256sQ?", raw)
    except Exception as e:
        return f"struct fail: {e}"

    return {
        "dc_id": dc_id,
        "api_id": api_id,
        "test_mode": bool(test),
        "auth_key_preview": auth_key[:4].hex(),
        "user_id": user_id,
        "is_bot": bool(is_bot),
    }


def inspect_telethon_string(s: str):
    """
    Telethon format is trickier. Len varies. If it decodes < 200 bytes
    it's almost certainly not a real Telethon session string.
    """
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
    except Exception as e:
        return f"base64 fail: {e}"
    return f"decoded {len(raw)} bytes (real Telethon ~271+)"


# ═══════════════════════════════════════════════════════════════
# LOGIN
# ═══════════════════════════════════════════════════════════════
async def _try_login(ss, uid, api_id, api_hash, tag):
    logger.info(f"   ▶ {tag} login (api_id={api_id})")
    try:
        async with Client(
            f"{tag}_{uuid.uuid4().hex[:6]}",
            api_id=api_id, api_hash=api_hash,
            session_string=ss,
            in_memory=True, no_updates=True,
        ) as app:
            me = await app.get_me()
            if me is None:
                logger.warning(f"   ❌ {tag}: get_me() None")
                return False
            phone = getattr(me, "phone_number", None) or f"id_{me.id}"
            name = getattr(me, "first_name", "?")
            ss2 = await app.export_session_string()
            await save_account(phone, ss2, name, uid, {"id": me.id})
            logger.info(f"   ✅ {tag} OK: {name} ({phone})")
            return True
    except Exception as e:
        logger.warning(f"   ❌ {tag} EXC {type(e).__name__}: {str(e)[:180]}")
        return False


# ═══════════════════════════════════════════════════════════════
# HEX
# ═══════════════════════════════════════════════════════════════
def parse_dc_hex(s):
    if not s:
        return None
    s = s.strip()

    m = re.match(r"^(\d{1,2}):([0-9a-fA-F]+)", s)
    if m:
        try:
            dc = int(m.group(1))
            if dc < 1 or dc > 5:
                logger.info(f"   hex DC {dc} out of range 1..5")
                return None
            hx = m.group(2)
            if len(hx) < 512:
                logger.info(f"   hex too short: {len(hx)}")
                return None
            if len(hx) > 512:
                hx = hx[:512]
            return dc, bytes.fromhex(hx)
        except Exception as e:
            logger.info(f"   hex parse fail: {e}")
            return None

    if len(s) == 512 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return 0, bytes.fromhex(s)
        except Exception:
            return None
    return None


def hex_to_pyrogram_string(dc, key, api_id):
    packed = struct.pack(">BI?256sq?", dc, api_id, False, key, 0, False)
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


# ═══════════════════════════════════════════════════════════════
# UNIVERSAL IMPORT
# ═══════════════════════════════════════════════════════════════
async def import_any_string(s, uid, api_id_hint=None, api_hash_hint=None):
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 20:
        return False

    use_id = api_id_hint or API_ID
    use_hash = api_hash_hint or API_HASH

    # ── 1. Hex ──
    hx = parse_dc_hex(s)
    if hx:
        dc, key = hx
        dcs = list(range(1, 6)) if dc == 0 else [dc]
        for d in dcs:
            try:
                ps = hex_to_pyrogram_string(d, key, use_id)
            except Exception as e:
                logger.info(f"   hex pack dc={d}: {e}")
                continue
            if await _try_login(ps, uid, use_id, use_hash, f"hex{d}"):
                return True
        return False

    # ── 2. Pyrogram ──
    if s.startswith("BQ"):
        info = inspect_pyrogram_string(s)
        logger.info(f"   pyrogram string inspect: {info}")
        if isinstance(info, dict):
            embedded_id = info["api_id"]
            logger.info(f"   embedded api_id={embedded_id}, will try both")
            # try embedded api_id first
            if embedded_id and embedded_id != use_id:
                if await _try_login(s, uid, embedded_id, use_hash, "pyro_embed"):
                    return True
            if await _try_login(s, uid, use_id, use_hash, "pyrogram"):
                return True
        else:
            logger.warning(f"   bad pyrogram string: {info}")
        return False

    # ── 3. Telethon ──
    if s.startswith("1"):
        info = inspect_telethon_string(s)
        logger.info(f"   telethon string inspect: {info}")
        if await _try_login(s, uid, use_id, use_hash, "telethon"):
            return True
        if use_id != API_ID:
            if await _try_login(s, uid, API_ID, API_HASH, "telethon_env"):
                return True
        return False

    logger.info(f"   unrecognized (len={len(s)}, prefix={s[:10]!r})")
    return False


# ═══════════════════════════════════════════════════════════════
# JSON
# ═══════════════════════════════════════════════════════════════
async def import_json_file(jf, uid):
    logger.info(f"📄 {os.path.basename(jf)}")
    try:
        data = json.load(open(jf, "r", encoding="utf-8"))
    except Exception as e:
        logger.warning(f"   read: {e}")
        return False

    if not isinstance(data, dict):
        return False

    jid = data.get("api_id") or data.get("app_id")
    jhash = data.get("api_hash") or data.get("app_hash")
    try:
        jid = int(jid) if jid else None
    except Exception:
        jid = None

    logger.info(f"   JSON api_id={jid}")

    for key in ("pyrogram_string", "session_string", "session",
                "string_session", "StringSession",
                "telethon_string", "hex_auth_key"):
        v = data.get(key)
        if not v or not isinstance(v, str) or v in ("null", "None"):
            continue
        logger.info(f"   ── {key} (len={len(v)})")
        if await import_any_string(v.strip(), uid,
                                   api_id_hint=jid,
                                   api_hash_hint=jhash):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# SQLITE
# ═══════════════════════════════════════════════════════════════
def extract_from_sqlite(db_path):
    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(sessions)")
            cols = {r[1] for r in c.fetchall()}
            if "auth_key" not in cols or "dc_id" not in cols:
                logger.info(f"   sqlite: cols={cols} (no auth_key/dc_id)")
                return None, None
            c.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1")
            row = c.fetchone()
            if not row:
                return None, None
            dc, key = row
            if not key or len(key) != 256:
                logger.info(f"   sqlite: bad key len {len(key) if key else 0}")
                return None, None
            aid = None
            if "api_id" in cols:
                c.execute("SELECT api_id FROM sessions LIMIT 1")
                r2 = c.fetchone()
                if r2 and r2[0]:
                    aid = int(r2[0])
            use = aid or API_ID
            packed = struct.pack(">BI?256sq?", int(dc), use,
                                 False, key, 0, False)
            ss = base64.urlsafe_b64encode(packed).decode().rstrip("=")
            logger.info(f"   sqlite: dc={dc} api_id={use}")
            return ss, use
    except Exception as e:
        logger.info(f"   sqlite EXC: {e}")
        return None, None


# ═══════════════════════════════════════════════════════════════
# WALK / ZIP
# ═══════════════════════════════════════════════════════════════
async def import_file(path, uid, extract_dir):
    ok = fail = 0

    for jf in glob.glob(f"{extract_dir}/**/*.json", recursive=True):
        if await import_json_file(jf, uid):
            ok += 1
        else:
            fail += 1

    for sf in glob.glob(f"{extract_dir}/**/*.session", recursive=True):
        ss, aid = extract_from_sqlite(sf)
        if not ss:
            continue
        logger.info(f"🗄️  {os.path.basename(sf)}")
        if await _try_login(ss, uid, aid or API_ID, API_HASH, "sqlite"):
            ok += 1
        else:
            fail += 1

    for tf in glob.glob(f"{extract_dir}/**/*.txt", recursive=True):
        try:
            for ln, line in enumerate(open(tf, "r", encoding="utf-8"), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if len(line) < 100:
                    continue
                if await import_any_string(line, uid):
                    ok += 1
                else:
                    logger.warning(f"   line {ln} failed")
                    fail += 1
        except Exception as e:
            logger.warning(f"   txt: {e}")

    logger.info(f"🏁 Import: ✅ {ok} | ❌ {fail}")
    return ok, fail


async def import_zip(zp, uid):
    tmp = f"tmp_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(zp) as z:
            z.extractall(tmp)
        for inner in glob.glob(f"{tmp}/**/*.zip", recursive=True):
            d = os.path.splitext(inner)[0]
            os.makedirs(d, exist_ok=True)
            try:
                with zipfile.ZipFile(inner) as z:
                    z.extractall(d)
            except Exception:
                pass
        return await import_file(zp, uid, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def import_any_file(path, uid):
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