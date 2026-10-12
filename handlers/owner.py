"""handlers/owner.py — full owner panel with plans, durations, bans"""
import asyncio, uuid
from datetime import datetime
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import OWNER_ID, API_ID, API_HASH
from database import users, plans, accounts, auto_channels, banned_users, all_accounts
from quota import set_plan, remove_plan, ban_user, unban_user, DURATIONS

_owner_flow = {}


def register(bot):

    @bot.on_message(filters.command("owner") & filters.user(OWNER_ID))
    async def _owner_cmd(c, m):
        await _show_panel(m)

    async def _show_panel(m):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Stats", callback_data="own_stats")],
            [InlineKeyboardButton("📋 List All Plans", callback_data="own_listplans")],
            [InlineKeyboardButton("💎 Set Plan", callback_data="own_setplan")],
            [InlineKeyboardButton("🚫 Remove Plan", callback_data="own_delplan")],
            [InlineKeyboardButton("👤 List Users", callback_data="own_listusers")],
            [InlineKeyboardButton("🔨 Ban User", callback_data="own_ban"),
             InlineKeyboardButton("✅ Unban User", callback_data="own_unban")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="own_broadcast")],
            [InlineKeyboardButton("🩺 Health Check", callback_data="own_health")],
            [InlineKeyboardButton("🗑 Clean Dead Sessions", callback_data="own_clean")],
        ])
        await m.reply("👑 **Owner Panel**\n\nChoose an action:",
                      reply_markup=kb)

    @bot.on_callback_query(filters.regex("^own_"))
    async def _cb(c, q):
        if q.from_user.id != OWNER_ID:
            return await q.answer("Owner only.", show_alert=True)
        d = q.data
        uid = q.from_user.id

        if d == "own_stats":
            u = await users.count_documents({})
            a = await accounts.count_documents({})
            ch = await auto_channels.count_documents({})
            p = await plans.count_documents({})
            b = await banned_users.count_documents({})
            return await q.message.reply(
                f"📊 **Stats**\n\n"
                f"👤 Users: `{u}`\n"
                f"📱 Accounts: `{a}`\n"
                f"📺 Auto-Channels: `{ch}`\n"
                f"💎 Active Plans: `{p}`\n"
                f"🔨 Banned: `{b}`")

        if d == "own_listplans":
            docs = await plans.find({}).to_list(200)
            if not docs:
                return await q.message.reply("📋 No plans assigned.")
            txt = f"📋 **Plans ({len(docs)})**\n\n"
            for p in docs:
                exp = p.get("expires_at")
                exp_str = exp.strftime("%Y-%m-%d") if exp else "lifetime"
                txt += (
                    f"`{p['user_id']}` — "
                    f"acc:{p.get('max_accounts',0)} "
                    f"❤️{p.get('max_reactions_per_day',0)} "
                    f"👀{p.get('max_views_per_day',0)} "
                    f"🎤{p.get('max_vc_joins',0)} "
                    f"📺{p.get('max_channels',0)} "
                    f"[{p.get('duration','?')}→{exp_str}]\n")
            kb = []
            for p in docs[:40]:
                kb.append([InlineKeyboardButton(
                    f"🚫 Remove {p['user_id']}",
                    callback_data=f"own_rmplan_{p['user_id']}")])
            return await q.message.reply(txt, reply_markup=InlineKeyboardMarkup(kb))

        if d.startswith("own_rmplan_"):
            target = int(d.split("_")[-1])
            ok = await remove_plan(target)
            if ok:
                await q.answer(f"✅ Removed plan for {target}", show_alert=True)
                try:
                    await q.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return
            return await q.answer("No plan found.", show_alert=True)

        if d == "own_setplan":
            _owner_flow[uid] = {"step": "plan_await_uid"}
            return await q.message.reply(
                "💎 **Set Plan — Step 1/7**\n\n"
                "Send the **User ID**.\n"
                "_(User can get their ID by sending /start to the bot)_\n\n"
                "Cancel: /cancel")

        if d == "own_delplan":
            _owner_flow[uid] = {"step": "plan_await_remove_uid"}
            return await q.message.reply(
                "🚫 Send the **User ID** to remove plan from.\n\nCancel: /cancel")

        if d == "own_listusers":
            docs = await users.find({}).to_list(200)
            if not docs:
                return await q.message.reply("No users.")
            txt = f"👥 **Users ({len(docs)})**\n\n"
            for doc in docs[:100]:
                uid_ = doc["user_id"]
                banned = await banned_users.find_one({"user_id": uid_})
                plan = await plans.find_one({"user_id": uid_})
                status = "🔨" if banned else ("💎" if plan else "👤")
                txt += f"{status} `{uid_}`\n"
            return await q.message.reply(txt)

        if d == "own_ban":
            _owner_flow[uid] = {"step": "ban_await_uid"}
            return await q.message.reply(
                "🔨 Send the **User ID** to BAN.\n\nCancel: /cancel")

        if d == "own_unban":
            _owner_flow[uid] = {"step": "unban_await_uid"}
            return await q.message.reply(
                "✅ Send the **User ID** to UNBAN.\n\nCancel: /cancel")

        if d == "own_broadcast":
            _owner_flow[uid] = {"step": "await_broadcast"}
            return await q.message.reply(
                "📢 Send the message to broadcast.\n\nCancel: /cancel")

        if d == "own_health":
            asyncio.create_task(_run_health(q.message))
            return await q.answer("Health check started...", show_alert=True)

        if d == "own_clean":
            asyncio.create_task(_clean_dead(q.message))
            return await q.answer("Cleaning dead sessions...", show_alert=True)

    @bot.on_message(filters.command("cancel") & filters.user(OWNER_ID))
    async def _cancel(c, m):
        _owner_flow.pop(m.from_user.id, None)
        await m.reply("✅ Cancelled.")

    @bot.on_message(filters.private & filters.user(OWNER_ID) & filters.text,
                    group=-20)
    async def _owner_text(c, m):
        uid = m.from_user.id
        flow = _owner_flow.get(uid)
        if not flow:
            return
        txt = (m.text or "").strip()
        if not txt or txt.startswith("/"):
            return
        step = flow["step"]

        if step == "plan_await_uid":
            try:
                target = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            flow["data"] = {"user_id": target}
            flow["step"] = "plan_await_acc"
            return await m.reply(
                f"✅ User: `{target}`\n\n"
                f"💎 **Step 2/7:** Max **accounts**?\n"
                f"Send a number (e.g. `5`)")

        if step == "plan_await_acc":
            try:
                n = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            flow["data"]["max_accounts"] = n
            flow["step"] = "plan_await_react"
            return await m.reply(f"✅ {n}\n\n❤️ **Step 3/7:** Max reactions/day?")

        if step == "plan_await_react":
            try:
                n = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            flow["data"]["max_reactions_per_day"] = n
            flow["step"] = "plan_await_view"
            return await m.reply(f"✅ {n}\n\n👀 **Step 4/7:** Max views/day?")

        if step == "plan_await_view":
            try:
                n = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            flow["data"]["max_views_per_day"] = n
            flow["step"] = "plan_await_vc"
            return await m.reply(f"✅ {n}\n\n🎤 **Step 5/7:** Max VC joins?")

        if step == "plan_await_vc":
            try:
                n = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            flow["data"]["max_vc_joins"] = n
            flow["step"] = "plan_await_chan"
            return await m.reply(f"✅ {n}\n\n📺 **Step 6/7:** Max channels?")

        if step == "plan_await_chan":
            try:
                n = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            flow["data"]["max_channels"] = n
            flow["step"] = "plan_await_duration"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("1 day", callback_data="own_dur_1d"),
                 InlineKeyboardButton("7 days", callback_data="own_dur_7d")],
                [InlineKeyboardButton("30 days", callback_data="own_dur_30d"),
                 InlineKeyboardButton("90 days", callback_data="own_dur_90d")],
                [InlineKeyboardButton("♾ Lifetime", callback_data="own_dur_life")],
            ])
            return await m.reply(
                f"✅ {n}\n\n⏰ **Step 7/7:** Choose duration:",
                reply_markup=kb)

        if step == "await_broadcast":
            _owner_flow.pop(uid, None)
            s = await m.reply("📢 Broadcasting...")
            ok = fail = 0
            async for u in users.find({}):
                try:
                    await m.copy(u["user_id"])
                    ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.05)
            await s.edit_text(f"✅ Sent `{ok}` | ❌ `{fail}`")

        if step == "plan_await_remove_uid":
            try:
                target = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            ok = await remove_plan(target)
            _owner_flow.pop(uid, None)
            if ok:
                await m.reply(f"✅ Removed plan for `{target}`")
            else:
                await m.reply(f"⚠️ No plan found for `{target}`")

        if step == "ban_await_uid":
            try:
                target = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            _owner_flow.pop(uid, None)
            await ban_user(target, "Manual ban by owner")
            await m.reply(f"🔨 Banned `{target}`")

        if step == "unban_await_uid":
            try:
                target = int(txt)
            except ValueError:
                return await m.reply("❌ Send a number.")
            _owner_flow.pop(uid, None)
            ok = await unban_user(target)
            if ok:
                await m.reply(f"✅ Unbanned `{target}`")
            else:
                await m.reply(f"⚠️ `{target}` was not banned")

@bot.on_callback_query()
    async def _duration_cb(c, q):
        # Only handle our duration buttons; ignore everything else
        if not (q.data or "").startswith("own_dur_"):
            return

        if q.from_user.id != OWNER_ID:
            return await q.answer("Owner only.", show_alert=True)

        flow = _owner_flow.get(q.from_user.id)
        if not flow or flow.get("step") != "plan_await_duration":
            return await q.answer("Start again with /owner → 💎 Set Plan.",
                                  show_alert=True)

        raw = q.data.replace("own_dur_", "")   # 1d / 7d / 30d / 90d / life
        days_map = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "life": 36500}
        duration = raw
        days = days_map.get(raw, 30)

        d = flow["data"]
        target = d["user_id"]

        await set_plan(
            target,
            duration=duration,
            max_accounts=d["max_accounts"],
            max_reactions_per_day=d["max_reactions_per_day"],
            max_views_per_day=d["max_views_per_day"],
            max_vc_joins=d["max_vc_joins"],
            max_channels=d["max_channels"])

        _owner_flow.pop(q.from_user.id, None)

        pretty = {"1d": "1 day", "7d": "7 days", "30d": "30 days",
                  "90d": "90 days", "life": "♾ Lifetime"}.get(raw, raw)

        try:
            await q.message.edit_text(
                f"✅ **Plan Set**\n\n"
                f"👤 User: `{target}`\n"
                f"📱 Accounts: `{d['max_accounts']}`\n"
                f"❤️ Reactions/day: `{d['max_reactions_per_day']}`\n"
                f"👀 Views/day: `{d['max_views_per_day']}`\n"
                f"🎤 VC Joins: `{d['max_vc_joins']}`\n"
                f"📺 Channels: `{d['max_channels']}`\n"
                f"⏰ Duration: **{pretty}** ({days} days)")
        except Exception:
            pass

        await q.answer(f"✅ Plan set for {pretty}")

    async def _run_health(m):
        s = await m.reply("🩺 Checking all sessions...")
        accs = await all_accounts()
        ok = dead = 0
        for i, a in enumerate(accs):
            try:
                async with Client(f"h_{uuid.uuid4().hex[:6]}",
                                  api_id=API_ID, api_hash=API_HASH,
                                  session_string=a["session"],
                                  in_memory=True, no_updates=True) as app:
                    await app.get_me()
                    ok += 1
            except Exception:
                dead += 1
            if i % 5 == 0 or i == len(accs) - 1:
                try:
                    await s.edit_text(f"🩺 `{i+1}/{len(accs)}` ✅{ok} ❌{dead}")
                except Exception:
                    pass
        await s.edit_text(f"🏁 **Health** ✅ `{ok}` 💀 `{dead}`")

    async def _clean_dead(m):
        s = await m.reply("🧹 Cleaning dead sessions...")
        accs = await all_accounts()
        removed = 0
        for i, a in enumerate(accs):
            try:
                async with Client(f"c_{uuid.uuid4().hex[:6]}",
                                  api_id=API_ID, api_hash=API_HASH,
                                  session_string=a["session"],
                                  in_memory=True, no_updates=True) as app:
                    await app.get_me()
            except Exception:
                await accounts.delete_one({"phone": a["phone"]})
                removed += 1
            if i % 5 == 0 or i == len(accs) - 1:
                try:
                    await s.edit_text(f"🧹 `{i+1}/{len(accs)}` — removed `{removed}`")
                except Exception:
                    pass
        await s.edit_text(f"🏁 Cleaned `{removed}` dead sessions")