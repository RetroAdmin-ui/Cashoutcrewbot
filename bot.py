import asyncio
import logging
import os
from datetime import datetime

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Start-Admins aus der Umgebungsvariable (werden beim ersten Start in die DB geschrieben)
INITIAL_ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

MINI_APP_URL = "https://lingering-base-8871.maxwindani-a8b.workers.dev"

# Feste Tastatur unten
mini_app_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Mini-App öffnen", web_app=WebAppInfo(url=MINI_APP_URL))]
    ],
    resize_keyboard=True,
    is_persistent=True
)

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")

# Pending custom inputs: {admin_id: ("xp"|"tickets", deposit_id)}
pending_custom = {}
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
                xp INTEGER DEFAULT 0,
                tickets INTEGER DEFAULT 0,
                confirmed INTEGER DEFAULT 0,
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
        "Mit /mystats siehst du deine XP und Tickets.\n"
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
        async with db.execute("SELECT last_insert_rowid()") as cursor:
            deposit_id = (await cursor.fetchone())[0]

    await message.answer("✅ Einzahlung gespeichert. Danke!")

    # Admins benachrichtigen – nur eine Nachricht mit Bild + Buttons
    admin_ids = await get_admin_ids()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+10 XP", callback_data=f"xp:{deposit_id}:10"),
            InlineKeyboardButton(text="+25 XP", callback_data=f"xp:{deposit_id}:25"),
            InlineKeyboardButton(text="+50 XP", callback_data=f"xp:{deposit_id}:50"),
        ],
        [
            InlineKeyboardButton(text="+5 Tickets", callback_data=f"tickets:{deposit_id}:5"),
            InlineKeyboardButton(text="+10 Tickets", callback_data=f"tickets:{deposit_id}:10"),
            InlineKeyboardButton(text="+25 Tickets", callback_data=f"tickets:{deposit_id}:25"),
        ],
        [
            InlineKeyboardButton(text="✏️ XP manuell", callback_data=f"customxp:{deposit_id}"),
            InlineKeyboardButton(text="✏️ Tickets manuell", callback_data=f"customtickets:{deposit_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Bestätigen", callback_data=f"confirm:{deposit_id}"),
        ]
    ])

    for admin_id in admin_ids:
        try:
            await bot.send_photo(
                admin_id,
                photo.file_id,
                caption=f"Neue Einzahlung von @{user.username or user.first_name} (ID: {user.id})\n\n{caption or ''}",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.warning(f"Konnte Admin {admin_id} nicht benachrichtigen: {e}")


# ---------- Callbacks für XP / Tickets ----------
@dp.callback_query(F.data.startswith("customxp:"))
async def cb_custom_xp(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Nur Admins", show_alert=True)
        return
    dep_id = int(callback.data.split(":")[1])
    pending_custom[callback.from_user.id] = ("xp", dep_id)
    await callback.answer()
    await callback.message.answer("Bitte gib die gewünschte XP-Anzahl ein (nur Zahl):")


@dp.callback_query(F.data.startswith("customtickets:"))
async def cb_custom_tickets(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Nur Admins", show_alert=True)
        return
    dep_id = int(callback.data.split(":")[1])
    pending_custom[callback.from_user.id] = ("tickets", dep_id)
    await callback.answer()
    await callback.message.answer("Bitte gib die gewünschte Ticket-Anzahl ein (nur Zahl):")


@dp.message(F.text.regexp(r"^\d+$"))
async def handle_custom_input(message: Message):
    if message.from_user.id not in pending_custom:
        return
    if not await is_admin(message.from_user.id):
        return

    typ, dep_id = pending_custom.pop(message.from_user.id)
    amount = int(message.text)

    async with aiosqlite.connect(DB_PATH) as db:
        if typ == "xp":
            await db.execute("UPDATE deposits SET xp = xp + ? WHERE id = ?", (amount, dep_id))
            await db.commit()
            async with db.execute("SELECT xp FROM deposits WHERE id = ?", (dep_id,)) as c:
                total = (await c.fetchone())[0]
            await message.answer(f"✅ +{amount} XP hinzugefügt (gesamt: {total})")
        else:
            await db.execute("UPDATE deposits SET tickets = tickets + ? WHERE id = ?", (amount, dep_id))
            await db.commit()
            async with db.execute("SELECT tickets FROM deposits WHERE id = ?", (dep_id,)) as c:
                total = (await c.fetchone())[0]
            await message.answer(f"✅ +{amount} Tickets hinzugefügt (gesamt: {total})")


@dp.callback_query(F.data.startswith("xp:"))
async def cb_xp(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Nur Admins", show_alert=True)
        return
    _, dep_id, amount = callback.data.split(":")
    dep_id, amount = int(dep_id), int(amount)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deposits SET xp = xp + ? WHERE id = ?", (amount, dep_id))
        await db.commit()
        async with db.execute("SELECT xp FROM deposits WHERE id = ?", (dep_id,)) as c:
            xp = (await c.fetchone())[0]
    await callback.answer(f"+{amount} XP (gesamt: {xp})")
    # Optional: Caption aktualisieren
    try:
        await callback.message.edit_caption(
            caption=callback.message.caption + f"\n\nXP: {xp}",
            reply_markup=callback.message.reply_markup
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("tickets:"))
async def cb_tickets(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Nur Admins", show_alert=True)
        return
    _, dep_id, amount = callback.data.split(":")
    dep_id, amount = int(dep_id), int(amount)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deposits SET tickets = tickets + ? WHERE id = ?", (amount, dep_id))
        await db.commit()
        async with db.execute("SELECT tickets FROM deposits WHERE id = ?", (dep_id,)) as c:
            tickets = (await c.fetchone())[0]
    await callback.answer(f"+{amount} Tickets (gesamt: {tickets})")
    try:
        await callback.message.edit_caption(
            caption=callback.message.caption + f"\nTickets: {tickets}",
            reply_markup=callback.message.reply_markup
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Nur Admins", show_alert=True)
        return
    dep_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deposits SET confirmed = 1 WHERE id = ?", (dep_id,))
        await db.commit()
        async with db.execute("SELECT user_id, xp, tickets FROM deposits WHERE id = ?", (dep_id,)) as c:
            row = await c.fetchone()
    await callback.answer("✅ Bestätigt und gespeichert")
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ Bestätigt",
        reply_markup=None
    )
    # Hier später XP/Tickets dem User gutschreiben und Mini-App aktualisieren


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


@dp.message(Command("mystats"))
async def cmd_mystats(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COALESCE(SUM(xp), 0), COALESCE(SUM(tickets), 0)
            FROM deposits
            WHERE user_id = ? AND confirmed = 1
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            xp, tickets = row if row else (0, 0)
    await message.answer(
        f"Deine Werte:\n\n"
        f"⭐ XP: {xp}\n"
        f"🎫 Tickets: {tickets}"
    )


@dp.message(Command("setxp"))
async def cmd_setxp(message: Message):
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].lstrip("-").isdigit():
        await message.answer("Benutzung: /setxp USER_ID XP\nBeispiel: /setxp 123456789 500")
        return
    user_id = int(args[1])
    xp = int(args[2])
    async with aiosqlite.connect(DB_PATH) as db:
        # Alten manuellen Eintrag löschen und neuen setzen (einfache Variante)
        await db.execute("DELETE FROM deposits WHERE user_id = ? AND file_id = 'manual'", (user_id,))
        await db.execute(
            "INSERT INTO deposits (user_id, file_id, caption, created_at, xp, tickets, confirmed) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (user_id, "manual", "Manuell gesetzt", datetime.utcnow().isoformat(), xp)
        )
        await db.commit()
    await message.answer(f"✅ XP für User {user_id} auf {xp} gesetzt.")


@dp.message(Command("settickets"))
async def cmd_settickets(message: Message):
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].lstrip("-").isdigit():
        await message.answer("Benutzung: /settickets USER_ID TICKETS\nBeispiel: /settickets 123456789 40")
        return
    user_id = int(args[1])
    tickets = int(args[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM deposits WHERE user_id = ? AND file_id = 'manual'", (user_id,))
        await db.execute(
            "INSERT INTO deposits (user_id, file_id, caption, created_at, xp, tickets, confirmed) VALUES (?, ?, ?, ?, 0, ?, 1)",
            (user_id, "manual", "Manuell gesetzt", datetime.utcnow().isoformat(), tickets)
        )
        await db.commit()
    await message.answer(f"✅ Tickets für User {user_id} auf {tickets} gesetzt.")


@dp.message(Command("addxp"))
async def cmd_addxp(message: Message):
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Benutzung: /addxp USER_ID XP\nBeispiel: /addxp 123456789 150")
        return
    user_id = int(args[1])
    xp = int(args[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deposits (user_id, file_id, caption, created_at, xp, tickets, confirmed) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (user_id, "manual", "Manuell hinzugefügt", datetime.utcnow().isoformat(), xp)
        )
        await db.commit()
    await message.answer(f"✅ {xp} XP für User {user_id} hinzugefügt.")


@dp.message(Command("addtickets"))
async def cmd_addtickets(message: Message):
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Benutzung: /addtickets USER_ID TICKETS\nBeispiel: /addtickets 123456789 25")
        return
    user_id = int(args[1])
    tickets = int(args[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deposits (user_id, file_id, caption, created_at, xp, tickets, confirmed) VALUES (?, ?, ?, ?, 0, ?, 1)",
            (user_id, "manual", "Manuell hinzugefügt", datetime.utcnow().isoformat(), tickets)
        )
        await db.commit()
    await message.answer(f"✅ {tickets} Tickets für User {user_id} hinzugefügt.")


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
            "/addxp ID XP – XP hinzufügen\n"
            "/addtickets ID TICKETS – Tickets hinzufügen\n"
            "/setxp ID XP – XP fest setzen\n"
            "/settickets ID TICKETS – Tickets fest setzen\n"
            "/help – Diese Hilfe"
        )
    else:
        await message.answer(
            "Schick mir einfach ein Foto deiner Einzahlung.\n"
            "Mit /mystats siehst du deine XP und Tickets."
        )


async def get_leaderboard():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT u.user_id, u.username, u.first_name,
                   COALESCE(SUM(d.xp), 0) as total_xp,
                   COALESCE(SUM(d.tickets), 0) as total_tickets
            FROM users u
            LEFT JOIN deposits d ON u.user_id = d.user_id AND d.confirmed = 1
            GROUP BY u.user_id
            ORDER BY total_xp DESC, total_tickets DESC
            LIMIT 50
        """) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "user_id": r[0],
                    "username": r[1] or "",
                    "first_name": r[2] or "",
                    "xp": r[3],
                    "tickets": r[4]
                }
                for r in rows
            ]


async def api_my_stats(request):
    user_id = request.query.get("user_id")
    if not user_id or not user_id.isdigit():
        resp = web.json_response({"error": "user_id required"}, status=400)
    else:
        user_id = int(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT COALESCE(SUM(xp), 0), COALESCE(SUM(tickets), 0)
                FROM deposits
                WHERE user_id = ? AND confirmed = 1
            """, (user_id,)) as cursor:
                row = await cursor.fetchone()
                xp, tickets = row if row else (0, 0)
        resp = web.json_response({"xp": xp, "tickets": tickets})

    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


async def api_leaderboard(request):
    data = await get_leaderboard()
    resp = web.json_response(data)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


async def handle_options(request):
    resp = web.Response()
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


async def start_web_server():
    app = web.Application()
    app.router.add_get("/api/leaderboard", api_leaderboard)
    app.router.add_get("/api/mystats", api_my_stats)
    app.router.add_route("OPTIONS", "/api/mystats", handle_options)
    app.router.add_route("OPTIONS", "/api/leaderboard", handle_options)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("API-Server läuft")


async def main():
    await init_db()
    logger.info("Bot startet...")
    await asyncio.gather(
        dp.start_polling(bot),
        start_web_server()
    )


if __name__ == "__main__":
    asyncio.run(main())
