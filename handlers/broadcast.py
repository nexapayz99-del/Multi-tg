from pyrogram import filters
from config import OWNER_ID
from database import users


def register(bot):
    @bot.on_message(filters.command("bc") & filters.user(OWNER_ID))
    async def _bc(c, m):
        if not m.reply_to_message:
            return await m.reply("Reply to a message with /bc")
        s = await m.reply("📢 Broadcasting...")
        ok = fail = 0
        async for u in users.find({}):
            try:
                await m.reply_to_message.copy(u["user_id"])
                ok += 1
            except Exception:
                fail += 1
        await s.edit_text(f"✅ Sent `{ok}` | ❌ `{fail}`")