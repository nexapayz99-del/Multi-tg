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

    @bot.on_message(filters.command("setplan") & filters.user(OWNER_ID))
    async def _setplan_cmd(c, m):
        try:
            parts = m.text.split()
            if len(parts) < 7:
                raise ValueError(
                    "Usage: /setplan <uid> <acc> <react> <view> <vc> <chan>")
            uid = int(parts[1])
            await set_plan(uid,
                max_accounts=int(parts[2]),
                max_reactions_per_day=int(parts[3]),
                max_views_per_day=int(parts[4]),
                max_vc_joins=int(parts[5]),
                max_channels=int(parts[6]))
            await m.reply(f"✅ Plan set for `{uid}`")
        except Exception as e:
            await m.reply(f"❌ {e}")

    @bot.on_message(filters.command("delplan") & filters.user(OWNER_ID))
    async def _delplan(c, m):
        try:
            uid = int(m.text.split()[1])
            await plans.delete_one({"user_id": uid})
            await m.reply(f"🗑 Plan removed for `{uid}`")
        except Exception as e:
            await m.reply(f"❌ Usage: /delplan <uid>")

    @bot.on_callback_query(filters.regex("^own_"))
    async def _owner_cb(c, q):
        if q.from_user.id != OWNER_ID:
            return await q.answer("Owner only.", show_alert=True)
        d = q.data

        if d == "own_stats":
            u = await users.count_documents({})
            a = await accounts.count_documents({})
            ch = await auto_channels.count_documents({})
            p = await plans.count_documents({})
            return await q.message.reply(
                f"📊 Users: `{u}` | Accounts: `{a}` | Channels: `{ch}` | Plans: `{p}`")

        if d == "own_plans":
            docs = await plans.find({}).to_list(100)
            if not docs:
                return await q.message.reply("No plans assigned.")
            txt = "📋 **Plans**\n\n"
            for p in docs:
                txt += (f"`{p['user_id']}` → "
                        f"acc:{p.get('max_accounts',0)} "
                        f"r:{p.get('max_reactions_per_day',0)} "
                        f"v:{p.get('max_views_per_day',0)} "
                        f"vc:{p.get('max_vc_joins',0)} "
                        f"ch:{p.get('max_channels',0)}\n")
            return await q.message.reply(txt)

        if d == "own_setplan":
            return await q.message.reply(
                "Send: `/setplan <uid> <accounts> <reactions> <views> <vc> <channels>`\n\n"
                "Example:\n`/setplan 123456789 5 500 500 3 2`")

        if d == "own_broadcast":
            c.user_data = getattr(c, "user_data", {})
            c.user_data[q.from_user.id] = "await_broadcast"
            return await q.message.reply("Send the broadcast message.")

        if d == "own_health":
            c.user_data = getattr(c, "user_data", {})
            c.user_data[q.from_user.id] = "await_health"
            return await q.message.reply("Running health check...")

    # ─── State machine for owner ───
    @bot.on_message(filters.private & filters.user(OWNER_ID), group=-10)
    async def _owner_state(c, m):
        state = getattr(c, "user_data", {}).get(m.from_user.id)
        if state == "await_broadcast":
            c.user_data.pop(m.from_user.id, None)
            s = await m.reply("📢 Broadcasting...")
            ok = fail = 0
            async for u in users.find({}):
                try:
                    await m.copy(u["user_id"]); ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.05)
            await s.edit_text(f"✅ Sent `{ok}` | Failed `{fail}`")
        elif state == "await_health":
            c.user_data.pop(m.from_user.id, None)
            await _run_health(c, m)

    async def _run_health(c, m):
        from database import all_accounts
        from pyrogram import Client
        from config import API_ID, API_HASH
        s = await m.reply("🩺 Health check running...")
        accs = await all_accounts()
        ok = dead = 0
        for a in accs:
            try:
                async with Client(f"h_{uuid.uuid4().hex[:6]}",
                                  api_id=API_ID, api_hash=API_HASH,
                                  session_string=a["session"],
                                  in_memory=True, no_updates=True) as app:
                    await app.get_me(); ok += 1
            except Exception:
                dead += 1
                await accounts.delete_one({"phone": a["phone"]})
        await s.edit_text(f"✅ Alive: `{ok}` | 💀 Removed: `{dead}`")


import uuid, asyncio
