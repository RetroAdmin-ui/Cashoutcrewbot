import asyncio
import logging
import os
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

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
        await db.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
        "Du bekommst hier später auch Rundmails der Admins."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    user = message.from_user
    photo = message.photo[-1]  # höchste Auflösung
    caption = message.caption or ""

    async with aiosqlite.connect(DB_PATH) as db:
        # User sicherstellen
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

    # Optional: Admins benachrichtigen
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"Neue Einzahlung von @{user.username or user.first_name} (ID: {user.id})"
            )
            await bot.send_photo(admin_id, photo.file_id, caption=caption or None)
        except Exception as e:
            logger.warning(f"Konnte Admin {admin_id} nicht benachrichtigen: {e}")


# ---------- Admin-Befehle ----------
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_admin(message.from_user.id):
        return

    # Text nach dem Befehl
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
            await asyncio.sleep(0.05)  # Rate-Limit schonen
        except Exception:
            failed += 1

    await message.answer(f"Rundmail gesendet: {sent} erfolgreich, {failed} fehlgeschlagen.")


@dp.message(Command("deposits"))
async def cmd_deposits(message: Message):
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if is_admin(message.from_user.id):
        await message.answer(
            "Admin-Befehle:\n"
            "/broadcast Text – Rundmail an alle\n"
            "/deposits – Letzte Einzahlungen anzeigen\n"
            "/users – Mitgliederliste\n"
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
