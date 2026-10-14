"""auto_worker.py — persistent pool, batched parallel reactions (public + private)."""
import asyncio, random, uuid, time, logging
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from pyrogram.raw.functions.messages import GetMessagesViews, SendReaction
from pyrogram.raw.types import ReactionEmoji
from config import (
    API_ID, API_HASH, AUTO_POLL_INTERVAL, POSITIVE_REACTIONS,
)
from database import (
    all_accounts, auto_channels, seen_posts, settings,
    mark_joined, has_joined, get_joined_set,
    get_cached_chat_id, cache_chat_id,
    set_flood_until, get_flood_until,
)
from vc_joiner import auto_vc_worker

logger = logging.getLogger(__name__)
_running_workers = {}
_reaction_locks = {}

MAX_POOL = 600
_client_pool = {}
_client_locks = {}

# ── Batch tuning ──
BATCH_SIZE = 5
BATCH_DELAY = 1.5     # seconds between batches
PER_ACC_DELAY = 0.0   # stagger inside a batch (0 = fire together)

# ⚠ Only TRUE session-death markers
DEAD_MARKERS = (
    "USER_DEACTIVATED", "SESSION_REVOKED", "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED_BAN", "PHONE_NUMBER_BANNED", "USER_BANNED",
)


def _is_private_link(link: str) -> bool:
    s = link.strip().lower()
    return "/+" in s or "joinchat/" in s


async def _get_client(phone, session):
    if phone not in _client_locks:
        _client_locks[phone] = asyncio.Lock()
    async with _client_locks[phone]:
        existing = _client_pool.get(phone)
        if existing and existing.is_connected:
            return existing
        if existing:
            try:
                await existing.stop()
            except Exception:
                pass
            _client_pool.pop(phone, None)

        app = Client(
            f"pool_{phone[-6:]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True, no_updates=True,
        )
        await app.start()
        _client_pool[phone] = app
        return app


async def _drop_dead(phone, err=""):
    """Delete account ONLY on confirmed session death."""
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
    app = _client_pool.pop(phone, None)
    if app:
        try:
            await app.stop()
        except Exception:
            pass


def _get_lock(cid, mid):
    k = f"{cid}:{mid}"
    if k not in _reaction_locks:
        _reaction_locks[k] = asyncio.Lock()
    return _reaction_locks[k]


async def _get_reactions():
    doc = await settings.find_one({"id": "reactions"})
    if doc and doc.get("list"):
        return doc["list"]
    return POSITIVE_REACTIONS


async def _react_and_view(app, peer, mid, emoji, cid):
    try:
        await app.invoke(GetMessagesViews(peer=peer, id=[mid], increment=True))
    except FloodWait as fw:
        await asyncio.sleep(min(fw.value, 5))
    except Exception as e:
        logger.debug(f"view fail: {e}")

    async with _get_lock(cid, mid):
        try:
            await app.invoke(SendReaction(
                peer=peer, msg_id=mid,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))
        except FloodWait as fw:
            await asyncio.sleep(min(fw.value, 5))
        except Exception as e:
            logger.debug(f"react fail: {e}")


async def _resolve_peer_for(app, chat_target, link, phone):
    """
    Resolve chat. If private (or get_chat fails), join with flood respect.
    Returns (peer, chat_id) or (None, None).
    """
    # Flood guard
    flood_until = await get_flood_until(phone)
    if flood_until > time.time():
        logger.info(f"   ⏳ {phone} flood for "
                    f"{int(flood_until-time.time())}s — skip")
        return None, None

    is_private = _is_private_link(link)

    # Cached private lookup
    if is_private and await has_joined(link, phone):
        cached_id, _ = await get_cached_chat_id(link)
        if cached_id:
            try:
                return await app.resolve_peer(cached_id), cached_id
            except Exception:
                pass

    # Direct resolve
    try:
        chat = await app.get_chat(chat_target)
        cid = chat.id
        if is_private:
            await cache_chat_id(link, cid, getattr(chat, "title", ""))
            await mark_joined(link, phone)
        return await app.resolve_peer(cid), cid
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
            return None, None
        logger.debug(f"get_chat failed for {phone}: {err[:80]}")

    # Join
    try:
        await app.join_chat(chat_target)
    except UserAlreadyParticipant:
        pass
    except FloodWait as fw:
        until = time.time() + fw.value
        await set_flood_until(phone, until)
        logger.warning(f"⚠️ {phone} FLOOD_WAIT {fw.value}s joining {chat_target}")
        return None, None
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
            return None, None
        logger.debug(f"join_chat failed {phone}: {err[:80]}")

    # Post-join resolve
    try:
        chat = await app.get_chat(chat_target)
        cid = chat.id
        if is_private:
            await cache_chat_id(link, cid, getattr(chat, "title", ""))
        await mark_joined(link, phone)
        return await app.resolve_peer(cid), cid
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
        logger.debug(f"post-join get_chat failed {phone}: {err[:80]}")
        return None, None


async def _react_one_account(acc, chat_target, cid, new_ids, reactions,
                             link, start_delay=0.0):
    phone = acc.get("phone")
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    try:
        app = await _get_client(phone, acc["session"])

        peer = None
        resolved_cid = cid
        if _is_private_link(link) and await has_joined(link, phone):
            cached_id, _ = await get_cached_chat_id(link)
            if cached_id:
                try:
                    peer = await app.resolve_peer(cached_id)
                    resolved_cid = cached_id
                except Exception:
                    peer = None

        if peer is None:
            peer, resolved_cid = await _resolve_peer_for(
                app, chat_target, link, phone)
            if peer is None:
                return False

        # All views + reactions for this account, parallel
        tasks = [
            _react_and_view(app, peer, mid, random.choice(reactions),
                            resolved_cid)
            for mid in new_ids
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        return True
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
        else:
            logger.debug(f"react err {phone}: {err[:80]}")
        return False


async def process_channel(ch_doc):
    link = ch_doc["link"]
    chat_target = link.strip().rstrip("/")
    accs = await all_accounts()
    accs = [a for a in accs if a.get("session")]
    if not accs:
        return
    reactions = await _get_reactions()

    # ── fetch recent posts via first account ──
    recent_ids, cid = [], None
    try:
        async with Client(
            f"fetch_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=accs[0]["session"],
            in_memory=True, no_updates=True,
        ) as app:
            peer, cid = await _resolve_peer_for(
                app, chat_target, link, accs[0]["phone"])
            if peer is None:
                logger.debug(f"cannot resolve {chat_target}")
                return
            async for msg in app.get_chat_history(cid, limit=10):
                if msg.id:
                    recent_ids.append(msg.id)
    except Exception as e:
        logger.debug(f"fetch err: {e}")
        return

    if not recent_ids or cid is None:
        return

    # ── seen tracking ──
    is_first = not await seen_posts.find_one({"chat_id": cid})
    if is_first:
        for mid in recent_ids:
            await seen_posts.update_one(
                {"chat_id": cid, "msg_id": mid},
                {"$set": {"seen_at": datetime.utcnow()}}, upsert=True)
        logger.info(f"📢 {chat_target}: first scan — "
                    f"marked {len(recent_ids)} seen")
        return

    new_ids = []
    for mid in recent_ids:
        seen = await seen_posts.find_one({"chat_id": cid, "msg_id": mid})
        if not seen:
            new_ids.append(mid)
            await seen_posts.update_one(
                {"chat_id": cid, "msg_id": mid},
                {"$set": {"seen_at": datetime.utcnow()}}, upsert=True)

    if not new_ids:
        return

    logger.info(
        f"⚡ {chat_target}: {len(new_ids)} new post(s) → "
        f"batching {len(accs)} accounts in groups of {BATCH_SIZE}")
    t0 = time.time()

    total_ok = total_fail = 0
    for i in range(0, len(accs), BATCH_SIZE):
        batch = accs[i:i + BATCH_SIZE]
        results = await asyncio.gather(
            *[_react_one_account(a, chat_target, cid, new_ids,
                                 reactions, link,
                                 start_delay=idx * PER_ACC_DELAY)
              for idx, a in enumerate(batch)],
            return_exceptions=True,
        )
        ok = sum(1 for r in results if r is True)
        fail = len(results) - ok
        total_ok += ok
        total_fail += fail
        logger.info(f"   batch {i//BATCH_SIZE + 1}: ✅ {ok} | ❌ {fail}")
        if i + BATCH_SIZE < len(accs):
            await asyncio.sleep(BATCH_DELAY)

    logger.info(
        f"⚡ {chat_target}: done in {time.time()-t0:.1f}s — "
        f"✅ {total_ok} | ❌ {total_fail}")


async def _ensure_vc_worker(doc):
    key = f"vc_{doc['link']}"
    if key in _running_workers and not _running_workers[key].done():
        return
    ev = asyncio.Event()
    task = asyncio.create_task(
        auto_vc_worker(key, doc["link"], ev,
                       is_private=_is_private_link(doc["link"]))
    )
    _running_workers[key] = task
    logger.info(f"🎤 spawned VC worker for {doc['link']}")


async def auto_watcher_loop():
    logger.info("🔁 Auto watcher started")
    while True:
        try:
            docs = await auto_channels.find({}).to_list(length=15)
            logger.info(f"🔁 Watch tick — {len(docs)} channel(s)")
            for doc in docs:
                try:
                    await process_channel(doc)
                    await _ensure_vc_worker(doc)
                except Exception as e:
                    logger.exception(f"❌ channel err {doc.get('link')}: {e}")
                await asyncio.sleep(2)
        except Exception as e:
            logger.exception(f"❌ watcher loop: {e}")
        await asyncio.sleep(AUTO_POLL_INTERVAL)


async def stop_channel_worker(link):
    key = f"vc_{link}"
    task = _running_workers.pop(key, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"🛑 stopped VC worker {link}")


async def stop_all():
    for t in _running_workers.values():
        t.cancel()
    _running_workers.clear()
    for app in _client_pool.values():
        try:
            await app.stop()
        except Exception:
            pass
    _client_pool.clear()


def pool_stats():
    return {"size": len(_client_pool), "max": MAX_POOL}