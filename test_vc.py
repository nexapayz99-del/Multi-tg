import asyncio
from vc_joiner import PYTGCALLS_OK, MediaStream, PyTgCalls

async def main():
    print(f"PyTgCalls OK: {PYTGCALLS_OK}")
    print(f"PyTgCalls class: {PyTgCalls}")
    print(f"MediaStream class: {MediaStream}")

asyncio.run(main())
