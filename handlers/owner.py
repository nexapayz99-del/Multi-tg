from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import OWNER_ID
from database import users, admins, pseudo_owners, plans, accounts, auto_channels
from quota import set_plan, get_plan

def register(bot):

    @bot.on_message(filters.command("owner") & filters.user(OWNER_ID))
    async def _owner(c, m):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Add Admin", callback_data="own_addadmin"),
             InlineKeyboardButton("❌ Remove Admin", callback_data="own_rmadmin")],
            [InlineKeyboardButton("🎫 Add Pseudo", callback_data="own_addpseudo"),
             InlineKeyboardButton("🚫 Remove Pseudo", callback_data="own_rmpseudo")],
            [InlineKeyboardButton("📊 Stats", callback_data="own_stats")],
            [InlineKeyboardButton("📋 List Plans", callback_data="own_plans")],
            [InlineKeyboardButton("💰 Set User Plan", callback_data="own_setplan")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="own_broadcast")],
            [InlineKeyboardButton("🩺 Health Check", callback_data="own_health")],
        ])
        await m.reply("👑 **Owner Panel**", reply_markup=kb)

    @bot.on_callback_query(filters.regex("^own_"))
    async def _owner_cb(c, q):
        if q.from_user.id != OWNER_ID:
            return await q.answer("Owner only.", show_alert=True)
        d = q.data
        if d == "own_stats":
            u = await users.count_documents({})
            a = await accounts.count_documents({})
            ch = await auto_channels.count_documents({})
            return await q.message.reply(
                f"📊 Users: `{u}` | Accounts: `{a}` | Channels: `{ch}`")
        if d == "own_plans":
            docs = await plans.find({}).to_list(100)
            txt = "📋 **Plans**\n\n"
            for p in docs:
                txt += (f"`{p['user_id']}` → acc:{p.get('max_accounts',0)} "
                        f"r:{p.get('max_reactions_per_day',0)} "
                        f"v:{p.get('max_views_per_day',0)} "
                        f"vc:{p.get('max_vc_joins',0)} "
                        f"ch:{p.get('max_channels',0)}\n")
            return await q.message.reply(txt or "No plans.")
        if d == "own_setplan":
            await q.message.reply(
                "Send: `user_id max_accounts max_reactions max_views max_vc max_channels`")
            c.user_data = getattr(c, "user_data", {})
            c.user_data[q.from_user.id] = "await_setplan"
            return
        if d == "own_broadcast":
            c.user_data = getattr(c, "user_data", {})
            c.user_data[q.from_user.id] = "await_broadcast"
            return await q.message.reply("Send the broadcast message.")

    @bot.on_message(filters.private, group=-1)
    async def _owner_state(c, m):
        if m.from_user.id != OWNER_ID or not m.text: return
        state = getattr(c, "user_data", {}).get(m.from_user.id)
        if state == "await_setplan":
            try:
                parts = m.text.split()
                uid = int(parts[0])
                await set_plan(uid,
                    max_accounts=int(parts[1]),
                    max_reactions_per_day=int(parts[2]),
                    max_views_per_day=int(parts[3]),
                    max_vc_joins=int(parts[4]),
                    max_channels=int(parts[5]))
                c.user_data.pop(m.from_user.id, None)
                await m.reply(f"✅ Plan set for `{uid}`")
            except Exception as e:
                await m.reply(f"❌ Format error: {e}")
        elif state == "await_broadcast":
            c.user_data.pop(m.from_user.id, None)
            s = await m.reply("📢 Broadcasting...")
            ok = fail = 0
            async for u in users.find({}):
                try:
                    await m.copy(u["user_id"]); ok += 1
                except Exception: fail += 1
            await s.edit_text(f"✅ Sent `{ok}` | Failed `{fail}`")
        elif state == "await_addadmin":
            try:
                await admins.update_one({"user_id": int(m.text)},
                                        {"$set": {}}, upsert=True)
                c.user_data.pop(m.from_user.id, None)
                await m.reply("✅ Admin added.")
            except Exception: await m.reply("❌ Bad ID.")
