from pyrogram import filters
from quota import get_plan
from datetime import datetime


def register(bot):
    @bot.on_message(filters.command("myplan"))
    async def _myplan(c, m):
        p = await get_plan(m.from_user.id)
        if not p or p.get("max_accounts") in (None, 0):
            return await m.reply(
                "💎 **No active plan.**\n\n"
                "Contact the owner to get access.")
        exp = p.get("expires_at")
        if exp:
            days_left = max(0, (exp - datetime.utcnow()).days)
            exp_line = f"⏰ Expires in: `{days_left} days` ({exp.strftime('%Y-%m-%d')})"
        else:
            exp_line = "⏰ Duration: `♾ Lifetime`"
        await m.reply(
            f"💎 **Your Plan**\n\n"
            f"📱 Max Accounts: `{p.get('max_accounts',0)}`\n"
            f"❤️ Reactions/day: `{p.get('max_reactions_per_day',0)}`\n"
            f"👀 Views/day: `{p.get('max_views_per_day',0)}`\n"
            f"🎤 VC Joins: `{p.get('max_vc_joins',0)}`\n"
            f"📺 Channels: `{p.get('max_channels',0)}`\n"
            f"{exp_line}")