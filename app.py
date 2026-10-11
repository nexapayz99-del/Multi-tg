import asyncio, logging, os
from pyrogram import Client, idle, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID
from database import users
import auto_worker
from quota import reset_daily
from vc_joiner import ensure_silent

# handlers
import handlers.owner, handlers.accounts, handlers.engagement
import handlers.plans, handlers.broadcast

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

bot = Client("multi_manager_bot",
             api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# register handlers
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
        [KeyboardButton("💎 My Plan"), KeyboardButton("🩺 Health")],
    ]
    if uid == OWNER_ID:
        kb.append([KeyboardButton("👑 Owner Panel")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

@bot.on_message(filters.command("start") & filters.private)
async def _start(c, m):
    await users.update_one({"user_id": m.from_user.id},
                           {"$setOnInsert": {"user_id": m.from_user.id}},
                           upsert=True)
    await m.reply(
        f"👋 **Welcome {m.from_user.first_name}**\n\n"
        f"I manage multi-account automation:\n"
        f"• Auto react & view on channels\n"
        f"• Auto VC join (PyTgCalls, muted mic)\n"
        f"• Indian name/DP randomizer\n"
        f"• Quota-based paid plans",
        reply_markup=main_menu(m.from_user.id))

@bot.on_message(filters.private & filters.regex("^📱 Add Account$"))
async def _btn_add(c, m):
    c.user_data = getattr(c, "user_data", {})
    c.user_data[m.from_user.id] = {"step": "phone"}
    await m.reply("Send phone (`+91...`)")

@bot.on_message(filters.private & filters.regex("^📦 Import$"))
async def _btn_imp(c, m):
    c.user_data = getattr(c, "user_data", {})
    c.user_data[m.from_user.id] = {"step": "import_file"}
    await m.reply("Send ZIP / session string / JSON / .session / .txt")

@bot.on_message(filters.private & filters.regex("^🎭 Randomize$"))
async def _btn_rnd(c, m):
    c.user_data = getattr(c, "user_data", {})
    c.user_data[m.from_user.id] = {"step": "randomizer_zip"}
    await m.reply("Send ZIP with names.txt + photos/ or pairs.json")

@bot.on_message(filters.private & filters.regex("^📋 My Accounts$"))
async def _btn_accs(c, m):
    from database import all_accounts
    accs = await all_accounts()
    mine = [a for a in accs if a.get("added_by") == m.from_user.id or m.from_user.id == OWNER_ID]
    txt = f"📱 **{len(mine)} accounts**\n\n" + "\n".join(
        f"• `{a.get('phone')}` — {a.get('name','?')}" for a in mine[:50])
    await m.reply(txt or "None.")

@bot.on_message(filters.private & filters.regex("^📺 Add Channel$"))
async def _btn_ach(c, m):
    c.user_data = getattr(c, "user_data", {})
    c.user_data[m.from_user.id] = {"step": "channel_link"}
    await m.reply("Send the channel link.")

@bot.on_message(filters.private & filters.regex("^📺 Channels$"))
async def _btn_chs(c, m):
    from database import auto_channels
    docs = await auto_channels.find({}).to_list(50)
    await m.reply("\n".join(f"• `{d['link']}`" for d in docs) or "None.")

@bot.on_message(filters.private & filters.regex("^💎 My Plan$"))
async def _btn_plan(c, m):
    from quota import get_plan
    p = await get_plan(m.from_user.id)
    await m.reply(f"📱 Accounts: `{p.get('max_accounts',0)}`\n"
                  f"❤️ Reactions/day: `{p.get('max_reactions_per_day',0)}`\n"
                  f"👀 Views/day: `{p.get('max_views_per_day',0)}`\n"
                  f"🎤 VC: `{p.get('max_vc_joins',0)}`\n"
                  f"📺 Channels: `{p.get('max_channels',0)}`")

@bot.on_message(filters.private & filters.regex("^👑 Owner Panel$"))
async def _btn_owner(c, m):
    if m.from_user.id != OWNER_ID: return await m.reply("Owner only.")
    await m.reply("Use /owner")

@bot.on_message(filters.private & filters.regex("^🩺 Health$"))
async def _btn_health(c, m):
    from database import all_accounts
    from pyrogram import Client
    from config import API_ID, API_HASH
    s = await m.reply("🩺 Checking all sessions...")
    accs = await all_accounts()
    ok = dead = 0
    for a in accs:
        try:
            async with Client(f"h_{a['phone']}", api_id=API_ID, api_hash=API_HASH,
                              session_string=a["session"],
                              in_memory=True, no_updates=True) as app:
                await app.get_me(); ok += 1
        except Exception:
            dead += 1
            from database import accounts
            await accounts.delete_one({"phone": a["phone"]})
    await s.edit_text(f"✅ Alive: `{ok}` | 💀 Removed: `{dead}`")

async def _daily_reset():
    while True:
        await asyncio.sleep(86400)
        try: await reset_daily()
        except Exception as e: log.error(f"daily reset: {e}")

async def _run():
    ensure_silent()
    await bot.start()
    log.info("🤖 Bot started")
    asyncio.create_task(auto_worker.auto_watcher_loop())
    asyncio.create_task(_daily_reset())
    await idle()
    await bot.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_run())
