from pyrogram import filters
from database import plans
from quota import get_plan

def register(bot):
    @bot.on_message(filters.command("myplan"))
    async def _myplan(c, m):
        p = await get_plan(m.from_user.id)
        txt = (
            f"💎 **Your Plan**\n\n"
            f"📱 Max Accounts: `{p.get('max_accounts',0)}`\n"
            f"❤️ Reactions/day: `{p.get('max_reactions_per_day',0)}` "
            f"(used `{p.get('reactions_used_today',0)}`)\n"
            f"👀 Views/day: `{p.get('max_views_per_day',0)}` "
            f"(used `{p.get('views_used_today',0)}`)\n"
            f"🎤 VC Joins: `{p.get('max_vc_joins',0)}`\n"
            f"📺 Channels: `{p.get('max_channels',0)}`\n"
        )
        exp = p.get("expires_at")
        if exp: txt += f"⏰ Expires: `{exp}`\n"
        await m.reply(txt)
