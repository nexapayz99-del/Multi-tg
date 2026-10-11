"""
auto_worker.py — maximum speed with persistent 600-client pool.
  • Clients stay alive in RAM (only killed on death/error)
  • Reactions fire in parallel across all accounts
  • Dead sessions dropped individually
"""
import asyncio, random, uuid, time, logging
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from pyrogram.raw.functions.messages import GetMessagesViews, SendReaction
from pyrogram.raw.types import ReactionEmoji
from config import (
    API_ID, API_HASH, AUTO_POLL_INTERVAL, POSITIVE_REACTIONS,
)
from database import all_accounts, auto_channels, seen_posts, settings
from vc_joiner import auto_vc_worker

logger = logging.getLogger(__name__)
_running_workers = {}
_reaction_locks = {}

# ── PERSISTENT CLIENT POOL ─────────────────────
MAX_POOL = 600            # for 8GB RAM VPS
_client_pool = {}         # phone -> Client (kept alive)
_client_locks = {}        # phone -> asyncio.Lock
_pool_sem = None          # lazy-init semaphore

DEAD_MARKERS = (
    "USERNAME_INVALID", "USER_DEACTIVATED", "SESSION_REVOKED",
    "AUTH_KEY_UNREGISTERED", "USERNAME_NOT_OCCUPIED",
    "PEER_ID_INVALID", "USER_ID_INVALID", "USER_BANNED",
    "PHONE_NUMBER_BANNED", "USER_DEACTIVATED_BAN",
)


async def _get_client(phone, session):
    """Return an existing client from pool, or create+start one."""
    if phone not in _client_locks:
        _client_locks[phone] = asyncio.Lock()
    async with _client_locks[phone]:
        existing = _client_pool.get(phone)
        if existing and existing.is_connected:
            return existing
        # Already evicted or broken — kill and recreate
        if existing:
            try: await existing.stop()
            except Exception: pass
            _client_pool.pop(phone, None)

        app = Client(
            f"pool_{phone[-6:]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session,
            in_memory=True, no_updates=True,
        )
        await app.start()
        _client_pool[phone] = app
        logger.debug(f"pool: +1 ({len(_client_pool)}/{MAX_POOL})")
        return app


async def _drop_dead(phone, err=""):
    """Remove a single account from DB + pool."""
    from database import accounts
    try:
        await accounts.delete_one({"phone": phone})
    except Exception:
        pass
    app = _client_pool.pop(phone, None)
    if app:
        try: await app.stop()
        except Exception: pass
    logger.warning(f"🗑 Dropped dead {phone} — {err[:80]}")


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
    """React + view on a single message."""
    # Reaction first (fast)
    try:
        await app.invoke(SendReaction(
            peer=peer, msg_id=mid,
            reaction=[ReactionEmoji(emoticon=emoji)]
        ))
    except FloodWait as fw:
        await asyncio.sleep(fw.value + 2)
    except Exception as e:
        logger.debug(f"react fail: {e}")
    # View second
    try:
        await app.invoke(GetMessagesViews(peer=peer, id=[mid], increment=True))
    except FloodWait as fw:
        await asyncio.sleep(fw.value + 2)
    except Exception as e:
        logger.debug(f"view fail: {e}")


async def _react_one_account(acc, chat_target, cid, new_ids, reactions):
    """One account reacts to all new posts. Runs in parallel with others."""
    phone = acc.get("phone")
    try:
        app = await _get_client(phone, acc["session"])
        try:
            peer = await app.resolve_peer(cid)
        except Exception as e:
            err = str(e)
            if any(m in err for m in DEAD_MARKERS):
                await _drop_dead(phone, err)
                return False
            # Try joining then resolving
            try:
                await app.join_chat(chat_target)
            except UserAlreadyParticipant:
                pass
            except Exception:
                pass
            try:
                chat = await app.get_chat(chat_target)
                peer = await app.resolve_peer(chat.id)
            except Exception as e2:
                if any(m in str(e2) for m in DEAD_MARKERS):
                    await _drop_dead(phone, str(e2))
                return False

        # Fire all reactions for this account in parallel
        tasks = []
        for mid in new_ids:
            tasks.append(_react_and_view(
                app, peer, mid, random.choice(reactions), cid
            ))
        await asyncio.gather(*tasks, return_exceptions=True)
        return True
    except Exception as e:
        err = str(e)
        if any(m in err for m in DEAD_MARKERS):
            await _drop_dead(phone, err)
        else:
            logger.debug(f"react err {phone}: {err[:80]}")
        _client_pool.pop(phone, None)
        return False


async def process_channel(ch_doc):
    """Fetch recent posts, react+view on new ones with all accounts."""
    link = ch_doc["link"]
    chat_target = link.strip().rstrip("/")
    accs = await all_accounts()
    accs = [a for a in accs if a.get("session")]
    if not accs:
        return
    reactions = await _get_reactions()

    # Fetch recent post IDs
    recent_ids, cid = [], None
    try:
        async with Client(
            f"fetch_{uuid.uuid4().hex[:6]}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=accs[0]["session"],
            in_memory=True, no_updates=True,
        ) as app:
            try:
                chat = await app.get_chat(chat_target)
            except Exception:
                try:
                    await app.join_chat(chat_target)
                    chat = await app.get_chat(chat_target)
                except Exception as e:
                    logger.debug(f"reach fail {chat_target}: {e}")
                    return
            cid = chat.id
            async for msg in app.get_chat_history(cid, limit=10):
                if msg.id:
                    recent_ids.append(msg.id)
    except Exception as e:
        logger.debug(f"fetch err: {e}")
        return

    if not recent_ids or cid is None:
        return

    # First scan: mark all as seen (no reactions)
    is_first = not await seen_posts.find_one({"chat_id": cid})
    if is_first:
        for mid in recent_ids:
            await seen_posts.update_one(
                {"chat_id": cid, "msg_id": mid},
                {"$set": {"seen_at": datetime.utcnow()}}, upsert=True)
        logger.info(f"📢 {chat_target}: first scan — "
                    f"marked {len(recent_ids)} seen (no reaction)")
        return

    # Only react to new posts
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

    # Fan out across all accounts in parallel
    results = await asyncio.gather(
        *[_react_one_account(a, chat_target, cid, new_ids, reactions)
          for a in accs],
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
    task = asyncio.create_task(auto_vc_worker(key, doc["link"], ev))
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
        try: await app.stop()
        except Exception: pass
    _client_pool.clear()


def pool_stats():
    return {"size": len(_client_pool), "max": MAX_POOL}
