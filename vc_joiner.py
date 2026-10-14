"""vc_joiner.py — fast parallel VC joins (public + private), unmuted mic, no proxy"""
import asyncio, uuid, os, wave, logging

# ═══ Pyrogram patches ═══
import pyrogram.errors as _pg_errors
import pyrogram.raw.functions.phone as _phone_module
from pyrogram.raw.core import TLObject


class _DummyGroupCallError(_pg_errors.BadRequest):
    pass


_MISSING_ERRORS = [
    "GroupcallForbidden", "GroupCallForbidden",
    "GroupcallInvalid", "GroupCallInvalid",
    "GroupcallNotFound", "GroupCallNotFound",
    "GroupcallAlreadyDiscarded", "GroupCallAlreadyDiscarded",
]
for _err_name in _MISSING_ERRORS:
    if not hasattr(_pg_errors, _err_name):
        setattr(_pg_errors, _err_name,
                type(_err_name, (_DummyGroupCallError,), {}))

try:
    import pyrogram.errors.exceptions.bad_request_400 as _b400
    for _n in _MISSING_ERRORS:
        if not hasattr(_b400, _n):
            setattr(_b400, _n, getattr(_pg_errors, _n))
except Exception:
    pass

try:
    import pyrogram.errors.exceptions.forbidden_403 as _f403
    for _n in _MISSING_ERRORS:
        if not hasattr(_f403, _n):
            setattr(_f403, _n, getattr(_pg_errors, _n))
except Exception:
    pass


class _StubTLObject(TLObject):
    ID = 0
    def __init__(self, *a, **k): pass
    def write(self): return b""
    @classmethod
    def read(cls, *a, **k): return None


_MISSING_RAW = {
    "GetGroupCallChainBlocks": 0xeeee22cc,
    "CreateConferenceCall": 0x7d0444bb,
    "GetGroupCallStreamChannels": 0x1ab21940,
    "GetGroupCallStreamRtmpUrl": 0xc5c1c8f4,
    "SendGroupCallMessage": 0xee1f9b1f,
    "InviteConferenceCallParticipant": 0x4b6d4b8a,
}
for _name, _id in _MISSING_RAW.items():
    if not hasattr(_phone_module, _name):
        setattr(_phone_module, _name,
                type(_name, (_StubTLObject,), {"ID": _id}))

from pyrogram import Client
from pyrogram.errors import UserAlreadyParticipant, FloodWait
from pyrogram.raw import functions
from config import API_ID, API_HASH, SILENT_WAV
from database import all_accounts, mark_joined, has_joined, get_cached_chat_id, cache_chat_id

logger = logging.getLogger(__name__)
logger.info("🔧 Pyrogram GroupCall patches applied")

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
VC_JOIN_DELAY = 1
VC_JOIN_PARALLEL = 3

# ⚠ Only TRUE session-death markers
DEAD_MARKERS = (
    "USER_DEACTIVATED", "SESSION_REVOKED", "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED_BAN", "PHONE_NUMBER_BANNED", "USER_BANNED",
)


def _is_private_link(link: str) -> bool:
    s = link.strip().lower()
    return "/+" in s or "joinchat/" in s


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


async def _drop_dead(phone, err=""):
    """Only delete on confirmed session death."""
    if not any(m in err for m in DEAD_MARKERS):
        logger.warning(
            f"⚠️ Refusing to drop {phone} — err not confirmed dead: {err[:80]}")
        return
    from database import accounts
    try:
        await accounts.delete_one({"phone": phone})
        logger.warning(f"🗑 Dropped {phone} — {err[:80]}")
    except Exception:
        pass


async def _play_unmuted(call_py, chat_id, media_path):
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
            pass
        return True
    except Exception as e:
        logger.error(f"play failed for {chat_id}: {str(e)[:100]}")
        return False


async def _resolve_and_join(app, chat_target, link, phone):
    """Resolve chat, join if needed. Returns chat.id or None."""
    is_private = _is_private_link(link)

    # Cached chat_id for private
    if is_private:
        cached_id, _ = await get_cached_chat_id(link)
        if cached_id and await has_joined(link, phone):
            return cached_id

    try:
        chat = await app.get_chat(chat_target)
        cid = chat.id
        if is_private:
            await cache_chat_id(link, cid, getattr(chat, "title", ""))
            await mark_joined(link, phone)
        return cid
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
            return None
        logger.debug(f"{phone}: get_chat failed ({err[:60]}) → joining")

    # Try join
    try:
        await app.join_chat(chat_target)
    except UserAlreadyParticipant:
        pass
    except FloodWait as fw:
        logger.warning(f"⚠️ {phone} FLOOD_WAIT {fw.value}s joining {chat_target}")
        await asyncio.sleep(min(fw.value, 30))
        try:
            await app.join_chat(chat_target)
        except Exception as e2:
            logger.debug(f"join retry failed {phone}: {str(e2)[:80]}")
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
            return None
        logger.debug(f"join failed {phone}: {err[:80]}")

    try:
        chat = await app.get_chat(chat_target)
        cid = chat.id
        if is_private:
            await cache_chat_id(link, cid, getattr(chat, "title", ""))
        await mark_joined(link, phone)
        return cid
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
        logger.warning(f"VC {phone}: post-join resolve fail {err[:80]}")
        return None


async def _join_one(phone, session, chat_target, link=""):
    app = None
    try:
        app = Client(
            f"vcj_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True, no_updates=True,
        )
        await app.start()

        chat_id = await _resolve_and_join(app, chat_target, link or chat_target, phone)
        if chat_id is None:
            try: await app.stop()
            except Exception: pass
            return None

        call_py = PyTgCalls(app)
        await call_py.start()
        played = await _play_unmuted(call_py, chat_id, SILENT_WAV)
        if not played:
            logger.warning(f"{phone}: play failed but staying in VC")

        _active_sessions[phone] = {
            "client": app,
            "call": call_py,
            "chat_id": chat_id,
            "title": "",
        }
        logger.info(f"🎤 {phone} JOINED VC: {chat_id}")
        return chat_id
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


async def _join_batch(accs, chat_target, link, stop_event):
    joined = failed = 0
    for acc in accs:
        if stop_event.is_set():
            break
        if acc["phone"] in _active_sessions:
            joined += 1
            continue
        result = await _join_one(acc["phone"], acc["session"], chat_target, link)
        if result:
            joined += 1
        else:
            failed += 1
        await asyncio.sleep(VC_JOIN_DELAY)
    return joined, failed


async def auto_vc_worker(key, chat_target, stop_event,
                         max_accounts=None, is_private=False):
    if not PYTGCALLS_OK: return
    ensure_silent()
    _stop_events[key] = stop_event
    joined = False
    logger.info(f"🎤 VC worker started: {chat_target} (private={is_private})")

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
                    cid = await _resolve_and_join(pc, chat_target,
                                                  chat_target, probe["phone"])
                    if cid:
                        live = await is_vc_live(pc, cid)
                    logger.info(f"🎤 probe {chat_target}: live={live}")
            except Exception as e:
                err = str(e)
                if any(m in err for m in DEAD_MARKERS):
                    await _drop_dead(probe["phone"], err)
                logger.warning(f"probe failed: {err[:80]}")

            if live and not joined:
                logger.info(f"🎤 VC LIVE — joining {len(accs)} accounts (parallel)")
                groups = [[] for _ in range(VC_JOIN_PARALLEL)]
                for i, a in enumerate(accs):
                    groups[i % VC_JOIN_PARALLEL].append(a)

                t0 = asyncio.get_event_loop().time()
                results = await asyncio.gather(
                    *[_join_batch(g, chat_target, chat_target, stop_event)
                      for g in groups],
                    return_exceptions=True,
                )
                total_joined = sum(r[0] for r in results if isinstance(r, tuple))
                total_failed = sum(r[1] for r in results if isinstance(r, tuple))
                elapsed = asyncio.get_event_loop().time() - t0
                logger.info(
                    f"🎤 VC JOIN DONE in {elapsed:.1f}s — "
                    f"✅ {total_joined} | ❌ {total_failed}")
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