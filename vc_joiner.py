cat > ~/Multi-tg/vc_joiner.py << 'ENDOFFILE'
"""vc_joiner.py — public + private VC join as silent listeners."""
import asyncio, uuid, os, wave, logging, time

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
    "GroupcallJoinMissing", "GroupCallJoinMissing",
    "GroupcallNotModified", "GroupCallNotModified",
    "GroupcallParticipantNotFound", "GroupCallParticipantNotFound",
    "GroupcallAlreadyJoined", "GroupCallAlreadyJoined",
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
}
for _name, _id in _MISSING_RAW.items():
    if not hasattr(_phone_module, _name):
        setattr(_phone_module, _name,
                type(_name, (_StubTLObject,), {"ID": _id}))


from pyrogram import Client
from pyrogram.errors import UserAlreadyParticipant, FloodWait
from pyrogram.raw import functions
from config import API_ID, API_HASH, SILENT_WAV
from database import (
    all_accounts, mark_joined, has_joined,
    get_cached_chat_id, cache_chat_id,
    get_flood_until, set_flood_until,
)

logger = logging.getLogger(__name__)
logger.info("Pyrogram GroupCall patches applied")

PyTgCalls = None
MediaStream = None
PYTGCALLS_OK = False
try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream
    PYTGCALLS_OK = True
    logger.info("PyTgCalls 3.0.0 loaded")
except Exception as e:
    logger.error(f"PyTgCalls unavailable: {e}")

_active_sessions = {}
_stop_events = {}

VC_BATCH_SIZE = 5
VC_BATCH_DELAY = 3
MAX_DEAD_TICKS = 3

DEAD_MARKERS = (
    "USER_DEACTIVATED", "SESSION_REVOKED", "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED_BAN", "PHONE_NUMBER_BANNED", "USER_BANNED",
)


def _is_private_link(link):
    s = link.strip().lower()
    return "/+" in s or "joinchat/" in s


def ensure_silent():
    try:
        if os.path.exists(SILENT_WAV) and os.path.getsize(SILENT_WAV) > 5_000_000:
            return
    except Exception:
        pass
    try:
        with wave.open(SILENT_WAV, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 44100 * 60)
        logger.info(f"{SILENT_WAV} created (60s)")
    except Exception as e:
        logger.error(f"silent.wav create failed: {e}")


async def is_vc_live(app, chat_id):
    try:
        peer = await app.resolve_peer(chat_id)
        try:
            full = await app.invoke(functions.channels.GetFullChannel(channel=peer))
            if getattr(full.full_chat, "call", None):
                return True
        except Exception:
            pass
        try:
            full = await app.invoke(functions.messages.GetFullChat(chat_id=chat_id))
            if getattr(full.full_chat, "call", None):
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


async def _drop_dead(phone, err=""):
    if not any(m in err for m in DEAD_MARKERS):
        return
    from database import accounts
    try:
        await accounts.delete_one({"phone": phone})
        logger.warning(f"Dropped {phone} - {err[:80]}")
    except Exception:
        pass


async def _resolve_and_join(app, chat_target, link, phone):
    flood_until = await get_flood_until(phone)
    if flood_until > time.time():
        logger.info(f"   {phone} flood {int(flood_until-time.time())}s")
        return None

    is_private = _is_private_link(link)

    if is_private and await has_joined(link, phone):
        cached_id, _ = await get_cached_chat_id(link)
        if cached_id:
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

    try:
        await app.join_chat(chat_target)
    except UserAlreadyParticipant:
        pass
    except FloodWait as fw:
        await set_flood_until(phone, time.time() + fw.value)
        logger.warning(f"   {phone} FLOOD_WAIT {fw.value}s")
        return None
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
            return None

    try:
        chat = await app.get_chat(chat_target)
        cid = chat.id
        if is_private:
            await cache_chat_id(link, cid, getattr(chat, "title", ""))
        await mark_joined(link, phone)
        return cid
    except Exception:
        return None


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
        err = str(e)
        if "CHAT_ADMIN_REQUIRED" in err or "GROUPCALL_FORBIDDEN" in err.upper():
            logger.info(f"   listener-mode join ({chat_id})")
            return True
        if "PEER_ID_INVALID" in err or "Peer id invalid" in err:
            logger.info(f"   peer not cached ({chat_id})")
            return True
        logger.error(f"play failed for {chat_id}: {err[:150]}")
        return False


async def _join_one(phone, session, chat_target, link="", key=""):
    app = None
    try:
        app = Client(
            f"vcj_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True, no_updates=True,
        )
        await app.start()

        chat_id = await _resolve_and_join(app, chat_target,
                                          link or chat_target, phone)
        if chat_id is None:
            try:
                await app.stop()
            except Exception:
                pass
            return None

        # ── Warm up peer cache so pytgcalls can resolve the ID ──
        try:
            await app.get_chat(chat_id)
        except Exception:
            pass
        try:
            await app.resolve_peer(chat_id)
        except Exception:
            pass
        try:
            await app.get_chat(chat_target)
        except Exception:
            pass
        try:
            await app.resolve_peer(chat_target)
        except Exception:
            pass

        call_py = PyTgCalls(app)
        await call_py.start()
        played = await _play_unmuted(call_py, chat_id, SILENT_WAV)
        if not played:
            logger.warning(f"{phone}: play failed but staying in VC")

        _active_sessions.setdefault(key, {})[phone] = {
            "client": app,
            "call": call_py,
            "chat_id": chat_id,
            "target": chat_target,
        }
        logger.info(f"{phone} joined VC {chat_target}: {chat_id}")
        return chat_id
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
        else:
            logger.error(f"VC join {phone}: {err[:120]}")
        if app:
            try:
                await app.stop()
            except Exception:
                pass
        return None


async def leave_one(phone, key=""):
    bucket = _active_sessions.get(key, {})
    blk = bucket.pop(phone, None)
    if not blk:
        return
    try:
        await blk["call"].leave_call(blk["chat_id"])
    except Exception:
        pass
    try:
        await blk["client"].stop()
    except Exception:
        pass
    logger.info(f"{phone} left VC {blk.get('target','')}")


async def leave_all(key=""):
    if key:
        bucket = _active_sessions.get(key, {})
        phones = list(bucket.keys())
        for i in range(0, len(phones), 5):
            batch = phones[i:i + 5]
            tasks = [leave_one(ph, key) for ph in batch]
            await asyncio.gather(*tasks, return_exceptions=True)
            if i + 5 < len(phones):
                await asyncio.sleep(1.0)
        _active_sessions.pop(key, None)
    else:
        for k in list(_active_sessions.keys()):
            bucket = _active_sessions[k]
            phones = list(bucket.keys())
            for i in range(0, len(phones), 5):
                batch = phones[i:i + 5]
                tasks = [leave_one(ph, k) for ph in batch]
                await asyncio.gather(*tasks, return_exceptions=True)
                if i + 5 < len(phones):
                    await asyncio.sleep(1.0)
            _active_sessions.pop(k, None)


async def _join_batch(accs, chat_target, link, stop_event, key):
    bucket = _active_sessions.setdefault(key, {})
    tasks = []
    for acc in accs:
        if stop_event.is_set():
            break
        phone = acc["phone"]
        if phone in bucket:
            continue
        tasks.append(asyncio.create_task(
            _join_one(phone, acc["session"], chat_target, link, key)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, int))
    return ok, len(tasks) - ok


async def auto_vc_worker(key, chat_target, stop_event,
                         max_accounts=None, is_private=False):
    if not PYTGCALLS_OK:
        return
    ensure_silent()
    _stop_events[key] = stop_event
    _active_sessions.setdefault(key, {})
    joined = False
    probe_idx = 0
    dead_ticks = 0
    logger.info(f"VC worker started: {chat_target} (private={is_private})")

    while not stop_event.is_set():
        try:
            accs = await all_accounts()
            accs = [a for a in accs if a.get("session")]
            if max_accounts:
                accs = accs[:max_accounts]
            if not accs:
                await asyncio.sleep(30)
                continue

            probe = accs[probe_idx % len(accs)]
            probe_idx += 1

            live = False
            try:
                async with Client(
                    f"probe_{uuid.uuid4().hex[:6]}",
                    api_id=API_ID, api_hash=API_HASH,
                    session_string=probe["session"],
                    in_memory=True, no_updates=True,
                ) as pc:
                    cid = None
                    if is_private:
                        try:
                            await pc.join_chat(chat_target)
                        except Exception:
                            pass
                        try:
                            chat = await pc.get_chat(chat_target)
                            cid = chat.id
                            await cache_chat_id(chat_target, cid,
                                                getattr(chat, "title", ""))
                        except Exception as e:
                            logger.debug(f"private probe get_chat: {e}")
                            cid = None
                    else:
                        try:
                            chat = await pc.get_chat(chat_target)
                            cid = chat.id
                        except Exception:
                            cid = None

                    if cid:
                        live = await is_vc_live(pc, cid)
                    logger.info(f"probe {chat_target}: live={live} cid={cid}")
            except Exception as e:
                err = str(e)
                if any(m in err for m in DEAD_MARKERS):
                    await _drop_dead(probe["phone"], err)
                logger.warning(f"probe failed: {err[:80]}")

            if live:
                dead_ticks = 0
                if not joined:
                    logger.info(f"VC LIVE {chat_target} - joining {len(accs)} accounts")
                    t0 = time.time()
                    total_ok = total_fail = 0

                    for i in range(0, len(accs), VC_BATCH_SIZE):
                        if stop_event.is_set():
                            break
                        batch = accs[i:i + VC_BATCH_SIZE]
                        ok, fail = await _join_batch(batch, chat_target,
                                                     chat_target, stop_event, key)
                        total_ok += ok
                        total_fail += fail
                        logger.info(f"   {chat_target} batch {i//VC_BATCH_SIZE + 1}: OK {ok} | FAIL {fail}")
                        if i + VC_BATCH_SIZE < len(accs):
                            await asyncio.sleep(VC_BATCH_DELAY)

                    logger.info(f"VC JOIN DONE {chat_target} in {time.time()-t0:.1f}s - OK {total_ok} | FAIL {total_fail}")
                    joined = True
            else:
                if joined:
                    dead_ticks += 1
                    logger.info(f"VC {chat_target}: dead tick {dead_ticks}/{MAX_DEAD_TICKS}")
                    if dead_ticks >= MAX_DEAD_TICKS:
                        logger.info(f"VC ended {chat_target} - leaving (staggered)")
                        await leave_all(key)
                        joined = False
                        dead_ticks = 0
        except Exception as e:
            logger.error(f"auto_vc_worker: {e}")
        await asyncio.sleep(15)

    await leave_all(key)
    _stop_events.pop(key, None)
    logger.info(f"VC worker stopped: {chat_target}")


def stop_worker(key):
    ev = _stop_events.get(key)
    if ev:
        ev.set()


def active_sessions(key=None):
    if key:
        return dict(_active_sessions.get(key, {}))
    total = {}
    for bucket in _active_sessions.values():
        total.update(bucket)
    return total
ENDOFFILE