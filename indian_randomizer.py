"""
indian_randomizer.py — bulk name/DP randomizer from a ZIP.

ZIP structure (any of these works):
  • names.txt + photos/*.jpg|png
  • pairs.json: [{"name": "...", "photo": "..."}, ...]
                Also accepts: "dp", "image", "picture", "file", "pic", "path"
  • Just photos (uses default Indian names)
"""
import os, random, json, glob, zipfile, shutil, uuid, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import all_accounts

logger = logging.getLogger(__name__)

DEFAULT_NAMES = [
    "Aarav Sharma", "Vivaan Patel", "Aditya Verma", "Vihaan Singh", "Arjun Reddy",
    "Sai Kumar", "Pranav Iyer", "Krishna Nair", "Reyansh Joshi", "Ayaan Khan",
    "Ananya Gupta", "Diya Mehta", "Aadhya Rao", "Saanvi Kapoor", "Myra Desai",
    "Anika Mishra", "Navya Bhat", "Kiara Pillai", "Ishita Chopra", "Riya Sen",
    "Rohan Malhotra", "Karan Bhatt", "Yash Trivedi", "Nikhil Saxena", "Harsh Vardhan",
    "Priya Kulkarni", "Sneha Menon", "Tanvi Shetty", "Pooja Das", "Neha Banerjee",
    "Rahul Yadav", "Vikram Chauhan", "Deepak Kumar", "Sanjay Pillai", "Arnav Ghosh",
    "Meera Pillai", "Kavya Reddy", "Riya Singh", "Anjali Deshpande", "Shreya Iyer",
]

# Possible key names for "photo" in pairs.json
_PHOTO_KEYS = ("photo", "dp", "image", "picture", "file", "pic", "path", "photo_path")


def _find_photo_path(pair, base_dir):
    """Given a JSON pair dict, find the photo path using any known key."""
    for key in _PHOTO_KEYS:
        if key in pair and pair[key]:
            val = str(pair[key]).strip()
            if not val:
                continue
            # Absolute path or relative
            if os.path.isabs(val) and os.path.exists(val):
                return val
            # Relative to base_dir
            candidate = os.path.join(base_dir, val)
            if os.path.exists(candidate):
                return candidate
            # Try stripping leading ./
            val2 = val.lstrip("./")
            candidate2 = os.path.join(base_dir, val2)
            if os.path.exists(candidate2):
                return candidate2
    return None


def load_pack(zip_path: str):
    """
    Returns (names, photos, pairs, tmp_dir).
    Never raises — returns defaults on failure.
    """
    tmp = f"rand_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
    except Exception as e:
        logger.error(f"ZIP extract failed: {e}")
        return list(DEFAULT_NAMES), [], None, tmp

    # ─── Names ───
    names = list(DEFAULT_NAMES)
    nf = glob.glob(f"{tmp}/**/names.txt", recursive=True)
    if nf:
        try:
            with open(nf[0], "r", encoding="utf-8") as f:
                custom = [l.strip() for l in f if l.strip()]
            if custom:
                names = custom
        except Exception as e:
            logger.warning(f"names.txt read failed: {e}")

    # ─── Photos ───
    photos = (
        glob.glob(f"{tmp}/**/*.jpg", recursive=True) +
        glob.glob(f"{tmp}/**/*.jpeg", recursive=True) +
        glob.glob(f"{tmp}/**/*.png", recursive=True) +
        glob.glob(f"{tmp}/**/*.webp", recursive=True)
    )
    # Filter out tiny files
    photos = [p for p in photos if os.path.getsize(p) > 500]

    # ─── pairs.json (optional) ───
    pairs = None
    pairs_file = glob.glob(f"{tmp}/**/pairs.json", recursive=True)
    if pairs_file:
        try:
            with open(pairs_file[0], "r", encoding="utf-8") as f:
                raw = json.load(f)

            if isinstance(raw, dict):
                # Maybe {"pairs": [...]} or single object
                if "pairs" in raw and isinstance(raw["pairs"], list):
                    raw = raw["pairs"]
                else:
                    raw = [raw]

            base = os.path.dirname(pairs_file[0])
            parsed = []
            for p in raw:
                if not isinstance(p, dict):
                    continue
                name = p.get("name") or p.get("full_name") or p.get("first_name")
                if not name:
                    continue
                photo = _find_photo_path(p, base)
                parsed.append({"name": str(name), "photo": photo})

            if parsed:
                pairs = parsed
                logger.info(f"✅ pairs.json loaded: {len(pairs)} entries")
            else:
                logger.warning("pairs.json had no valid entries, using random names")
        except json.JSONDecodeError as e:
            logger.warning(f"pairs.json invalid JSON: {e}")
        except Exception as e:
            logger.warning(f"pairs.json read failed: {e}")

    # ─── Log what we loaded ───
    logger.info(
        f"📦 Randomizer pack loaded:\n"
        f"   names: {len(names)}\n"
        f"   photos: {len(photos)}\n"
        f"   pairs: {len(pairs) if pairs else 0}")

    return names, photos, pairs, tmp


async def apply_randomizer(zip_path: str, uid: int, progress=None):
    """Apply random names + DPs to every account."""
    names, photos, pairs, tmp = load_pack(zip_path)
    accs = await all_accounts()
    accs = [a for a in accs if a.get("session")]
    if not accs:
        shutil.rmtree(tmp, ignore_errors=True)
        return 0, 0

    logger.info(f"🎭 Applying randomizer to {len(accs)} accounts")

    ok = fail = 0
    for i, acc in enumerate(accs):
        try:
            async with Client(
                f"rand_{uuid.uuid4().hex[:6]}",
                api_id=API_ID, api_hash=API_HASH,
                session_string=acc["session"],
                in_memory=True, no_updates=True,
            ) as app:
                # Pick name/photo
                if pairs:
                    pick = random.choice(pairs)
                    new_name = pick["name"]
                    photo = pick.get("photo")
                else:
                    new_name = random.choice(names) if names else "User"
                    photo = random.choice(photos) if photos else None

                # Split name
                parts = new_name.split(maxsplit=1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""

                # Update profile
                try:
                    await app.update_profile(first_name=first, last_name=last)
                except Exception as e:
                    logger.debug(f"  profile update failed {acc.get('phone')}: {e}")
                    fail += 1
                    continue

                # Set photo
                if photo and os.path.exists(photo):
                    try:
                        await app.set_profile_photo(photo=photo)
                    except Exception as e:
                        logger.debug(f"  photo set failed {acc.get('phone')}: {e}")

                ok += 1
                logger.info(f"  ✅ {acc.get('phone')} → {new_name}")
        except Exception as e:
            logger.warning(f"  ❌ {acc.get('phone')}: {str(e)[:100]}")
            fail += 1

        if progress and (i % 5 == 0 or i == len(accs) - 1):
            try:
                await progress(i + 1, len(accs), ok, fail)
            except Exception:
                pass

    shutil.rmtree(tmp, ignore_errors=True)
    logger.info(f"🏁 Randomizer done: ✅ {ok} | ❌ {fail}")
    return ok, fail