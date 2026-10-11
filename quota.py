"""
Owner assigns quotas per user:
  – max_reactions   (total per day)
  – max_views       (total per day)
  – max_vc_joins    (concurrent accounts in VC)
  – max_channels    (auto-channels the user can own)
  – max_accounts    (accounts user can store)
"""
from datetime import datetime, timedelta
from database import plans, accounts, auto_channels

DEFAULT_PLAN = {
    "max_accounts": 5,
    "max_reactions_per_day": 500,
    "max_views_per_day": 500,
    "max_vc_joins": 3,
    "max_channels": 1,
    "expires_at": None,
}

async def get_plan(uid: int):
    doc = await plans.find_one({"user_id": uid})
    return doc or DEFAULT_PLAN

async def set_plan(uid: int, **kwargs):
    await plans.update_one({"user_id": uid}, {"$set": kwargs}, upsert=True)

async def check_quota(uid: int, kind: str, amount: int = 1) -> tuple[bool, int]:
    """kind in: accounts | reactions | views | vc | channels. Returns (allowed, remaining)."""
    plan = await get_plan(uid)
    expires = plan.get("expires_at")
    if expires and datetime.utcnow() > expires:
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

async def add_usage(uid: int, kind: str, amount: int = 1):
    if kind in ("reactions", "views"):
        await plans.update_one(
            {"user_id": uid},
            {"$inc": {f"{kind}_used_today": amount}},
            upsert=True)

async def reset_daily():
    """Run once a day at 00:00 UTC."""
    await plans.update_many(
        {},
        {"$set": {"reactions_used_today": 0, "views_used_today": 0}}
    )
