import asyncio
import os
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply,
)

from dotenv import load_dotenv


# ============================================================
# НАСТРОЙКА
# ============================================================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DISPLAY_TIMEZONE = ZoneInfo("Europe/Moscow")
HISTORY_PAGE_SIZE = 10

bot = Bot(token=TOKEN)
dp = Dispatcher()


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

db = sqlite3.connect("splitwise.db")
cursor = db.cursor()


# Группы
cursor.execute("""
CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT
)
""")


# Участники групп
cursor.execute("""
CREATE TABLE IF NOT EXISTS group_members (
    chat_id INTEGER,
    user_id INTEGER,
    first_name TEXT,
    username TEXT,
    PRIMARY KEY (chat_id, user_id)
)
""")


# Расходы
cursor.execute("""
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    paid_by INTEGER NOT NULL,
    amount_kopecks INTEGER NOT NULL,
    comment TEXT,
    created_by INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")


# Участники конкретного расхода
cursor.execute("""
CREATE TABLE IF NOT EXISTS expense_participants (
    expense_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    share_kopecks INTEGER NOT NULL,
    PRIMARY KEY (expense_id, user_id)
)
""")
# Выполненные расчёты между участниками
cursor.execute("""
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    amount_kopecks INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
# Если база была создана раньше,
# добавляем поле created_by автоматически.

cursor.execute(
    "PRAGMA table_info(settlements)"
)

settlement_columns = [
    row[1]
    for row in cursor.fetchall()
]

if "created_by" not in settlement_columns:

    cursor.execute("""
        ALTER TABLE settlements
        ADD COLUMN created_by INTEGER
    """)

# Для старых переводов можно безопасно считать,
# что запись внёс тот, кто платил:
# раньше только он мог нажать кнопку "Оплачено".

cursor.execute("""
UPDATE settlements
SET created_by = from_user
WHERE created_by IS NULL
""")

db.commit()


# ============================================================
# FSM — СОСТОЯНИЯ СОЗДАНИЯ РАСХОДА
# ============================================================

class ExpenseForm(StatesGroup):
    choosing_payer = State()
    entering_amount = State()
    choosing_participants = State()
    entering_comment = State()

class EditExpenseForm(StatesGroup):
    entering_amount = State()
    choosing_payer = State()
    choosing_participants = State()
    entering_comment = State()
# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def format_money(kopecks: int) -> str:
    rubles = Decimal(kopecks) / Decimal(100)

    if kopecks % 100 == 0:
        return f"{int(rubles)} ₽"

    return f"{rubles:.2f} ₽".replace(".", ",")

def format_datetime(value) -> str:

    if not value:
        return "Дата неизвестна"

    try:

        # SQLite хранит CURRENT_TIMESTAMP в UTC.
        dt = datetime.fromisoformat(
            str(value).replace(" ", "T")
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        dt = dt.astimezone(
            DISPLAY_TIMEZONE
        )

        return dt.strftime(
            "%d.%m.%Y %H:%M"
        )

    except ValueError:

        return str(value)

def parse_money(text: str):
    """
    Превращает:
    2500
    2500.50
    2500,50

    в количество копеек.
    """

    cleaned = (
        text
        .replace("₽", "")
        .replace("руб.", "")
        .replace("руб", "")
        .replace(" ", "")
        .replace(",", ".")
        .strip()
    )

    try:
        value = Decimal(cleaned)

        if value <= 0:
            return None

        value = value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

        return int(value * 100)

    except InvalidOperation:
        return None


def get_members(chat_id: int):

    cursor.execute(
        """
        SELECT user_id, first_name, username
        FROM group_members
        WHERE chat_id = ?
        ORDER BY first_name
        """,
        (chat_id,)
    )

    return cursor.fetchall()


def get_member_name(chat_id: int, user_id: int):

    cursor.execute(
        """
        SELECT first_name
        FROM group_members
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user_id)
    )

    result = cursor.fetchone()

    if result:
        return result[0]

    return "Неизвестный пользователь"

def get_expense_participant_ids(expense_id: int):

    cursor.execute(
        """
        SELECT user_id
        FROM expense_participants
        WHERE expense_id = ?
        ORDER BY user_id
        """,
        (expense_id,)
    )

    return [
        row[0]
        for row in cursor.fetchall()
    ]


def recalculate_expense_shares(
    expense_id: int,
    amount_kopecks: int,
    user_ids: list
):

    if not user_ids:
        return

    participant_count = len(user_ids)

    base_share = (
        amount_kopecks
        // participant_count
    )

    remainder = (
        amount_kopecks
        % participant_count
    )

    # Удаляем старое распределение
    cursor.execute(
        """
        DELETE FROM expense_participants
        WHERE expense_id = ?
        """,
        (expense_id,)
    )

    # Создаём новое
    for index, user_id in enumerate(user_ids):

        share = base_share

        if index < remainder:
            share += 1

        cursor.execute(
            """
            INSERT INTO expense_participants
            (
                expense_id,
                user_id,
                share_kopecks
            )
            VALUES (?, ?, ?)
            """,
            (
                expense_id,
                user_id,
                share
            )
        )


def calculate_balances(chat_id: int):
    """
    Возвращает чистый баланс каждого участника.

    Положительный баланс:
    человеку должны деньги.

    Отрицательный баланс:
    человек должен деньги.
    """

    members = get_members(chat_id)

    balances = {
        user_id: 0
        for user_id, first_name, username in members
    }

    # ------------------------------------
    # Учитываем расходы
    # ------------------------------------

    cursor.execute(
        """
        SELECT id, paid_by, amount_kopecks
        FROM expenses
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    expenses = cursor.fetchall()

    for expense_id, paid_by, amount_kopecks in expenses:

        # Тот, кто заплатил, должен получить
        # потраченные деньги обратно.
        balances.setdefault(paid_by, 0)

        balances[paid_by] += amount_kopecks

        # Теперь вычитаем доли всех участников.
        cursor.execute(
            """
            SELECT user_id, share_kopecks
            FROM expense_participants
            WHERE expense_id = ?
            """,
            (expense_id,)
        )

        participants = cursor.fetchall()

        for user_id, share_kopecks in participants:

            balances.setdefault(user_id, 0)

            balances[user_id] -= share_kopecks

    # ------------------------------------
    # Учитываем уже совершённые переводы
    # ------------------------------------

    cursor.execute(
        """
        SELECT from_user, to_user, amount_kopecks
        FROM settlements
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    settlements = cursor.fetchall()

    for from_user, to_user, amount_kopecks in settlements:

        balances.setdefault(from_user, 0)
        balances.setdefault(to_user, 0)

        # Должник заплатил деньги —
        # его отрицательный баланс уменьшается.
        balances[from_user] += amount_kopecks

        # Получатель получил деньги —
        # сумма, которую ему должны, уменьшается.
        balances[to_user] -= amount_kopecks

    return balances


def simplify_debts(balances: dict):
    """
    Превращает балансы в минимизированный
    список переводов.

    Возвращает:
    [
        (кто_платит, кому_платит, сумма),
        ...
    ]
    """

    creditors = []
    debtors = []

    for user_id, balance in balances.items():

        if balance > 0:
            creditors.append([
                user_id,
                balance
            ])

        elif balance < 0:
            debtors.append([
                user_id,
                -balance
            ])

    # Сначала самые крупные долги.
    creditors.sort(
        key=lambda item: item[1],
        reverse=True
    )

    debtors.sort(
        key=lambda item: item[1],
        reverse=True
    )

    transfers = []

    debtor_index = 0
    creditor_index = 0

    while (
        debtor_index < len(debtors)
        and
        creditor_index < len(creditors)
    ):

        debtor_id, debt = debtors[debtor_index]
        creditor_id, credit = creditors[creditor_index]

        amount = min(
            debt,
            credit
        )

        transfers.append(
            (
                debtor_id,
                creditor_id,
                amount
            )
        )

        debtors[debtor_index][1] -= amount
        creditors[creditor_index][1] -= amount

        if debtors[debtor_index][1] == 0:
            debtor_index += 1

        if creditors[creditor_index][1] == 0:
            creditor_index += 1

    return transfers

def get_history_records(chat_id: int):

    records = []

    # ====================================
    # Расходы
    # ====================================

    cursor.execute(
        """
        SELECT
            id,
            paid_by,
            amount_kopecks,
            comment,
            created_by,
            created_at
        FROM expenses
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    expenses = cursor.fetchall()

    for (
        expense_id,
        paid_by,
        amount,
        comment,
        created_by,
        created_at
    ) in expenses:

        records.append({
            "type": "expense",
            "id": expense_id,
            "paid_by": paid_by,
            "amount": amount,
            "comment": comment,
            "created_by": created_by,
            "created_at": created_at
        })

    # ====================================
    # Переводы
    # ====================================

    cursor.execute(
        """
        SELECT
            id,
            from_user,
            to_user,
            amount_kopecks,
            created_by,
            created_at
        FROM settlements
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    settlements = cursor.fetchall()

    for (
        settlement_id,
        from_user,
        to_user,
        amount,
        created_by,
        created_at
    ) in settlements:

        records.append({
            "type": "settlement",
            "id": settlement_id,
            "from_user": from_user,
            "to_user": to_user,
            "amount": amount,
            "created_by": created_by,
            "created_at": created_at
        })

    # Сначала новые операции
    records.sort(
        key=lambda item: (
            item["created_at"] or "",
            item["id"]
        ),
        reverse=True
    )

    return records

async def replace_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
    parse_mode="HTML"
):
    """
    Удаляет текущий экран бота
    и отправляет новый.
    """

    chat_id = callback.message.chat.id

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        # Если Telegram по какой-то причине
        # не дал удалить сообщение,
        # бот всё равно продолжит работу.
        pass

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def get_main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить расход",
                    callback_data="add_expense"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Балансы",
                    callback_data="balances"
                ),
                InlineKeyboardButton(
                    text="📜 История",
                    callback_data="history"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📈 Статистика",
                    callback_data="statistics"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Рассчитаться",
                    callback_data="settle"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Участники",
                    callback_data="members"
                ),
                InlineKeyboardButton(
                    text="🙋 Я участвую",
                    callback_data="join"
                )
            ]
        ]
    )


def get_payer_keyboard(chat_id: int):

    members = get_members(chat_id)

    rows = []

    for user_id, first_name, username in members:

        rows.append([
            InlineKeyboardButton(
                text=f"💳 {first_name}",
                callback_data=f"payer:{user_id}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="expense_cancel"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def get_participants_keyboard(
    chat_id: int,
    selected_users: list
):

    members = get_members(chat_id)

    rows = []

    for user_id, first_name, username in members:

        if user_id in selected_users:
            icon = "✅"
        else:
            icon = "⬜"

        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {first_name}",
                callback_data=f"participant:{user_id}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="👥 Выбрать всех",
            callback_data="participants_all"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text="➡️ Продолжить",
            callback_data="participants_done"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="expense_cancel"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )

def get_edit_payer_keyboard(
    chat_id: int,
    expense_id: int
):

    members = get_members(chat_id)

    rows = []

    for user_id, first_name, username in members:

        rows.append([
            InlineKeyboardButton(
                text=f"💳 {first_name}",
                callback_data=(
                    f"edit_payer:"
                    f"{expense_id}:"
                    f"{user_id}"
                )
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"edit_cancel:{expense_id}"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )

def get_edit_participants_keyboard(
    chat_id: int,
    selected_users: list
):

    members = get_members(chat_id)

    rows = []

    for user_id, first_name, username in members:

        icon = (
            "✅"
            if user_id in selected_users
            else "⬜"
        )

        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {first_name}",
                callback_data=(
                    f"edit_participant:{user_id}"
                )
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="👥 Выбрать всех",
            callback_data="edit_participants_all"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text="✅ Сохранить",
            callback_data="edit_participants_done"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="edit_cancel"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def get_back_to_main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="main_menu"
                )
            ]
        ]
    )

# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    if message.chat.type == "private":

        await message.answer(
            "👋 Привет!\n\n"
            "Я бот для совместных расходов.\n\n"
            "Добавь меня в Telegram-группу, "
            "где вы хотите делить расходы."
        )

        return

    await show_menu(message)
# ============================================================
@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    text = (
        "💸 <b>Общие расходы</b>\n\n"
        "Добавляйте расходы, следите за балансами "
        "и рассчитывайтесь друг с другом."
    )

    await replace_screen(
        callback,
        text,
        reply_markup=get_main_keyboard()
    )

    await callback.answer()
# ============================================================
# /MENU
# ============================================================

@dp.message(Command("menu"))
async def menu_handler(message: Message):

    if message.chat.type == "private":

        await message.answer(
            "Добавь меня в группу, "
            "чтобы начать вести общие расходы."
        )

        return

    await show_menu(message)


async def show_menu(message: Message):

    cursor.execute(
        """
        INSERT OR REPLACE INTO groups
        (chat_id, title)
        VALUES (?, ?)
        """,
        (
            message.chat.id,
            message.chat.title
        )
    )

    db.commit()

    await message.answer(
        "💸 <b>Общие расходы</b>\n\n"
        "Добавляйте расходы, следите за балансами "
        "и рассчитывайтесь друг с другом.",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


# ============================================================
# РЕГИСТРАЦИЯ УЧАСТНИКА
# ============================================================

@dp.callback_query(F.data == "join")
async def join_handler(callback: CallbackQuery):

    if callback.message.chat.type == "private":

        await callback.answer(
            "Эта функция работает только в группе.",
            show_alert=True
        )

        return

    user = callback.from_user
    chat = callback.message.chat

    cursor.execute(
        """
        INSERT OR IGNORE INTO group_members
        (chat_id, user_id, first_name, username)
        VALUES (?, ?, ?, ?)
        """,
        (
            chat.id,
            user.id,
            user.first_name,
            user.username
        )
    )

    db.commit()

    await callback.answer(
        "✅ Ты добавлен в участники!",
        show_alert=True
    )


# ============================================================
# СПИСОК УЧАСТНИКОВ
# ============================================================

@dp.callback_query(F.data == "members")
async def members_handler(callback: CallbackQuery):

    chat_id = callback.message.chat.id

    members = get_members(chat_id)

    if not members:

        text = (
            "👥 <b>Участники</b>\n\n"
            "Пока никто не зарегистрировался.\n\n"
            "Нажмите «🙋 Я участвую»."
        )

    else:

        text = "👥 <b>Участники группы:</b>\n\n"

        for number, member in enumerate(
            members,
            start=1
        ):

            user_id, first_name, username = member

            safe_name = escape(first_name)

            if username:

                text += (
                    f"{number}. "
                    f"{safe_name} "
                    f"(@{escape(username)})\n"
                )

            else:

                text += (
                    f"{number}. "
                    f"{safe_name}\n"
                )

    await replace_screen(
    callback,
    text,
    reply_markup=get_back_to_main_keyboard()
    )

    await callback.answer()


# ============================================================
# ➕ НАЧАЛО ДОБАВЛЕНИЯ РАСХОДА
# ============================================================

@dp.callback_query(F.data == "add_expense")
async def add_expense_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    chat_id = callback.message.chat.id

    members = get_members(chat_id)

    if len(members) == 0:

        await callback.answer(
            "Сначала добавьте участников.",
            show_alert=True
        )

        return

    await state.clear()

    await state.set_state(
        ExpenseForm.choosing_payer
    )

    await replace_screen(
        callback,
        "💳 <b>Кто заплатил?</b>",
        reply_markup=get_payer_keyboard(chat_id)
    )

    await callback.answer()


# ============================================================
# ВЫБОР ТОГО, КТО ЗАПЛАТИЛ
# ============================================================

@dp.callback_query(
    ExpenseForm.choosing_payer,
    F.data.startswith("payer:")
)
async def choose_payer(
    callback: CallbackQuery,
    state: FSMContext
):

    payer_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    payer_name = get_member_name(
        chat_id,
        payer_id
    )

    await state.update_data(
        payer_id=payer_id,
        payer_name=payer_name,
        chat_id=chat_id
    )

    await state.set_state(
        ExpenseForm.entering_amount
    )

    await callback.message.answer(
        f"💰 <b>Введите сумму расхода</b>\n\n"
        f"Заплатил: <b>{escape(payer_name)}</b>\n\n"
        f"Например:\n"
        f"<code>2500</code>\n"
        f"или\n"
        f"<code>2500,50</code>",
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Например: 2500"
        )
    )

    await callback.answer()


# ============================================================
# ВВОД СУММЫ
# ============================================================

@dp.message(ExpenseForm.entering_amount)
async def enter_amount(
    message: Message,
    state: FSMContext
):

    amount_kopecks = parse_money(
        message.text or ""
    )

    if amount_kopecks is None:

        await message.answer(
            "❌ Не получилось распознать сумму.\n\n"
            "Введите положительное число.\n"
            "Например: <code>2500</code> "
            "или <code>2500,50</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(
                selective=True
            )
        )

        return

    await state.update_data(
        amount_kopecks=amount_kopecks,
        selected_users=[]
    )

    await state.set_state(
        ExpenseForm.choosing_participants
    )

    await message.answer(
        "👥 <b>Между кем делим расход?</b>\n\n"
        "Нажмите на людей, которые участвовали "
        "в этом расходе.\n\n"
        "✅ — выбран\n"
        "⬜ — не выбран",
        parse_mode="HTML",
        reply_markup=get_participants_keyboard(
            message.chat.id,
            []
        )
    )


# ============================================================
# ВЫБОР УЧАСТНИКОВ РАСХОДА
# ============================================================

@dp.callback_query(
    ExpenseForm.choosing_participants,
    F.data.startswith("participant:")
)
async def toggle_participant(
    callback: CallbackQuery,
    state: FSMContext
):

    user_id = int(
        callback.data.split(":")[1]
    )

    data = await state.get_data()

    selected_users = data.get(
        "selected_users",
        []
    )

    if user_id in selected_users:

        selected_users.remove(user_id)

    else:

        selected_users.append(user_id)

    await state.update_data(
        selected_users=selected_users
    )

    await callback.message.edit_reply_markup(
        reply_markup=get_participants_keyboard(
            callback.message.chat.id,
            selected_users
        )
    )

    await callback.answer()


# ============================================================
# ВЫБРАТЬ ВСЕХ
# ============================================================

@dp.callback_query(
    ExpenseForm.choosing_participants,
    F.data == "participants_all"
)
async def select_all_participants(
    callback: CallbackQuery,
    state: FSMContext
):

    members = get_members(
        callback.message.chat.id
    )

    all_users = [
        member[0]
        for member in members
    ]

    data = await state.get_data()

    selected_users = data.get(
        "selected_users",
        []
    )

    # Если уже выбраны все —
    # кнопка снимет выделение.
    if set(selected_users) == set(all_users):

        selected_users = []

    else:

        selected_users = all_users

    await state.update_data(
        selected_users=selected_users
    )

    await callback.message.edit_reply_markup(
        reply_markup=get_participants_keyboard(
            callback.message.chat.id,
            selected_users
        )
    )

    await callback.answer()


# ============================================================
# ПОДТВЕРЖДЕНИЕ УЧАСТНИКОВ
# ============================================================

@dp.callback_query(
    ExpenseForm.choosing_participants,
    F.data == "participants_done"
)
async def participants_done(
    callback: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    selected_users = data.get(
        "selected_users",
        []
    )

    if not selected_users:

        await callback.answer(
            "Выберите хотя бы одного участника.",
            show_alert=True
        )

        return

    await state.set_state(
        ExpenseForm.entering_comment
    )

    await callback.message.answer(
        "💬 <b>Введите комментарий</b>\n\n"
        "Например:\n"
        "<code>Такси</code>\n"
        "<code>Ужин</code>\n"
        "<code>Продукты</code>\n\n"
        "Если комментарий не нужен — отправьте "
        "<code>-</code>",
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Например: Ужин"
        )
    )

    await callback.answer()


# ============================================================
# КОММЕНТАРИЙ + СОХРАНЕНИЕ
# ============================================================

@dp.message(ExpenseForm.entering_comment)
async def enter_comment(
    message: Message,
    state: FSMContext
):

    comment = (
        message.text or ""
    ).strip()

    if comment == "-":
        comment = ""

    data = await state.get_data()

    chat_id = data["chat_id"]
    payer_id = data["payer_id"]
    payer_name = data["payer_name"]
    amount_kopecks = data["amount_kopecks"]
    selected_users = data["selected_users"]

    # ------------------------------------
    # Распределяем сумму без ошибок округления
    # ------------------------------------

    participant_count = len(
        selected_users
    )

    base_share = (
        amount_kopecks
        // participant_count
    )

    remainder = (
        amount_kopecks
        % participant_count
    )

    # ------------------------------------
    # Сохраняем расход
    # ------------------------------------

    cursor.execute(
        """
        INSERT INTO expenses
        (
            chat_id,
            paid_by,
            amount_kopecks,
            comment,
            created_by
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            payer_id,
            amount_kopecks,
            comment,
            message.from_user.id
        )
    )

    expense_id = cursor.lastrowid

    # ------------------------------------
    # Сохраняем доли участников
    # ------------------------------------

    participant_rows = []

    for index, user_id in enumerate(
        selected_users
    ):

        share = base_share

        # Остаток копеек распределяем
        # по первым участникам.
        if index < remainder:
            share += 1

        cursor.execute(
            """
            INSERT INTO expense_participants
            (
                expense_id,
                user_id,
                share_kopecks
            )
            VALUES (?, ?, ?)
            """,
            (
                expense_id,
                user_id,
                share
            )
        )

        participant_rows.append(
            (user_id, share)
        )

    db.commit()

    # ------------------------------------
    # Формируем красивый ответ
    # ------------------------------------

    if comment:
        title = escape(comment)
    else:
        title = "Без комментария"

    text = (
        "✅ <b>Расход сохранён!</b>\n\n"
        f"📝 <b>{title}</b>\n"
        f"💰 Сумма: "
        f"<b>{format_money(amount_kopecks)}</b>\n"
        f"💳 Заплатил: "
        f"<b>{escape(payer_name)}</b>\n\n"
        f"👥 <b>Разделено между:</b>\n"
    )

    for user_id, share in participant_rows:

        name = get_member_name(
            chat_id,
            user_id
        )

        text += (
            f"• {escape(name)} — "
            f"{format_money(share)}\n"
        )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

    await state.clear()


# ============================================================
# ❌ ОТМЕНА СОЗДАНИЯ РАСХОДА
# ============================================================

@dp.callback_query(F.data == "expense_cancel")
async def cancel_expense(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    await replace_screen(
        callback,
        "❌ <b>Добавление расхода отменено.</b>",
        reply_markup=get_back_to_main_keyboard()
    )

    await callback.answer()


# ============================================================
# 📜 ИСТОРИЯ
# ============================================================

async def show_history_page(
    callback: CallbackQuery,
    page: int,
    edit_message: bool = False
):

    chat_id = callback.message.chat.id

    records = get_history_records(
        chat_id
    )

    # ====================================
    # История пустая
    # ====================================

    if not records:

        if edit_message:

            await callback.message.edit_text(
                "📜 История пока пустая."
            )

        else:

            await callback.message.answer(
                "📜 История пока пустая."
            )

        return

    # ====================================
    # Считаем страницы
    # ====================================

    total_records = len(records)

    total_pages = (
        total_records
        + HISTORY_PAGE_SIZE
        - 1
    ) // HISTORY_PAGE_SIZE

    # Защита от неправильного номера страницы
    if page < 0:
        page = 0

    if page >= total_pages:
        page = total_pages - 1

    start_index = (
        page * HISTORY_PAGE_SIZE
    )

    end_index = (
        start_index + HISTORY_PAGE_SIZE
    )

    page_records = records[
        start_index:end_index
    ]

    # ====================================
    # Заголовок
    # ====================================

    text = (
        "📜 <b>История операций</b>\n"
        f"Страница "
        f"<b>{page + 1}</b> из "
        f"<b>{total_pages}</b>\n\n"
    )

    keyboard_rows = []

    # ====================================
    # Выводим операции
    # ====================================

    for record in page_records:

        # --------------------------------
        # РАСХОД
        # --------------------------------

        if record["type"] == "expense":

            payer_name = get_member_name(
                chat_id,
                record["paid_by"]
            )

            creator_name = get_member_name(
                chat_id,
                record["created_by"]
            )

            title = (
                record["comment"]
                or "Без комментария"
            )

            text += (
                f"🧾 <b>Расход "
                f"#{record['id']} — "
                f"{escape(title)}</b>\n"
                f"💰 "
                f"{format_money(record['amount'])}\n"
                f"💳 Заплатил: "
                f"{escape(payer_name)}\n"
                f"🕒 "
                f"{format_datetime(record['created_at'])}\n"
                f"✍️ Внёс: "
                f"{escape(creator_name)}\n\n"
            )

            button_title = title

            if len(button_title) > 18:

                button_title = (
                    button_title[:18] + "…"
                )

            keyboard_rows.append([
                InlineKeyboardButton(
                    text=(
                        f"🧾 #{record['id']} "
                        f"{button_title}"
                    ),
                    callback_data=(
                        f"expense_view:"
                        f"{record['id']}"
                    )
                )
            ])

        # --------------------------------
        # ПЕРЕВОД
        # --------------------------------

        else:

            from_name = get_member_name(
                chat_id,
                record["from_user"]
            )

            to_name = get_member_name(
                chat_id,
                record["to_user"]
            )

            creator_name = get_member_name(
                chat_id,
                record["created_by"]
            )

            text += (
                f"💸 <b>Перевод "
                f"#{record['id']}</b>\n"
                f"{escape(from_name)} → "
                f"{escape(to_name)}: "
                f"<b>"
                f"{format_money(record['amount'])}"
                f"</b>\n"
                f"🕒 "
                f"{format_datetime(record['created_at'])}\n"
                f"✍️ Отметил: "
                f"{escape(creator_name)}\n\n"
            )

            keyboard_rows.append([
                InlineKeyboardButton(
                    text=(
                        f"💸 #{record['id']} "
                        f"{from_name} → {to_name}"
                    ),
                    callback_data=(
                        f"settlement_view:"
                        f"{record['id']}"
                    )
                )
            ])

    # ====================================
    # Кнопки пагинации
    # ====================================

    navigation_row = []

    if page > 0:

        navigation_row.append(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=(
                    f"history_page:{page - 1}"
                )
            )
        )

    navigation_row.append(
        InlineKeyboardButton(
            text=f"{page + 1} / {total_pages}",
            callback_data="history_ignore"
        )
    )

    if page < total_pages - 1:

        navigation_row.append(
            InlineKeyboardButton(
                text="Вперёд ▶️",
                callback_data=(
                    f"history_page:{page + 1}"
                )
            )
        )

    keyboard_rows.append(
        navigation_row
    )

    keyboard_rows.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="main_menu"
        )
    ])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=keyboard_rows
    )

    # ====================================
    # Отправляем или обновляем сообщение
    # ====================================

    if edit_message:

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    else:

        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

@dp.callback_query(F.data == "history")
async def history_handler(
    callback: CallbackQuery
):

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    await show_history_page(
        callback,
        page=0,
        edit_message=False
    )

    await callback.answer()

@dp.callback_query(
    F.data.startswith("history_page:")
)
async def history_page_handler(
    callback: CallbackQuery
):

    page = int(
        callback.data.split(":")[1]
    )

    await show_history_page(
        callback,
        page=page,
        edit_message=True
    )

    await callback.answer()

@dp.callback_query(
    F.data == "history_ignore"
)
async def history_ignore_handler(
    callback: CallbackQuery
):

    await callback.answer()


# ============================================================
# 📈 СТАТИСТИКА ЗА ВСЁ ВРЕМЯ
# ============================================================

@dp.callback_query(F.data == "statistics")
async def statistics_handler(
    callback: CallbackQuery
):

    chat_id = callback.message.chat.id

    # ========================================================
    # ОСНОВНАЯ СТАТИСТИКА
    # ========================================================

    cursor.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(amount_kopecks), 0)
        FROM expenses
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    expense_count, total_amount = cursor.fetchone()

    if expense_count == 0:

        await callback.message.answer(
            "📈 <b>Статистика за всё время</b>\n\n"
            "Пока нет ни одного расхода.",
            parse_mode="HTML"
        )

        await callback.answer()

        return

    # Средний расход.
    # Всё считаем в копейках.
    average_amount = (
        total_amount + expense_count // 2
    ) // expense_count

    # ========================================================
    # САМЫЙ КРУПНЫЙ РАСХОД
    # ========================================================

    cursor.execute(
        """
        SELECT
            id,
            paid_by,
            amount_kopecks,
            comment
        FROM expenses
        WHERE chat_id = ?
        ORDER BY amount_kopecks DESC
        LIMIT 1
        """,
        (chat_id,)
    )

    biggest_expense = cursor.fetchone()

    # ========================================================
    # СКОЛЬКО ЗАПЛАТИЛ КАЖДЫЙ
    # ========================================================

    cursor.execute(
        """
        SELECT
            paid_by,
            SUM(amount_kopecks),
            COUNT(*)
        FROM expenses
        WHERE chat_id = ?
        GROUP BY paid_by
        ORDER BY SUM(amount_kopecks) DESC
        """,
        (chat_id,)
    )

    paid_statistics = cursor.fetchall()

    # ========================================================
    # СКОЛЬКО РАСХОДОВ ПРИШЛОСЬ НА КАЖДОГО
    # ========================================================

    cursor.execute(
        """
        SELECT
            ep.user_id,
            SUM(ep.share_kopecks)
        FROM expense_participants ep
        JOIN expenses e
            ON e.id = ep.expense_id
        WHERE e.chat_id = ?
        GROUP BY ep.user_id
        ORDER BY SUM(ep.share_kopecks) DESC
        """,
        (chat_id,)
    )

    participant_statistics = cursor.fetchall()

    # ========================================================
    # СТАТИСТИКА ПЕРЕВОДОВ
    # ========================================================

    cursor.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(amount_kopecks), 0)
        FROM settlements
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    settlement_count, settlement_total = (
        cursor.fetchone()
    )

    # ========================================================
    # ФОРМИРУЕМ СООБЩЕНИЕ
    # ========================================================

    text = (
        "📈 <b>Статистика за всё время</b>\n\n"

        f"🧾 Расходов: "
        f"<b>{expense_count}</b>\n"

        f"💰 Потрачено: "
        f"<b>{format_money(total_amount)}</b>\n"

        f"📊 Средний расход: "
        f"<b>{format_money(average_amount)}</b>\n"
    )

    # ========================================================
    # САМЫЙ КРУПНЫЙ РАСХОД
    # ========================================================

    if biggest_expense:

        (
            biggest_id,
            biggest_payer,
            biggest_amount,
            biggest_comment
        ) = biggest_expense

        biggest_title = (
            biggest_comment
            or "Без комментария"
        )

        biggest_payer_name = (
            get_member_name(
                chat_id,
                biggest_payer
            )
        )

        text += (
            "\n🏆 <b>Самый крупный расход</b>\n"
            f"📝 {escape(biggest_title)}\n"
            f"💰 <b>"
            f"{format_money(biggest_amount)}"
            f"</b>\n"
            f"💳 Заплатил: "
            f"{escape(biggest_payer_name)}\n"
        )

    # ========================================================
    # КТО СКОЛЬКО ЗАПЛАТИЛ
    # ========================================================

    text += (
        "\n💳 <b>Кто сколько оплатил:</b>\n"
    )

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for index, (
        user_id,
        paid_amount,
        paid_count
    ) in enumerate(paid_statistics):

        name = get_member_name(
            chat_id,
            user_id
        )

        if index < 3:
            icon = medals[index]
        else:
            icon = "•"

        text += (
            f"{icon} "
            f"{escape(name)} — "
            f"<b>{format_money(paid_amount)}</b>"
            f" · {paid_count} "
        )

        if paid_count == 1:
            text += "расход\n"
        elif 2 <= paid_count <= 4:
            text += "расхода\n"
        else:
            text += "расходов\n"

    # ========================================================
    # НА КОГО СКОЛЬКО ПРИШЛОСЬ
    # ========================================================

    text += (
        "\n👥 <b>На кого пришлось расходов:</b>\n"
    )

    for user_id, share_amount in participant_statistics:

        name = get_member_name(
            chat_id,
            user_id
        )

        percentage = (
            share_amount
            * 100
            / total_amount
        )

        text += (
            f"• {escape(name)} — "
            f"<b>{format_money(share_amount)}</b>"
            f" ({percentage:.1f}%)\n"
        )

    # ========================================================
    # ПЕРЕВОДЫ
    # ========================================================

    text += (
        "\n💸 <b>Расчёты между участниками:</b>\n"
        f"Переводов: "
        f"<b>{settlement_count}</b>\n"
        f"Переведено: "
        f"<b>{format_money(settlement_total)}</b>"
    )

    await replace_screen(
    callback,
    text,
    reply_markup=get_back_to_main_keyboard()
    )

    await callback.answer()

# ============================================================
# 🧾 ПРОСМОТР РАСХОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_view:")
)
async def expense_view_handler(
    callback: CallbackQuery
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT
            paid_by,
            amount_kopecks,
            comment,
            created_by,
            created_at
        FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    expense = cursor.fetchone()

    if not expense:

        await callback.answer(
            "Расход не найден.",
            show_alert=True
        )

        return

    (
        paid_by,
        amount,
        comment,
        created_by,
        created_at
    ) = expense

    payer_name = get_member_name(
        chat_id,
        paid_by
    )

    creator_name = get_member_name(
        chat_id,
        created_by
    )

    if comment:
        title = escape(comment)
    else:
        title = "Без комментария"

    text = (
        f"🧾 <b>Расход #{expense_id}</b>\n\n"
        f"📝 {title}\n"
        f"💰 Сумма: "
        f"<b>{format_money(amount)}</b>\n"
        f"💳 Заплатил: "
        f"<b>{escape(payer_name)}</b>\n"
        f"🕒 Дата: "
        f"{format_datetime(created_at)}\n"
        f"✍️ Внёс: "
        f"{escape(creator_name)}\n\n"
        f"👥 <b>Участники:</b>\n"
    )

    cursor.execute(
        """
        SELECT
            user_id,
            share_kopecks
        FROM expense_participants
        WHERE expense_id = ?
        ORDER BY user_id
        """,
        (expense_id,)
    )

    participants = cursor.fetchall()

    for user_id, share in participants:

        name = get_member_name(
            chat_id,
            user_id
        )

        text += (
            f"• {escape(name)} — "
            f"{format_money(share)}\n"
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить",
                    callback_data=f"expense_edit:{expense_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"expense_delete:{expense_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 К истории",
                    callback_data="history"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="main_menu"
                )
            ]
        ]
    )

    await replace_screen(
        callback,
        text,
        reply_markup=keyboard
    )

# ============================================================
# ✏️ МЕНЮ РЕДАКТИРОВАНИЯ РАСХОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_edit:")
)
async def expense_edit_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT
            paid_by,
            amount_kopecks,
            comment
        FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    expense = cursor.fetchone()

    if not expense:

        await callback.answer(
            "Расход не найден.",
            show_alert=True
        )
        return

    paid_by, amount, comment = expense

    payer_name = get_member_name(
        chat_id,
        paid_by
    )

    title = comment or "Без комментария"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Сумму",
                    callback_data=(
                        f"expense_edit_amount:"
                        f"{expense_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Кто заплатил",
                    callback_data=(
                        f"expense_edit_payer:"
                        f"{expense_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Участников",
                    callback_data=(
                        f"expense_edit_participants:"
                        f"{expense_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="📝 Комментарий",
                    callback_data=(
                        f"expense_edit_comment:"
                        f"{expense_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад к расходу",
                    callback_data=(
                        f"expense_view:"
                        f"{expense_id}"
                    )
                )
            ]
        ]
    )

    await callback.message.answer(
        f"✏️ <b>Редактирование расхода "
        f"#{expense_id}</b>\n\n"
        f"📝 {escape(title)}\n"
        f"💰 {format_money(amount)}\n"
        f"💳 Заплатил: "
        f"{escape(payer_name)}\n\n"
        f"<b>Что изменить?</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    await callback.answer()

# ============================================================
# 💰 ИЗМЕНЕНИЕ СУММЫ
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_edit_amount:")
)
async def expense_edit_amount_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT amount_kopecks
        FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    expense = cursor.fetchone()

    if not expense:

        await callback.answer(
            "Расход не найден.",
            show_alert=True
        )
        return

    current_amount = expense[0]

    await state.set_state(
        EditExpenseForm.entering_amount
    )

    await state.update_data(
        edit_expense_id=expense_id,
        edit_chat_id=chat_id
    )

    await callback.message.answer(
        "💰 <b>Введите новую сумму</b>\n\n"
        f"Сейчас: "
        f"<b>{format_money(current_amount)}</b>\n\n"
        f"Например: <code>3500</code>",
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Новая сумма"
        )
    )

    await callback.answer()
@dp.message(EditExpenseForm.entering_amount)
async def edit_expense_amount_message(
    message: Message,
    state: FSMContext
):

    amount = parse_money(
        message.text or ""
    )

    if amount is None:

        await message.answer(
            "❌ Введите корректную сумму.\n"
            "Например: <code>3500</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(
                selective=True
            )
        )
        return

    data = await state.get_data()

    expense_id = data["edit_expense_id"]
    chat_id = data["edit_chat_id"]

    participants = (
        get_expense_participant_ids(
            expense_id
        )
    )

    cursor.execute(
        """
        UPDATE expenses
        SET amount_kopecks = ?
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            amount,
            expense_id,
            chat_id
        )
    )

    recalculate_expense_shares(
        expense_id,
        amount,
        participants
    )

    db.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Сумма изменена!</b>\n\n"
        f"Новая сумма: "
        f"<b>{format_money(amount)}</b>\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
# ============================================================
# 💳 ИЗМЕНЕНИЕ ПЛАТЕЛЬЩИКА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_edit_payer:")
)
async def expense_edit_payer_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    await state.set_state(
        EditExpenseForm.choosing_payer
    )

    await state.update_data(
        edit_expense_id=expense_id,
        edit_chat_id=chat_id
    )

    await callback.message.answer(
        "💳 <b>Кто на самом деле заплатил?</b>",
        parse_mode="HTML",
        reply_markup=get_edit_payer_keyboard(
            chat_id,
            expense_id
        )
    )

    await callback.answer()    


@dp.callback_query(
    EditExpenseForm.choosing_payer,
    F.data.startswith("edit_payer:")
)
async def edit_payer_selected(
    callback: CallbackQuery,
    state: FSMContext
):

    parts = callback.data.split(":")

    expense_id = int(parts[1])
    payer_id = int(parts[2])

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        UPDATE expenses
        SET paid_by = ?
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            payer_id,
            expense_id,
            chat_id
        )
    )

    db.commit()

    payer_name = get_member_name(
        chat_id,
        payer_id
    )

    await state.clear()

    await callback.message.answer(
        "✅ <b>Плательщик изменён!</b>\n\n"
        f"Теперь заплатил: "
        f"<b>{escape(payer_name)}</b>\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

    await callback.answer()



# ============================================================
# 👥 ИЗМЕНЕНИЕ УЧАСТНИКОВ
# ============================================================

@dp.callback_query(
    F.data.startswith(
        "expense_edit_participants:"
    )
)
async def expense_edit_participants_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    selected_users = (
        get_expense_participant_ids(
            expense_id
        )
    )

    await state.set_state(
        EditExpenseForm.choosing_participants
    )

    await state.update_data(
        edit_expense_id=expense_id,
        edit_chat_id=chat_id,
        edit_selected_users=selected_users
    )

    await callback.message.answer(
        "👥 <b>Кто участвовал в расходе?</b>",
        parse_mode="HTML",
        reply_markup=(
            get_edit_participants_keyboard(
                chat_id,
                selected_users
            )
        )
    )

    await callback.answer()

@dp.callback_query(
    EditExpenseForm.choosing_participants,
    F.data.startswith("edit_participant:")
)
async def edit_participant_toggle(
    callback: CallbackQuery,
    state: FSMContext
):

    user_id = int(
        callback.data.split(":")[1]
    )

    data = await state.get_data()

    selected = data.get(
        "edit_selected_users",
        []
    )

    if user_id in selected:
        selected.remove(user_id)
    else:
        selected.append(user_id)

    await state.update_data(
        edit_selected_users=selected
    )

    await callback.message.edit_reply_markup(
        reply_markup=(
            get_edit_participants_keyboard(
                callback.message.chat.id,
                selected
            )
        )
    )

    await callback.answer()

@dp.callback_query(
    EditExpenseForm.choosing_participants,
    F.data == "edit_participants_all"
)
async def edit_participants_all_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    members = get_members(
        callback.message.chat.id
    )

    all_users = [
        member[0]
        for member in members
    ]

    data = await state.get_data()

    selected = data.get(
        "edit_selected_users",
        []
    )

    if set(selected) == set(all_users):
        selected = []
    else:
        selected = all_users

    await state.update_data(
        edit_selected_users=selected
    )

    await callback.message.edit_reply_markup(
        reply_markup=(
            get_edit_participants_keyboard(
                callback.message.chat.id,
                selected
            )
        )
    )

    await callback.answer()

@dp.callback_query(
    EditExpenseForm.choosing_participants,
    F.data == "edit_participants_done"
)
async def edit_participants_done_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    selected = data.get(
        "edit_selected_users",
        []
    )

    if not selected:

        await callback.answer(
            "Нужно выбрать хотя бы одного человека.",
            show_alert=True
        )
        return

    expense_id = data[
        "edit_expense_id"
    ]

    chat_id = data[
        "edit_chat_id"
    ]

    cursor.execute(
        """
        SELECT amount_kopecks
        FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    expense = cursor.fetchone()

    if not expense:

        await callback.answer(
            "Расход не найден.",
            show_alert=True
        )
        return

    amount = expense[0]

    recalculate_expense_shares(
        expense_id,
        amount,
        selected
    )

    db.commit()

    await state.clear()

    await callback.message.answer(
        "✅ <b>Участники изменены!</b>\n\n"
        "Доли и балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

    await callback.answer()



# ============================================================
# 📝 ИЗМЕНЕНИЕ КОММЕНТАРИЯ
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_edit_comment:")
)
async def expense_edit_comment_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    await state.set_state(
        EditExpenseForm.entering_comment
    )

    await state.update_data(
        edit_expense_id=expense_id,
        edit_chat_id=chat_id
    )

    await callback.message.answer(
        "📝 <b>Введите новый комментарий</b>\n\n"
        "Например: <code>Продукты</code>\n\n"
        "Отправьте <code>-</code>, "
        "чтобы удалить комментарий.",
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Новый комментарий"
        )
    )

    await callback.answer()

@dp.message(EditExpenseForm.entering_comment)
async def edit_comment_message(
    message: Message,
    state: FSMContext
):

    comment = (
        message.text or ""
    ).strip()

    if comment == "-":
        comment = ""

    data = await state.get_data()

    expense_id = data[
        "edit_expense_id"
    ]

    chat_id = data[
        "edit_chat_id"
    ]

    cursor.execute(
        """
        UPDATE expenses
        SET comment = ?
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            comment,
            expense_id,
            chat_id
        )
    )

    db.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Комментарий изменён!</b>",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(
    F.data.startswith("edit_cancel")
)
async def edit_cancel_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    await callback.message.answer(
        "👌 Редактирование отменено.",
        reply_markup=get_main_keyboard()
    )

    await callback.answer()
# ============================================================
# 🗑 ЗАПРОС УДАЛЕНИЯ РАСХОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_delete:")
)
async def expense_delete_handler(
    callback: CallbackQuery
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT comment, amount_kopecks
        FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    expense = cursor.fetchone()

    if not expense:

        await callback.answer(
            "Расход уже удалён или не найден.",
            show_alert=True
        )
        return

    comment, amount = expense

    title = comment or "Без комментария"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=(
                        f"expense_delete_confirm:"
                        f"{expense_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=(
                        f"expense_delete_cancel:"
                        f"{expense_id}"
                    )
                )
            ]
        ]
    )

    await callback.message.answer(
        "⚠️ <b>Удалить расход?</b>\n\n"
        f"📝 {escape(title)}\n"
        f"💰 {format_money(amount)}\n\n"
        "После удаления балансы будут "
        "пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    await callback.answer()


# ============================================================
# ✅ ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ РАСХОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_delete_confirm:")
)
async def expense_delete_confirm_handler(
    callback: CallbackQuery
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT id
        FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    if not cursor.fetchone():

        await callback.answer(
            "Расход уже удалён.",
            show_alert=True
        )
        return

    # Сначала удаляем участников расхода
    cursor.execute(
        """
        DELETE FROM expense_participants
        WHERE expense_id = ?
        """,
        (expense_id,)
    )

    # Затем сам расход
    cursor.execute(
        """
        DELETE FROM expenses
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            expense_id,
            chat_id
        )
    )

    db.commit()

    await callback.message.answer(
        f"🗑 <b>Расход #{expense_id} удалён.</b>\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

    await callback.answer(
        "Расход удалён"
    )




@dp.callback_query(
    F.data.startswith("expense_delete_cancel:")
)
async def expense_delete_cancel_handler(
    callback: CallbackQuery
):

    await callback.message.answer(
        "👌 Удаление отменено."
    )

    await callback.answer()

# ============================================================
# 💸 ПРОСМОТР ПЕРЕВОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("settlement_view:")
)
async def settlement_view_handler(
    callback: CallbackQuery
):

    settlement_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT
            from_user,
            to_user,
            amount_kopecks,
            created_by,
            created_at
        FROM settlements
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            settlement_id,
            chat_id
        )
    )

    settlement = cursor.fetchone()

    if not settlement:

        await callback.answer(
            "Перевод не найден.",
            show_alert=True
        )

        return

    (
        from_user,
        to_user,
        amount,
        created_by,
        created_at
    ) = settlement

    from_name = get_member_name(
        chat_id,
        from_user
    )

    to_name = get_member_name(
        chat_id,
        to_user
    )

    creator_name = get_member_name(
        chat_id,
        created_by
    )

    text = (
        f"💸 <b>Перевод "
        f"#{settlement_id}</b>\n\n"
        f"👤 От: "
        f"<b>{escape(from_name)}</b>\n"
        f"👤 Кому: "
        f"<b>{escape(to_name)}</b>\n"
        f"💰 Сумма: "
        f"<b>{format_money(amount)}</b>\n"
        f"🕒 Дата: "
        f"{format_datetime(created_at)}\n"
        f"✍️ Отметил: "
        f"{escape(creator_name)}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Отменить перевод",
                    callback_data=(
                        f"settlement_delete:"
                        f"{settlement_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 К истории",
                    callback_data="history"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="main_menu"
                )
            ]
        ]
    )

    await replace_screen(
        callback,
        text,
        reply_markup=keyboard
    )



# ============================================================
# ↩️ ЗАПРОС ОТМЕНЫ ПЕРЕВОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("settlement_delete:")
)
async def settlement_delete_handler(
    callback: CallbackQuery
):

    settlement_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        SELECT
            from_user,
            to_user,
            amount_kopecks
        FROM settlements
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            settlement_id,
            chat_id
        )
    )

    settlement = cursor.fetchone()

    if not settlement:

        await callback.answer(
            "Перевод уже отменён или не найден.",
            show_alert=True
        )
        return

    from_user, to_user, amount = settlement

    from_name = get_member_name(
        chat_id,
        from_user
    )

    to_name = get_member_name(
        chat_id,
        to_user
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, отменить",
                    callback_data=(
                        f"settlement_delete_confirm:"
                        f"{settlement_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Не отменять",
                    callback_data=(
                        f"settlement_delete_cancel:"
                        f"{settlement_id}"
                    )
                )
            ]
        ]
    )

    await callback.message.answer(
        "⚠️ <b>Отменить перевод?</b>\n\n"
        f"{escape(from_name)} → "
        f"{escape(to_name)}\n"
        f"💰 <b>{format_money(amount)}</b>\n\n"
        "Долг снова появится в балансах.",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    await callback.answer()



# ============================================================
# ✅ ПОДТВЕРЖДЕНИЕ ОТМЕНЫ ПЕРЕВОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("settlement_delete_confirm:")
)
async def settlement_delete_confirm_handler(
    callback: CallbackQuery
):

    settlement_id = int(
        callback.data.split(":")[1]
    )

    chat_id = callback.message.chat.id

    cursor.execute(
        """
        DELETE FROM settlements
        WHERE id = ?
        AND chat_id = ?
        """,
        (
            settlement_id,
            chat_id
        )
    )

    deleted = cursor.rowcount

    db.commit()

    if deleted == 0:

        await callback.answer(
            "Перевод уже отменён.",
            show_alert=True
        )
        return

    await callback.message.answer(
        f"↩️ <b>Перевод #{settlement_id} отменён.</b>\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

    await callback.answer(
        "Перевод отменён"
    )


# ============================================================
# 📊 БАЛАНСЫ
# ============================================================

@dp.callback_query(F.data == "balances")
async def balances_handler(
    callback: CallbackQuery
):

    chat_id = callback.message.chat.id

    balances = calculate_balances(
        chat_id
    )

    if not balances:

        await callback.message.answer(
            "📊 Пока нет данных для расчёта."
        )

        await callback.answer()

        return

    text = "📊 <b>Текущие балансы</b>\n\n"

    has_debts = False

    for user_id, balance in balances.items():

        name = get_member_name(
            chat_id,
            user_id
        )

        if balance > 0:

            has_debts = True

            text += (
                f"🟢 <b>{escape(name)}</b>\n"
                f"должен получить "
                f"{format_money(balance)}\n\n"
            )

        elif balance < 0:

            has_debts = True

            text += (
                f"🔴 <b>{escape(name)}</b>\n"
                f"должен "
                f"{format_money(-balance)}\n\n"
            )

        else:

            text += (
                f"⚪ <b>{escape(name)}</b>\n"
                f"всё ровно\n\n"
            )

    if not has_debts:

        text += (
            "✅ <b>Все рассчитались!</b>"
        )

    else:

        transfers = simplify_debts(
            balances
        )

        text += (
            "──────────────\n"
            "💸 <b>Как рассчитаться:</b>\n\n"
        )

        for (
            from_user,
            to_user,
            amount
        ) in transfers:

            from_name = get_member_name(
                chat_id,
                from_user
            )

            to_name = get_member_name(
                chat_id,
                to_user
            )

            text += (
                f"• {escape(from_name)} → "
                f"{escape(to_name)}: "
                f"<b>{format_money(amount)}</b>\n"
            )

    await replace_screen(
    callback,
    text,
    reply_markup=get_back_to_main_keyboard()
    )

    await callback.answer()

# ============================================================
# 💳 РАССЧИТАТЬСЯ
# ============================================================

@dp.callback_query(F.data == "settle")
async def settle_handler(
    callback: CallbackQuery
):

    chat_id = callback.message.chat.id

    balances = calculate_balances(
        chat_id
    )

    transfers = simplify_debts(
        balances
    )

    if not transfers:

        await replace_screen(
            callback,
            "✅ <b>Все рассчитались!</b>\n\n"
            "Никто никому ничего не должен.",
            reply_markup=get_back_to_main_keyboard()
        )

        await callback.answer()

        return

    text = (
        "💳 <b>Расчёты</b>\n\n"
        "После перевода денег нажмите "
        "соответствующую кнопку «Оплачено».\n\n"
    )

    keyboard_rows = []

    for (
        from_user,
        to_user,
        amount
    ) in transfers:

        from_name = get_member_name(
            chat_id,
            from_user
        )

        to_name = get_member_name(
            chat_id,
            to_user
        )

        text += (
            f"💸 {escape(from_name)} → "
            f"{escape(to_name)}\n"
            f"<b>{format_money(amount)}</b>\n\n"
        )

        keyboard_rows.append([
            InlineKeyboardButton(
                text=(
                    f"✅ {from_name} → "
                    f"{to_name}: "
                    f"{format_money(amount)}"
                ),
                callback_data=(
                    f"settlement_done:"
                    f"{from_user}:"
                    f"{to_user}:"
                    f"{amount}"
                )
            )
        ])

    keyboard_rows.append([
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data="settle"
        )
    ])
    keyboard_rows.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="main_menu"
        )
    ])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=keyboard_rows
    )

    await replace_screen(
        callback,
        text,
        reply_markup=keyboard
    )


# ============================================================
# ✅ ПЕРЕВОД ВЫПОЛНЕН
# ============================================================

@dp.callback_query(
    F.data.startswith("settlement_done:")
)
async def settlement_done_handler(
    callback: CallbackQuery
):

    parts = callback.data.split(":")

    from_user = int(parts[1])
    to_user = int(parts[2])
    amount = int(parts[3])

    chat_id = callback.message.chat.id

    # ------------------------------------
    # Перевод может отметить только тот,
    # кто должен заплатить.
    # ------------------------------------
    #if callback.from_user.id != from_user:
    #   from_name = get_member_name(
    #        chat_id,
    #        from_user
    #   )
    #    await callback.answer(
    #        f"Этот перевод должен отметить {from_name}.",
    #        show_alert=True
    #    )
    #    return



    # ------------------------------------
    # Проверяем, что кнопка ещё актуальна.
    # Например, пользователь мог нажать
    # старую кнопку второй раз.
    # ------------------------------------

    balances = calculate_balances(
        chat_id
    )

    current_transfers = simplify_debts(
        balances
    )

    transfer_exists = False

    for (
        current_from,
        current_to,
        current_amount
    ) in current_transfers:

        if (
            current_from == from_user
            and
            current_to == to_user
            and
            current_amount == amount
        ):

            transfer_exists = True
            break

    if not transfer_exists:

        await callback.answer(
            "Этот расчёт уже изменился. "
            "Нажмите «Рассчитаться» ещё раз.",
            show_alert=True
        )

        return

    # ------------------------------------
    # Записываем расчёт в базу
    # ------------------------------------

    cursor.execute(
        """
        INSERT INTO settlements
        (
            chat_id,
            from_user,
            to_user,
            amount_kopecks,
            created_by
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            from_user,
            to_user,
            amount,
            callback.from_user.id
        )
    )

    db.commit()

    from_name = get_member_name(
        chat_id,
        from_user
    )

    to_name = get_member_name(
        chat_id,
        to_user
    )

    await callback.message.answer(
        "✅ <b>Расчёт учтён!</b>\n\n"
        f"{escape(from_name)} → "
        f"{escape(to_name)}\n"
        f"💰 <b>{format_money(amount)}</b>",
        parse_mode="HTML"
    )

    await callback.answer(
        "✅ Перевод отмечен"
    )

# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    print("Бот запущен!")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

# start 
#       .\.venv\Scripts\python.exe bot.py