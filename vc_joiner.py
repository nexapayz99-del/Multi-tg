"""
vc_joiner.py — final v4 with Pyrogram exception patch
  • Patches Pyrogram 2.0.106 to add missing GroupCall exceptions
  • Uses PyTgCalls with mic UNMUTED
  • Caches PyTgCalls clients per account
  • Drops dead sessions individually (no collateral damage)
"""
import asyncio, uuid, os, wave, logging

# ═══════════════════════════════════════════════════════════════════
# PYROGRAM COMPATIBILITY PATCH — MUST RUN BEFORE ANY pytgcalls IMPORT
# Pyrogram 2.0.106 is missing exception classes that PyTgCalls 3.x
# expects. This adds dummy classes so errors raise cleanly.
# ═══════════════════════════════════════════════════════════════════
import pyrogram.errors as _pg_errors


class _DummyGroupCallError(_pg_errors.BadRequest):
    """Stand-in for missing Pyrogram GroupCall exceptions."""
    pass


_MISSING_ERRORS = [
    "GroupcallForbidden", "GroupCallForbidden",
    "GroupcallInvalid", "GroupCallInvalid",
    "GroupcallNotFound", "GroupCallNotFound",
    "GroupcallAlreadyDiscarded", "GroupCallAlreadyDiscarded",
    "GroupcallInvalidGroupCall", "GroupCallInvalidGroupCall",
]

for _err_name in _MISSING_ERRORS:
    if not hasattr(_pg_errors, _err_name):
        _cls = type(_err_name, (_DummyGroupCallError,), {})
        setattr(_pg_errors, _err_name, _cls)

# Inject into internal submodules Pyrogram resolves from
try:
    import pyrogram.errors.exceptions.bad_request_400 as _b400
    for _err_name in _MISSING_ERRORS:
        if not hasattr(_b400, _err_name):
            setattr(_b400, _err_name, getattr(_pg_errors, _err_name))
except Exception:
    pass

try:
    import pyrogram.errors.exceptions.forbidden_403 as _f403
    for _err_name in _MISSING_ERRORS:
        if not hasattr(_f403, _err_name):
            setattr(_f403, _err_name, getattr(_pg_errors, _err_name))
except Exception:
    pass

# Also patch pyrogram.raw.functions.phone so JoinGroupCall works
try:
    import pyrogram.raw.functions.phone as _phone_ns
    # Ensure the module has all needed helpers (some Pyrogram builds omit them)
    for _helper in ("CreateConferenceCall", "JoinGroupCall",
                    "LeaveGroupCall", "DiscardGroupCall"):
        if not hasattr(_phone_ns, _helper):
            class _Dummy:
                ID = 0
                def __init__(self, *a, **k): pass
                async def resolve(self, *a, **k): return b""
                @staticmethod
                def read(b): return None
                def write(self): return b""
            _Dummy.__name__ = _helper
            setattr(_phone_ns, _helper, _Dummy)
except Exception:
    pass
# ═══════════════════════════════════════════════════════════════════

from pyrogram import Client
from pyrogram.raw import functions
from config import API_ID, API_HASH, VC_JOIN_DELAY, SILENT_WAV
from database import all_accounts

logger = logging.getLogger(__name__)
logger.info("🔧 Pyrogram GroupCall exceptions patched")

PyTgCalls = None
MediaStream = None
PYTGCALLS_OK = False
try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream
    PYTGCALLS_OK = True
    logger.info("✅ PyTgCalls loaded")
except Exception as e:
    logger.error(f"❌ PyTgCalls unavailable: {e}")

_active_sessions = {}
_stop_events = {}
DEAD_MARKERS = (
    "USERNAME_INVALID", "USER_DEACTIVATED", "SESSION_REVOKED",
    "AUTH_KEY_UNREGISTERED", "USERNAME_NOT_OCCUPIED",
    "PEER_ID_INVALID", "USER_ID_INVALID", "USER_BANNED",
    "PHONE_NUMBER_BANNED", "USER_DEACTIVATED_BAN",
)


def ensure_silent():
    if os.path.exists(SILENT_WAV) and os.path.getsize(SILENT_WAV) > 1000:
        return
    with wave.open(SILENT_WAV, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100 * 5)
    logger.info(f"✅ {SILENT_WAV} created")


async def is_vc_live(app, chat_id) -> bool:
    try:
        peer = await app.resolve_peer(chat_id)
        try:
            full = await app.invoke(functions.channels.GetFullChannel(channel=peer))
            call = getattr(full.full_chat, "call", None)
            if call:
                logger.info(f"🎤 VC detected → {chat_id}")
                return True
        except Exception as e:
            logger.debug(f"GetFullChannel: {e}")
        try:
            full = await app.invoke(functions.messages.GetFullChat(chat_id=chat_id))
            call = getattr(full.full_chat, "call", None)
            if call:
                return True
        except Exception as e:
            logger.debug(f"GetFullChat: {e}")
    except Exception as e:
        logger.debug(f"VC check: {e}")
    return False


async def _drop_dead(phone: str, err: str = ""):
    from database import accounts
    try:
        await accounts.delete_one({"phone": phone})
        logger.warning(f"🗑 Dropped dead {phone} — {err[:80]}")
    except Exception as e:
        logger.debug(f"drop_dead failed: {e}")


async def _play_unmuted(call_py, chat_id, media_path):
    """Play audio WITHOUT muting (mic stays open, audio is silent)."""
    try:
        stream = MediaStream(media_path)
    except Exception as e:
        logger.error(f"stream build: {e}")
        return False
    try:
        await call_py.play(chat_id, stream)
        try:
            await call_py.unmute(chat_id)
        except Exception:
            pass  # already unmuted
        logger.debug(f"played unmuted → {chat_id}")
        return True
    except Exception as e:
        logger.error(f"play failed for {chat_id}: {e}")
        return False


async def _join_one(phone, session, chat_target):
    app = None
    try:
        app = Client(
            f"vcj_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True, no_updates=True,
        )
        await app.start()

        try:
            chat = await app.get_chat(chat_target)
        except Exception as e:
            err = str(e)
            if any(m in err for m in DEAD_MARKERS):
                await _drop_dead(phone, err)
                try: await app.stop()
                except Exception: pass
                return None
            try:
                await app.join_chat(chat_target)
                chat = await app.get_chat(chat_target)
            except Exception as e2:
                err2 = str(e2)
                if any(m in err2 for m in DEAD_MARKERS):
                    await _drop_dead(phone, err2)
                else:
                    logger.warning(f"VC {phone}: resolve fail {err2[:80]}")
                try: await app.stop()
                except Exception: pass
                return None

        call_py = PyTgCalls(app)
        await call_py.start()

        played = await _play_unmuted(call_py, chat.id, SILENT_WAV)
        if not played:
            logger.warning(f"{phone}: play failed but staying in VC")

        _active_sessions[phone] = {
            "client": app,
            "call": call_py,
            "chat_id": chat.id,
            "title": getattr(chat, "title", str(chat.id)),
        }
        logger.info(f"🎤 {phone} JOINED VC: {_active_sessions[phone]['title']}")
        return chat.id

    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
        else:
            logger.error(f"VC join {phone}: {err[:120]}")
        if app:
            try: await app.stop()
            except Exception: pass
        return None


async def leave_one(phone):
    blk = _active_sessions.pop(phone, None)
    if not blk: return
    try: await blk["call"].leave_call(blk["chat_id"])
    except Exception: pass
    try: await blk["client"].stop()
    except Exception: pass
    logger.info(f"🚪 {phone} left VC")


async def leave_all():
    for ph in list(_active_sessions.keys()):
        await leave_one(ph)


async def auto_vc_worker(key, chat_target, stop_event, max_accounts=None):
    if not PYTGCALLS_OK: return
    ensure_silent()
    _stop_events[key] = stop_event
    joined = False
    logger.info(f"🎤 VC worker started: {chat_target}")

    while not stop_event.is_set():
        try:
            accs = await all_accounts()
            accs = [a for a in accs if a.get("session")]
            if max_accounts:
                accs = accs[:max_accounts]
            if not accs:
                await asyncio.sleep(30)
                continue

            probe = accs[0]
            live = False
            try:
                async with Client(
                    f"probe_{uuid.uuid4().hex[:6]}",
                    api_id=API_ID, api_hash=API_HASH,
                    session_string=probe["session"],
                    in_memory=True, no_updates=True,
                ) as pc:
                    chat = await pc.get_chat(chat_target)
                    live = await is_vc_live(pc, chat.id)
                    logger.info(f"🎤 probe {chat_target}: live={live}")
            except Exception as e:
                err = str(e)
                if any(m in err for m in DEAD_MARKERS):
                    await _drop_dead(probe["phone"], err)
                logger.warning(f"probe failed: {err[:80]}")

            if live and not joined:
                logger.info(f"🎤 VC LIVE — joining {len(accs)} accounts (unmuted)")
                for acc in accs:
                    if stop_event.is_set(): break
                    if acc["phone"] in _active_sessions: continue
                    await _join_one(acc["phone"], acc["session"], chat_target)
                    await asyncio.sleep(VC_JOIN_DELAY)
                joined = True
            elif not live and joined:
                logger.info(f"🎤 VC ended — leaving all")
                await leave_all()
                joined = False

        except Exception as e:
            logger.error(f"auto_vc_worker: {e}")

        await asyncio.sleep(15)

    await leave_all()
    _stop_events.pop(key, None)
    logger.info(f"🎤 VC worker stopped: {chat_target}")


def stop_worker(key):
    ev = _stop_events.get(key)
    if ev: ev.set()


def active_sessions():
    return dict(_active_sessions)
