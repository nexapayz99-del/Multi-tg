"""
Joins voice chats using PyTgCalls. Mic stays muted; plays silent WAV.
Multiple accounts can join the SAME VC in parallel.
"""
import asyncio, uuid, os, wave, logging
from pyrogram import Client
from pyrogram.raw import functions
from pyrogram.enums import ChatType
from config import API_ID, API_HASH, VC_JOIN_DELAY, SILENT_WAV
from database import all_accounts

logger = logging.getLogger(__name__)

try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream
    PYTGCALLS_OK = True
except Exception as e:
    PYTGCALLS_OK = False
    logger.error(f"PyTgCalls unavailable: {e}")

_active_sessions = {}     # phone -> {"client","call","chat_id","task"}
_stop_events  = {}        # key -> asyncio.Event

def ensure_silent():
    if os.path.exists(SILENT_WAV) and os.path.getsize(SILENT_WAV) > 1000: return
    with wave.open(SILENT_WAV, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100 * 5)
    logger.info("silent.wav created")

async def is_vc_live(app: Client, chat_id) -> bool:
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
    stream = MediaStream(media_path)
    try:
        await call_py.play(chat_id, stream, muted=True)
        return True
    except TypeError:
        try:
            await call_py.play(chat_id, stream)
            try: await call_py.mute(chat_id)
            except Exception: pass
            return True
        except Exception as e:
            logger.debug(f"play fallback failed: {e}")
            return False
    except Exception as e:
        logger.debug(f"play(muted) failed: {e}")
        return False

async def _join_one(phone: str, session: str, chat_target: str):
    app = None
    try:
        app = Client(f"vcj_{uuid.uuid4().hex[:6]}",
                     api_id=API_ID, api_hash=API_HASH,
                     session_string=session, in_memory=True, no_updates=True)
        await app.start()

        try:
            chat = await app.get_chat(chat_target)
        except Exception:
            try:
                await app.join_chat(chat_target)
                chat = await app.get_chat(chat_target)
            except Exception as e:
                logger.warning(f"VC join: cannot resolve {chat_target}: {e}")
                await app.stop(); return None

        call_py = PyTgCalls(app)
        await call_py.start()
        played = await _play_muted(call_py, chat.id, SILENT_WAV)
        if not played:
            logger.warning(f"{phone}: joined {chat.id} but play failed (still in VC)")
        _active_sessions[phone] = {"client": app, "call": call_py, "chat_id": chat.id}
        return chat.id
    except Exception as e:
        logger.error(f"VC join {phone} failed: {e}")
        if app:
            try: await app.stop()
            except Exception: pass
        return None

async def leave_one(phone: str):
    blk = _active_sessions.pop(phone, None)
    if not blk: return
    try: await blk["call"].leave_call(blk["chat_id"])
    except Exception: pass
    try: await blk["client"].stop()
    except Exception: pass

async def leave_all():
    for ph in list(_active_sessions.keys()):
        await leave_one(ph)

async def auto_vc_worker(key: str, chat_target: str, stop_event: asyncio.Event,
                        max_accounts: int | None = None,
                        join_scan: bool = False):
    """
    Continuously monitor a channel/group. When a VC starts, join with all
    (or max_accounts) sessions — respecting VC_JOIN_DELAY.
    When VC ends, leave all.
    """
    if not PYTGCALLS_OK: return
    ensure_silent()
    _stop_events[key] = stop_event

    joined = False
    while not stop_event.is_set():
        try:
            accs = await all_accounts()
            accs = [a for a in accs if a.get("session")]
            if max_accounts: accs = accs[:max_accounts]
            if not accs:
                await asyncio.sleep(30); continue

            # Check VC status with first account
            probe = accs[0]
            try:
                async with Client(f"probe_{uuid.uuid4().hex[:6]}",
                                  api_id=API_ID, api_hash=API_HASH,
                                  session_string=probe["session"],
                                  in_memory=True, no_updates=True) as pc:
                    chat = await pc.get_chat(chat_target)
                    live = await is_vc_live(pc, chat.id)
            except Exception as e:
                logger.debug(f"probe failed: {e}")
                live = False

            if live and not joined:
                logger.info(f"🎤 VC live in {chat_target} — joining with {len(accs)} accounts")
                for i, acc in enumerate(accs):
                    if stop_event.is_set(): break
                    if acc["phone"] in _active_sessions: continue
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
    if ev: ev.set()
