import asyncio
import logging
import os
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Start-Admins aus der Umgebungsvariable (werden beim ersten Start in die DB geschrieben)
INITIAL_ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

MINI_APP_URL = "https://tight-unit-516b.maxwindani-a8b.workers.dev"

# Feste Tastatur unten
mini_app_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Mini-App öffnen", web_app=WebAppInfo(url=MINI_APP_URL))]
    ],
    resize_keyboard=True,
    is_persistent=True
)

DB_PATH = "bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_id TEXT,
                caption TEXT,
                created_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT
            )
        """)
        # Initiale Admins aus ENV eintragen (nur wenn noch nicht vorhanden)
        for admin_id in INITIAL_ADMIN_IDS:
            await db.execute(
                "INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)",
                (admin_id, datetime.utcnow().isoformat())
            )
        await db.commit()


async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None


async def get_admin_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ---------- Mitglieder ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at) VALUES (?, ?, ?, ?)",
            (user.id, user.username, user.first_name, datetime.utcnow().isoformat())
        )
        await db.commit()

    await message.answer(
        "Willkommen!\n\n"
        "Schick mir einfach ein Foto von deiner Einzahlung (optional mit Text dazu).\n"
        "Ich speichere es für die Admins.\n\n"
        "Du bekommst hier später auch Rundmails der Admins.",
        reply_markup=mini_app_keyboard
    )


@dp.message(Command("app"))
async def cmd_app(message: Message):
    await message.answer(
        "Öffne die Cashout Crew Mini-App:",
        reply_markup=mini_app_keyboard
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    user = message.from_user
    photo = message.photo[-1]  # höchste Auflösung
    caption = message.caption or ""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at) VALUES (?, ?, ?, ?)",
            (user.id, user.username, user.first_name, datetime.utcnow().isoformat())
        )
        await db.execute(
            "INSERT INTO deposits (user_id, file_id, caption, created_at) VALUES (?, ?, ?, ?)",
            (user.id, photo.file_id, caption, datetime.utcnow().isoformat())
        )
        await db.commit()

    await message.answer("✅ Einzahlung gespeichert. Danke!")

    # Admins benachrichtigen
    admin_ids = await get_admin_ids()
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"Neue Einzahlung von @{user.username or user.first_name} (ID: {user.id})"
            )
            await bot.send_photo(admin_id, photo.file_id, caption=caption or None)
        except Exception as e:
            logger.warning(f"Konnte Admin {admin_id} nicht benachrichtigen: {e}")


# ---------- Admin-Befehle ----------
@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not await is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Benutzung: /addadmin 123456789")
        return

    new_admin_id = int(args[1].strip())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)",
            (new_admin_id, datetime.utcnow().isoformat())
        )
        await db.commit()

    await message.answer(f"✅ User {new_admin_id} wurde als Admin hinzugefügt.")


@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not await is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Benutzung: /removeadmin 123456789")
        return

    admin_id = int(args[1].strip())
    # Verhindern, dass man sich selbst entfernt, wenn man der letzte Admin ist
    current_admins = await get_admin_ids()
    if admin_id not in current_admins:
        await message.answer("Dieser User ist kein Admin.")
        return
    if len(current_admins) == 1 and admin_id == message.from_user.id:
        await message.answer("Du kannst dich nicht entfernen – du bist der letzte Admin.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (admin_id,))
        await db.commit()

    await message.answer(f"✅ User {admin_id} wurde als Admin entfernt.")


@dp.message(Command("admins"))
async def cmd_admins(message: Message):
    if not await is_admin(message.from_user.id):
        return

    admin_ids = await get_admin_ids()
    if not admin_ids:
        await message.answer("Keine Admins eingetragen.")
        return

    text = "Aktuelle Admins:\n"
    for aid in admin_ids:
        text += f"• {aid}\n"
    await message.answer(text)


@dp.message(Command("adduser"))
async def cmd_adduser(message: Message):
    if not await is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Benutzung: /adduser 123456789")
        return

    user_id = int(args[1].strip())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at) VALUES (?, ?, ?, ?)",
            (user_id, None, None, datetime.utcnow().isoformat())
        )
        await db.commit()

    await message.answer(f"✅ Mitglied {user_id} wurde hinzugefügt.\n\nHinweis: Der Bot kann ihm nur schreiben, wenn er den Bot schon einmal gestartet hat.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not await is_admin(message.from_user.id):
        return

    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Benutzung: /broadcast Deine Nachricht hier")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    sent = 0
    failed = 0
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(f"Rundmail gesendet: {sent} erfolgreich, {failed} fehlgeschlagen.")


@dp.message(Command("deposits"))
async def cmd_deposits(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT d.id, d.user_id, u.username, u.first_name, d.file_id, d.caption, d.created_at
            FROM deposits d
            LEFT JOIN users u ON d.user_id = u.user_id
            ORDER BY d.id DESC
            LIMIT 20
        """) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer("Noch keine Einzahlungen vorhanden.")
        return

    await message.answer(f"Letzte {len(rows)} Einzahlungen:")

    for row in rows:
        dep_id, user_id, username, first_name, file_id, caption, created_at = row
        name = f"@{username}" if username else first_name or str(user_id)
        text = (
            f"#{dep_id} | {name} (ID: {user_id})\n"
            f"Zeit: {created_at[:19]}\n"
            f"Text: {caption or '–'}"
        )
        try:
            await bot.send_photo(message.chat.id, file_id, caption=text)
            await asyncio.sleep(0.1)
        except Exception as e:
            await message.answer(f"#{dep_id}: Konnte Bild nicht senden ({e})")


@dp.message(Command("users"))
async def cmd_users(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            count = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT user_id, username, first_name FROM users ORDER BY registered_at DESC LIMIT 30"
        ) as cursor:
            users = await cursor.fetchall()

    text = f"Registrierte Mitglieder: {count}\n\nLetzte:\n"
    for uid, uname, fname in users:
        text += f"• {fname or ''} @{uname or '–'} ({uid})\n"

    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if await is_admin(message.from_user.id):
        await message.answer(
            "Admin-Befehle:\n"
            "/broadcast Text – Rundmail an alle\n"
            "/deposits – Letzte Einzahlungen anzeigen\n"
            "/users – Mitgliederliste\n"
            "/addadmin ID – Admin hinzufügen\n"
            "/removeadmin ID – Admin entfernen\n"
            "/admins – Admin-Liste\n"
            "/adduser ID – Mitglied hinzufügen\n"
            "/help – Diese Hilfe"
        )
    else:
        await message.answer(
            "Schick mir einfach ein Foto deiner Einzahlung.\n"
            "Mehr braucht du nicht zu tun."
        )


async def main():
    await init_db()
    logger.info("Bot startet...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
