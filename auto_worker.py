"""auto_worker.py — persistent pool, fast parallel reactions (public + private)."""
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
)
from vc_joiner import auto_vc_worker

logger = logging.getLogger(__name__)
_running_workers = {}
_reaction_locks = {}

MAX_POOL = 600
_client_pool = {}
_client_locks = {}

# ⚠ Only TRUE session-death markers — never chat-resolution errors
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
    """Delete account ONLY on confirmed session death. Never on chat errors."""
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
    Try to resolve chat_target. If private (or get_chat fails), join first.
    Returns (peer, chat_id) or (None, None).
    """
    is_private = _is_private_link(link)

    # Try cached chat_id first (private channels)
    if is_private:
        cached_id, _ = await get_cached_chat_id(link)
        if cached_id:
            try:
                return await app.resolve_peer(cached_id), cached_id
            except Exception:
                pass  # fall through to fresh resolve

    # Try direct get_chat
    try:
        chat = await app.get_chat(chat_target)
        return await app.resolve_peer(chat.id), chat.id
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
            return None, None
        logger.debug(f"get_chat failed for {phone}: {err[:80]}")

    # Public channel: try join_chat(@username) — becomes member
    # Private channel: join_chat(invite_link)
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
            return None, None
        logger.debug(f"join_chat failed for {phone}: {err[:80]}")

    # After join, resolve again
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

        # If account already joined a private channel, skip expensive get_chat
        peer = None
        if _is_private_link(link):
            if await has_joined(link, phone):
                cached_id, _ = await get_cached_chat_id(link)
                if cached_id:
                    try:
                        peer = await app.resolve_peer(cached_id)
                    except Exception:
                        peer = None

        if peer is None:
            peer, resolved_cid = await _resolve_peer_for(app, chat_target,
                                                         link, phone)
            if peer is None:
                return False
            cid = resolved_cid

        tasks = [
            _react_and_view(app, peer, mid, random.choice(reactions), cid)
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

    recent_ids, cid = [], None
    # Try cached chat_id first
    cached_id, _ = await get_cached_chat_id(link)
    probe_client = None
    try:
        async with Client(
            f"fetch_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=accs[0]["session"],
            in_memory=True, no_updates=True,
        ) as app:
            probe_client = app
            # Resolve — for private, one account joins so we can read history
            peer, cid = await _resolve_peer_for(app, chat_target, link,
                                                accs[0]["phone"])
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

    logger.info(f"⚡ {chat_target}: {len(new_ids)} new post(s) → "
                f"PARALLEL reaction from {len(accs)} accounts")
    t0 = time.time()

    STAGGER_WINDOW = 2.0
    step = STAGGER_WINDOW / max(len(accs), 1)

    results = await asyncio.gather(
        *[_react_one_account(a, chat_target, cid, new_ids, reactions,
                             link, start_delay=i * step)
          for i, a in enumerate(accs)],
        return_exceptions=True,
    )

    ok = sum(1 for r in results if r is True)
    fail = len(results) - ok
    logger.info(f"⚡ {chat_target}: done in {time.time()-t0:.1f}s — "
                f"✅ {ok} | ❌ {fail}")


async def _ensure_vc_worker(doc):
    key = f"vc_{doc['link']}"
    if key in _running_workers and not _running_workers[key].done():
        return
    ev = asyncio.Event()
    task = asyncio.create_task(
        auto_vc_worker(key, doc["link"], ev, is_private=_is_private_link(doc["link"]))
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