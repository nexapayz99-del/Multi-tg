# ═══════════════════════════════════════════════════════════
# BLOCK PYROGRAM INTERACTIVE INPUT
# ═══════════════════════════════════════════════════════════
import builtins as _builtins


def _no_input(prompt=""):
    raise RuntimeError(
        f"Pyrogram tried to prompt for input: {prompt!r}\n"
        f"A Client() was created without session_string or bot_token."
    )


_builtins.input = _no_input
# ═══════════════════════════════════════════════════════════

import asyncio, logging, io, os, time as _time
from datetime import datetime
from pyrogram import Client, filters, idle
from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID
from database import (
    users, accounts, auto_channels, all_accounts,
    backup_count, restore_from_backups, export_all_backups,
    flood_state,
)
from quota import (
    reset_daily, has_active_plan, check_quota, get_plan,
    is_banned, is_admin,
)
import auto_worker
from vc_joiner import ensure_silent, leave_all, active_sessions

import handlers.owner, handlers.accounts, handlers.engagement
import handlers.plans, handlers.broadcast

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# Silence Pyrogram's chatty logs
for _noisy in (
    "pyrogram.session.session", "pyrogram.connection.connection",
    "pyrogram.session.auth", "pyrogram.crypto.aes",
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


# ═══════════════════════════════════════════════════════════
# MENU
# ═══════════════════════════════════════════════════════════
def main_menu(uid, admin=False, owner=False):
    kb = [
        [KeyboardButton("📱 Add Account"), KeyboardButton("📦 Import")],
        [KeyboardButton("🎭 Randomize"), KeyboardButton("📋 Stats")],
        [KeyboardButton("📺 Add Channel"), KeyboardButton("📺 Channels")],
        [KeyboardButton("🎤 Join VC"), KeyboardButton("🚪 Leave VC")],
        [KeyboardButton("🔗 Join via Link"), KeyboardButton("🚪 Leave Link")],
        [KeyboardButton("💎 My Plan")],
    ]
    if admin or owner:
        kb.append([KeyboardButton("📊 Admin Stats"), KeyboardButton("🩺 Health")])
    if owner:
        kb.append([KeyboardButton("👑 Owner Panel"),
                   KeyboardButton("🗂 My Accounts")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


# ═══════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════
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
        reply_markup=main_menu(uid, await is_admin(uid), uid == OWNER_ID))


# ═══════════════════════════════════════════════════════════
# BUTTONS
# ═══════════════════════════════════════════════════════════
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
    await m.reply(
        "📦 **Send your file**\n\n"
        "• `.zip` (any mix)\n"
        "• `.session` (SQLite)\n"
        "• `.json`\n"
        "• `.txt` (one string per line)\n"
        "• `.hex`\n"
        "• Or paste a session string directly")


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


# ═══════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.private & filters.regex("^📋 Stats$"))
async def _btn_stats(c, m):
    from stats import get_user_stats, format_user_stats
    uid = m.from_user.id
    stats = await get_user_stats(uid)
    plan = await get_plan(uid)
    await m.reply(format_user_stats(m.from_user.first_name, stats, plan))


@bot.on_message(filters.command("stats"))
async def _cmd_stats(c, m):
    from stats import get_user_stats, format_user_stats
    uid = m.from_user.id
    stats = await get_user_stats(uid)
    plan = await get_plan(uid)
    await m.reply(format_user_stats(m.from_user.first_name, stats, plan))


@bot.on_message(filters.private & filters.regex("^📊 Admin Stats$"))
async def _btn_adminstats(c, m):
    if not await is_admin(m.from_user.id):
        return await m.reply("🚫 Admins only.")
    from stats import get_global_stats, format_admin_stats
    g = await get_global_stats()
    await m.reply(format_admin_stats(g))


@bot.on_message(filters.command("globalstats"))
async def _cmd_globalstats(c, m):
    if not await is_admin(m.from_user.id):
        return await m.reply("🚫 Admins only.")
    from stats import get_global_stats, format_admin_stats
    g = await get_global_stats()
    await m.reply(format_admin_stats(g))


# ═══════════════════════════════════════════════════════════
# MY ACCOUNTS
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.private & filters.regex("^🗂 My Accounts$"))
async def _btn_myaccts(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("👑 Owner only.")
    accs = await all_accounts()
    if not accs:
        return await m.reply("📋 No accounts yet.")
    kb = []
    for a in accs[:50]:
        kb.append([InlineKeyboardButton(
            f"⚙️ {a.get('name','?')} | {a.get('phone','?')}",
            callback_data=f"acct_{a.get('phone','')}")])
    await m.reply(f"🗂 **All Accounts** ({len(accs)})\n\nTap to manage:",
                  reply_markup=InlineKeyboardMarkup(kb))


@bot.on_message(filters.command("myaccount"))
async def _cmd_myaccount(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("👑 Owner only.")
    accs = await all_accounts()
    if not accs:
        return await m.reply("📋 No accounts.")
    txt = f"🗂 **Owner Accounts ({len(accs)})**\n\n"
    for a in accs[:80]:
        txt += f"• `{a.get('phone')}` — {a.get('name','?')}\n"
    await m.reply(txt)


# ═══════════════════════════════════════════════════════════
# CHANNELS
# ═══════════════════════════════════════════════════════════
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
        "• `@username` (public)\n"
        "• `https://t.me/username` (public)\n"
        "• `https://t.me/+invite` (private)")


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
        icon = "🔒" if ("/+" in d["link"] or "joinchat/" in d["link"]) else "🌐"
        txt += f"{icon} `{d['link']}`\n"
        kb.append([InlineKeyboardButton(f"🗑 Remove {d['link']}",
                                        callback_data=f"rmch_{d['link']}")])
    await m.reply(txt, reply_markup=InlineKeyboardMarkup(kb))


# ═══════════════════════════════════════════════════════════
# VC
# ═══════════════════════════════════════════════════════════
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
        "• Public: `@channel` or `https://t.me/channel`\n"
        "• Private: `https://t.me/+invite`")


@bot.on_message(filters.private & filters.regex("^🚪 Leave VC$"))
async def _btn_leavevc(c, m):
    uid = m.from_user.id
    if uid != OWNER_ID and not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    n = len(active_sessions())
    await leave_all()
    await m.reply(f"🚪 Left {n} VC session(s).")


# ═══════════════════════════════════════════════════════════
# LINK JOIN / LEAVE
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.private & filters.regex("^🔗 Join via Link$"))
async def _btn_joinlink(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 Banned.")
    if not await has_active_plan(uid):
        return await m.reply("🚫 No active plan.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "joinlink_input"}
    await m.reply("🔗 Send the group/channel link to JOIN:")


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


# ═══════════════════════════════════════════════════════════
# MY PLAN
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.private & filters.regex("^💎 My Plan$"))
async def _btn_myplan(c, m):
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


@bot.on_message(filters.command("myplan"))
async def _cmd_myplan(c, m):
    await _btn_myplan(c, m)


# ═══════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.private & filters.regex("^🩺 Health$"))
async def _btn_health(c, m):
    if not await is_admin(m.from_user.id):
        return await m.reply("🚫 Admins only.")
    await _do_health(c, m)


@bot.on_message(filters.command("health") & filters.private)
async def _cmd_health(c, m):
    if not await is_admin(m.from_user.id):
        return await m.reply("🚫 Admins only.")
    await _do_health(c, m)


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


# ═══════════════════════════════════════════════════════════
# OWNER PANEL
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.private & filters.regex("^👑 Owner Panel$"))
async def _btn_ownerpanel(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("👑 Owner only.")
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
        [InlineKeyboardButton("💾 Backup Info", callback_data="own_backup_info")],
        [InlineKeyboardButton("♻️ Restore From Backup", callback_data="own_restore")],
        [InlineKeyboardButton("📤 Export All Sessions", callback_data="own_export")],
        [InlineKeyboardButton("🧹 Clear Flood State", callback_data="own_clear_flood")],
    ])
    await m.reply("👑 **Owner Panel**\n\nChoose an action:",
                  reply_markup=kb)


# ═══════════════════════════════════════════════════════════
# OWNER CALLBACKS (backup + extras)
# ═══════════════════════════════════════════════════════════
@bot.on_callback_query(filters.regex(r"^own_(backup_info|restore|export|clear_flood)$"))
async def _owner_backup_cb(c, q):
    if q.from_user.id != OWNER_ID:
        return await q.answer("👑 Owner only.", show_alert=True)

    action = q.data.replace("own_", "")

    if action == "backup_info":
        live = await accounts.count_documents({})
        bk = await backup_count()
        await q.message.reply(
            f"💾 **Backup Info**\n\n"
            f"📱 Live accounts: `{live}`\n"
            f"🗄 Backup rows: `{bk}`\n\n"
            f"Every session you import is stored here permanently. "
            f"If the live table is wiped, press **♻️ Restore From Backup**."
        )
        return await q.answer()

    if action == "restore":
        await q.answer("Restoring…")
        msg = await q.message.reply("♻️ Restoring from backup…")
        n = await restore_from_backups()
        await msg.edit_text(f"♻️ Restored `{n}` account(s) from backup.")
        return

    if action == "export":
        await q.answer("Building export…")
        rows = await export_all_backups()
        if not rows:
            return await q.message.reply("📭 No backups yet.")

        buf = io.StringIO()
        buf.write("# Telegram sessions export\n")
        buf.write(f"# generated: {datetime.utcnow()}\n")
        buf.write(f"# count: {len(rows)}\n\n")
        for r in rows:
            buf.write(f"# {r['phone']} | {r['name']}\n")
            buf.write(f"{r['session']}\n\n")

        fname = f"sessions_export_{int(_time.time())}.txt"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())

        await q.message.reply_document(
            fname,
            caption=f"📤 **{len(rows)} sessions**\n"
                    f"Re-importable via 📦 Import")
        try:
            os.remove(fname)
        except Exception:
            pass
        return

    if action == "clear_flood":
        await q.answer()
        r = await flood_state.delete_many({})
        await q.message.reply(f"🧹 Cleared `{r.deleted_count}` flood entries.")


# ═══════════════════════════════════════════════════════════
# OWNER COMMANDS
# ═══════════════════════════════════════════════════════════
@bot.on_message(filters.command("backup") & filters.private)
async def _cmd_backup(c, m):
    if m.from_user.id != OWNER_ID:
        return
    live = await accounts.count_documents({})
    bk = await backup_count()
    await m.reply(f"💾 Live: `{live}` | Backup: `{bk}`")


@bot.on_message(filters.command("restore") & filters.private)
async def _cmd_restore(c, m):
    if m.from_user.id != OWNER_ID:
        return
    n = await restore_from_backups()
    await m.reply(f"♻️ Restored `{n}` account(s).")


@bot.on_message(filters.command("export") & filters.private)
async def _cmd_export(c, m):
    if m.from_user.id != OWNER_ID:
        return
    rows = await export_all_backups()
    if not rows:
        return await m.reply("📭 No backups.")
    buf = io.StringIO()
    for r in rows:
        buf.write(f"# {r['phone']} | {r['name']}\n{r['session']}\n\n")
    fname = f"sessions_export_{int(_time.time())}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    await m.reply_document(fname, caption=f"📤 {len(rows)} sessions")
    try:
        os.remove(fname)
    except Exception:
        pass


@bot.on_message(filters.command("clearflood") & filters.private)
async def _cmd_clearflood(c, m):
    if m.from_user.id != OWNER_ID:
        return
    r = await flood_state.delete_many({})
    await m.reply(f"🧹 Cleared `{r.deleted_count}` flood entries.")


# ═══════════════════════════════════════════════════════════
# TEXT INPUT HANDLERS
# ═══════════════════════════════════════════════════════════
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
        from handlers.engagement import _join_link_bulk
        await _join_link_bulk(m, txt, leave=False)
        return

    if step == "leavelink_input":
        ud.pop(m.from_user.id, None)
        from handlers.engagement import _join_link_bulk
        await _join_link_bulk(m, txt, leave=True)
        return


# ═══════════════════════════════════════════════════════════
# DAILY RESET + RUN
# ═══════════════════════════════════════════════════════════
async def _daily_reset():
    while True:
        await asyncio.sleep(86400)
        try:
            await reset_daily()
        except Exception as e:
            log.error(f"daily reset: {e}")


async def _run():
    ensure_silent()
    log.info(f"🤖 Starting bot | OWNER_ID={OWNER_ID} | "
             f"Pool MAX={auto_worker.MAX_POOL}")
    await bot.start()
    log.info("🤖 Bot started")
    asyncio.create_task(auto_worker.auto_watcher_loop())
    asyncio.create_task(_daily_reset())
    await idle()
    await bot.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_run())