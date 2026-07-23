from __future__ import annotations

import asyncio
import calendar
import contextlib
import html
import os
from datetime import date, datetime, time, timedelta
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
router = Router()

MONTHS = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


class AddMedicine(StatesGroup):
    name = State()
    duration = State()
    custom_duration = State()
    start_date = State()
    frequency = State()
    confirm = State()


class EditMedicine(StatesGroup):
    menu = State()
    custom_duration = State()
    start_date = State()


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


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💊 Сегодня", callback_data="day:today")],
        [InlineKeyboardButton(text="📅 Календарь", callback_data="calendar:today")],
        [InlineKeyboardButton(text="➕ Добавить препарат", callback_data="medicine:add")],
        [InlineKeyboardButton(text="📋 Мои препараты", callback_data="medicine:list")],
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
            ORDER BY m.name
            """,
            (user_id, iso_day, iso_day),
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


async def send_due_reminders(bot: Bot, now: datetime | None = None) -> int:
    current = now or datetime.now(TIMEZONE)
    current_time = current.time().replace(tzinfo=None)
    if current_time >= time(23, 0):
        due_slots = ((2, 2), (3, 3))
    elif current_time >= time(17, 0):
        due_slots = ((2, 1), (3, 2))
    elif current_time >= time(11, 0):
        due_slots = ((2, 1), (3, 1))
    else:
        return 0

    iso_day = current.date().isoformat()
    sent = 0
    for frequency, dose_number in due_slots:
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                """
                SELECT m.id, m.user_id, m.name
                FROM medicines m
                LEFT JOIN dose_log d ON d.medicine_id = m.id
                    AND d.user_id = m.user_id
                    AND d.intake_date = ?
                    AND d.dose_number = ?
                LEFT JOIN reminder_log r ON r.medicine_id = m.id
                    AND r.user_id = m.user_id
                    AND r.reminder_date = ?
                    AND r.dose_number = ?
                WHERE m.active = 1
                  AND m.frequency = ?
                  AND (m.start_date IS NULL OR m.start_date <= ?)
                  AND (m.end_date IS NULL OR m.end_date >= ?)
                  AND d.medicine_id IS NULL
                  AND r.medicine_id IS NULL
                """,
                (
                    iso_day, dose_number, iso_day, dose_number,
                    frequency, iso_day, iso_day,
                ),
            )).fetchall()

        for medicine_id, user_id, name in rows:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Принято",
                    callback_data=f"remtaken:{medicine_id}:{dose_number}:{iso_day}",
                ),
                InlineKeyboardButton(
                    text="⏳ Ещё нет",
                    callback_data=f"remlater:{medicine_id}:{dose_number}:{iso_day}",
                ),
            ]])
            try:
                await bot.send_message(
                    user_id,
                    f"💊 <b>{html.escape(name)}</b> — приём {dose_number} из {frequency}\n\nУже приняли?",
                    reply_markup=keyboard,
                )
            except Exception:
                continue
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO reminder_log VALUES (?, ?, ?, ?, ?)",
                    (user_id, medicine_id, iso_day, dose_number, current.isoformat()),
                )
                await db.commit()
            sent += 1
    return sent


async def reminder_loop(bot: Bot) -> None:
    while True:
        await send_due_reminders(bot)
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
            """SELECT id, start_date, end_date, frequency FROM medicines
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
            for medicine_id, start_raw, end_raw, frequency in medicines
            if (not start_raw or start_raw <= iso_day) and (not end_raw or end_raw >= iso_day)
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
        "<b>Шаг 1 из 5. Название</b>\n\nВведите название препарата.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]
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
    text = "<b>Шаг 3 из 5. Дата начала</b>\n\nКогда начался или начнётся курс?"
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
            "<b>Шаг 2 из 5. Другой срок</b>\n\nВведите количество дней курса, например: <code>14</code>",
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
        "<b>Шаг 2 из 5. Длительность курса</b>\n\nКак долго нужно принимать препарат?",
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
        "<b>Шаг 4 из 5. Частота приёма</b>\n\nСколько раз в день нужно принимать препарат?",
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
    data = await state.get_data()
    await state.set_state(AddMedicine.confirm)
    end_text = date.fromisoformat(data["end_date"]).strftime("%d.%m.%Y") if data["end_date"] else "не ограничен"
    text = (
        "<b>Шаг 5 из 5. Проверка</b>\n\n"
        f"Препарат: <b>{html.escape(data['name'])}</b>\n"
        f"Курс: {data['duration_label']}\n"
        f"Начало: {date.fromisoformat(data['start_date']).strftime('%d.%m.%Y')}\n"
        f"Последний день приёма: {end_text}\n"
        f"Частота: {frequency} раз(а) в день"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="medicine:save")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


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
        "<b>Шаг 3 из 5. Дата начала</b>\n\nВыберите день начала курса:",
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
                   user_id, name, start_date, end_date, frequency, duration_kind, duration_amount
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                callback.from_user.id, data["name"], data["start_date"], data["end_date"],
                data["frequency"], data["duration_kind"], data["duration_amount"],
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
            SELECT m.id, m.name, m.start_date, m.end_date, m.frequency
            FROM medicines m
            WHERE m.user_id = ? AND m.active = 1
            ORDER BY m.name
            """,
            (callback.from_user.id,),
        )).fetchall()
    descriptions = []
    for _, name, start_date, end_date, frequency in rows:
        course = "бессрочно" if not end_date else f"до {date.fromisoformat(end_date).strftime('%d.%m.%Y')}"
        descriptions.append(f"• <b>{html.escape(name)}</b>: {frequency} раз(а) в день; {course}")
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
            """SELECT id, name, start_date, end_date, frequency, duration_kind, duration_amount
               FROM medicines WHERE id = ? AND user_id = ? AND active = 1""",
            (medicine_id, user_id),
        )).fetchone()


async def render_edit_menu(callback: CallbackQuery, state: FSMContext, medicine_id: int) -> None:
    medicine = await medicine_for_user(callback.from_user.id, medicine_id)
    if not medicine:
        await callback.answer("Препарат не найден", show_alert=True)
        return
    _, name, start_raw, end_raw, frequency, _, _ = medicine
    start_text = date.fromisoformat(start_raw).strftime("%d.%m.%Y") if start_raw else "не указана"
    end_text = date.fromisoformat(end_raw).strftime("%d.%m.%Y") if end_raw else "не ограничен"
    await state.set_state(EditMedicine.menu)
    await state.update_data(edit_medicine_id=medicine_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить длительность", callback_data="editfield:duration")],
        [InlineKeyboardButton(text="Изменить дату начала", callback_data="editfield:start")],
        [InlineKeyboardButton(text="Изменить частоту", callback_data="editfield:frequency")],
        [InlineKeyboardButton(text="🗑 Удалить препарат", callback_data=f"medicine:delete:{medicine_id}")],
        [InlineKeyboardButton(text="← Мои препараты", callback_data="medicine:list")],
    ])
    await callback.message.edit_text(
        f"✏️ <b>{html.escape(name)}</b>\n\n"
        f"Дата начала: {start_text}\n"
        f"Последний день приёма: {end_text}\n"
        f"Частота: {frequency} раз(а) в день\n\n"
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
                 AND (m.start_date IS NULL OR m.start_date <= ?)
                 AND (m.end_date IS NULL OR m.end_date >= ?)""",
            (medicine_id, callback.from_user.id, dose_number, iso_day, iso_day),
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
               WHERE id = ? AND user_id = ? AND active = 1 AND frequency IN (2, 3)
                 AND ? BETWEEN 1 AND frequency
                 AND (start_date IS NULL OR start_date <= ?)
                 AND (end_date IS NULL OR end_date >= ?)""",
            (medicine_id, user_id, dose_number, iso_day, iso_day),
        )).fetchone()
    return (row[0], row[1]) if row else None


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
        await db.commit()
    await callback.message.edit_text(
        f"✅ <b>{html.escape(name)}</b> — приём {dose_number} из {frequency} отмечен"
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
    await callback.message.edit_text(
        f"⏳ <b>{html.escape(name)}</b> — приём {dose_number} из {frequency} пока не отмечен"
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
    reminders = asyncio.create_task(reminder_loop(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        reminders.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminders


if __name__ == "__main__":
    asyncio.run(main())
