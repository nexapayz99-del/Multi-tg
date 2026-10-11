"""
Joins voice chats using PyTgCalls 2.x. Mic stays muted; plays silent WAV.
Multiple accounts can join the SAME VC in parallel.
"""
import asyncio, uuid, os, wave, logging
from pyrogram import Client
from pyrogram.raw import functions
from config import API_ID, API_HASH, VC_JOIN_DELAY, SILENT_WAV
from database import all_accounts

logger = logging.getLogger(__name__)

# --- Import PyTgCalls with v2-compatible fallback ---
PyTgCalls = None
MediaStream = None
PYTGCALLS_OK = False

try:
    from pytgcalls import PyTgCalls
    try:
        from pytgcalls.types import MediaStream
    except ImportError:
        # v0.9.x fallback path
        from pytgcalls.types.input_stream import InputStream as MediaStream
    PYTGCALLS_OK = True
    logger.info(f"PyTgCalls loaded: {PyTgCalls}")
except Exception as e:
    logger.error(f"PyTgCalls unavailable: {e}")

_active_sessions = {}     # phone -> {"client","call","chat_id"}
_stop_events = {}         # key -> asyncio.Event


def ensure_silent():
    if os.path.exists(SILENT_WAV) and os.path.getsize(SILENT_WAV) > 1000:
        return
    with wave.open(SILENT_WAV, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100 * 5)
    logger.info(f"✅ {SILENT_WAV} created")


async def is_vc_live(app: Client, chat_id) -> bool:
    """Detect an active voice chat via raw API."""
    try:
        peer = await app.resolve_peer(chat_id)
        try:
            full = await app.invoke(functions.channels.GetFullChannel(channel=peer))
            return bool(getattr(full.full_chat, "call", None))
        except Exception:
            try:
                full = await app.invoke(functions.messages.GetFullChat(chat_id=chat_id))
                return bool(getattr(full.full_chat, "call", None))
            except Exception:
                return False
    except Exception:
        return False


async def _play_muted(call_py, chat_id, media_path):
    """
    PyTgCalls v2.x: play then mute.
    PyTgCalls v0.9.x: play(muted=True) supported.
    """
    # Try v2 API first
    try:
        stream = MediaStream(media_path)
    except TypeError:
        # Some v2 versions want MediaStream(audio=...)
        try:
            from pytgcalls.types import AudioPiped
            stream = AudioPiped(media_path)
        except Exception:
            stream = media_path

    # Attempt 1: v0.9.x muted kwarg
    try:
        await call_py.play(chat_id, stream, muted=True)
        logger.debug(f"played muted via v0.9 API → {chat_id}")
        return True
    except TypeError:
        pass
    except Exception as e:
        logger.debug(f"play(muted=True) failed: {e}")

    # Attempt 2: v2.x play + mute
    try:
        await call_py.play(chat_id, stream)
        await asyncio.sleep(0.5)
        try:
            await call_py.mute(chat_id)
        except Exception as me:
            logger.debug(f"mute() failed (still joined): {me}")
        logger.debug(f"played then muted via v2 API → {chat_id}")
        return True
    except Exception as e:
        logger.error(f"play failed for {chat_id}: {e}")
        return False


async def _join_one(phone: str, session: str, chat_target: str):
    """One account joins the VC. Returns chat_id or None."""
    app = None
    try:
        app = Client(
            f"vcj_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True,
            no_updates=True,
        )
        await app.start()

        # Resolve chat
        try:
            chat = await app.get_chat(chat_target)
        except Exception:
            try:
                await app.join_chat(chat_target)
                chat = await app.get_chat(chat_target)
            except Exception as e:
                logger.warning(f"VC join {phone}: cannot resolve {chat_target}: {e}")
                await app.stop()
                return None

        # Start PyTgCalls
        call_py = PyTgCalls(app)
        await call_py.start()

        # Play muted
        played = await _play_muted(call_py, chat.id, SILENT_WAV)
        if not played:
            logger.warning(f"{phone}: play failed, but staying in VC")

        _active_sessions[phone] = {
            "client": app,
            "call": call_py,
            "chat_id": chat.id,
            "title": getattr(chat, "title", str(chat.id)),
        }
        logger.info(f"🎤 {phone} joined VC: {_active_sessions[phone]['title']}")
        return chat.id

    except Exception as e:
        logger.error(f"VC join {phone} failed: {e}")
        if app:
            try: await app.stop()
            except Exception: pass
        return None


async def leave_one(phone: str):
    blk = _active_sessions.pop(phone, None)
    if not blk:
        return
    try:
        await blk["call"].leave_call(blk["chat_id"])
    except Exception as e:
        logger.debug(f"leave_call {phone}: {e}")
    try:
        await blk["client"].stop()
    except Exception:
        pass
    logger.info(f"🚪 {phone} left VC")


async def leave_all():
    for ph in list(_active_sessions.keys()):
        await leave_one(ph)


async def auto_vc_worker(key: str, chat_target: str, stop_event: asyncio.Event,
                         max_accounts: int | None = None,
                         join_scan: bool = False):
    """
    Continuously monitor a channel/group:
      • VC live  → join with all (or max_accounts) sessions
      • VC ended → leave all
    """
    if not PYTGCALLS_OK:
        logger.error("auto_vc_worker: PyTgCalls not available")
        return

    ensure_silent()
    _stop_events[key] = stop_event

    joined = False
    while not stop_event.is_set():
        try:
            accs = await all_accounts()
            accs = [a for a in accs if a.get("session")]
            if max_accounts:
                accs = accs[:max_accounts]
            if not accs:
                await asyncio.sleep(30)
                continue

            # Probe VC status with the first account (cheap, no join)
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
            except Exception as e:
                logger.debug(f"probe failed: {e}")

            if live and not joined:
                logger.info(f"🎤 VC LIVE in {chat_target} — joining with {len(accs)} accounts")
                for acc in accs:
                    if stop_event.is_set():
                        break
                    if acc["phone"] in _active_sessions:
                        continue
                    await _join_one(acc["phone"], acc["session"], chat_target)
                    await asyncio.sleep(VC_JOIN_DELAY)
                joined = True

            elif not live and joined:
                logger.info(f"🎤 VC ended in {chat_target} — leaving all")
                await leave_all()
                joined = False

        except Exception as e:
            logger.error(f"auto_vc_worker {key}: {e}")

        await asyncio.sleep(15)

    await leave_all()
    _stop_events.pop(key, None)


def stop_worker(key: str):
    ev = _stop_events.get(key)
    if ev:
        ev.set()


def active_sessions():
    return dict(_active_sessions)
