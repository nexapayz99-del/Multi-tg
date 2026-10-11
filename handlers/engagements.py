from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import auto_channels, all_accounts
from config import OWNER_ID, MAX_AUTO_CHANNELS
from quota import check_quota

def register(bot):

    @bot.on_message(filters.command("addchannel"))
    async def _addch(c, m):
        uid = m.from_user.id
        ok, rem = await check_quota(uid, "channels", 1)
        if not ok:
            return await m.reply(f"❌ Channel quota reached (remaining: {rem})")
        c.user_data = getattr(c, "user_data", {})
        c.user_data[uid] = {"step": "channel_link"}
        await m.reply("📺 Send the channel link (`@username` or `https://t.me/...`)")

    @bot.on_message(filters.command("channels"))
    async def _list(c, m):
        docs = await auto_channels.find({}).to_list(MAX_AUTO_CHANNELS)
        if not docs: return await m.reply("No auto-channels.")
        txt = f"📺 **Auto Channels** ({len(docs)}/{MAX_AUTO_CHANNELS})\n\n"
        for d in docs:
            txt += f"• `{d['link']}` — owner `{d.get('added_by')}`\n"
        await m.reply(txt)

    @bot.on_message(filters.command("vcstop"))
    async def _vcstop(c, m):
        from vc_joiner import leave_all
        await leave_all()
        await m.reply("🛑 All VC sessions released.")

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
            await m.reply(f"✅ Added `{link}`. Auto VC + react + view will start within 30s.")
