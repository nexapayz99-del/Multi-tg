"""handlers/engagement.py — safe VC commands, no collateral damage."""
import asyncio, uuid, base64, logging
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import API_ID, API_HASH, MAX_AUTO_CHANNELS, OWNER_ID
from database import auto_channels, accounts, all_accounts, seen_posts, vc_state
from quota import check_quota, has_active_plan

logger = logging.getLogger(__name__)


def _is_valid_session(s: str) -> bool:
    """Fast sanity check — does the session string decode to 271 bytes?"""
    if not s or not isinstance(s, str):
        return False
    try:
        pad = (-len(s)) % 4
        raw = base64.urlsafe_b64decode(s + "=" * pad)
        return len(raw) == 271
    except Exception:
        return False


def register(bot):

    @bot.on_message(filters.command("addchannel"))
    async def _addch(c, m):
        uid = m.from_user.id
        if not await has_active_plan(uid):
            return await m.reply(f"🚫 No active plan.\nYour ID: `{uid}`")
        ok, rem = await check_quota(uid, "channels", 1)
        if not ok:
            return await m.reply(f"❌ Channel quota reached (remaining: {rem})")
        c.user_data = getattr(c, "user_data", {})
        c.user_data[uid] = {"step": "channel_link"}
        await m.reply("📺 Send channel link (`@username` or `https://t.me/...`)")

    @bot.on_message(filters.command("channels"))
    async def _list(c, m):
        uid = m.from_user.id
        query = {} if uid == OWNER_ID else {"added_by": uid}
        docs = await auto_channels.find(query).to_list(MAX_AUTO_CHANNELS)
        if not docs:
            return await m.reply("No auto-channels.")
        txt = f"📺 **Auto Channels** ({len(docs)}/{MAX_AUTO_CHANNELS})\n\n"
        kb = []
        for d in docs:
            txt += f"• `{d['link']}`\n"
            kb.append([InlineKeyboardButton(f"🗑 Remove {d['link']}",
                                            callback_data=f"rmch_{d['link']}")])
        await m.reply(txt, reply_markup=InlineKeyboardMarkup(kb))

    @bot.on_callback_query(filters.regex(r"^rmch_"))
    async def _rmchan(c, q):
        link = q.data.split("_", 1)[1]
        uid = q.from_user.id
        doc = await auto_channels.find_one({"link": link})
        if not doc:
            return await q.answer("Not found.", show_alert=True)
        if uid != OWNER_ID and doc.get("added_by") != uid:
            return await q.answer("Not yours.", show_alert=True)
        acc = await accounts.find_one({})
        if acc and _is_valid_session(acc.get("session", "")):
            try:
                async with Client(f"rm_{uuid.uuid4().hex[:6]}",
                                  api_id=API_ID, api_hash=API_HASH,
                                  session_string=acc["session"],
                                  in_memory=True, no_updates=True) as app:
                    chat = await app.get_chat(link)
                    await seen_posts.delete_many({"chat_id": chat.id})
            except Exception as e:
                logger.warning(f"cleanup: {e}")
        await vc_state.delete_one({"key": f"vc_{link}"})
        await auto_channels.delete_one({"link": link})
        try:
            from auto_worker import stop_channel_worker
            await stop_channel_worker(link)
        except Exception: pass
        try: await q.message.edit_text(f"✅ Removed `{link}`")
        except Exception: await q.answer("Removed.", show_alert=True)

    @bot.on_message(filters.command("joinvc"))
    async def _joinvc(c, m):
        uid = m.from_user.id
        if not (await has_active_plan(uid) or uid == OWNER_ID):
            return await m.reply("🚫 No active plan.")
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            return await m.reply("Usage: `/joinvc <channel_link>`")
        target = parts[1].strip()
        await _safe_join_vc(m, target)

    async def _safe_join_vc(m, target):
        """Join VC with all valid sessions. Never kills other sessions."""
        from vc_joiner import _join_one, ensure_silent
        ensure_silent()

        all_accs = await all_accounts()
        valid = [a for a in all_accs if a.get("session") and _is_valid_session(a["session"])]
        invalid = len(all_accs) - len(valid)
        logger.info(f"VC join: {len(valid)} valid / {invalid} corrupt sessions")

        if not valid:
            return await m.reply("❌ No valid sessions in DB.")

        s = await m.reply(
            f"🎤 **Joining VC** `{target}`\n"
            f"Valid sessions: `{len(valid)}`\n"
            f"Corrupt skipped: `{invalid}`")
        joined = failed = 0
        for i, acc in enumerate(valid):
            cid = await _join_one(acc["phone"], acc["session"], target)
            if cid:
                joined += 1
            else:
                failed += 1
            if i % 3 == 0 or i == len(valid) - 1:
                try:
                    await s.edit_text(
                        f"🎤 **Joining VC** `{target}`\n"
                        f"✅ `{joined}` ❌ `{failed}` ({i+1}/{len(valid)})")
                except Exception:
                    pass
            await asyncio.sleep(2)  # fast pacing
        await s.edit_text(
            f"🏁 **VC Join Complete**\n✅ `{joined}` ❌ `{failed}`")

    @bot.on_message(filters.command("leavevc"))
    async def _leavevc(c, m):
        if m.from_user.id != OWNER_ID and not await has_active_plan(m.from_user.id):
            return await m.reply("🚫 No active plan.")
        from vc_joiner import leave_all, active_sessions
        n = len(active_sessions())
        await leave_all()
        await m.reply(f"🚪 Left {n} VC session(s).")

    @bot.on_message(filters.command("joinlink"))
    async def _joinlink(c, m):
        uid = m.from_user.id
        if not (await has_active_plan(uid) or uid == OWNER_ID):
            return await m.reply("🚫 No active plan.")
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            return await m.reply("Usage: `/joinlink <https://t.me/+xxx>`")
        target = parts[1].strip()
        s = await m.reply(f"🔗 Joining `{target}`...")
        accs = await all_accounts()
        accs = [a for a in accs if a.get("session") and _is_valid_session(a["session"])]
        if not accs:
            return await s.edit_text("❌ No valid sessions.")
        ok = fail = already = 0
        for i, acc in enumerate(accs):
            app = None
            try:
                app = Client(f"jl_{uuid.uuid4().hex[:6]}",
                             api_id=API_ID, api_hash=API_HASH,
                             session_string=acc["session"],
                             in_memory=True, no_updates=True)
                await app.start()
                try:
                    await app.join_chat(target); ok += 1
                except Exception as e:
                    em = str(e).lower()
                    if "already" in em:
                        already += 1
                    else:
                        fail += 1
            except Exception:
                fail += 1
            finally:
                if app:
                    try: await app.stop()
                    except Exception: pass
            if i % 5 == 0 or i == len(accs) - 1:
                try:
                    await s.edit_text(
                        f"🔗 `{ok}` ✅ | `{already}` ⏭ | `{fail}` ❌ "
                        f"({i+1}/{len(accs)})")
                except Exception: pass
        await s.edit_text(
            f"🏁 **Join Complete**\n✅ `{ok}`\n⏭ `{already}`\n❌ `{fail}`")

    @bot.on_message(filters.command("leavelink"))
    async def _leavelink(c, m):
        uid = m.from_user.id
        if not (await has_active_plan(uid) or uid == OWNER_ID):
            return await m.reply("🚫 No active plan.")
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            return await m.reply("Usage: `/leavelink <link>`")
        target = parts[1].strip()
        s = await m.reply(f"🚪 Leaving `{target}`...")
        accs = await all_accounts()
        accs = [a for a in accs if a.get("session") and _is_valid_session(a["session"])]
        if not accs:
            return await s.edit_text("❌ No valid sessions.")
        ok = fail = 0
        for i, acc in enumerate(accs):
            app = None
            try:
                app = Client(f"ll_{uuid.uuid4().hex[:6]}",
                             api_id=API_ID, api_hash=API_HASH,
                             session_string=acc["session"],
                             in_memory=True, no_updates=True)
                await app.start()
                try:
                    chat = await app.get_chat(target)
                    await app.leave_chat(chat.id); ok += 1
                except Exception:
                    fail += 1
            except Exception:
                fail += 1
            finally:
                if app:
                    try: await app.stop()
                    except Exception: pass
            if i % 5 == 0 or i == len(accs) - 1:
                try:
                    await s.edit_text(
                        f"🚪 `{ok}` ✅ ❌ `{fail}` ({i+1}/{len(accs)})")
                except Exception: pass
        await s.edit_text(f"🏁 **Leave Complete**\n✅ `{ok}` ❌ `{fail}`")

    @bot.on_message(filters.private, group=-3)
    async def _state(c, m):
        ud = getattr(c, "user_data", {})
        st = ud.get(m.from_user.id)
        if not st or not isinstance(st, dict): return
        if st.get("step") == "channel_link" and m.text:
            link = m.text.strip()
            if "t.me/" not in link and not link.startswith("@"):
                return await m.reply("❌ Invalid link.")
            await auto_channels.update_one(
                {"link": link},
                {"$set": {"link": link, "added_by": m.from_user.id}},
                upsert=True)
            ud.pop(m.from_user.id, None)
            await m.reply(
                f"✅ Added `{link}`.\n"
                f"First scan will just mark existing posts.\n"
                f"New posts + VC auto-handled within 20s.")
