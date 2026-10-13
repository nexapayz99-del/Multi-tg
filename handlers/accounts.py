import os, uuid, shutil, zipfile, logging, base64
from pyrogram import filters, Client
from pyrogram.errors import SessionPasswordNeeded
from config import API_ID, API_HASH, OWNER_ID
from database import all_accounts, save_account
from quota import check_quota, has_active_plan, is_banned
from session_manager import import_any_string, import_file, import_zip
from indian_randomizer import apply_randomizer

logger = logging.getLogger(__name__)


async def _addacc_impl(c, m):
    uid = m.from_user.id
    if await is_banned(uid):
        return await m.reply("🔨 **You are banned.** Contact the owner.")
    if not await has_active_plan(uid):
        return await m.reply(
            f"🚫 **No active plan.**\n\nContact the owner.\n🆔 `{uid}`")
    ok, rem = await check_quota(uid, "accounts", 1)
    if not ok:
        return await m.reply(f"❌ Account quota reached (remaining: {rem})")
    c.user_data = getattr(c, "user_data", {})
    c.user_data[uid] = {"step": "phone"}
    await m.reply("📱 Send phone number (`+91...`)")


def register(bot):

    @bot.on_message(filters.command("addacc"))
    async def _addacc(c, m):
        await _addacc_impl(c, m)

    @bot.on_message(filters.command("import"))
    async def _import(c, m):
        if await is_banned(m.from_user.id):
            return await m.reply("🔨 Banned.")
        if not await has_active_plan(m.from_user.id):
            return await m.reply("🚫 No active plan.")
        c.user_data = getattr(c, "user_data", {})
        c.user_data[m.from_user.id] = {"step": "import_file"}
        await m.reply("📦 Send zip/session/json/txt or session string.")

    @bot.on_message(filters.command("randomize"))
    async def _randomize(c, m):
        if await is_banned(m.from_user.id):
            return await m.reply("🔨 Banned.")
        if not await has_active_plan(m.from_user.id):
            return await m.reply("🚫 No active plan.")
        c.user_data = getattr(c, "user_data", {})
        c.user_data[m.from_user.id] = {"step": "randomizer_zip"}
        await m.reply("📦 Send ZIP with names.txt + photos or pairs.json.")

    @bot.on_message(filters.command("accounts"))
    async def _list(c, m):
        accs = await all_accounts()
        mine = [a for a in accs
                if a.get("added_by") == m.from_user.id or m.from_user.id == OWNER_ID]
        if not mine:
            return await m.reply("No accounts.")
        txt = f"📱 **Accounts** ({len(mine)})\n\n"
        for a in mine[:50]:
            txt += f"• `{a.get('phone')}` — {a.get('name','?')}\n"
        await m.reply(txt)

    @bot.on_message(filters.private, group=-2)
    async def _states(c, m):
        ud = getattr(c, "user_data", {})
        st = ud.get(m.from_user.id)
        if not st:
            return
        step = st.get("step") if isinstance(st, dict) else None

        if step == "import_file" and (m.document or m.text):
            if m.document:
                s = await m.reply("⏳ Downloading...")
                path = await m.download()
                try:
                    from session_manager import import_any_file
                    ok, fail = await import_any_file(path, m.from_user.id)
                    await s.edit_text(
                        f"✅ Imported: `{ok}` | ❌ `{fail}`\n\n"
                        f"_If 0 imported, ensure the file contains "
                        f"valid session strings or .session SQLite files._")
                except Exception as e:
                    await s.edit_text(f"❌ Import error: `{e}`")
                finally:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                ud.pop(m.from_user.id, None)
                return
            if m.text and await import_any_string(m.text.strip(), m.from_user.id):
                await m.reply("✅ Session imported.")
            else:
                await m.reply("❌ Invalid session.")
            ud.pop(m.from_user.id, None)
            return
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    shutil.rmtree(tmp, ignore_errors=True)
                ud.pop(m.from_user.id, None)
                return
            if m.text and await import_any_string(m.text.strip(), m.from_user.id):
                await m.reply("✅ Session imported.")
            else:
                await m.reply("❌ Invalid session.")
            ud.pop(m.from_user.id, None)
            return

        if step == "randomizer_zip" and m.document:
            s = await m.reply("⏳ Applying...")
            path = await m.download()
            try:
                async def prog(i, t, o, f):
                    try:
                        await s.edit_text(f"🎭 `{i}/{t}` ✅{o} ❌{f}")
                    except Exception:
                        pass
                ok, fail = await apply_randomizer(path, m.from_user.id, prog)
                await s.edit_text(f"✅ `{ok}` | ❌ `{fail}`")
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass
            ud.pop(m.from_user.id, None)
            return

        if step == "phone" and m.text:
            try:
                temp = Client(f"temp_{uuid.uuid4().hex[:6]}",
                              api_id=API_ID, api_hash=API_HASH,
                              in_memory=True, no_updates=True)
                await temp.connect()
                co = await temp.send_code(m.text.strip())
                ud[m.from_user.id] = {"step": "code", "phone": m.text.strip(),
                                      "hash": co.phone_code_hash, "client": temp}
                await m.reply("📩 Enter OTP:")
            except Exception as e:
                await m.reply(f"❌ {e}")
                ud.pop(m.from_user.id, None)
            return

        if step == "code" and m.text:
            try:
                await ud[m.from_user.id]["client"].sign_in(
                    ud[m.from_user.id]["phone"],
                    ud[m.from_user.id]["hash"], m.text.strip())
                await _finalize(c, m)
            except SessionPasswordNeeded:
                ud[m.from_user.id]["step"] = "password"
                await m.reply("🔐 Enter 2FA password:")
            except Exception as e:
                await m.reply(f"❌ {e}")
                ud.pop(m.from_user.id, None)
            return

        if step == "password" and m.text:
            try:
                await ud[m.from_user.id]["client"].check_password(m.text.strip())
                await _finalize(c, m)
            except Exception as e:
                await m.reply(f"❌ {e}")
                ud.pop(m.from_user.id, None)
            return

    async def _finalize(c, m):
        ud = getattr(c, "user_data", {})
        temp = ud[m.from_user.id]["client"]
        phone = ud[m.from_user.id]["phone"]
        ss = await temp.export_session_string()
        me = await temp.get_me()
        await save_account(phone, ss, me.first_name, m.from_user.id, {"id": me.id})
        await m.reply(f"✅ Logged in as **{me.first_name}**")
        try:
            await temp.disconnect()
        except Exception:
            pass
        ud.pop(m.from_user.id, None)