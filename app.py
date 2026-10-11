import asyncio, logging, os
from pyrogram import Client, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID
from database import users
import auto_worker
from quota import reset_daily
from vc_joiner import ensure_silent

import handlers.accounts, handlers.engagement, handlers.owner
import handlers.plans, handlers.broadcast

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

bot = Client("multi_manager_bot",
             api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# register all handlers BEFORE any messages
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


@bot.on_message(filters.command("start") & filters.private)
async def _start(c, m):
    uid = m.from_user.id
    await users.update_one({"user_id": uid},
                           {"$setOnInsert": {"user_id": uid}}, upsert=True)

    from quota import has_active_plan
    if not await has_active_plan(uid):
        return await m.reply(
            f"👋 **Welcome {m.from_user.first_name}**\n\n"
            f"🚫 **You don't have an active plan.**\n\n"
            f"Contact the owner to get access.\n\n"
            f"🆔 Your User ID: `{uid}`\n"
            f"_(Send this ID to the owner)_")

    await m.reply(
        f"👋 **Welcome {m.from_user.first_name}**\n\n"
        f"Use the buttons below:",
        reply_markup=main_menu(uid))


# ─── Button handlers ───
@bot.on_message(filters.private & filters.regex("^📱 Add Account$"))
async def _btn_add(c, m):
    return await _btn_forward(c, m, "/addacc")

@bot.on_message(filters.private & filters.regex("^📦 Import$"))
async def _btn_imp(c, m):
    return await _btn_forward(c, m, "/import")

@bot.on_message(filters.private & filters.regex("^🎭 Randomize$"))
async def _btn_rnd(c, m):
    return await _btn_forward(c, m, "/randomize")

@bot.on_message(filters.private & filters.regex("^📋 My Accounts$"))
async def _btn_accs(c, m):
    return await _btn_forward(c, m, "/accounts")

@bot.on_message(filters.private & filters.regex("^📺 Add Channel$"))
async def _btn_ach(c, m):
    return await _btn_forward(c, m, "/addchannel")

@bot.on_message(filters.private & filters.regex("^📺 Channels$"))
async def _btn_chs(c, m):
    return await _btn_forward(c, m, "/channels")

@bot.on_message(filters.private & filters.regex("^🎤 Join VC$"))
async def _btn_jvc(c, m):
    return await m.reply(
        "Usage: `/joinvc <channel_link_or_@username>`\n\n"
        "Example: `/joinvc @unfindmusic`")

@bot.on_message(filters.private & filters.regex("^🚪 Leave VC$"))
async def _btn_lvc(c, m):
    return await _btn_forward(c, m, "/leavevc")

@bot.on_message(filters.private & filters.regex("^🔗 Join via Link$"))
async def _btn_jl(c, m):
    return await m.reply(
        "Usage: `/joinlink <https://t.me/+xxxx>` or `/joinlink @channel`")

@bot.on_message(filters.private & filters.regex("^🚪 Leave Link$"))
async def _btn_ll(c, m):
    return await m.reply(
        "Usage: `/leavelink <https://t.me/+xxxx>` or `/leavelink @channel`")

@bot.on_message(filters.private & filters.regex("^💎 My Plan$"))
async def _btn_plan(c, m):
    return await _btn_forward(c, m, "/myplan")

@bot.on_message(filters.private & filters.regex("^🩺 Health$"))
async def _btn_health(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("Owner only.")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[m.from_user.id] = "await_health"
    return await m.reply("Running health check...")

@bot.on_message(filters.private & filters.regex("^👑 Owner Panel$"))
async def _btn_owner(c, m):
    if m.from_user.id != OWNER_ID:
        return await m.reply("Owner only.")
    return await _btn_forward(c, m, "/owner")


async def _btn_forward(c, m, cmd):
    """Simulate sending a command by injecting text."""
    # Re-dispatch as if user sent the command
    m.text = cmd
    # We can't easily re-trigger, so just handle directly by calling handlers
    if cmd == "/addacc":
        from handlers.accounts import _addacc
    # Simpler: instruct user
    await m.reply(f"Send `{cmd}` to continue.")


async def _daily_reset():
    while True:
        await asyncio.sleep(86400)
        try:
            await reset_daily()
        except Exception as e:
            log.error(f"daily reset: {e}")


async def _run():
    ensure_silent()
    await bot.start()
    log.info("🤖 Bot started")
    asyncio.create_task(auto_worker.auto_watcher_loop())
    asyncio.create_task(_daily_reset())
    from pyrogram import idle
    await idle()
    await bot.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_run())
