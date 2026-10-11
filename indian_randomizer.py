"""
Bulk name/DP randomizer from a ZIP file.

ZIP structure expected:
  names.txt          → one Indian name per line (optional)
  photos/*.jpg|png   → profile photos
  OR
  pairs.json         → [{"name": "Aarav Sharma", "photo": "01.jpg"}, ...]
"""
import os, random, json, glob, zipfile, shutil, uuid, logging
from pyrogram import Client
from config import API_ID, API_HASH
from database import all_accounts

logger = logging.getLogger(__name__)

DEFAULT_NAMES = [
    "Aarav Sharma","Vivaan Patel","Aditya Verma","Vihaan Singh","Arjun Reddy",
    "Sai Kumar","Pranav Iyer","Krishna Nair","Reyansh Joshi","Ayaan Khan",
    "Ananya Gupta","Diya Mehta","Aadhya Rao","Saanvi Kapoor","Myra Desai",
    "Anika Mishra","Navya Bhat","Kiara Pillai","Ishita Chopra","Riya Sen",
    "Rohan Malhotra","Karan Bhatt","Yash Trivedi","Nikhil Saxena","Harsh Vardhan",
    "Priya Kulkarni","Sneha Menon","Tanvi Shetty","Pooja Das","Neha Banerjee",
]

def load_pack(zip_path: str):
    """Returns (names, photo_paths)."""
    tmp = f"rand_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z: z.extractall(tmp)

    names = list(DEFAULT_NAMES)
    nf = glob.glob(f"{tmp}/**/names.txt", recursive=True)
    if nf:
        with open(nf[0]) as f:
            names = [l.strip() for l in f if l.strip()] or names

    photos = (glob.glob(f"{tmp}/**/*.jpg", recursive=True) +
              glob.glob(f"{tmp}/**/*.jpeg", recursive=True) +
              glob.glob(f"{tmp}/**/*.png", recursive=True))

    pairs_file = glob.glob(f"{tmp}/**/pairs.json", recursive=True)
    pairs = None
    if pairs_file:
        with open(pairs_file[0]) as f: pairs = json.load(f)
        # resolve photo paths relative to json location
        base = os.path.dirname(pairs_file[0])
        pairs = [{"name": p["name"], "photo": os.path.join(base, p["photo"])} for p in pairs]

    return names, photos, pairs, tmp

async def apply_randomizer(zip_path: str, uid: int, progress=None):
    names, photos, pairs, tmp = load_pack(zip_path)
    accs = await all_accounts()
    accs = [a for a in accs if a.get("session")]
    if not accs: return 0, 0

    ok = fail = 0
    for i, acc in enumerate(accs):
        try:
            async with Client(f"rand_{uuid.uuid4().hex[:6]}",
                              api_id=API_ID, api_hash=API_HASH,
                              session_string=acc["session"],
                              in_memory=True, no_updates=True) as app:
                if pairs:
                    pair = random.choice(pairs)
                    new_name, photo = pair["name"], pair["photo"]
                else:
                    new_name = random.choice(names)
                    photo = random.choice(photos) if photos else None

                first, *rest = new_name.split(maxsplit=1)
                last = rest[0] if rest else ""
                await app.update_profile(first_name=first, last_name=last)
                if photo and os.path.exists(photo):
                    try: await app.set_profile_photo(photo=photo)
                    except Exception as e: logger.debug(f"dp fail: {e}")
                ok += 1
        except Exception as e:
            logger.debug(f"randomizer fail {acc.get('phone')}: {e}")
            fail += 1
        if progress and (i % 5 == 0 or i == len(accs) - 1):
            await progress(i + 1, len(accs), ok, fail)

    shutil.rmtree(tmp, ignore_errors=True)
    return ok, fail
