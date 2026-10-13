"""stats.py — user/admin stats helpers"""
import logging
from database import (
    accounts, users, plans, auto_channels, banned_users, admins, all_accounts,
)
from quota import is_admin, get_plan
from config import OWNER_ID

logger = logging.getLogger(__name__)


async def get_user_stats(uid: int):
    """Full stats for one user."""
    own_accounts = await accounts.count_documents({"added_by": uid})
    plan = await get_plan(uid)
    reactions = plan.get("reactions_used_today", 0)
    views = plan.get("views_used_today", 0)
    vc_joins = plan.get("max_vc_joins", 0)
    channels = await auto_channels.count_documents({"added_by": uid})
    return {
        "accounts": own_accounts,
        "reactions_today": reactions,
        "views_today": views,
        "vc_quota": vc_joins,
        "channels": channels,
    }


async def get_global_stats():
    """Global stats for the whole bot."""
    total_accounts = await accounts.count_documents({})
    total_users = await users.count_documents({})
    total_banned = await banned_users.count_documents({})
    total_channels = await auto_channels.count_documents({})
    total_admins = await admins.count_documents({})
    total_plans = await plans.count_documents({})

    # Sum total reactions and views across all plans
    reactions = views = 0
    async for doc in plans.find({}, {"reactions_used_today": 1, "views_used_today": 1}):
        reactions += doc.get("reactions_used_today", 0)
        views += doc.get("views_used_today", 0)

    # Count VC joins currently active
    try:
        from vc_joiner import active_sessions
        active_vc = len(active_sessions())
    except Exception:
        active_vc = 0

    return {
        "total_accounts": total_accounts,
        "total_users": total_users,
        "total_banned": total_banned,
        "total_channels": total_channels,
        "total_admins": total_admins,
        "total_plans": total_plans,
        "total_reactions": reactions,
        "total_views": views,
        "active_vc": active_vc,
    }


def format_user_stats(name, stats, plan):
    exp = plan.get("expires_at")
    if exp:
        from datetime import datetime
        days_left = max(0, (exp - datetime.utcnow()).days)
        plan_line = f"⏰ Expires in `{days_left}` days"
    else:
        plan_line = "⏰ Duration: `♾ Lifetime`" if plan.get("max_accounts") else "❌ No plan"

    return (
        f"📊 **Your Stats**\n\n"
        f"📱 Your Accounts: `{stats['accounts']}`\n"
        f"❤️ Reactions today: `{stats['reactions_today']}`\n"
        f"👀 Views today: `{stats['views_today']}`\n"
        f"🎤 VC Joins: `{stats['vc_quota']}`\n"
        f"📺 Your Channels: `{stats['channels']}`\n\n"
        f"{plan_line}"
    )


def format_admin_stats(g):
    return (
        f"📊 **Global Stats**\n\n"
        f"👤 Users: `{g['total_users']}`\n"
        f"📱 Logged-in IDs: `{g['total_accounts']}`\n"
        f"❤️ Reactions today: `{g['total_reactions']}`\n"
        f"👀 Views today: `{g['total_views']}`\n"
        f"🎤 Active VC Sessions: `{g['active_vc']}`\n"
        f"📺 Channels: `{g['total_channels']}`\n"
        f"💎 Plans: `{g['total_plans']}`\n"
        f"👮 Admins: `{g['total_admins']}`\n"
        f"🔨 Banned: `{g['total_banned']}`"
    )
