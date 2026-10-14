cat > ~/Multi-tg/vc_joiner.py << 'PYEOF'
"""vc_joiner.py — public + private VC join as silent listeners.
py-tgcalls 3.0.0 + pyrogram 2.0.106 (block= wrapped via sitecustomize).
"""
import asyncio, uuid, os, wave, logging, time

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

PyTgCalls = None
MediaStream = None
PYTGCALLS_OK = False
try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream
    PYTGCALLS_OK = True
    logger.info("✅ PyTgCalls 3.0.0 loaded")
except Exception as e:
    logger.error(f"❌ PyTgCalls unavailable: {e}")

_active_sessions = {}
_stop_events = {}

VC_BATCH_SIZE  = 5
VC_BATCH_DELAY = 3

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
    try:
        with wave.open(SILENT_WAV, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 44100 * 5)
        logger.info(f"✅ {SILENT_WAV} created")
    except Exception as e:
        logger.error(f"❌ silent.wav create failed: {e}")


async def is_vc_live(app, chat_id) -> bool:
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
        logger.warning(f"🗑 Dropped {phone} — {err[:80]}")
    except Exception:
        pass


async def _resolve_and_join(app, chat_target, link, phone):
    flood_until = await get_flood_until(phone)
    if flood_until > time.time():
        logger.info(f"   ⏳ {phone} flood {int(flood_until-time.time())}s")
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
        logger.warning(f"   ⚠️ {phone} FLOOD_WAIT {fw.value}s")
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


async def _real_join(call_py, chat_id) -> bool:
    try:
        stream = MediaStream(SILENT_WAV)
    except Exception as e:
        logger.error(f"   MediaStream build failed: {e}")
        return False

    try:
        await call_py.play(chat_id, stream)
        logger.info(f"   ✅ play({chat_id}) OK")
        return True
    except Exception as e:
        err = str(e)
        if "CHAT_ADMIN_REQUIRED" in err:
            logger.info(f"   🎧 joined as listener ({chat_id})")
            return True
        if "GROUPCALL" in err.upper():
            logger.info(f"   🎧 joined (no mic) ({chat_id})")
            return True
        logger.error(f"   play failed: {err[:150]}")
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
            try: await app.stop()
            except Exception: pass
            return None

        call_py = PyTgCalls(app)
        await call_py.start()

        ok = await _real_join(call_py, chat_id)
        if not ok:
            logger.warning(f"⚠️ {phone}: VC join failed on {chat_target}")
            try: await call_py.leave_call(chat_id)
            except Exception: pass
            try: await app.stop()
            except Exception: pass
            return None

        _active_sessions.setdefault(key, {})[phone] = {
            "client": app,
            "call": call_py,
            "chat_id": chat_id,
            "target": chat_target,
        }
        logger.info(f"🎧 {phone} joined VC {chat_target}: {chat_id}")
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


async def leave_one(phone, key=""):
    bucket = _active_sessions.get(key, {})
    blk = bucket.pop(phone, None)
    if not blk:
        return
    try: await blk["call"].leave_call(blk["chat_id"])
    except Exception: pass
    try: await blk["client"].stop()
    except Exception: pass
    logger.info(f"🚪 {phone} left VC {blk.get('target','')}")


async def leave_all(key=""):
    if key:
        for ph in list(_active_sessions.get(key, {}).keys()):
            await leave_one(ph, key)
        _active_sessions.pop(key, None)
    else:
        for k in list(_active_sessions.keys()):
            for ph in list(_active_sessions[k].keys()):
                await leave_one(ph, k)
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
                        if await has_joined(chat_target, probe["phone"]):
                            cached_id, _ = await get_cached_chat_id(chat_target)
                            if cached_id:
                                cid = cached_id
                    else:
                        try:
                            chat = await pc.get_chat(chat_target)
                            cid = chat.id
                        except Exception:
                            cid = None

                    if cid:
                        live = await is_vc_live(pc, cid)
                    logger.info(f"🎤 probe {chat_target}: live={live}")
            except Exception as e:
                err = str(e)
                if any(m in err for m in DEAD_MARKERS):
                    await _drop_dead(probe["phone"], err)
                logger.warning(f"probe failed: {err[:80]}")

            if live and not joined:
                logger.info(f"🎤 VC LIVE {chat_target} — joining {len(accs)} accounts")
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
                    logger.info(f"   {chat_target} batch {i//VC_BATCH_SIZE + 1}: ✅ {ok} | ❌ {fail}")
                    if i + VC_BATCH_SIZE < len(accs):
                        await asyncio.sleep(VC_BATCH_DELAY)

                logger.info(f"🎤 VC JOIN DONE {chat_target} in {time.time()-t0:.1f}s — ✅ {total_ok} | ❌ {total_fail}")
                joined = True
            elif not live and joined:
                logger.info(f"🎤 VC ended {chat_target} — leaving")
                await leave_all(key)
                joined = False
        except Exception as e:
            logger.error(f"auto_vc_worker: {e}")
        await asyncio.sleep(15)

    await leave_all(key)
    _stop_events.pop(key, None)
    logger.info(f"🎤 VC worker stopped: {chat_target}")


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
PYEOF