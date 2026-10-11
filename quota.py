from datetime import datetime
from database import plans, accounts, auto_channels
from config import OWNER_ID

DEFAULT_PLAN = {
    "max_accounts": 0,
    "max_reactions_per_day": 0,
    "max_views_per_day": 0,
    "max_vc_joins": 0,
    "max_channels": 0,
    "expires_at": None,
}

async def get_plan(uid: int):
    doc = await plans.find_one({"user_id": uid})
    return doc or DEFAULT_PLAN

async def set_plan(uid: int, **kwargs):
    await plans.update_one({"user_id": uid}, {"$set": kwargs}, upsert=True)

async def has_active_plan(uid: int) -> bool:
    """Owner always passes. Others need a real non-expired plan."""
    if uid == OWNER_ID:
        return True
    plan = await plans.find_one({"user_id": uid})
    if not plan:
        return False
    if not plan.get("max_accounts"):
        return False
    exp = plan.get("expires_at")
    if exp and datetime.utcnow() > exp:
        return False
    return True

async def check_quota(uid: int, kind: str, amount: int = 1) -> tuple[bool, int]:
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
    await plans.update_many(
        {},
        {"$set": {"reactions_used_today": 0, "views_used_today": 0}})
