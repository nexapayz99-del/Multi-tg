"""auto_worker.py — parallel reactions, VC worker per channel."""
import asyncio, random, uuid, time, logging
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from pyrogram.raw.functions.messages import GetMessagesViews, SendReaction
from pyrogram.raw.types import ReactionEmoji
from config import API_ID, API_HASH, AUTO_POLL_INTERVAL, POSITIVE_REACTIONS
from database import all_accounts, auto_channels, seen_posts, settings
from vc_joiner import auto_vc_worker

logger = logging.getLogger(__name__)
_running_workers = {}
_reaction_locks = {}


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
    async with _get_lock(cid, mid):
        try:
            await app.invoke(SendReaction(peer=peer, msg_id=mid,
                reaction=[ReactionEmoji(emoticon=emoji)]))
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 2)
        except Exception as e:
            logger.debug(f"react fail: {e}")
        try:
            await app.invoke(GetMessagesViews(peer=peer, id=[mid], increment=True))
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 2)
        except Exception as e:
            logger.debug(f"view fail: {e}")


async def _react_one_account(acc, chat_target, cid, new_ids, reactions):
    app = None
    try:
        app = Client(f"rv_{uuid.uuid4().hex[:6]}",
                     api_id=API_ID, api_hash=API_HASH,
                     session_string=acc["session"],
                     in_memory=True, no_updates=True)
        await app.start()
        try:
            peer = await app.resolve_peer(cid)
        except Exception:
            try:
                await app.join_chat(chat_target)
            except UserAlreadyParticipant:
                pass
            except Exception:
                pass
            chat = await app.get_chat(chat_target)
            peer = await app.resolve_peer(chat.id)
        for mid in new_ids:
            await _react_and_view(app, peer, mid, random.choice(reactions), cid)
        return True
    except Exception as e:
        logger.debug(f"react err {acc.get('phone')}: {e}")
        return False
    finally:
        if app:
            try:
                await app.stop()
            except Exception:
                pass


async def process_channel(ch_doc):
    link = ch_doc["link"]
    chat_target = link.strip().rstrip("/")
    accs = await all_accounts()
    accs = [a for a in accs if a.get("session")]
    if not accs:
        return
    reactions = await _get_reactions()
    recent_ids, cid = [], None
    try:
        async with Client(f"fetch_{uuid.uuid4().hex[:6]}",
                          api_id=API_ID, api_hash=API_HASH,
                          session_string=accs[0]["session"],
                          in_memory=True, no_updates=True) as app:
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
    is_first = not await seen_posts.find_one({"chat_id": cid})
    if is_first:
        for mid in recent_ids:
            await seen_posts.update_one({"chat_id": cid, "msg_id": mid},
                {"$set": {"seen_at": datetime.utcnow()}}, upsert=True)
        logger.info(f"📢 {chat_target}: first scan — {len(recent_ids)} marked (no reaction)")
        return
    new_ids = []
    for mid in recent_ids:
        seen = await seen_posts.find_one({"chat_id": cid, "msg_id": mid})
        if not seen:
            new_ids.append(mid)
            await seen_posts.update_one({"chat_id": cid, "msg_id": mid},
                {"$set": {"seen_at": datetime.utcnow()}}, upsert=True)
    if not new_ids:
        return
    logger.info(f"⚡ {chat_target}: {len(new_ids)} new post(s) → PARALLEL "
                f"reaction from {len(accs)} accounts")
    t0 = time.time()
    results = await asyncio.gather(
        *[_react_one_account(a, chat_target, cid, new_ids, reactions)
          for a in accs],
        return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    fail = len(results) - ok
    logger.info(f"⚡ {chat_target}: done in {time.time()-t0:.1f}s — ✅ {ok} | ❌ {fail}")


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
                await asyncio.sleep(3)
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
