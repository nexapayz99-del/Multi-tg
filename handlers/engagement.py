"""
handlers/engagement.py
  • /addchannel — add channel to auto-watch
  • /channels   — list + remove buttons
  • /joinvc <link>    — join VC in a specific channel NOW (all accounts)
  • /leavevc          — leave all VCs
  • /joinlink <invite>  — accounts join a private group via invite link
  • /leavelink <invite> — accounts leave that group
"""
import asyncio, logging, uuid
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import API_ID, API_HASH, MAX_AUTO_CHANNELS, OWNER_ID, SILENT_WAV
from database import auto_channels, accounts, all_accounts, seen_posts, vc_state
from quota import check_quota, has_active_plan

logger = logging.getLogger(__name__)

def register(bot):

    # ─────────────────────────────────────────
    # /addchannel
    # ─────────────────────────────────────────
    @bot.on_message(filters.command("addchannel"))
    async def _addch(c, m):
        uid = m.from_user.id
        if not await has_active_plan(uid):
            return await m.reply(
                f"🚫 No active plan.\nYour ID: `{uid}`")
        ok, rem = await check_quota(uid, "channels", 1)
        if not ok:
            return await m.reply(f"❌ Channel quota reached (remaining: {rem})")
        c.user_data = getattr(c, "user_data", {})
        c.user_data[uid] = {"step": "channel_link"}
        await m.reply("📺 Send the channel link (`@username` or `https://t.me/...`)")

    # ─────────────────────────────────────────
    # /channels — list + remove buttons
    # ─────────────────────────────────────────
    @bot.on_message(filters.command("channels"))
    async def _list(c, m):
        uid = m.from_user.id
        query = {} if uid == OWNER_ID else {"added_by": uid}
        docs = await auto_channels.find(query).to_list(MAX_AUTO_CHANNELS)
        if not docs:
            return await m.reply("No auto-channels registered.")
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

        # permission check
        doc = await auto_channels.find_one({"link": link})
        if not doc:
            return await q.answer("Not found.", show_alert=True)
        if uid != OWNER_ID and doc.get("added_by") != uid:
            return await q.answer("Not yours.", show_alert=True)

        # cleanup seen-posts
        acc = await accounts.find_one({})
        if acc:
            try:
                async with Client(f"rm_{uuid.uuid4().hex[:6]}",
                                  api_id=API_ID, api_hash=API_HASH,
                                  session_string=acc["session"],
                                  in_memory=True, no_updates=True) as app:
                    chat = await app.get_chat(link)
                    await seen_posts.delete_many({"chat_id": chat.id})
            except Exception as e:
                logger.warning(f"cleanup err: {e}")

        await vc_state.delete_one({"key": f"vc_{link}"})
        await auto_channels.delete_one({"link": link})

        # stop VC worker
        try:
            from auto_worker import stop_channel_worker
            await stop_channel_worker(link)
        except Exception:
            pass

        try:
            await q.message.edit_text(f"✅ Removed `{link}`")
        except Exception:
            await q.answer("Removed.", show_alert=True)

    # ─────────────────────────────────────────
    # /joinvc <link>  — join VC in given channel NOW
    # ─────────────────────────────────────────
    @bot.on_message(filters.command("joinvc"))
    async def _joinvc(c, m):
        uid = m.from_user.id
        if not (await has_active_plan(uid) or uid == OWNER_ID):
            return await m.reply("🚫 No active plan.")
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            return await m.reply("Usage: `/joinvc <channel_link_or_@username>`")
        target = parts[1].strip()

        s = await m.reply(f"🎤 Joining VC in `{target}` with all accounts...")
        from vc_joiner import _join_one, ensure_silent
        ensure_silent()

        accs = await all_accounts()
        accs = [a for a in accs if a.get("session")]
        if not accs:
            return await s.edit_text("❌ No accounts.")

        joined = failed = 0
        for i, acc in enumerate(accs):
            cid = await _join_one(acc["phone"], acc["session"], target)
            if cid:
                joined += 1
            else:
                failed += 1
            try:
                await s.edit_text(
                    f"🎤 **Joining VC** `{target}`\n✅ `{joined}` ❌ `{failed}` "
                    f"({i+1}/{len(accs)})")
            except Exception:
                pass
            await asyncio.sleep(7)

        await s.edit_text(
            f"🎤 **VC Join Complete**\n✅ `{joined}` joined\n❌ `{failed}` failed")

    # ─────────────────────────────────────────
    # /leavevc — leave all active VCs
    # ─────────────────────────────────────────
    @bot.on_message(filters.command("leavevc"))
    async def _leavevc(c, m):
        if m.from_user.id != OWNER_ID and not await has_active_plan(m.from_user.id):
            return await m.reply("🚫 No active plan.")
        from vc_joiner import leave_all, active_sessions
        n = len(active_sessions())
        await leave_all()
        await m.reply(f"🚪 Left {n} VC session(s).")

    # ─────────────────────────────────────────
    # /joinlink <invite_or_username> — accounts join group via link
    # ─────────────────────────────────────────
    @bot.on_message(filters.command("joinlink"))
    async def _joinlink(c, m):
        uid = m.from_user.id
        if not (await has_active_plan(uid) or uid == OWNER_ID):
            return await m.reply("🚫 No active plan.")
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            return await m.reply(
                "Usage: `/joinlink <https://t.me/+xxxx>` or `/joinlink @channel`")
        target = parts[1].strip()

        s = await m.reply(f"🔗 Joining `{target}` with all accounts...")
        accs = await all_accounts()
        accs = [a for a in accs if a.get("session")]
        if not accs:
            return await s.edit_text("❌ No accounts.")

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
                    await app.join_chat(target)
                    ok += 1
                except Exception as e:
                    em = str(e).lower()
                    if "already" in
