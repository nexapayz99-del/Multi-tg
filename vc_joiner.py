"""
vc_joiner.py — PyTgCalls v3 compatible
Joins voice chats with muted mic by playing silent WAV.
Multiple accounts join the same VC in parallel.
"""
import asyncio, uuid, os, wave, logging
from pyrogram import Client
from pyrogram.raw import functions
from config import API_ID, API_HASH, VC_JOIN_DELAY, SILENT_WAV
from database import all_accounts

logger = logging.getLogger(__name__)

# ─── Detect available PyTgCalls API ───────────────────────
PyTgCalls = None
MediaStream = None
AudioQuality = None
MediaStreamAudio = None
PYTGCALLS_OK = False

try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream
    try:
        from pytgcalls.types import AudioQuality
    except ImportError:
        AudioQuality = None
    try:
        from pytgcalls.types import MediaStreamAudio
    except ImportError:
        MediaStreamAudio = None
    PYTGCALLS_OK = True
    logger.info(f"✅ PyTgCalls loaded — MediaStream={MediaStream}, "
                f"AudioQuality={'yes' if AudioQuality else 'no'}, "
                f"MediaStreamAudio={'yes' if MediaStreamAudio else 'no'}")
except Exception as e:
    logger.error(f"❌ PyTgCalls unavailable: {e}")


_active_sessions = {}     # phone -> {"client","call","chat_id","title"}
_stop_events = {}         # key -> asyncio.Event


def ensure_silent():
    if os.path.exists(SILENT_WAV) and os.path.getsize(SILENT_WAV) > 1000:
        return
    with wave.open(SILENT_WAV, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100 * 5)
    logger.info(f"✅ {SILENT_WAV} created")


def _build_stream(path: str):
    """
    Build a stream object compatible with whichever PyTgCalls API is present.

    v3.x: MediaStream(audio=MediaStreamAudio(path, AudioQuality.HIGH))
    v2.x: MediaStream(path)
    v0.9: AudioPiped(path)
    """
    # ── Try v3 typed API ──
    if MediaStreamAudio is not None:
        try:
            if AudioQuality is not None:
                audio = MediaStreamAudio(path, AudioQuality.HIGH)
            else:
                audio = MediaStreamAudio(path)
            return MediaStream(audio=audio)
        except Exception as e:
            logger.debug(f"v3 MediaStreamAudio build failed: {e}")

    # ── Try v2 positional ──
    try:
        return MediaStream(path)
    except Exception as e:
        logger.debug(f"v2 MediaStream(pos) failed: {e}")

    # ── Try v0.9 AudioPiped ──
    try:
        from pytgcalls.types.input_stream import AudioPiped
        return AudioPiped(path)
    except Exception as e:
        logger.debug(f"AudioPiped failed: {e}")

    # ── Last resort — pass raw path ──
    logger.warning("No compatible MediaStream constructor — passing raw path")
    return path


async def is_vc_live(app: Client, chat_id) -> bool:
    """Detect active voice chat via raw MTProto call."""
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
    Play the media then immediately mute.
    Works across PyTgCalls v0.9 / v2 / v3.
    """
    stream = _build_stream(media_path)

    # ── Attempt 1: v0.9 style (muted=True kwarg) ──
    try:
        await call_py.play(chat_id, stream, muted=True)
        logger.debug(f"✅ play(muted=True) OK → {chat_id}")
        return True
    except TypeError:
        pass  # v2/v3 — no muted param
    except Exception as e:
        logger.debug(f"play(muted=True) failed: {e}")

    # ── Attempt 2: v2/v3 (play, then mute) ──
    try:
        await call_py.play(chat_id, stream)
        await asyncio.sleep(0.7)
        try:
            await call_py.mute(chat_id)
            logger.debug(f"✅ play + mute OK → {chat_id}")
        except Exception as me:
            logger.debug(f"mute() failed (still joined): {me}")
        return True
    except Exception as e:
        logger.error(f"❌ play failed for {chat_id}: {e}")
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

        call_py = PyTgCalls(app)
        await call_py.start()

        played = await _play_muted(call_py, chat.id, SILENT_WAV)
        if not played:
            logger.warning(f"{phone}: play failed but staying in VC (silent)")

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
    """Monitor channel. VC live → join all. VC ends → leave all."""
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

            # Cheap probe: is VC live?
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
                logger.info(f"🎤 VC LIVE in {chat_target} — joining {len(accs)} accounts")
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
