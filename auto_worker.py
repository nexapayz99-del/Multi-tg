"""
Single unified watcher:
  • For each auto_channel doc
    – Poll latest posts
    – React with random positive emoji from ALL accounts
    – Increment views with ALL accounts
    – Ensure VC worker is running (joins on VC start)
"""
import asyncio, random, uuid, logging
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from pyrogram.raw.functions.messages import GetMessagesViews, SendReaction
from pyrogram.raw.types import ReactionEmoji
from config import API_ID, API_HASH, AUTO_POLL_INTERVAL, POSITIVE_REACTIONS
from database import all_accounts, auto_channels, seen_posts, settings, vc_state
from vc_joiner import auto_vc_worker, stop_worker

logger = logging.getLogger(__name__)

_running_workers = {}      # key -> task
_reaction_locks = {}

def _get_lock(cid, mid):
    k = f"{cid}:{mid}"
    if k not in _reaction_locks:
        _reaction_locks[k] = asyncio.Lock()
    return _reaction_locks[k]

async def _get_reactions():
    doc = await settings.find_one({"id": "reactions"})
    if doc and doc.get("list"): return doc["list"]
    return POSITIVE_REACTIONS

async def _react_and_view(app, peer, mid, emoji, cid):
    async with _get_lock(cid, mid):
        try:
            await app.invoke(SendReaction(
                peer=peer, msg_id=mid,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))
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

async def process_channel(ch_doc):
    link = ch_doc["link"]
    chat_target = link.strip().rstrip("/")
    accs = await all_accounts()
    accs = [a for a in accs if a.get("session")]
    if not accs: return

    reactions = await _get_reactions()

    # 1. Fetch recent posts using first account
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
                    logger.debug(f"cannot reach {chat_target}: {e}"); return
            cid = chat.id
            async for m in app.get_chat_history(cid, limit=10):
                if m.id: recent_ids.append(m.id)
    except Exception as e:
        logger.debug(f"fetch err {chat_target}: {e}"); return

    if not recent_ids or cid is None: return

    # 2. Filter unseen
    new_ids = []
    for mid in recent_ids:
        seen = await seen_posts.find_one({"chat_id": cid, "msg_id": mid})
        if not seen:
            new_ids.append(mid)
            await seen_posts.update_one(
                {"chat_id": cid, "msg_id": mid},
                {"$set": {"seen_at": datetime.utcnow()}}, upsert=True)

    if not new_ids: return
    logger.info(f"📢 {chat_target}: {len(new_ids)} new post(s)")

    # 3. React + view with ALL accounts
    for acc in accs:
        app = None
        try:
            app = Client(f"rv_{uuid.uuid4().hex[:6]}",
                         api_id=API_ID, api_hash=API_HASH,
                         session_string=acc["session"], in_memory=True, no_updates=True)
            await app.start()
            try:
                peer = await app.resolve_peer(cid)
            except Exception:
                try: await app.join_chat(chat_target)
                except UserAlreadyParticipant: pass
                except Exception: pass
                chat = await app.get_chat(chat_target)
                peer = await app.resolve_peer(chat.id)

            for mid in new_ids:
                await _react_and_view(app, peer, mid, random.choice(reactions), cid)
                await asyncio.sleep(random.uniform(1.2, 2.8))
        except Exception as e:
            logger.debug(f"work err {acc.get('phone')}: {e}")
        finally:
            if app:
                try: await app.stop()
                except Exception: pass

async def _ensure_vc_worker(doc):
    key = f"vc_{doc['link']}"
    if key in _running_workers and not _running_workers[key].done():
        return
    ev = asyncio.Event()
    max_acc = doc.get("max_vc_accounts")  # per-channel quota (from plan)
    task = asyncio.create_task(auto_vc_worker(key, doc["link"], ev, max_accounts=max_acc))
    _running_workers[key] = task

async def auto_watcher_loop():
    logger.info("🔁 Auto watcher started")
    while True:
        try:
            docs = await auto_channels.find({}).to_list(length=15)
            for doc in docs:
                try:
                    await process_channel(doc)
                    await _ensure_vc_worker(doc)
                except Exception as e:
                    logger.error(f"channel err {doc.get('link')}: {e}")
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"watcher loop: {e}")
        await asyncio.sleep(AUTO_POLL_INTERVAL)

async def stop_all():
    for t in _running_workers.values():
        t.cancel()
    _running_workers.clear()
