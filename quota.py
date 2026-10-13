"""quota.py — plans, bans, admin check"""
from datetime import datetime, timedelta
from database import plans, accounts, auto_channels, banned_users, admins
from config import OWNER_ID

UNLIMITED = 10**9
DURATIONS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "life": None}


async def is_banned(uid):
    if uid == OWNER_ID:
        return False
    doc = await banned_users.find_one({"user_id": uid})
    return bool(doc)


async def ban_user(uid, reason=""):
    await banned_users.update_one(
        {"user_id": uid},
        {"$set": {"user_id": uid, "reason": reason,
                  "banned_at": datetime.utcnow()}},
        upsert=True)


async def unban_user(uid):
    r = await banned_users.delete_one({"user_id": uid})
    return r.deleted_count > 0


async def is_admin(uid):
    if uid == OWNER_ID:
        return True
    doc = await admins.find_one({"user_id": uid})
    return bool(doc)


def _has_plan_shape(plan):
    return plan and plan.get("max_accounts") not in (None, 0)


async def get_plan(uid):
    if uid == OWNER_ID:
        return {
            "max_accounts": UNLIMITED, "max_reactions_per_day": UNLIMITED,
            "max_views_per_day": UNLIMITED, "max_vc_joins": UNLIMITED,
            "max_channels": UNLIMITED, "expires_at": None, "owner": True,
        }
    doc = await plans.find_one({"user_id": uid})
    if not doc:
        return {"max_accounts": 0, "max_reactions_per_day": 0,
                "max_views_per_day": 0, "max_vc_joins": 0,
                "max_channels": 0, "expires_at": None}
    exp = doc.get("expires_at")
    if exp and datetime.utcnow() > exp:
        await plans.delete_one({"user_id": uid})
        return {"max_accounts": 0, "max_reactions_per_day": 0,
                "max_views_per_day": 0, "max_vc_joins": 0,
                "max_channels": 0, "expires_at": None}
    return doc


async def set_plan(uid, duration="30d", **limits):
    days = DURATIONS.get(duration, 30)
    payload = dict(limits)
    payload["user_id"] = uid
    payload["started_at"] = datetime.utcnow()
    payload["duration"] = duration
    payload["expires_at"] = None if days is None else datetime.utcnow() + timedelta(days=days)
    await plans.update_one({"user_id": uid}, {"$set": payload}, upsert=True)


async def remove_plan(uid):
    r = await plans.delete_one({"user_id": uid})
    return r.deleted_count > 0


async def has_active_plan(uid):
    if uid == OWNER_ID:
        return True
    if await is_banned(uid):
        return False
    plan = await get_plan(uid)
    return _has_plan_shape(plan)


async def check_quota(uid, kind, amount=1):
    if uid == OWNER_ID:
        return True, UNLIMITED
    if await is_banned(uid):
        return False, 0
    plan = await get_plan(uid)
    if not _has_plan_shape(plan):
        return False, 0
    exp = plan.get("expires_at")
    if exp and datetime.utcnow() > exp:
        return False, 0
    if kind == "accounts":
        have = await accounts.count_documents({"added_by": uid})
        limit = plan.get("max_accounts", 0)
        return have + amount <= limit, max(0, limit - have)
    if kind == "channels":
        have = await auto_channels.count_documents({"added_by": uid})
        limit = plan.get("max_channels", 0)
        return have + amount <= limit, max(0, limit - have)
    if kind == "vc":
        limit = plan.get("max_vc_joins", 0)
        return amount <= limit, limit
    if kind == "reactions":
        used = plan.get("reactions_used_today", 0)
        limit = plan.get("max_reactions_per_day", 0)
        return used + amount <= limit, max(0, limit - used)
    if kind == "views":
        used = plan.get("views_used_today", 0)
        limit = plan.get("max_views_per_day", 0)
        return used + amount <= limit, max(0, limit - used)
    return False, 0


async def add_usage(uid, kind, amount=1):
    if uid == OWNER_ID:
        return
    if kind in ("reactions", "views"):
        await plans.update_one(
            {"user_id": uid},
            {"$inc": {f"{kind}_used_today": amount}}, upsert=True)


async def reset_daily():
    await plans.update_many({},
        {"$set": {"reactions_used_today": 0, "views_used_today": 0}})