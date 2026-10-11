import asyncio, os
from dotenv import load_dotenv
load_dotenv()

async def main():
    print("=" * 60)
    print("ENV CHECK")
    print("=" * 60)
    for k in ("API_ID","API_HASH","BOT_TOKEN","MONGO_URI","OWNER_ID","SESSION_KEY"):
        v = os.getenv(k)
        if k in ("API_HASH","BOT_TOKEN","SESSION_KEY"):
            print(f"{k}: {v[:10] + '...' if v else 'MISSING'}")
        else:
            print(f"{k}: {v}")

    print("\n" + "=" * 60)
    print("MONGODB CHECK")
    print("=" * 60)
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = cli["tg_manager"]
    for name in ("accounts","users","auto_channels","admins","pseudo_owners"):
        n = await db[name].count_documents({})
        print(f"  {name}: {n} docs")

    print("\nACCOUNTS:")
    async for a in db["accounts"].find({}).limit(10):
        print(f"  • {a.get('phone')} | {a.get('name')} | added_by={a.get('added_by')}")

    print("\nAUTO CHANNELS:")
    async for ch in db["auto_channels"].find({}).limit(20):
        print(f"  • {ch.get('link')}")

    print("\n" + "=" * 60)
    print("PYTGCALLS CHECK")
    print("=" * 60)
    try:
        from pytgcalls import PyTgCalls
        from pytgcalls.types import MediaStream
        print("  ✅ PyTgCalls import OK")
        s = MediaStream("silent.wav")
        print(f"  ✅ MediaStream OK: {s}")
    except Exception as e:
        print(f"  ❌ PyTgCalls failed: {e}")

    print("\n" + "=" * 60)
    print("SILENT.WAV CHECK")
    print("=" * 60)
    if os.path.exists("silent.wav"):
        print(f"  ✅ exists ({os.path.getsize('silent.wav')} bytes)")
    else:
        print("  ❌ MISSING — run app.py once to regenerate")

asyncio.run(main())
