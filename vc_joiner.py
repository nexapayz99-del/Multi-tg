"""
vc_joiner.py — final v3
  • Uses PyTgCalls with mic UNMUTED (plays silent.wav as keepalive)
  • Detects dead sessions and drops ONLY the specific one
  • Caches PyTgCalls clients for speed
"""
import asyncio, uuid, os, wave, logging
from pyrogram import Client
from pyrogram.raw import functions
from config import API_ID, API_HASH, VC_JOIN_DELAY, SILENT_WAV
from database import all_accounts

logger = logging.getLogger(__name__)

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

_active_sessions = {}    # phone -> {"client","call","chat_id","title"}
_stop_events = {}
DEAD_MARKERS = (
    "USERNAME_INVALID", "USER_DEACTIVATED", "SESSION_REVOKED",
    "AUTH_KEY_UNREGISTERED", "USERNAME_NOT_OCCUPIED",
    "PEER_ID_INVALID", "USER_ID_INVALID", "USER_BANNED",
    "PHONE_NUMBER_BANNED", "USER_DEACTIVATED_BAN",
)


def ensure_silent():
    """5-second silent WAV used as VC audio (mic stays 'unmuted' but silent)."""
    if os.path.exists(SILENT_WAV) and os.path.getsize(SILENT_WAV) > 1000:
        return
    with wave.open(SILENT_WAV, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100 * 5)
    logger.info(f"✅ {SILENT_WAV} created")


async def is_vc_live(app, chat_id) -> bool:
    """Detect active VC. Uses 2 MTProto calls for reliability."""
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
    """Remove a single dead session from DB. Never touches other sessions."""
    from database import accounts
    try:
        await accounts.delete_one({"phone": phone})
        logger.warning(f"🗑 Dropped dead {phone} — {err[:80]}")
    except Exception as e:
        logger.debug(f"drop_dead failed: {e}")


async def _play_unmuted(call_py, chat_id, media_path):
    """
    Play audio without muting. Tries v2/v3 API first, falls back to v0.9.
    """
    try:
        stream = MediaStream(media_path)
    except Exception as e:
        logger.error(f"stream build: {e}")
        return False

    # v2/v3: play audio (mic stays live)
    try:
        await call_py.play(chat_id, stream)
        # Ensure we are NOT muted
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
    """Join VC with a single account. Never affects other accounts on failure."""
    app = None
    try:
        app = Client(
            f"vcj_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True, no_updates=True,
        )
        await app.start()

        # Resolve chat — drop ONLY this account if session is dead
        try:
            chat = await app.get_chat(chat_target)
        except Exception as e:
            err = str(e)
            if any(m in err for m in DEAD_MARKERS):
                await _drop_dead(phone, err)
                try: await app.stop()
                except Exception: pass
                return None
            # Try join
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

        # Start PyTgCalls for this account
        call_py = PyTgCalls(app)
        await call_py.start()

        # Play unmuted
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
    """Watch channel VC state. Join all when live, leave all when ended."""
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

            # Probe VC with first available account
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
