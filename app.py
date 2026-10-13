import asyncio, logging
from pyrogram import Client, filters, idle
from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID
from database import users, accounts, all_accounts, auto_channels
from quota import reset_daily, has_active_plan, check_quota, get_plan, is_banned
import auto_worker
from vc_joiner import ensure_silent, leave_all, active_sessions

import handlers.owner, handlers.accounts, handlers.engagement
import handlers.plans, handlers.broadcast

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# Silence Pyrogram's session spam — keeps our import logs visible
for _noisy in (
    "pyrogram.session.session",
    "pyrogram.connection.connection",
    "pyrogram.session.auth",
    "pyrogram.crypto.aes",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")

bot = Client("multi_manager_bot",
             api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

handlers.owner.register(bot)
handlers.accounts.register(bot)
handlers.engagement.register(bot)
handlers.plans.register(bot)
handlers.broadcast.register(bot)


def main_menu(uid):
    kb = [
        [KeyboardButton("📱 Add Account"), KeyboardButton("📦 Import")],
        [KeyboardButton("🎭 Randomize"), KeyboardButton("📋 My Accounts")],
        [KeyboardButton("📺 Add Channel"), KeyboardButton("📺 Channels")],
        [KeyboardButton("🎤 Join VC"), KeyboardButton("🚪 Leave VC")],
        [KeyboardButton("🔗 Join via Link"), KeyboardButton("🚪 Leave Link")],
        [KeyboardButton("💎 My Plan"), KeyboardButton("🩺 Health")],
    ]
    if uid == OWNER_ID:
        kb.append([KeyboardButton("👑 Owner Panel")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


async def force_join_check(c, m_or_cb):
    return True


@bot.on_message(filters.command("start") & filters.private)
async def _start(c, m):
    uid = m.from_user.id
    await users.update_one({"user_id": uid},
        {"$setOnInsert": {"user_id": uid}}, upsert=True)
    if await is_banned(uid):
        return await m.reply(
            "🔨 **You are banned from using this bot.**\n\n"
            f"🆔 Your ID: `{uid}`")
    if not await has_active_plan(uid):
        return await m.reply(
            f"👋 **Welcome {m.from_user.first_name}**\n\n"
            f"🚫 **You don't have an active plan.**\n\n"
            f"Contact the owner to get access.\n\n"
            f"🆔 Your User ID: `{uid}`")
    await m.reply(
        f"👋 **Welcome {m.from_user.first_name}**\n\n"
        f"Use the buttons below 👇",
        reply_markup=main_menu(uid))


@bot.on_message(filters.private & filters.regex("^📱 Add Account$"))
async def _btn_addacc(c, m):
    from handlers.accounts import _addacc_impl
    await _addacc_impl(c, m)


@bot.on_message(filters.private & filters.regex("^📦 Import$"))
async def _btn_import(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "import_file"}
    await m.reply("📦 Send ZIP / session string / JSON / .session / .txt")


@bot.on_message(filters.private & filters.regex("^🎭 Randomize$"))
async def _btn_randomize(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "randomizer_zip"}
    await m.reply("🎭 Send ZIP with names.txt + photos/ or pairs.json")


@bot.on_message(filters.private & filters.regex("^📋 My Accounts$"))
async def _btn_myaccts(c, m):
    uid = m.from_user.id
    accs = await all_accounts()
    mine = [a for a in accs
            if a.get("added_by") == uid or uid == OWNER_ID]
    if not mine:
        return await m.reply("📋 No accounts yet.\n\nUse 📱 Add Account")
    kb = []
    for a in mine[:50]:
        kb.append([InlineKeyboardButton(
            f"⚙️ {a.get('name','?')} | {a.get('phone','?')}",
            callback_data=f"acct_{a.get('phone','')}")])
    await m.reply(f"📋 **Your Accounts** ({len(mine)})\n\nTap to manage:",
                  reply_markup=InlineKeyboardMarkup(kb))


@bot.on_message(filters.private & filters.regex("^📺 Add Channel$"))
async def _btn_addchan(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply(f"🚫 No active plan.\nYour ID: `{uid}`")
    ok, rem = await check_quota(uid, "channels", 1)
    if not ok:
        return await m.reply(f"❌ Channel quota reached (remaining: {rem})")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "channel_link"}
    await m.reply(
        "📺 Send the channel link:\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invite`")


@bot.on_message(filters.private & filters.regex("^📺 Channels$"))
async def _btn_channels(c, m):
    from config import MAX_AUTO_CHANNELS
    uid = m.from_user.id
    query = {} if uid == OWNER_ID else {"added_by": uid}
    docs = await auto_channels.find(query).to_list(MAX_AUTO_CHANNELS)
    if not docs:
        return await m.reply("📺 No auto-channels yet.")
    txt = f"📺 **Auto Channels** ({len(docs)}/{MAX_AUTO_CHANNELS})\n\n"
    kb = []
    for d in docs:
        txt += f"• `{d['link']}`\n"
        kb.append([InlineKeyboardButton(f"🗑 Remove {d['link']}",
                                        callback_data=f"rmch_{d['link']}")])
    await m.reply(txt, reply_markup=InlineKeyboardMarkup(kb))


@bot.on_message(filters.private & filters.regex("^🎤 Join VC$"))
async def _btn_joinvc(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "joinvc_link"}
    await m.reply(
        "🎤 Send the channel link where VC is LIVE:\n\n"
        "Example: `@unfindmusic`\n\n"
        "All valid accounts will join (unmuted).")


@bot.on_message(filters.private & filters.regex("^🚪 Leave VC$"))
async def _btn_leavevc(c, m):
    uid = m.from_user.id
    if uid != OWNER_ID and not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    n = len(active_sessions())
    await leave_all()
    await m.reply(f"🚪 Left {n} VC session(s).")


@bot.on_message(filters.private & filters.regex("^🔗 Join via Link$"))
async def _btn_joinlink(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "joinlink_input"}
    await m.reply(
        "🔗 Send the group/channel link:\n"
        "• Public: `@username`\n"
        "• Private: `https://t.me/+hash`")


@bot.on_message(filters.private & filters.regex("^🚪 Leave Link$"))
async def _btn_leavelink(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "leavelink_input"}
    await m.reply("🚪 Send the link for accounts to LEAVE:")


@bot.on_message(filters.private & filters.regex("^💎 My Plan$"))
async def _btn_myplan(c, m):
    from datetime import datetime
    p = await get_plan(m.from_user.id)
    if not p or p.get("max_accounts") in (None, 0):
        return await m.reply("💎 **No active plan.** Contact the owner.")
    exp = p.get("expires_at")
    exp_line = (f"⏰ Expires: `{exp.strftime('%Y-%m-%d')}`"
                if exp else "⏰ Duration: `♾ Lifetime`")
    await m.reply(
        f"💎 **Your Plan**\n\n"
        f"📱 Max Accounts: `{p.get('max_accounts',0)}`\n"
        f"❤️ Reactions/day: `{p.get('max_reactions_per_day',0)}`\n"
        f"👀 Views/day: `{p.get('max_views_per_day',0)}`\n"
        f"🎤 VC Joins: `{p.get('max_vc_joins',0)}`\n"
        f"📺 Channels: `{p.get('max_channels',0)}`\n"
        f"{exp_line}")


@bot.on_message(filters.private & filters.regex("^🩺 Health$"))
async def _btn_health(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("🩺 Owner only.")
    await _do_health(c, m)


@bot.on_message(filters.private & filters.regex("^👑 Owner Panel$"))
async def _btn_ownerpanel(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("👑 Owner only.")
    from handlers.owner import register
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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


async def _do_health(c, m):
    import uuid
    s = await m.reply("🩺 Health check...")
    accs = await all_accounts()
    ok = dead = 0
    for a in accs:
        try:
            async with Client(f"h_{uuid.uuid4().hex[:6]}",
                              api_id=API_ID, api_hash=API_HASH,
                              session_string=a["session"],
                              in_memory=True, no_updates=True) as app:
                await app.get_me()
                ok += 1
        except Exception:
            dead += 1
            await accounts.delete_one({"phone": a["phone"]})
    await s.edit_text(f"🏁 Alive: `{ok}` | 💀 Removed: `{dead}`")


@bot.on_message(filters.private & filters.text, group=5)
async def _text_states(c, m):
    ud = getattr(c, "user_data", {})
    st = ud.get(m.from_user.id)
    if not st or not isinstance(st, dict):
        return
    step = st.get("step")
    txt = (m.text or "").strip()
    if not txt or txt.startswith("/"):
        return

    if step == "joinvc_link":
        ud.pop(m.from_user.id, None)
        from handlers.engagement import _safe_join_vc
        await _safe_join_vc(m, txt)
        return

    if step == "joinlink_input":
        ud.pop(m.from_user.id, None)
        s = await m.reply(f"🔗 Joining `{txt}` with all accounts...")
        import uuid
        from handlers.engagement import _is_valid_session
        accs = await all_accounts()
        accs = [a for a in accs
                if a.get("session") and _is_valid_session(a["session"])]
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
                    await app.join_chat(txt)
                    ok += 1
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
                    try:
                        await app.stop()
                    except Exception:
                        pass
            if i % 5 == 0 or i == len(accs) - 1:
                try:
                    await s.edit_text(
                        f"🔗 `{ok}` ✅ | `{already}` ⏭ | `{fail}` ❌ "
                        f"({i+1}/{len(accs)})")
                except Exception:
                    pass
        return await s.edit_text(
            f"🏁 **Join Complete**\n✅ `{ok}`\n⏭ `{already}`\n❌ `{fail}`")

    if step == "leavelink_input":
        ud.pop(m.from_user.id, None)
        s = await m.reply(f"🚪 Leaving `{txt}`...")
        import uuid
        from handlers.engagement import _is_valid_session
        accs = await all_accounts()
        accs = [a for a in accs
                if a.get("session") and _is_valid_session(a["session"])]
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
                    chat = await app.get_chat(txt)
                    await app.leave_chat(chat.id)
                    ok += 1
                except Exception:
                    fail += 1
            except Exception:
                fail += 1
            finally:
                if app:
                    try:
                        await app.stop()
                    except Exception:
                        pass
            if i % 5 == 0 or i == len(accs) - 1:
                try:
                    await s.edit_text(
                        f"🚪 `{ok}` ✅ ❌ `{fail}` ({i+1}/{len(accs)})")
                except Exception:
                    pass
        return await s.edit_text(
            f"🏁 **Leave Complete**\n✅ `{ok}` ❌ `{fail}`")


async def _daily_reset():
    while True:
        await asyncio.sleep(86400)
        try:
            await reset_daily()
        except Exception as e:
            log.error(f"daily reset: {e}")


async def _run():
    ensure_silent()
    log.info(f"🤖 Starting bot | OWNER_ID={OWNER_ID} | Pool MAX={auto_worker.MAX_POOL}")
    await bot.start()
    log.info("🤖 Bot started")
    asyncio.create_task(auto_worker.auto_watcher_loop())
    asyncio.create_task(_daily_reset())
    await idle()
    await bot.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_run())