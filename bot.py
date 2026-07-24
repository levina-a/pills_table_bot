from __future__ import annotations

import asyncio
import calendar
import contextlib
import html
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv


load_dotenv()
DB_PATH = os.getenv("DATABASE_PATH", "pills.db")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
MAX_ACTIVE_MEDICINES = 20
MAX_NOTE_LENGTH = 200
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pills_bot")
router = Router()

MONTHS = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
ALL_WEEKDAYS = tuple(range(7))
DEFAULT_NOTIFICATION_TIMES = {
    1: ("11:00",),
    2: ("11:00", "23:00"),
    3: ("11:00", "17:00", "23:00"),
}


class AddMedicine(StatesGroup):
    name = State()
    duration = State()
    custom_duration = State()
    start_date = State()
    frequency = State()
    weekdays = State()
    note = State()
    confirm = State()


class EditMedicine(StatesGroup):
    menu = State()
    custom_duration = State()
    start_date = State()
    weekdays = State()
    note = State()


class NotificationSettings(StatesGroup):
    global_times = State()
    medicine_times = State()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
                intake_time TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intake_log (
                user_id INTEGER NOT NULL,
                schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                intake_date TEXT NOT NULL,
                taken_at TEXT NOT NULL,
                PRIMARY KEY (user_id, schedule_id, intake_date)
            );
            CREATE TABLE IF NOT EXISTS medicine_log (
                user_id INTEGER NOT NULL,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
                intake_date TEXT NOT NULL,
                taken_at TEXT NOT NULL,
                PRIMARY KEY (user_id, medicine_id, intake_date)
            );
            CREATE TABLE IF NOT EXISTS dose_log (
                user_id INTEGER NOT NULL,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
                intake_date TEXT NOT NULL,
                dose_number INTEGER NOT NULL,
                taken_at TEXT NOT NULL,
                PRIMARY KEY (user_id, medicine_id, intake_date, dose_number)
            );
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminder_log (
                user_id INTEGER NOT NULL,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
                reminder_date TEXT NOT NULL,
                dose_number INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (user_id, medicine_id, reminder_date, dose_number)
            );
            CREATE TABLE IF NOT EXISTS notification_settings (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                times_1 TEXT NOT NULL DEFAULT '11:00',
                times_2 TEXT NOT NULL DEFAULT '11:00,23:00',
                times_3 TEXT NOT NULL DEFAULT '11:00,17:00,23:00'
            );
            CREATE TABLE IF NOT EXISTS medicine_notification_times (
                medicine_id INTEGER PRIMARY KEY
                    REFERENCES medicines(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                times TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(medicines)")).fetchall()}
        if "start_date" not in columns:
            await db.execute("ALTER TABLE medicines ADD COLUMN start_date TEXT")
        if "end_date" not in columns:
            await db.execute("ALTER TABLE medicines ADD COLUMN end_date TEXT")
        if "frequency" not in columns:
            await db.execute("ALTER TABLE medicines ADD COLUMN frequency INTEGER NOT NULL DEFAULT 1")
        if "duration_kind" not in columns:
            await db.execute("ALTER TABLE medicines ADD COLUMN duration_kind TEXT")
        if "duration_amount" not in columns:
            await db.execute("ALTER TABLE medicines ADD COLUMN duration_amount INTEGER")
        if "note" not in columns:
            await db.execute("ALTER TABLE medicines ADD COLUMN note TEXT")
        if "weekdays" not in columns:
            await db.execute(
                "ALTER TABLE medicines "
                "ADD COLUMN weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6'"
            )
        reminder_columns = {
            row[1]
            for row in await (await db.execute(
                "PRAGMA table_info(reminder_log)"
            )).fetchall()
        }
        if "response_status" not in reminder_columns:
            await db.execute(
                "ALTER TABLE reminder_log ADD COLUMN response_status TEXT"
            )
        if "responded_at" not in reminder_columns:
            await db.execute(
                "ALTER TABLE reminder_log ADD COLUMN responded_at TEXT"
            )
        await db.execute(
            """UPDATE medicines
               SET duration_kind = CASE WHEN end_date IS NULL THEN 'forever' ELSE 'days' END,
                   duration_amount = CASE
                       WHEN end_date IS NULL THEN NULL
                       WHEN start_date IS NULL THEN 1
                       ELSE CAST(julianday(end_date) - julianday(start_date) + 1 AS INTEGER)
                   END
               WHERE duration_kind IS NULL"""
        )
        migrated = await (await db.execute(
            "SELECT 1 FROM app_meta WHERE key = 'dose_log_migrated'"
        )).fetchone()
        if not migrated:
            await db.execute(
                """INSERT OR IGNORE INTO medicine_log(user_id, medicine_id, intake_date, taken_at)
                   SELECT l.user_id, s.medicine_id, l.intake_date, MIN(l.taken_at)
                   FROM intake_log l JOIN schedules s ON s.id = l.schedule_id
                   GROUP BY l.user_id, s.medicine_id, l.intake_date"""
            )
            await db.execute(
                """INSERT OR IGNORE INTO dose_log(user_id, medicine_id, intake_date, dose_number, taken_at)
                   SELECT user_id, medicine_id, intake_date, 1, taken_at FROM medicine_log"""
            )
            await db.execute("INSERT INTO app_meta VALUES ('dose_log_migrated', '1')")
        await db.commit()
    logger.info("Database initialized: %s", DB_PATH)


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💊 Сегодня", callback_data="day:today")],
        [InlineKeyboardButton(text="📅 Календарь", callback_data="calendar:today")],
        [InlineKeyboardButton(text="➕ Добавить препарат", callback_data="medicine:add")],
        [InlineKeyboardButton(text="📋 Мои препараты", callback_data="medicine:list")],
        [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="notifications")],
    ])


async def day_data(user_id: int, iso_day: str) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        medicines = await (await db.execute(
            """
            SELECT m.id, m.name, m.frequency
            FROM medicines m
            WHERE m.user_id = ? AND m.active = 1
              AND (m.start_date IS NULL OR m.start_date <= ?)
              AND (m.end_date IS NULL OR m.end_date >= ?)
              AND instr(',' || m.weekdays || ',', ',' || ? || ',') > 0
            ORDER BY m.name
            """,
            (user_id, iso_day, iso_day, date.fromisoformat(iso_day).weekday()),
        )).fetchall()
        taken_rows = await (await db.execute(
            "SELECT medicine_id, dose_number FROM dose_log WHERE user_id = ? AND intake_date = ?",
            (user_id, iso_day),
        )).fetchall()
    taken = set(taken_rows)
    return [
        (medicine_id, name, dose_number, frequency, (medicine_id, dose_number) in taken)
        for medicine_id, name, frequency in medicines
        for dose_number in range(1, frequency + 1)
    ]


def serialize_times(values: tuple[str, ...] | list[str]) -> str:
    return ",".join(values)


def parse_times(value: str | None, frequency: int) -> tuple[str, ...]:
    if value:
        values = tuple(item.strip() for item in value.split(","))
        if len(values) == frequency and all(
            re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item)
            for item in values
        ):
            return values
    return DEFAULT_NOTIFICATION_TIMES[frequency]


def validate_times_input(raw: str, frequency: int) -> tuple[tuple[str, ...] | None, str | None]:
    value = raw.strip()
    if not value or (frequency > 1 and "," not in value):
        return None, (
            "Разделите время запятыми.\n\n"
            f"Например: {', '.join(DEFAULT_NOTIFICATION_TIMES[frequency])}"
        )
    parts = [item.strip() for item in value.split(",")]
    if any(not item for item in parts):
        return None, (
            "Разделите время запятыми без пустых значений.\n\n"
            f"Например: {', '.join(DEFAULT_NOTIFICATION_TIMES[frequency])}"
        )
    if len(parts) != frequency:
        return None, (
            f"Для схемы «{frequency} раз{'а' if frequency in (2, 3) else ''} в день» "
            f"нужно указать ровно {frequency} "
            f"{'время' if frequency == 1 else 'времени'}.\n\n"
            f"Например: {', '.join(DEFAULT_NOTIFICATION_TIMES[frequency])}"
        )
    if any(
        not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item)
        for item in parts
    ):
        return None, (
            "Введите время в формате ЧЧ:ММ.\n\n"
            f"Например: {', '.join(DEFAULT_NOTIFICATION_TIMES[frequency])}"
        )
    if len(set(parts)) != len(parts):
        return None, "Время приёмов не должно повторяться."
    return tuple(sorted(parts)), None


async def ensure_notification_settings(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO notification_settings(user_id) VALUES (?)",
        (user_id,),
    )


async def send_due_reminders(bot: Bot, now: datetime | None = None) -> int:
    current = now or datetime.now(TIMEZONE)
    iso_day = current.date().isoformat()
    current_hhmm = current.strftime("%H:%M")
    reminder_groups: dict[
        tuple[int, str], dict[tuple[int, int], tuple[int, str, int, int]]
    ] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            """
            SELECT m.id, m.user_id, m.name, m.frequency,
                   COALESCE(
                       mt.times,
                       CASE m.frequency
                           WHEN 1 THEN ns.times_1
                           WHEN 2 THEN ns.times_2
                           WHEN 3 THEN ns.times_3
                       END
                   ) AS effective_times
            FROM medicines m
            LEFT JOIN notification_settings ns ON ns.user_id = m.user_id
            LEFT JOIN medicine_notification_times mt
                ON mt.medicine_id = m.id AND mt.user_id = m.user_id
            WHERE m.active = 1
              AND COALESCE(ns.enabled, 1) = 1
              AND instr(',' || m.weekdays || ',', ',' || ? || ',') > 0
              AND (m.start_date IS NULL OR m.start_date <= ?)
              AND (m.end_date IS NULL OR m.end_date >= ?)
            """,
            (current.weekday(), iso_day, iso_day),
        )).fetchall()
        sent_rows = await (await db.execute(
            """SELECT user_id, medicine_id, dose_number
               FROM reminder_log WHERE reminder_date = ?""",
            (iso_day,),
        )).fetchall()
        taken_rows = await (await db.execute(
            """SELECT user_id, medicine_id, dose_number
               FROM dose_log WHERE intake_date = ?""",
            (iso_day,),
        )).fetchall()
    already_sent = set(sent_rows)
    already_taken = set(taken_rows)

    for medicine_id, user_id, name, frequency, raw_times in rows:
        times = parse_times(raw_times, frequency)
        due = [
            (dose_number, scheduled_time)
            for dose_number, scheduled_time in enumerate(times, start=1)
            if scheduled_time <= current_hhmm
            and (user_id, medicine_id, dose_number) not in already_sent
            and (user_id, medicine_id, dose_number) not in already_taken
        ]
        if not due:
            continue
        # Если бот ненадолго перезапускался, отправляем последний актуальный
        # приём, а не несколько отдельных старых уведомлений одновременно.
        dose_number, scheduled_time = due[-1]
        reminder_groups.setdefault((user_id, scheduled_time), {})[
            (medicine_id, dose_number)
        ] = (medicine_id, name, dose_number, frequency)

    # Один пользователь получает одну актуальную группу за проход планировщика.
    # Это защищает от нескольких сообщений сразу после перезапуска бота.
    latest_by_user: dict[
        int, tuple[str, dict[tuple[int, int], tuple[int, str, int, int]]]
    ] = {}
    for (user_id, scheduled_time), reminder_map in reminder_groups.items():
        previous = latest_by_user.get(user_id)
        if previous is None:
            latest_by_user[user_id] = (scheduled_time, dict(reminder_map))
            continue
        latest_time = max(previous[0], scheduled_time)
        previous[1].update(reminder_map)
        latest_by_user[user_id] = (latest_time, previous[1])
    reminder_groups = {
        (user_id, scheduled_time): reminder_map
        for user_id, (scheduled_time, reminder_map) in latest_by_user.items()
    }

    sent = 0
    for (user_id, scheduled_time), reminder_map in sorted(reminder_groups.items()):
        async with aiosqlite.connect(DB_PATH) as db:
            unanswered = await (await db.execute(
                """
                SELECT m.id, m.name, r.dose_number, m.frequency
                FROM reminder_log r
                JOIN medicines m ON m.id = r.medicine_id
                    AND m.user_id = r.user_id
                LEFT JOIN dose_log d ON d.medicine_id = r.medicine_id
                    AND d.user_id = r.user_id
                    AND d.intake_date = r.reminder_date
                    AND d.dose_number = r.dose_number
                WHERE r.user_id = ?
                  AND r.reminder_date = ?
                  AND r.response_status IS NULL
                  AND d.medicine_id IS NULL
                  AND m.active = 1
                  AND instr(',' || m.weekdays || ',', ',' || ? || ',') > 0
                  AND (m.start_date IS NULL OR m.start_date <= ?)
                  AND (m.end_date IS NULL OR m.end_date >= ?)
                """,
                (user_id, iso_day, current.weekday(), iso_day, iso_day),
            )).fetchall()
        for medicine_id, name, dose_number, frequency in unanswered:
            reminder_map.setdefault(
                (medicine_id, dose_number),
                (medicine_id, name, dose_number, frequency),
            )
        reminders = sorted(
            reminder_map.values(),
            key=lambda item: (item[1].casefold(), item[2]),
        )
        medicine_names = ", ".join(
            html.escape(name)
            for name in dict.fromkeys(item[1] for item in reminders)
        )
        keyboard_rows = []
        for medicine_id, name, dose_number, frequency in reminders:
            keyboard_rows.append([
                InlineKeyboardButton(
                    text=f"✅ {name} — принято {dose_number} из {frequency}",
                    callback_data=f"remtaken:{medicine_id}:{dose_number}:{iso_day}",
                ),
                InlineKeyboardButton(
                    text="⏳ Ещё нет",
                    callback_data=f"remlater:{medicine_id}:{dose_number}:{iso_day}",
                ),
            ])
        try:
            await bot.send_message(
                user_id,
                f"💊 <b>Приём препаратов {medicine_names} · {scheduled_time}</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
            )
        except Exception:
            logger.exception(
                "Failed to send reminder group: time=%s, medicines=%d",
                scheduled_time,
                len(reminders),
            )
            continue
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """INSERT OR IGNORE INTO reminder_log(
                       user_id, medicine_id, reminder_date, dose_number, sent_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        user_id,
                        medicine_id,
                        iso_day,
                        dose_number,
                        current.isoformat(),
                    )
                    for medicine_id, _, dose_number, _ in reminders
                ],
            )
            await db.commit()
        sent += len(reminders)
        logger.info(
            "Reminder group sent: time=%s, medicines=%d",
            scheduled_time,
            len(reminders),
        )
    return sent


async def reminder_loop(bot: Bot) -> None:
    logger.info("Reminder scheduler started")
    while True:
        try:
            await send_due_reminders(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder scheduler iteration failed")
        await asyncio.sleep(30)


async def render_day(
    target: Message | CallbackQuery,
    user_id: int,
    selected: date,
    origin: str = "calendar",
) -> None:
    rows = await day_data(user_id, selected.isoformat())
    title = f"💊 Приёмы на {selected.strftime('%d.%m.%Y')}"
    buttons = []
    if rows:
        medicines: dict[int, dict] = {}
        for medicine_id, name, dose_number, frequency, taken in rows:
            medicine = medicines.setdefault(
                medicine_id,
                {"name": name, "frequency": frequency, "doses": []},
            )
            medicine["doses"].append((dose_number, taken))
        total = len(rows)
        completed = sum(taken for *_, taken in rows)
        text = (
            title
            + f"\n\nВыполнено: <b>{completed} из {total}</b>"
            + "\nНажмите на приём, чтобы изменить отметку."
        )
        for medicine_id, medicine in medicines.items():
            doses = medicine["doses"]
            medicine_completed = sum(taken for _, taken in doses)
            status = "✅" if medicine_completed == medicine["frequency"] else "🟡" if medicine_completed else "○"
            header_callback = "noop"
            if medicine["frequency"] == 1:
                dose_number, _ = doses[0]
                header_callback = f"toggle:{medicine_id}:{dose_number}:{selected.isoformat()}:{origin}"
            buttons.append([InlineKeyboardButton(
                text=f"{status} 💊 {medicine['name']} · {medicine_completed}/{medicine['frequency']}",
                callback_data=header_callback,
            )])
            if medicine["frequency"] > 1:
                dose_buttons = [
                    InlineKeyboardButton(
                        text=f"{'✅' if taken else '○'} Приём {dose_number}",
                        callback_data=f"toggle:{medicine_id}:{dose_number}:{selected.isoformat()}:{origin}",
                    )
                    for dose_number, taken in doses
                ]
                buttons.append(dose_buttons)
    else:
        text = title + "\n\nНа этот день активных препаратов пока нет."
    if origin == "menu":
        back_text = "← Меню"
        back_callback = "menu"
    else:
        back_text = "← Назад в календарь"
        back_callback = f"calendar:{selected.isoformat()}"
    buttons.append([InlineKeyboardButton(text=back_text, callback_data=back_callback)])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def month_statuses(user_id: int, year: int, month: int) -> dict[str, tuple[int, int]]:
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        medicines = await (await db.execute(
            """SELECT id, start_date, end_date, frequency, weekdays FROM medicines
               WHERE user_id = ? AND active = 1
                 AND (start_date IS NULL OR start_date <= ?)
                 AND (end_date IS NULL OR end_date >= ?)""",
            (user_id, last_day.isoformat(), first_day.isoformat()),
        )).fetchall()
        logs = await (await db.execute(
            """SELECT medicine_id, intake_date, dose_number FROM dose_log
               WHERE user_id = ? AND intake_date BETWEEN ? AND ?""",
            (user_id, first_day.isoformat(), last_day.isoformat()),
        )).fetchall()
    taken = {(medicine_id, intake_date, dose_number) for medicine_id, intake_date, dose_number in logs}
    statuses = {}
    for day_number in range(1, last_day.day + 1):
        current = date(year, month, day_number)
        iso_day = current.isoformat()
        active = [
            (medicine_id, frequency)
            for medicine_id, start_raw, end_raw, frequency, weekdays in medicines
            if (not start_raw or start_raw <= iso_day)
            and (not end_raw or end_raw >= iso_day)
            and current.weekday() in parse_weekdays(weekdays)
        ]
        total = sum(frequency for _, frequency in active)
        if total:
            completed = sum(
                (medicine_id, iso_day, dose_number) in taken
                for medicine_id, frequency in active
                for dose_number in range(1, frequency + 1)
            )
            statuses[iso_day] = (total, completed)
    return statuses


async def calendar_markup(user_id: int, year: int, month: int) -> InlineKeyboardMarkup:
    statuses = await month_statuses(user_id, year, month)
    rows = [[InlineKeyboardButton(text=f"{MONTHS[month]} {year}", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text=x, callback_data="noop") for x in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")])
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        buttons = []
        for day in week:
            if not day:
                buttons.append(InlineKeyboardButton(text=" ", callback_data="noop"))
                continue
            iso_day = f"{year:04d}-{month:02d}-{day:02d}"
            total, completed = statuses.get(iso_day, (0, 0))
            marker = "✅" if total and completed == total else "🟡" if completed else "○" if total else ""
            buttons.append(InlineKeyboardButton(text=f"{marker}{day}", callback_data=f"day:{iso_day}"))
        rows.append(buttons)
    previous = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    rows.append([
        InlineKeyboardButton(text="‹", callback_data=f"calendar:{previous.isoformat()}"),
        InlineKeyboardButton(text="Сегодня", callback_data="day:today"),
        InlineKeyboardButton(text="›", callback_data=f"calendar:{following.isoformat()}"),
    ])
    rows.append([InlineKeyboardButton(text="← Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Привет! Я помогу отмечать приём препаратов по датам.\n\n"
        "Сначала добавьте препарат, срок курса и частоту приёма.",
        reply_markup=menu(),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Добавление отменено.", reply_markup=menu())


@router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Выберите действие", reply_markup=menu())
    await callback.answer()


async def render_notification_settings(
    callback: CallbackQuery,
    state: FSMContext | None = None,
) -> None:
    if state:
        await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_notification_settings(db, callback.from_user.id)
        settings = await (await db.execute(
            """SELECT enabled, times_1, times_2, times_3
               FROM notification_settings WHERE user_id = ?""",
            (callback.from_user.id,),
        )).fetchone()
        await db.commit()
    enabled, times_1, times_2, times_3 = settings
    status = "✅ включены" if enabled else "❌ выключены"
    toggle_text = "🔕 Выключить уведомления" if enabled else "🔔 Включить уведомления"
    text = (
        "🔔 <b>Настройки уведомлений</b>\n\n"
        f"Статус: {status}\n\n"
        "Общее расписание:\n"
        f"• 1 раз в день — {', '.join(parse_times(times_1, 1))}\n"
        f"• 2 раза в день — {', '.join(parse_times(times_2, 2))}\n"
        f"• 3 раза в день — {', '.join(parse_times(times_3, 3))}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="notifications:toggle")],
        [InlineKeyboardButton(
            text="Настроить другое время для всех",
            callback_data="notifications:global",
        )],
        [InlineKeyboardButton(
            text="Настроить для препарата",
            callback_data="notifications:medicine",
        )],
        [InlineKeyboardButton(text="← Меню", callback_data="menu")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "notifications")
async def notification_settings(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await render_notification_settings(callback, state)


@router.callback_query(F.data == "notifications:toggle")
async def toggle_notifications(callback: CallbackQuery) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_notification_settings(db, callback.from_user.id)
        await db.execute(
            """UPDATE notification_settings
               SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END
               WHERE user_id = ?""",
            (callback.from_user.id,),
        )
        await db.commit()
    await render_notification_settings(callback)


@router.callback_query(F.data == "notifications:global")
async def choose_global_notification_frequency(callback: CallbackQuery) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 раз в день", callback_data="notifyglobal:1")],
        [InlineKeyboardButton(text="2 раза в день", callback_data="notifyglobal:2")],
        [InlineKeyboardButton(text="3 раза в день", callback_data="notifyglobal:3")],
        [InlineKeyboardButton(text="← Назад", callback_data="notifications")],
    ])
    await callback.message.edit_text(
        "<b>Для какой частоты изменить общее время?</b>",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("notifyglobal:"))
async def ask_global_notification_times(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    frequency = int(callback.data.split(":", 1)[1])
    if frequency not in (1, 2, 3):
        await callback.answer("Некорректная частота", show_alert=True)
        return
    await state.set_state(NotificationSettings.global_times)
    await state.update_data(notification_frequency=frequency)
    example = ", ".join(DEFAULT_NOTIFICATION_TIMES[frequency])
    await callback.message.edit_text(
        f"<b>Введите {frequency} "
        f"{'время' if frequency == 1 else 'времени'} через запятую.</b>\n\n"
        f"Например: <code>{example}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="notifications")
        ]]),
    )
    await callback.answer()


@router.message(NotificationSettings.global_times)
async def save_global_notification_times(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    frequency = data["notification_frequency"]
    values, error = validate_times_input(message.text or "", frequency)
    if error:
        await message.answer(
            error,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="notifications")
            ]]),
        )
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_notification_settings(db, message.from_user.id)
        await db.execute(
            f"UPDATE notification_settings SET times_{frequency} = ? WHERE user_id = ?",
            (serialize_times(values), message.from_user.id),
        )
        await db.commit()
    await state.clear()
    await message.answer(
        "Общее время сохранено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← К настройкам", callback_data="notifications")
        ]]),
    )


@router.callback_query(F.data == "notifications:medicine")
async def choose_notification_medicine(callback: CallbackQuery) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        medicines = await (await db.execute(
            """SELECT id, name FROM medicines
               WHERE user_id = ? AND active = 1 ORDER BY name""",
            (callback.from_user.id,),
        )).fetchall()
    rows = [
        [InlineKeyboardButton(text=f"💊 {name}", callback_data=f"notifymed:{medicine_id}")]
        for medicine_id, name in medicines
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="notifications")])
    await callback.message.edit_text(
        "<b>Выберите препарат:</b>\n\n"
        + ("" if medicines else "Активных препаратов пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def notification_medicine_data(user_id: int, medicine_id: int) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_notification_settings(db, user_id)
        row = await (await db.execute(
            """
            SELECT m.name, m.frequency, mt.times,
                   CASE m.frequency
                       WHEN 1 THEN ns.times_1
                       WHEN 2 THEN ns.times_2
                       WHEN 3 THEN ns.times_3
                   END
            FROM medicines m
            JOIN notification_settings ns ON ns.user_id = m.user_id
            LEFT JOIN medicine_notification_times mt
                ON mt.medicine_id = m.id AND mt.user_id = m.user_id
            WHERE m.id = ? AND m.user_id = ? AND m.active = 1
            """,
            (medicine_id, user_id),
        )).fetchone()
        await db.commit()
    return row


async def render_notification_medicine(
    callback: CallbackQuery,
    medicine_id: int,
) -> None:
    medicine = await notification_medicine_data(callback.from_user.id, medicine_id)
    if not medicine:
        await callback.answer("Препарат не найден", show_alert=True)
        return
    name, frequency, custom_times, global_times = medicine
    effective = parse_times(custom_times or global_times, frequency)
    source = "индивидуальное" if custom_times else "общее"
    rows = [[InlineKeyboardButton(
        text="Изменить время",
        callback_data=f"notifymededit:{medicine_id}",
    )]]
    if custom_times:
        rows.append([InlineKeyboardButton(
            text="Использовать общее расписание",
            callback_data=f"notifymedreset:{medicine_id}",
        )])
    rows.append([InlineKeyboardButton(
        text="← К препаратам",
        callback_data="notifications:medicine",
    )])
    await callback.message.edit_text(
        f"💊 <b>{html.escape(name)}</b>\n\n"
        f"Частота: {frequency} раз(а) в день\n"
        f"Время: {', '.join(effective)}\n"
        f"Расписание: {source}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("notifymed:"))
async def show_notification_medicine(callback: CallbackQuery) -> None:
    medicine_id = int(callback.data.split(":", 1)[1])
    await render_notification_medicine(callback, medicine_id)


@router.callback_query(F.data.startswith("notifymededit:"))
async def ask_medicine_notification_times(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    medicine_id = int(callback.data.split(":", 1)[1])
    medicine = await notification_medicine_data(callback.from_user.id, medicine_id)
    if not medicine:
        await callback.answer("Препарат не найден", show_alert=True)
        return
    name, frequency, _, _ = medicine
    await state.set_state(NotificationSettings.medicine_times)
    await state.update_data(
        notification_medicine_id=medicine_id,
        notification_frequency=frequency,
    )
    example = ", ".join(DEFAULT_NOTIFICATION_TIMES[frequency])
    await callback.message.edit_text(
        f"💊 <b>{html.escape(name)}</b>\n\n"
        f"Введите {frequency} "
        f"{'время' if frequency == 1 else 'времени'} через запятую.\n\n"
        f"Например: <code>{example}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=f"notifymed:{medicine_id}",
            )
        ]]),
    )
    await callback.answer()


@router.message(NotificationSettings.medicine_times)
async def save_medicine_notification_times(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    frequency = data["notification_frequency"]
    medicine_id = data["notification_medicine_id"]
    values, error = validate_times_input(message.text or "", frequency)
    if error:
        await message.answer(
            error,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"notifymed:{medicine_id}",
                )
            ]]),
        )
        return
    medicine = await notification_medicine_data(message.from_user.id, medicine_id)
    if not medicine or medicine[1] != frequency:
        await state.clear()
        await message.answer(
            "Частота препарата изменилась. Настройте время заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← К настройкам", callback_data="notifications")
            ]]),
        )
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO medicine_notification_times(medicine_id, user_id, times)
               VALUES (?, ?, ?)
               ON CONFLICT(medicine_id) DO UPDATE SET
                   user_id=excluded.user_id, times=excluded.times""",
            (medicine_id, message.from_user.id, serialize_times(values)),
        )
        await db.commit()
    await state.clear()
    await message.answer(
        "Индивидуальное время сохранено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="← К препарату",
                callback_data=f"notifymed:{medicine_id}",
            )
        ]]),
    )


@router.callback_query(F.data.startswith("notifymedreset:"))
async def reset_medicine_notification_times(callback: CallbackQuery) -> None:
    medicine_id = int(callback.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """DELETE FROM medicine_notification_times
               WHERE medicine_id = ? AND user_id = ?""",
            (medicine_id, callback.from_user.id),
        )
        await db.commit()
    await render_notification_medicine(callback, medicine_id)


@router.callback_query(F.data == "medicine:add")
async def add_medicine(callback: CallbackQuery, state: FSMContext) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        active_count = await (await db.execute(
            "SELECT COUNT(*) FROM medicines WHERE user_id = ? AND active = 1",
            (callback.from_user.id,),
        )).fetchone()
    if active_count[0] >= MAX_ACTIVE_MEDICINES:
        await callback.answer(
            f"Достигнут лимит в {MAX_ACTIVE_MEDICINES} препаратов. "
            "Удалите один из них, чтобы добавить новый.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.set_state(AddMedicine.name)
    await callback.message.edit_text(
        "<b>Шаг 1 из 7. Название</b>\n\nВведите название препарата.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]
    ])


def serialize_weekdays(days: list[int] | tuple[int, ...] | set[int]) -> str:
    return ",".join(str(day) for day in sorted(days))


def parse_weekdays(value: str | None) -> tuple[int, ...]:
    if not value:
        return ALL_WEEKDAYS
    return tuple(int(day) for day in value.split(","))


def weekdays_text(days: list[int] | tuple[int, ...] | set[int]) -> str:
    ordered = tuple(sorted(days))
    if ordered == ALL_WEEKDAYS:
        return "Каждый день"
    return ", ".join(WEEKDAY_LABELS[day] for day in ordered)


def weekdays_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Каждый день", callback_data="weekdays:all")],
        [InlineKeyboardButton(text="Выбрать дни", callback_data="weekdays:custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])


def custom_weekdays_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    day_buttons = [
        InlineKeyboardButton(
            text=f"{'✅ ' if day in selected else ''}{label}",
            callback_data=f"weekday:{day}",
        )
        for day, label in enumerate(WEEKDAY_LABELS)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        day_buttons[:4],
        day_buttons[4:],
        [InlineKeyboardButton(text="✅ Готово", callback_data="weekdays:done")],
        [InlineKeyboardButton(text="← Назад", callback_data="weekdays:back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])


def course_dates(start_date: date, kind: str, amount: int | None = None) -> tuple[date | None, str]:
    if kind == "forever":
        return None, "бессрочно"
    if kind == "days":
        end_date = start_date + timedelta(days=(amount or 1) - 1)
        return end_date, f"{amount} дн."
    month_index = start_date.month - 1 + (amount or 1)
    year = start_date.year + month_index // 12
    month = month_index % 12 + 1
    next_date = date(year, month, min(start_date.day, calendar.monthrange(year, month)[1]))
    return next_date - timedelta(days=1), f"{amount} мес."


def duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней", callback_data="duration:days:7")],
        [InlineKeyboardButton(text="1 месяц", callback_data="duration:months:1")],
        [InlineKeyboardButton(text="3 месяца", callback_data="duration:months:3")],
        [InlineKeyboardButton(text="Бессрочно", callback_data="duration:forever")],
        [InlineKeyboardButton(text="Другой срок", callback_data="duration:custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])


async def ask_start_date(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddMedicine.start_date)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать сегодня", callback_data="startdate:today")],
        [InlineKeyboardButton(text="Выбрать дату", callback_data="startdate:choose")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])
    text = "<b>Шаг 3 из 7. Дата начала</b>\n\nКогда начался или начнётся курс?"
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(AddMedicine.duration, F.data.startswith("duration:"))
async def choose_duration(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if parts[1] == "custom":
        await state.set_state(AddMedicine.custom_duration)
        await callback.message.edit_text(
            "<b>Шаг 2 из 7. Другой срок</b>\n\nВведите количество дней курса, например: <code>14</code>",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()
        return
    kind = parts[1]
    amount = int(parts[2]) if len(parts) == 3 else None
    label = "бессрочно" if kind == "forever" else f"{amount} {'дн.' if kind == 'days' else 'мес.'}"
    await state.update_data(duration_kind=kind, duration_amount=amount, duration_label=label)
    await ask_start_date(callback, state)


@router.message(AddMedicine.custom_duration)
async def custom_duration(message: Message, state: FSMContext) -> None:
    try:
        days = int((message.text or "").strip())
    except ValueError:
        days = 0
    if not 1 <= days <= 3650:
        await message.answer("Введите число от 1 до 3650.", reply_markup=cancel_keyboard())
        return
    await state.update_data(duration_kind="days", duration_amount=days, duration_label=f"{days} дн.")
    await ask_start_date(message, state)


@router.message(AddMedicine.name)
async def medicine_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer("Введите название длиной до 80 символов.", reply_markup=cancel_keyboard())
        return
    await state.update_data(name=name)
    await state.set_state(AddMedicine.duration)
    await message.answer(
        "<b>Шаг 2 из 7. Длительность курса</b>\n\nКак долго нужно принимать препарат?",
        reply_markup=duration_keyboard(),
    )


async def save_start_date(target: CallbackQuery, state: FSMContext, selected: date) -> None:
    data = await state.get_data()
    end_date, duration_label = course_dates(selected, data["duration_kind"], data["duration_amount"])
    await state.update_data(
        start_date=selected.isoformat(),
        end_date=end_date.isoformat() if end_date else None,
        duration_label=duration_label,
    )
    await state.set_state(AddMedicine.frequency)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 раз в день", callback_data="frequency:1")],
        [InlineKeyboardButton(text="2 раза в день", callback_data="frequency:2")],
        [InlineKeyboardButton(text="3 раза в день", callback_data="frequency:3")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])
    await target.message.edit_text(
        "<b>Шаг 4 из 7. Частота приёма</b>\n\nСколько раз в день нужно принимать препарат?",
        reply_markup=keyboard,
    )
    await target.answer()


@router.callback_query(AddMedicine.frequency, F.data.startswith("frequency:"))
async def choose_frequency(callback: CallbackQuery, state: FSMContext) -> None:
    frequency = int(callback.data.split(":", 1)[1])
    if frequency not in (1, 2, 3):
        await callback.answer("Некорректная частота", show_alert=True)
        return
    await state.update_data(frequency=frequency)
    await state.set_state(AddMedicine.weekdays)
    await callback.message.edit_text(
        "<b>Шаг 5 из 7. В какие дни принимать препарат?</b>",
        reply_markup=weekdays_choice_keyboard(),
    )
    await callback.answer()


async def ask_note(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddMedicine.note)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Пропустить", callback_data="note:skip"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu"),
    ]])
    await callback.message.edit_text(
        "<b>Шаг 6 из 7. Добавьте заметку о приёме препарата.</b>\n\n"
        "Например: 2 таблетки после еды",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(AddMedicine.weekdays, F.data == "weekdays:all")
async def choose_every_day(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(weekdays=list(ALL_WEEKDAYS))
    await ask_note(callback, state)


@router.callback_query(AddMedicine.weekdays, F.data == "weekdays:custom")
async def choose_custom_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = set(data.get("weekdays", []))
    await state.update_data(weekdays=list(selected))
    await callback.message.edit_text(
        "<b>Шаг 5 из 7. Выберите дни приёма:</b>",
        reply_markup=custom_weekdays_keyboard(selected),
    )
    await callback.answer()


@router.callback_query(AddMedicine.weekdays, F.data.startswith("weekday:"))
async def toggle_weekday(callback: CallbackQuery, state: FSMContext) -> None:
    day = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("weekdays", []))
    if day in selected:
        selected.remove(day)
    else:
        selected.add(day)
    await state.update_data(weekdays=list(selected))
    await callback.message.edit_reply_markup(
        reply_markup=custom_weekdays_keyboard(selected)
    )
    await callback.answer()


@router.callback_query(AddMedicine.weekdays, F.data == "weekdays:done")
async def confirm_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("weekdays"):
        await callback.answer("Выберите хотя бы один день.", show_alert=True)
        return
    await ask_note(callback, state)


@router.callback_query(AddMedicine.weekdays, F.data == "weekdays:back")
async def back_to_weekdays_choice(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "<b>Шаг 5 из 7. В какие дни принимать препарат?</b>",
        reply_markup=weekdays_choice_keyboard(),
    )
    await callback.answer()


async def show_add_confirmation(
    target: Message | CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.set_state(AddMedicine.confirm)
    end_text = date.fromisoformat(data["end_date"]).strftime("%d.%m.%Y") if data["end_date"] else "не ограничен"
    note = data.get("note")
    note_text = f"\nЗаметка: {html.escape(note)}" if note else ""
    text = (
        "<b>Шаг 7 из 7. Проверка</b>\n\n"
        f"Препарат: <b>{html.escape(data['name'])}</b>\n"
        f"Курс: {data['duration_label']}\n"
        f"Начало: {date.fromisoformat(data['start_date']).strftime('%d.%m.%Y')}\n"
        f"Последний день приёма: {end_text}\n"
        f"Частота: {data['frequency']} раз(а) в день\n"
        f"Дни: {weekdays_text(data['weekdays'])}"
        f"{note_text}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="medicine:save")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(AddMedicine.note, F.data == "note:skip")
async def skip_note(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(note=None)
    await show_add_confirmation(callback, state)


@router.message(AddMedicine.note)
async def add_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    if not note:
        await message.answer("Введите текст заметки или нажмите «Пропустить».")
        return
    if len(note) > MAX_NOTE_LENGTH:
        await message.answer(
            f"Заметка должна быть не длиннее {MAX_NOTE_LENGTH} символов."
        )
        return
    await state.update_data(note=note)
    await show_add_confirmation(message, state)


def start_calendar_markup(year: int, month: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{MONTHS[month]} {year}", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text=x, callback_data="noop") for x in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")])
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        rows.append([
            InlineKeyboardButton(
                text=str(day) if day else " ",
                callback_data=f"startpick:{year:04d}-{month:02d}-{day:02d}" if day else "noop",
            )
            for day in week
        ])
    previous = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    rows.append([
        InlineKeyboardButton(text="‹", callback_data=f"startcal:{previous.isoformat()}"),
        InlineKeyboardButton(text="Сегодня", callback_data="startdate:today"),
        InlineKeyboardButton(text="›", callback_data=f"startcal:{following.isoformat()}"),
    ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AddMedicine.start_date, F.data == "startdate:today")
async def start_today(callback: CallbackQuery, state: FSMContext) -> None:
    await save_start_date(callback, state, datetime.now(TIMEZONE).date())


@router.callback_query(AddMedicine.start_date, F.data == "startdate:choose")
async def choose_start_date(callback: CallbackQuery) -> None:
    today = datetime.now(TIMEZONE).date()
    await callback.message.edit_text(
        "<b>Шаг 3 из 7. Дата начала</b>\n\nВыберите день начала курса:",
        reply_markup=start_calendar_markup(today.year, today.month),
    )
    await callback.answer()


@router.callback_query(AddMedicine.start_date, F.data.startswith("startcal:"))
async def navigate_start_calendar(callback: CallbackQuery) -> None:
    selected = date.fromisoformat(callback.data.split(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=start_calendar_markup(selected.year, selected.month))
    await callback.answer()


@router.callback_query(AddMedicine.start_date, F.data.startswith("startpick:"))
async def pick_start_date(callback: CallbackQuery, state: FSMContext) -> None:
    selected = date.fromisoformat(callback.data.split(":", 1)[1])
    await save_start_date(callback, state, selected)


@router.callback_query(AddMedicine.confirm, F.data == "medicine:save")
async def save_medicine(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        active_count = await (await db.execute(
            "SELECT COUNT(*) FROM medicines WHERE user_id = ? AND active = 1",
            (callback.from_user.id,),
        )).fetchone()
        if active_count[0] >= MAX_ACTIVE_MEDICINES:
            await state.clear()
            await callback.message.edit_text(
                f"Достигнут лимит в {MAX_ACTIVE_MEDICINES} препаратов. "
                "Удалите один из них, чтобы добавить новый.",
                reply_markup=menu(),
            )
            await callback.answer()
            return
        await db.execute(
            """INSERT INTO medicines(
                   user_id, name, start_date, end_date, frequency,
                   duration_kind, duration_amount, note, weekdays
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                callback.from_user.id, data["name"], data["start_date"], data["end_date"],
                data["frequency"], data["duration_kind"], data["duration_amount"], data.get("note"),
                serialize_weekdays(data["weekdays"]),
            ),
        )
        await db.commit()
    await state.clear()
    await callback.message.edit_text(
        f"Готово: <b>{html.escape(data['name'])}</b> добавлен в календарь.",
        reply_markup=menu(),
    )
    await callback.answer("Препарат сохранён")


@router.callback_query(F.data == "medicine:list")
async def list_medicines(callback: CallbackQuery) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            """
            SELECT m.id, m.name, m.start_date, m.end_date, m.frequency, m.note, m.weekdays
            FROM medicines m
            WHERE m.user_id = ? AND m.active = 1
            ORDER BY m.name
            """,
            (callback.from_user.id,),
        )).fetchall()
    descriptions = []
    for _, name, start_date, end_date, frequency, note, weekdays in rows:
        course = "бессрочно" if not end_date else f"до {date.fromisoformat(end_date).strftime('%d.%m.%Y')}"
        description = (
            f"• <b>{html.escape(name)}</b>: {frequency} раз(а) в день; "
            f"{weekdays_text(parse_weekdays(weekdays))}; {course}"
        )
        if note:
            preview = note if len(note) <= 80 else note[:77] + "..."
            description += f"\n  📝 {html.escape(preview)}"
        descriptions.append(description)
    text = "📋 <b>Мои препараты</b>\n\n" + ("\n".join(descriptions) if rows else "Список пуст.")
    buttons = [
        [InlineKeyboardButton(text=f"✏️ {name}", callback_data=f"medicine:edit:{item_id}")]
        for item_id, name, *_ in rows
    ]
    buttons += [[InlineKeyboardButton(text="➕ Добавить", callback_data="medicine:add")], [InlineKeyboardButton(text="← Меню", callback_data="menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


async def medicine_for_user(user_id: int, medicine_id: int) -> tuple | None:
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute(
            """SELECT id, name, start_date, end_date, frequency,
                      duration_kind, duration_amount, note, weekdays
               FROM medicines WHERE id = ? AND user_id = ? AND active = 1""",
            (medicine_id, user_id),
        )).fetchone()


async def render_edit_menu(callback: CallbackQuery, state: FSMContext, medicine_id: int) -> None:
    medicine = await medicine_for_user(callback.from_user.id, medicine_id)
    if not medicine:
        await callback.answer("Препарат не найден", show_alert=True)
        return
    _, name, start_raw, end_raw, frequency, _, _, note, weekdays = medicine
    start_text = date.fromisoformat(start_raw).strftime("%d.%m.%Y") if start_raw else "не указана"
    end_text = date.fromisoformat(end_raw).strftime("%d.%m.%Y") if end_raw else "не ограничен"
    await state.set_state(EditMedicine.menu)
    await state.update_data(edit_medicine_id=medicine_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить длительность", callback_data="editfield:duration")],
        [InlineKeyboardButton(text="Изменить дату начала", callback_data="editfield:start")],
        [InlineKeyboardButton(text="Изменить частоту", callback_data="editfield:frequency")],
        [InlineKeyboardButton(text="Изменить дни приёма", callback_data="editfield:weekdays")],
        [InlineKeyboardButton(text="Изменить заметку", callback_data="editfield:note")],
        [InlineKeyboardButton(text="🗑 Удалить препарат", callback_data=f"medicine:delete:{medicine_id}")],
        [InlineKeyboardButton(text="← Мои препараты", callback_data="medicine:list")],
    ])
    await callback.message.edit_text(
        f"✏️ <b>{html.escape(name)}</b>\n\n"
        f"Дата начала: {start_text}\n"
        f"Последний день приёма: {end_text}\n"
        f"Частота: {frequency} раз(а) в день\n"
        f"Дни: {weekdays_text(parse_weekdays(weekdays))}\n"
        f"Заметка: {html.escape(note) if note else 'не добавлена'}\n\n"
        "Что изменить?",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("medicine:edit:"))
async def edit_medicine(callback: CallbackQuery, state: FSMContext) -> None:
    await render_edit_menu(callback, state, int(callback.data.rsplit(":", 1)[1]))


@router.callback_query(EditMedicine.menu, F.data == "editfield:duration")
async def edit_duration(callback: CallbackQuery) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней", callback_data="editdur:days:7")],
        [InlineKeyboardButton(text="1 месяц", callback_data="editdur:months:1")],
        [InlineKeyboardButton(text="3 месяца", callback_data="editdur:months:3")],
        [InlineKeyboardButton(text="Бессрочно", callback_data="editdur:forever")],
        [InlineKeyboardButton(text="Другой срок", callback_data="editdur:custom")],
        [InlineKeyboardButton(text="← Назад", callback_data="editfield:back")],
    ])
    await callback.message.edit_text("Выберите новую длительность курса:", reply_markup=keyboard)
    await callback.answer()


async def update_duration(user_id: int, medicine_id: int, kind: str, amount: int | None) -> bool:
    medicine = await medicine_for_user(user_id, medicine_id)
    if not medicine:
        return False
    start_date = date.fromisoformat(medicine[2]) if medicine[2] else datetime.now(TIMEZONE).date()
    end_date, _ = course_dates(start_date, kind, amount)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE medicines SET duration_kind=?, duration_amount=?, start_date=?, end_date=?
               WHERE id=? AND user_id=? AND active=1""",
            (
                kind, amount, start_date.isoformat(), end_date.isoformat() if end_date else None,
                medicine_id, user_id,
            ),
        )
        await db.commit()
    return True


@router.callback_query(EditMedicine.menu, F.data.startswith("editdur:"))
async def choose_edit_duration(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if parts[1] == "custom":
        await state.set_state(EditMedicine.custom_duration)
        await callback.message.edit_text("Введите новое количество дней курса — от 1 до 3650:")
        await callback.answer()
        return
    kind = parts[1]
    amount = int(parts[2]) if len(parts) == 3 else None
    data = await state.get_data()
    await update_duration(callback.from_user.id, data["edit_medicine_id"], kind, amount)
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.message(EditMedicine.custom_duration)
async def edit_custom_duration(message: Message, state: FSMContext) -> None:
    try:
        days = int((message.text or "").strip())
    except ValueError:
        days = 0
    if not 1 <= days <= 3650:
        await message.answer("Введите число от 1 до 3650.")
        return
    data = await state.get_data()
    await update_duration(message.from_user.id, data["edit_medicine_id"], "days", days)
    await state.set_state(EditMedicine.menu)
    await message.answer(
        "Длительность обновлена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="← Вернуться к препарату",
                callback_data=f"medicine:edit:{data['edit_medicine_id']}",
            )]
        ]),
    )


@router.callback_query(EditMedicine.menu, F.data == "editfield:frequency")
async def edit_frequency(callback: CallbackQuery) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 раз в день", callback_data="editfreq:1")],
        [InlineKeyboardButton(text="2 раза в день", callback_data="editfreq:2")],
        [InlineKeyboardButton(text="3 раза в день", callback_data="editfreq:3")],
        [InlineKeyboardButton(text="← Назад", callback_data="editfield:back")],
    ])
    await callback.message.edit_text("Выберите новую частоту приёма:", reply_markup=keyboard)
    await callback.answer()


def edit_custom_weekdays_keyboard(
    selected: set[int],
    medicine_id: int,
) -> InlineKeyboardMarkup:
    day_buttons = [
        InlineKeyboardButton(
            text=f"{'✅ ' if day in selected else ''}{label}",
            callback_data=f"editweekday:{day}",
        )
        for day, label in enumerate(WEEKDAY_LABELS)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        day_buttons[:4],
        day_buttons[4:],
        [InlineKeyboardButton(text="✅ Готово", callback_data="editweekdays:done")],
        [InlineKeyboardButton(
            text="← Назад",
            callback_data=f"medicine:edit:{medicine_id}",
        )],
    ])


@router.callback_query(EditMedicine.menu, F.data == "editfield:weekdays")
async def edit_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    medicine_id = data["edit_medicine_id"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Каждый день", callback_data="editweekdays:all")],
        [InlineKeyboardButton(text="Выбрать дни", callback_data="editweekdays:custom")],
        [InlineKeyboardButton(
            text="← Назад",
            callback_data=f"medicine:edit:{medicine_id}",
        )],
    ])
    await callback.message.edit_text(
        "В какие дни принимать препарат?",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(EditMedicine.menu, F.data == "editweekdays:all")
async def edit_every_day(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE medicines SET weekdays=? WHERE id=? AND user_id=? AND active=1",
            (
                serialize_weekdays(ALL_WEEKDAYS),
                data["edit_medicine_id"],
                callback.from_user.id,
            ),
        )
        await db.commit()
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.callback_query(EditMedicine.menu, F.data == "editweekdays:custom")
async def edit_custom_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    medicine = await medicine_for_user(
        callback.from_user.id, data["edit_medicine_id"]
    )
    if not medicine:
        await callback.answer("Препарат не найден", show_alert=True)
        return
    selected = set(parse_weekdays(medicine[8]))
    await state.update_data(edit_weekdays=list(selected))
    await state.set_state(EditMedicine.weekdays)
    await callback.message.edit_text(
        "Выберите дни приёма:",
        reply_markup=edit_custom_weekdays_keyboard(
            selected, data["edit_medicine_id"]
        ),
    )
    await callback.answer()


@router.callback_query(EditMedicine.weekdays, F.data.startswith("editweekday:"))
async def toggle_edit_weekday(callback: CallbackQuery, state: FSMContext) -> None:
    day = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("edit_weekdays", []))
    if day in selected:
        selected.remove(day)
    else:
        selected.add(day)
    await state.update_data(edit_weekdays=list(selected))
    await callback.message.edit_reply_markup(
        reply_markup=edit_custom_weekdays_keyboard(
            selected, data["edit_medicine_id"]
        )
    )
    await callback.answer()


@router.callback_query(EditMedicine.weekdays, F.data == "editweekdays:done")
async def save_edit_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("edit_weekdays", [])
    if not selected:
        await callback.answer("Выберите хотя бы один день.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE medicines SET weekdays=? WHERE id=? AND user_id=? AND active=1",
            (
                serialize_weekdays(selected),
                data["edit_medicine_id"],
                callback.from_user.id,
            ),
        )
        await db.commit()
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.callback_query(EditMedicine.menu, F.data == "editfield:note")
async def edit_note(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    medicine = await medicine_for_user(
        callback.from_user.id, data["edit_medicine_id"]
    )
    if not medicine:
        await callback.answer("Препарат не найден", show_alert=True)
        return
    await state.set_state(EditMedicine.note)
    buttons = []
    if medicine[7]:
        buttons.append([
            InlineKeyboardButton(text="Удалить заметку", callback_data="editnote:delete")
        ])
    buttons.append([
        InlineKeyboardButton(
            text="← Назад",
            callback_data=f"medicine:edit:{data['edit_medicine_id']}",
        )
    ])
    await callback.message.edit_text(
        f"Введите новую заметку — до {MAX_NOTE_LENGTH} символов.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.message(EditMedicine.note)
async def save_edited_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    if not note:
        await message.answer("Введите текст заметки.")
        return
    if len(note) > MAX_NOTE_LENGTH:
        await message.answer(
            f"Заметка должна быть не длиннее {MAX_NOTE_LENGTH} символов."
        )
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE medicines SET note=? WHERE id=? AND user_id=? AND active=1",
            (note, data["edit_medicine_id"], message.from_user.id),
        )
        await db.commit()
    await state.set_state(EditMedicine.menu)
    await message.answer(
        "Заметка обновлена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="← Вернуться к препарату",
                callback_data=f"medicine:edit:{data['edit_medicine_id']}",
            )
        ]]),
    )


@router.callback_query(EditMedicine.note, F.data == "editnote:delete")
async def delete_note(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE medicines SET note=NULL WHERE id=? AND user_id=? AND active=1",
            (data["edit_medicine_id"], callback.from_user.id),
        )
        await db.commit()
    await render_edit_menu(callback, state, data["edit_medicine_id"])


def edit_start_calendar_markup(year: int, month: int, medicine_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{MONTHS[month]} {year}", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text=x, callback_data="noop") for x in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")])
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        rows.append([
            InlineKeyboardButton(
                text=str(day) if day else " ",
                callback_data=f"editstartpick:{year:04d}-{month:02d}-{day:02d}" if day else "noop",
            )
            for day in week
        ])
    previous = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    rows.append([
        InlineKeyboardButton(text="‹", callback_data=f"editstartcal:{previous.isoformat()}"),
        InlineKeyboardButton(text="Сегодня", callback_data="editstart:today"),
        InlineKeyboardButton(text="›", callback_data=f"editstartcal:{following.isoformat()}"),
    ])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"medicine:edit:{medicine_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(EditMedicine.menu, F.data == "editfield:start")
async def edit_start_date(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    medicine_id = data["edit_medicine_id"]
    await state.set_state(EditMedicine.start_date)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать сегодня", callback_data="editstart:today")],
        [InlineKeyboardButton(text="Выбрать дату", callback_data="editstart:choose")],
        [InlineKeyboardButton(text="← Назад", callback_data=f"medicine:edit:{medicine_id}")],
    ])
    await callback.message.edit_text("Выберите новую дату начала курса:", reply_markup=keyboard)
    await callback.answer()


async def update_start_date(user_id: int, medicine_id: int, selected: date) -> bool:
    medicine = await medicine_for_user(user_id, medicine_id)
    if not medicine:
        return False
    kind, amount = medicine[5], medicine[6]
    end_date, _ = course_dates(selected, kind, amount)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE medicines SET start_date=?, end_date=? WHERE id=? AND user_id=? AND active=1",
            (selected.isoformat(), end_date.isoformat() if end_date else None, medicine_id, user_id),
        )
        await db.commit()
    return True


@router.callback_query(EditMedicine.start_date, F.data == "editstart:today")
async def choose_edit_start_today(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await update_start_date(callback.from_user.id, data["edit_medicine_id"], datetime.now(TIMEZONE).date())
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.callback_query(EditMedicine.start_date, F.data == "editstart:choose")
async def choose_edit_start_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    today = datetime.now(TIMEZONE).date()
    await callback.message.edit_text(
        "Выберите новую дату начала курса:",
        reply_markup=edit_start_calendar_markup(today.year, today.month, data["edit_medicine_id"]),
    )
    await callback.answer()


@router.callback_query(EditMedicine.start_date, F.data.startswith("editstartcal:"))
async def navigate_edit_start_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    selected = date.fromisoformat(callback.data.split(":", 1)[1])
    data = await state.get_data()
    await callback.message.edit_reply_markup(
        reply_markup=edit_start_calendar_markup(selected.year, selected.month, data["edit_medicine_id"])
    )
    await callback.answer()


@router.callback_query(EditMedicine.start_date, F.data.startswith("editstartpick:"))
async def choose_edit_start_day(callback: CallbackQuery, state: FSMContext) -> None:
    selected = date.fromisoformat(callback.data.split(":", 1)[1])
    data = await state.get_data()
    await update_start_date(callback.from_user.id, data["edit_medicine_id"], selected)
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.callback_query(EditMedicine.menu, F.data.startswith("editfreq:"))
async def choose_edit_frequency(callback: CallbackQuery, state: FSMContext) -> None:
    frequency = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE medicines SET frequency=? WHERE id=? AND user_id=? AND active=1",
            (frequency, data["edit_medicine_id"], callback.from_user.id),
        )
        await db.execute(
            """DELETE FROM medicine_notification_times
               WHERE medicine_id=? AND user_id=?""",
            (data["edit_medicine_id"], callback.from_user.id),
        )
        await db.commit()
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.callback_query(EditMedicine.menu, F.data == "editfield:back")
async def back_to_edit_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await render_edit_menu(callback, state, data["edit_medicine_id"])


@router.callback_query(F.data.startswith("medicine:delete:"))
async def delete_medicine(callback: CallbackQuery) -> None:
    medicine_id = int(callback.data.rsplit(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE medicines SET active = 0 WHERE id = ? AND user_id = ?", (medicine_id, callback.from_user.id))
        await db.commit()
    await list_medicines(callback)


@router.callback_query(F.data.startswith("calendar:"))
async def show_calendar(callback: CallbackQuery) -> None:
    value = callback.data.split(":", 1)[1]
    selected = datetime.now(TIMEZONE).date() if value == "today" else date.fromisoformat(value)
    await callback.message.edit_text(
        "Выберите дату:\n\n✅ всё принято  ·  🟡 принято частично  ·  ○ пока не отмечено",
        reply_markup=await calendar_markup(callback.from_user.id, selected.year, selected.month),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("day:"))
async def show_day(callback: CallbackQuery) -> None:
    value = callback.data.split(":", 1)[1]
    selected = datetime.now(TIMEZONE).date() if value == "today" else date.fromisoformat(value)
    origin = "menu" if value == "today" else "calendar"
    await render_day(callback, callback.from_user.id, selected, origin)


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_intake(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    _, medicine_raw, dose_raw, iso_day = parts[:4]
    origin = parts[4] if len(parts) > 4 else "calendar"
    medicine_id = int(medicine_raw)
    dose_number = int(dose_raw)
    async with aiosqlite.connect(DB_PATH) as db:
        owned = await (await db.execute(
            """SELECT 1 FROM medicines m
               WHERE m.id=? AND m.user_id=? AND m.active=1
                 AND ? BETWEEN 1 AND m.frequency
                 AND instr(',' || m.weekdays || ',', ',' || ? || ',') > 0
                 AND (m.start_date IS NULL OR m.start_date <= ?)
                 AND (m.end_date IS NULL OR m.end_date >= ?)""",
            (
                medicine_id, callback.from_user.id, dose_number,
                date.fromisoformat(iso_day).weekday(), iso_day, iso_day,
            ),
        )).fetchone()
        if not owned:
            await callback.answer("Приём не найден", show_alert=True)
            return
        exists = await (await db.execute(
            "SELECT 1 FROM dose_log WHERE user_id=? AND medicine_id=? AND intake_date=? AND dose_number=?",
            (callback.from_user.id, medicine_id, iso_day, dose_number),
        )).fetchone()
        if exists:
            await db.execute(
                "DELETE FROM dose_log WHERE user_id=? AND medicine_id=? AND intake_date=? AND dose_number=?",
                (callback.from_user.id, medicine_id, iso_day, dose_number),
            )
        else:
            await db.execute(
                "INSERT INTO dose_log VALUES (?, ?, ?, ?, ?)",
                (callback.from_user.id, medicine_id, iso_day, dose_number, datetime.now(TIMEZONE).isoformat()),
            )
        await db.commit()
    await render_day(callback, callback.from_user.id, date.fromisoformat(iso_day), origin)


async def reminder_medicine(
    user_id: int,
    medicine_id: int,
    dose_number: int,
    iso_day: str,
) -> tuple[str, int] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            """SELECT name, frequency FROM medicines
               WHERE id = ? AND user_id = ? AND active = 1 AND frequency IN (1, 2, 3)
                 AND ? BETWEEN 1 AND frequency
                 AND instr(',' || weekdays || ',', ',' || ? || ',') > 0
                 AND (start_date IS NULL OR start_date <= ?)
                 AND (end_date IS NULL OR end_date >= ?)""",
            (
                medicine_id, user_id, dose_number,
                date.fromisoformat(iso_day).weekday(), iso_day, iso_day,
            ),
        )).fetchone()
    return (row[0], row[1]) if row else None


async def update_reminder_button(
    callback: CallbackQuery,
    status_text: str,
) -> None:
    markup = callback.message.reply_markup
    if not markup:
        await callback.message.edit_text(status_text)
        return
    updated_rows = []
    replaced = False
    for row in markup.inline_keyboard:
        if any(button.callback_data == callback.data for button in row):
            updated_rows.append([
                InlineKeyboardButton(text=status_text, callback_data="noop")
            ])
            replaced = True
        else:
            updated_rows.append(list(row))
    if replaced:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=updated_rows)
        )


@router.callback_query(F.data.startswith("remtaken:"))
async def reminder_taken(callback: CallbackQuery) -> None:
    _, medicine_raw, dose_raw, iso_day = callback.data.split(":")
    medicine_id = int(medicine_raw)
    dose_number = int(dose_raw)
    medicine = await reminder_medicine(
        callback.from_user.id, medicine_id, dose_number, iso_day
    )
    if not medicine:
        await callback.answer("Приём не найден", show_alert=True)
        return
    name, frequency = medicine
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO dose_log VALUES (?, ?, ?, ?, ?)",
            (
                callback.from_user.id,
                medicine_id,
                iso_day,
                dose_number,
                datetime.now(TIMEZONE).isoformat(),
            ),
        )
        await db.execute(
            """UPDATE reminder_log
               SET response_status='taken', responded_at=?
               WHERE user_id=? AND medicine_id=?
                 AND reminder_date=? AND dose_number=?""",
            (
                datetime.now(TIMEZONE).isoformat(),
                callback.from_user.id,
                medicine_id,
                iso_day,
                dose_number,
            ),
        )
        await db.commit()
    await update_reminder_button(
        callback,
        f"✅ {name} — принято {dose_number} из {frequency}",
    )
    await callback.answer("Приём отмечен")


@router.callback_query(F.data.startswith("remlater:"))
async def reminder_not_yet(callback: CallbackQuery) -> None:
    _, medicine_raw, dose_raw, iso_day = callback.data.split(":")
    medicine_id = int(medicine_raw)
    dose_number = int(dose_raw)
    medicine = await reminder_medicine(
        callback.from_user.id, medicine_id, dose_number, iso_day
    )
    if not medicine:
        await callback.answer("Приём не найден", show_alert=True)
        return
    name, frequency = medicine
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE reminder_log
               SET response_status='not_yet', responded_at=?
               WHERE user_id=? AND medicine_id=?
                 AND reminder_date=? AND dose_number=?""",
            (
                datetime.now(TIMEZONE).isoformat(),
                callback.from_user.id,
                medicine_id,
                iso_day,
                dose_number,
            ),
        )
        await db.commit()
    await update_reminder_button(
        callback,
        f"⏳ {name} — ещё нет · {dose_number} из {frequency}",
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token or token == "put_your_bot_token_here":
        raise RuntimeError("Укажите BOT_TOKEN в файле .env")
    await init_db()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    logger.info("Starting Telegram bot polling")
    reminders = asyncio.create_task(reminder_loop(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        logger.info("Stopping Telegram bot")
        reminders.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminders


if __name__ == "__main__":
    asyncio.run(main())
