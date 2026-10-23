import asyncio
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.dispatcher.flags import get_flag
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

from aiogram.client.session.aiohttp import AiohttpSession #для сервака


from dotenv import load_dotenv


# ============================================================
# НАСТРОЙКА
# ============================================================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DISPLAY_TIMEZONE = ZoneInfo("Europe/Moscow")
HISTORY_PAGE_SIZE = 10

# Прокси для доступа к Telegram (на сервере — xray).
# Чтобы работать без прокси, задайте в .env пустое PROXY_URL=
PROXY_URL = os.getenv("PROXY_URL", "socks5://127.0.0.1:10808")

# В Docker база лежит в примонтированной папке /app/data
DATABASE_PATH = os.getenv("DATABASE_PATH", "splitwise.db")

if PROXY_URL:
    bot = Bot(token=TOKEN, session=AiohttpSession(proxy=PROXY_URL))
else:
    bot = Bot(token=TOKEN)

dp = Dispatcher()


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

db = sqlite3.connect(DATABASE_PATH)
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

# Какую группу пользователь выбрал
# для работы с ботом в личных сообщениях
cursor.execute("""
CREATE TABLE IF NOT EXISTS user_active_group (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL
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


def ensure_local_numbers(table_name: str):

    cursor.execute(
        f"PRAGMA table_info({table_name})"
    )

    columns = [
        row[1]
        for row in cursor.fetchall()
    ]

    # Добавляем колонку, если её ещё нет
    if "local_number" not in columns:

        cursor.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN local_number INTEGER
            """
        )

    # Получаем все группы, в которых
    # ещё есть записи без локального номера
    cursor.execute(
        f"""
        SELECT DISTINCT chat_id
        FROM {table_name}
        WHERE local_number IS NULL
        """
    )

    chats = cursor.fetchall()

    for (chat_id,) in chats:

        # Узнаём последний уже существующий номер
        cursor.execute(
            f"""
            SELECT COALESCE(MAX(local_number), 0)
            FROM {table_name}
            WHERE chat_id = ?
            """,
            (chat_id,)
        )

        current_number = cursor.fetchone()[0]

        # Старые записи нумеруем по порядку ID
        cursor.execute(
            f"""
            SELECT id
            FROM {table_name}
            WHERE chat_id = ?
            AND local_number IS NULL
            ORDER BY id
            """,
            (chat_id,)
        )

        rows = cursor.fetchall()

        for (row_id,) in rows:

            current_number += 1

            cursor.execute(
                f"""
                UPDATE {table_name}
                SET local_number = ?
                WHERE id = ?
                """,
                (
                    current_number,
                    row_id
                )
            )


ensure_local_numbers("expenses")
ensure_local_numbers("settlements")

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

def format_balance(kopecks: int) -> str:

    if kopecks > 0:
        return f"+{format_money(kopecks)}"

    if kopecks < 0:
        return f"-{format_money(-kopecks)}"

    return "0 ₽"


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

# Больше 100 млн ₽ за раз не бывает,
# а огромные числа не влезут в базу.
MAX_AMOUNT_KOPECKS = 100_000_000 * 100

MAX_EXPRESSION_LENGTH = 100

EXPRESSION_TOKEN = re.compile(
    r"\d+(?:\.\d*)?|\.\d+|[-+*/()]"
)


def clean_money_text(text: str) -> str:

    return (
        text
        .replace("₽", "")
        .replace("руб.", "")
        .replace("руб", "")
        .replace(" ", "")
        .replace(",", ".")
        # Другие способы написать знаки
        .replace("×", "*")
        .replace("х", "*")
        .replace("x", "*")
        .replace("÷", "/")
        .replace(":", "/")
        .replace("−", "-")
        .replace("–", "-")
        .strip()
    )


def calculate_expression(text: str):
    """
    Считает выражение из чисел,
    + - * / и скобок, например:
    99+322
    (1200+800)/3

    eval не используем — так нельзя
    выполнить через бота чужой код.

    Возвращает Decimal или None,
    если выражение некорректное.
    """

    if not text or len(text) > MAX_EXPRESSION_LENGTH:
        return None

    tokens = EXPRESSION_TOKEN.findall(text)

    # Если что-то не попало в токены —
    # в тексте есть посторонние символы
    if "".join(tokens) != text:
        return None

    position = 0

    def peek():
        if position < len(tokens):
            return tokens[position]
        return None

    def take():
        nonlocal position
        position += 1
        return tokens[position - 1]

    # выражение := слагаемое (+|- слагаемое)*
    def parse_sum():

        value = parse_product()

        while peek() in ("+", "-"):

            if take() == "+":
                value += parse_product()
            else:
                value -= parse_product()

        return value

    # слагаемое := множитель (*|/ множитель)*
    def parse_product():

        value = parse_factor()

        while peek() in ("*", "/"):

            if take() == "*":
                value *= parse_factor()

            else:

                divider = parse_factor()

                if divider == 0:
                    raise ValueError("деление на ноль")

                value /= divider

        return value

    # множитель := число | (выражение) | -множитель
    def parse_factor():

        token = peek()

        if token is None:
            raise ValueError("выражение оборвалось")

        if token in ("+", "-"):

            take()

            value = parse_factor()

            return value if token == "+" else -value

        if token == "(":

            take()

            value = parse_sum()

            if peek() != ")":
                raise ValueError("нет закрывающей скобки")

            take()

            return value

        if token in (")", "*", "/"):
            raise ValueError("знак не на своём месте")

        take()

        return Decimal(token)

    try:

        result = parse_sum()

    except (ValueError, InvalidOperation):

        return None

    # Остались лишние символы, например «5)»
    if position != len(tokens):
        return None

    return result


def is_money_expression(text: str) -> bool:
    """
    Пользователь ввёл выражение,
    а не просто число.
    """

    return any(
        sign in clean_money_text(text)[1:]
        for sign in "+-*/()"
    )


def parse_money(text: str):
    """
    Превращает:
    2500
    2500.50
    2500,50
    99+322
    3000/4

    в количество копеек.
    """

    value = calculate_expression(
        clean_money_text(text)
    )

    if value is None or value <= 0:
        return None

    try:

        value = value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

    except InvalidOperation:
        return None

    kopecks = int(value * 100)

    if kopecks <= 0 or kopecks > MAX_AMOUNT_KOPECKS:
        return None

    return kopecks


def format_entered_amount(
    text: str,
    kopecks: int
) -> str:
    """
    «421 ₽», а для выражения —
    «421 ₽ (99+322)», чтобы было видно,
    что именно посчитано.
    """

    result = f"<b>{format_money(kopecks)}</b>"

    if is_money_expression(text):

        expression = escape(
            (text or "").strip()
        )

        result += f" (<code>{expression}</code>)"

    return result


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
            local_number,
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
        local_number,
        paid_by,
        amount,
        comment,
        created_by,
        created_at
    ) in expenses:

        records.append({
            "type": "expense",
            "id": expense_id,
            "number": local_number,
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
            local_number,
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
        local_number,
        from_user,
        to_user,
        amount,
        created_by,
        created_at
    ) in settlements:

        records.append({
            "type": "settlement",
            "id": settlement_id,
            "number": local_number,
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

    chat_id = callback.message.chat.id

    # В личке меняем текущее сообщение.
    # В группе удаляем и отправляем новое,
    # чтобы меню оставалось внизу чата.
    if is_private(callback.message.chat):

        try:

            return await callback.message.edit_text(
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )

        except TelegramBadRequest as error:

            # Нажали «Обновить», а ничего не изменилось
            if "message is not modified" in str(error):
                return callback.message

            # Сообщение уже удалено или его нельзя
            # изменить — отправляем новое, как в группе

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    sent_message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )

    return sent_message

def get_next_expense_number(chat_id: int) -> int:

    cursor.execute(
        """
        SELECT COALESCE(MAX(local_number), 0) + 1
        FROM expenses
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    return cursor.fetchone()[0]


def get_next_settlement_number(chat_id: int) -> int:

    cursor.execute(
        """
        SELECT COALESCE(MAX(local_number), 0) + 1
        FROM settlements
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    return cursor.fetchone()[0]



def calculate_expense_impact(
    expense_id: int
):

    cursor.execute(
        """
        SELECT
            paid_by,
            amount_kopecks
        FROM expenses
        WHERE id = ?
        """,
        (expense_id,)
    )

    expense = cursor.fetchone()

    if not expense:
        return {}

    paid_by, amount = expense

    impact = {}

    # Плательщику баланс увеличивается
    impact[paid_by] = (
        impact.get(paid_by, 0)
        + amount
    )

    cursor.execute(
        """
        SELECT
            user_id,
            share_kopecks
        FROM expense_participants
        WHERE expense_id = ?
        """,
        (expense_id,)
    )

    participants = cursor.fetchall()

    # Доля каждого уменьшает его баланс
    for user_id, share in participants:

        impact[user_id] = (
            impact.get(user_id, 0)
            - share
        )

    return impact


async def remember_flow_message(
    state: FSMContext,
    message: Message
):

    data = await state.get_data()

    message_ids = data.get(
        "flow_bot_message_ids",
        []
    )

    if message.message_id not in message_ids:
        message_ids.append(
            message.message_id
        )

    await state.update_data(
        flow_bot_message_ids=message_ids
    )


async def cleanup_flow_messages(
    chat_id: int,
    state: FSMContext
):

    data = await state.get_data()

    message_ids = data.get(
        "flow_bot_message_ids",
        []
    )

    for message_id in message_ids:

        try:

            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id
            )

        except TelegramBadRequest:
            pass

def get_operation_balance_changes(
    chat_id: int,
    target_type: str,
    target_id: int
):
    """
    Возвращает баланс участников
    непосредственно ДО и ПОСЛЕ операции.

    target_type:
    "expense" или "settlement"
    """

    balances = {}

    for user_id, first_name, username in get_members(chat_id):
        balances[user_id] = 0

    operations = []

    # ====================================
    # Расходы
    # ====================================

    cursor.execute(
        """
        SELECT
            id,
            paid_by,
            amount_kopecks,
            created_at
        FROM expenses
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    for (
        expense_id,
        paid_by,
        amount,
        created_at
    ) in cursor.fetchall():

        operations.append({
            "type": "expense",
            "id": expense_id,
            "created_at": created_at,
            "paid_by": paid_by,
            "amount": amount
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
            created_at
        FROM settlements
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    for (
        settlement_id,
        from_user,
        to_user,
        amount,
        created_at
    ) in cursor.fetchall():

        operations.append({
            "type": "settlement",
            "id": settlement_id,
            "created_at": created_at,
            "from_user": from_user,
            "to_user": to_user,
            "amount": amount
        })

    # ====================================
    # Старые операции сначала
    # ====================================

    operations.sort(
        key=lambda item: (
            item["created_at"] or "",
            0 if item["type"] == "expense" else 1,
            item["id"]
        )
    )

    for operation in operations:

        is_target = (
            operation["type"] == target_type
            and
            operation["id"] == target_id
        )

        # =================================
        # РАСХОД
        # =================================

        if operation["type"] == "expense":

            expense_id = operation["id"]
            paid_by = operation["paid_by"]
            amount = operation["amount"]

            impact = {}

            impact[paid_by] = (
                impact.get(paid_by, 0)
                + amount
            )

            cursor.execute(
                """
                SELECT
                    user_id,
                    share_kopecks
                FROM expense_participants
                WHERE expense_id = ?
                """,
                (expense_id,)
            )

            for user_id, share in cursor.fetchall():

                impact[user_id] = (
                    impact.get(user_id, 0)
                    - share
                )

        # =================================
        # ПЕРЕВОД
        # =================================

        else:

            from_user = operation["from_user"]
            to_user = operation["to_user"]
            amount = operation["amount"]

            impact = {
                from_user: amount,
                to_user: -amount
            }

        # =================================
        # Если это нужная операция —
        # запоминаем было → стало
        # =================================

        if is_target:

            result = {}

            for user_id, change in impact.items():

                before = balances.get(
                    user_id,
                    0
                )

                after = before + change

                result[user_id] = (
                    before,
                    after
                )

            return result

        # =================================
        # Иначе применяем операцию
        # и идём дальше
        # =================================

        for user_id, change in impact.items():

            balances[user_id] = (
                balances.get(user_id, 0)
                + change
            )

    return {}

# ============================================================
# ЛИЧНЫЕ СООБЩЕНИЯ: ВЫБРАННАЯ ГРУППА
# ============================================================

def is_private(chat) -> bool:
    return chat.type == "private"


def get_group_title(chat_id: int) -> str:

    cursor.execute(
        """
        SELECT title
        FROM groups
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    result = cursor.fetchone()

    if result and result[0]:
        return result[0]

    return f"Группа {chat_id}"


def is_group_member(chat_id: int, user_id: int) -> bool:

    cursor.execute(
        """
        SELECT 1
        FROM group_members
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user_id)
    )

    return cursor.fetchone() is not None


def get_active_group_id(user_id: int):

    cursor.execute(
        """
        SELECT chat_id
        FROM user_active_group
        WHERE user_id = ?
        """,
        (user_id,)
    )

    result = cursor.fetchone()

    if not result:
        return None

    chat_id = result[0]

    # Пользователь мог перестать быть
    # участником этой группы
    if not is_group_member(chat_id, user_id):
        return None

    return chat_id


def set_active_group_id(user_id: int, chat_id: int):

    cursor.execute(
        """
        INSERT OR REPLACE INTO user_active_group
        (user_id, chat_id)
        VALUES (?, ?)
        """,
        (user_id, chat_id)
    )

    db.commit()


async def is_in_telegram_group(
    chat_id: int,
    user_id: int
) -> bool:
    """
    Проверяет через Telegram, что и бот,
    и пользователь всё ещё состоят в группе.
    """

    try:

        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id
        )

    except TelegramAPIError:

        # Бота удалили из группы,
        # группа удалена или стала супергруппой
        return False

    if member.status == "restricted":
        return member.is_member

    return member.status not in ("left", "kicked")


async def get_user_groups(user_id: int):
    """
    Группы, в которых пользователь
    нажал «Я участвую» и где он до сих пор состоит.
    """

    cursor.execute(
        """
        SELECT gm.chat_id
        FROM group_members gm
        WHERE gm.user_id = ?
        """,
        (user_id,)
    )

    chat_ids = [
        row[0]
        for row in cursor.fetchall()
    ]

    groups = []

    for chat_id in chat_ids:

        if await is_in_telegram_group(chat_id, user_id):

            groups.append((
                chat_id,
                get_group_title(chat_id)
            ))

    groups.sort(
        key=lambda item: item[1].lower()
    )

    return groups


async def get_group_picker(user_id: int):
    """
    Возвращает текст и клавиатуру
    экрана выбора группы.
    """

    groups = await get_user_groups(user_id)

    if not groups:

        text = (
            "🤷 <b>Не нашёл групп, где ты участвуешь.</b>\n\n"
            "Добавь меня в Telegram-группу, отправь там "
            "/menu и нажми «🙋 Я участвую» — после этого "
            "группа появится здесь.\n\n"
            "Потом снова отправь мне /groups."
        )

        return text, None

    active_group_id = get_active_group_id(user_id)

    rows = []

    for chat_id, title in groups:

        icon = (
            "✅"
            if chat_id == active_group_id
            else "👥"
        )

        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {title}",
                callback_data=f"select_group:{chat_id}"
            )
        ])

    text = (
        "👥 <b>Выберите группу</b>\n\n"
        "Все действия в этом чате будут относиться "
        "к выбранной группе, пока вы её не смените."
    )

    return text, InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def get_menu_text(group_id: int, private: bool) -> str:

    text = "💸 <b>Общие расходы</b>\n\n"

    if private:
        text += (
            f"👥 Группа: "
            f"<b>{escape(get_group_title(group_id))}</b>\n\n"
        )

    text += (
        "Добавляйте расходы, следите за балансами "
        "и рассчитывайтесь друг с другом."
    )

    return text


class GroupContextMiddleware(BaseMiddleware):
    """
    Передаёт в обработчики group_id —
    группу, с которой идёт работа.

    В группе это сам чат.
    В личных сообщениях — группа,
    которую пользователь выбрал.
    """

    async def __call__(self, handler, event, data):

        if isinstance(event, CallbackQuery):
            chat = event.message.chat
        else:
            chat = event.chat

        if not is_private(chat):

            data["group_id"] = chat.id

            return await handler(event, data)

        group_id = get_active_group_id(
            event.from_user.id
        )

        data["group_id"] = group_id

        if (
            group_id is None
            and
            not get_flag(data, "without_group")
        ):

            await data["state"].clear()

            text, keyboard = await get_group_picker(
                event.from_user.id
            )

            if isinstance(event, CallbackQuery):

                await event.answer()

                await event.message.answer(
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            else:

                await event.answer(
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            return

        return await handler(event, data)


dp.message.middleware(GroupContextMiddleware())
dp.callback_query.middleware(GroupContextMiddleware())

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def get_main_keyboard(private: bool = False):

    if private:

        last_row = [
            InlineKeyboardButton(
                text="👥 Участники",
                callback_data="members"
            ),
            InlineKeyboardButton(
                text="🔄 Сменить группу",
                callback_data="switch_group"
            )
        ]

    else:

        last_row = [
            InlineKeyboardButton(
                text="👥 Участники",
                callback_data="members"
            ),
            InlineKeyboardButton(
                text="🙋 Я участвую",
                callback_data="join"
            )
        ]

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
            last_row
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

@dp.message(
    CommandStart(),
    flags={"without_group": True}
)
async def start_handler(
    message: Message,
    group_id: int | None
):

    if is_private(message.chat):

        if group_id is None:

            text, keyboard = await get_group_picker(
                message.from_user.id
            )

            await message.answer(
                "👋 Привет!\n\n"
                "Я бот для совместных расходов.\n"
                "Со мной можно работать и в группе, "
                "и здесь, в личных сообщениях.\n\n"
                + text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

            return

        await show_private_menu(message, group_id)

        return

    await show_menu(message)
# ============================================================
@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(
    callback: CallbackQuery,
    state: FSMContext,
    group_id: int
):

    await state.clear()

    private = is_private(callback.message.chat)

    await replace_screen(
        callback,
        get_menu_text(group_id, private),
        reply_markup=get_main_keyboard(private)
    )

    await callback.answer()
# ============================================================
# /MENU
# ============================================================

@dp.message(
    Command("menu"),
    flags={"without_group": True}
)
async def menu_handler(
    message: Message,
    group_id: int | None
):

    if is_private(message.chat):

        if group_id is None:

            text, keyboard = await get_group_picker(
                message.from_user.id
            )

            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

            return

        await show_private_menu(message, group_id)

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
        get_menu_text(message.chat.id, False),
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


async def show_private_menu(
    message: Message,
    group_id: int
):

    await message.answer(
        get_menu_text(group_id, True),
        reply_markup=get_main_keyboard(True),
        parse_mode="HTML"
    )


# ============================================================
# 👥 ВЫБОР ГРУППЫ В ЛИЧНЫХ СООБЩЕНИЯХ
# ============================================================

@dp.message(
    Command("groups"),
    flags={"without_group": True}
)
async def groups_command_handler(
    message: Message,
    state: FSMContext
):

    if not is_private(message.chat):

        await message.answer(
            "Выбор группы нужен только "
            "в личных сообщениях со мной."
        )

        return

    await state.clear()

    text, keyboard = await get_group_picker(
        message.from_user.id
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(
    F.data == "switch_group",
    flags={"without_group": True}
)
async def switch_group_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    text, keyboard = await get_group_picker(
        callback.from_user.id
    )

    await replace_screen(
        callback,
        text,
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("select_group:"),
    flags={"without_group": True}
)
async def select_group_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    if not is_private(callback.message.chat):

        await callback.answer()

        return

    chat_id = int(
        callback.data.split(":")[1]
    )

    user_id = callback.from_user.id

    if (
        not is_group_member(chat_id, user_id)
        or
        not await is_in_telegram_group(chat_id, user_id)
    ):

        await callback.answer(
            "Эта группа недоступна. "
            "Выберите другую.",
            show_alert=True
        )

        return

    set_active_group_id(user_id, chat_id)

    await state.clear()

    await replace_screen(
        callback,
        get_menu_text(chat_id, True),
        reply_markup=get_main_keyboard(True)
    )

    await callback.answer(
        f"Выбрана группа: {get_group_title(chat_id)}"
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
async def members_handler(
    callback: CallbackQuery,
    group_id: int
):

    chat_id = group_id

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
    state: FSMContext,
    group_id: int
):

    chat_id = group_id

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

    screen = await replace_screen(
        callback,
        "💳 <b>Кто заплатил?</b>",
        reply_markup=get_payer_keyboard(chat_id)
    )

    await state.update_data(
        flow_bot_message_ids=[
            screen.message_id
        ]
    )


# ============================================================
# ВЫБОР ТОГО, КТО ЗАПЛАТИЛ
# ============================================================

@dp.callback_query(
    ExpenseForm.choosing_payer,
    F.data.startswith("payer:")
)
async def choose_payer(
    callback: CallbackQuery,
    state: FSMContext,
    group_id: int
):

    payer_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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

    prompt = await callback.message.answer(
        f"💰 <b>Введите сумму расхода</b>\n\n"
        f"Заплатил: <b>{escape(payer_name)}</b>\n\n"
        f"Например:\n"
        f"<code>2500</code>\n"
        f"или\n"
        f"<code>2500,50</code>\n\n"
        f"🧮 Можно сразу посчитать: "
        f"<code>1200+350</code>, "
        f"<code>3000/4</code>",
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Например: 2500"
        )
    )

    await remember_flow_message(
        state,
        prompt
    )


# ============================================================
# ВВОД СУММЫ
# ============================================================

@dp.message(ExpenseForm.entering_amount)
async def enter_amount(
    message: Message,
    state: FSMContext,
    group_id: int
):

    amount_kopecks = parse_money(
        message.text or ""
    )

    if amount_kopecks is None:

        await message.answer(
            "❌ Не получилось распознать сумму.\n\n"
            "Введите положительное число "
            "или пример, который надо посчитать.\n"
            "Например: <code>2500</code>, "
            "<code>2500,50</code> "
            "или <code>1200+350</code>",
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

    prompt = await message.answer(
        f"💰 Сумма: "
        f"{format_entered_amount(message.text, amount_kopecks)}\n\n"
        "👥 <b>Между кем делим расход?</b>\n\n"
        "Нажмите на людей, которые участвовали "
        "в этом расходе.\n\n"
        "✅ — выбран\n"
        "⬜ — не выбран",
        parse_mode="HTML",
        reply_markup=get_participants_keyboard(
            group_id,
            []
        )
    )

    await remember_flow_message(
        state,
        prompt
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
    state: FSMContext,
    group_id: int
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
            group_id,
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
    state: FSMContext,
    group_id: int
):

    members = get_members(
        group_id
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
            group_id,
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

    prompt = await callback.message.answer(
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

    await remember_flow_message(
        state,
        prompt
    )


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
    expense_number = get_next_expense_number(
        chat_id
    )   
    
    cursor.execute(
        """
        INSERT INTO expenses
        (
            chat_id,
            local_number,
            paid_by,
            amount_kopecks,
            comment,
            created_by
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            expense_number,
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

    # Сообщения удаляем в том чате, где шёл диалог:
    # в личке он не совпадает с группой расхода.
    await cleanup_flow_messages(
        message.chat.id,
        state
    )

    await state.clear()


    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard(
            is_private(message.chat)
        )
    )

    


# ============================================================
# ❌ ОТМЕНА СОЗДАНИЯ РАСХОДА
# ============================================================

@dp.callback_query(F.data == "expense_cancel")
async def cancel_expense(
    callback: CallbackQuery,
    state: FSMContext
):
    
    await cleanup_flow_messages(
        callback.message.chat.id,
        state
    )

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
    group_id: int,
    page: int,
    edit_message: bool = False
):

    chat_id = group_id

    records = get_history_records(
        chat_id
    )

    # ====================================
    # История пустая
    # ====================================

    if not records:

        if edit_message:

            await callback.message.edit_text(
                "📜 История пока пустая.",
                reply_markup=get_back_to_main_keyboard()
            )

        else:

            await callback.message.answer(
                "📜 История пока пустая.",
                reply_markup=get_back_to_main_keyboard()
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
                f"#{record['number']} — "
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
                        f"🧾 #{record['number']} "
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
                f"#{record['number']}</b>\n"
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
                        f"💸 #{record['number']} "
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
    callback: CallbackQuery,
    group_id: int
):

    # В личке открываем историю в том же сообщении
    private = is_private(callback.message.chat)

    if not private:

        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

    await show_history_page(
        callback,
        group_id,
        page=0,
        edit_message=private
    )

    await callback.answer()

@dp.callback_query(
    F.data.startswith("history_page:")
)
async def history_page_handler(
    callback: CallbackQuery,
    group_id: int
):

    page = int(
        callback.data.split(":")[1]
    )

    await show_history_page(
        callback,
        group_id,
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
    callback: CallbackQuery,
    group_id: int
):

    chat_id = group_id

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
    callback: CallbackQuery,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

    cursor.execute(
        """
        SELECT
            local_number,
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
        local_number,
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
        f"🧾 <b>Расход #{local_number}</b>\n\n"
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

    changes = get_operation_balance_changes(
        chat_id,
        "expense",
        expense_id
    )

    text += (
        "\n📊 <b>Баланс: было → стало</b>\n"
    )

    for user_id, (
        before,
        after
    ) in changes.items():

        name = get_member_name(
            chat_id,
            user_id
        )

        text += (
            f"• {escape(name)}: "
            f"<b>{format_balance(before)}</b>"
            f" → "
            f"<b>{format_balance(after)}</b>\n"
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
    state: FSMContext,
    group_id: int
):

    await state.clear()

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

    cursor.execute(
        """
        SELECT
            local_number,
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

    local_number, paid_by, amount, comment = expense

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
        f"#{local_number}</b>\n\n"
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
    state: FSMContext,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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
        f"Например: <code>3500</code> "
        f"или <code>3000+500</code>",
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
            "Например: <code>3500</code> "
            "или <code>3000+500</code>",
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
        f"{format_entered_amount(message.text, amount)}\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(
            is_private(message.chat)
        )
    )
# ============================================================
# 💳 ИЗМЕНЕНИЕ ПЛАТЕЛЬЩИКА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_edit_payer:")
)
async def expense_edit_payer_handler(
    callback: CallbackQuery,
    state: FSMContext,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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
    state: FSMContext,
    group_id: int
):

    parts = callback.data.split(":")

    expense_id = int(parts[1])
    payer_id = int(parts[2])

    chat_id = group_id

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
        reply_markup=get_main_keyboard(
            is_private(callback.message.chat)
        )
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
    state: FSMContext,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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
    state: FSMContext,
    group_id: int
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
                group_id,
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
    state: FSMContext,
    group_id: int
):

    members = get_members(
        group_id
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
                group_id,
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
        reply_markup=get_main_keyboard(
            is_private(callback.message.chat)
        )
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
    state: FSMContext,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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
        reply_markup=get_main_keyboard(
            is_private(message.chat)
        )
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
        reply_markup=get_main_keyboard(
            is_private(callback.message.chat)
        )
    )

    await callback.answer()
# ============================================================
# 🗑 ЗАПРОС УДАЛЕНИЯ РАСХОДА
# ============================================================

@dp.callback_query(
    F.data.startswith("expense_delete:")
)
async def expense_delete_handler(
    callback: CallbackQuery,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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
    callback: CallbackQuery,
    group_id: int
):

    expense_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

    cursor.execute(
        """
        SELECT local_number
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
            "Расход уже удалён.",
            show_alert=True
        )
        return

    local_number = expense[0]

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
        f"🗑 <b>Расход #{local_number} удалён.</b>\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(
            is_private(callback.message.chat)
        )
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
    callback: CallbackQuery,
    group_id: int
):

    settlement_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

    cursor.execute(
        """
        SELECT
            local_number,
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
        local_number,
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
        f"#{local_number}</b>\n\n"
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

    changes = get_operation_balance_changes(
        chat_id,
        "settlement",
        settlement_id
    )

    text += (
        "\n\n📊 <b>Баланс: было → стало</b>\n"
    )

    for user_id, (
        before,
        after
    ) in changes.items():

        name = get_member_name(
            chat_id,
            user_id
        )

        text += (
            f"• {escape(name)}: "
            f"<b>{format_balance(before)}</b>"
            f" → "
            f"<b>{format_balance(after)}</b>\n"
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
    callback: CallbackQuery,
    group_id: int
):

    settlement_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

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
    callback: CallbackQuery,
    group_id: int
):

    settlement_id = int(
        callback.data.split(":")[1]
    )

    chat_id = group_id

    # Номер перевода в группе нужно узнать
    # до удаления, чтобы показать его в ответе
    cursor.execute(
        """
        SELECT local_number
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
        f"↩️ <b>Перевод #{settlement[0]} отменён.</b>\n\n"
        "📊 Балансы пересчитаны автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(
            is_private(callback.message.chat)
        )
    )

    await callback.answer(
        "Перевод отменён"
    )


# ============================================================
# 📊 БАЛАНСЫ
# ============================================================

@dp.callback_query(F.data == "balances")
async def balances_handler(
    callback: CallbackQuery,
    group_id: int
):

    chat_id = group_id

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
    callback: CallbackQuery,
    group_id: int
):

    chat_id = group_id

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
    callback: CallbackQuery,
    group_id: int
):

    parts = callback.data.split(":")

    from_user = int(parts[1])
    to_user = int(parts[2])
    amount = int(parts[3])

    chat_id = group_id

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
    settlement_number = get_next_settlement_number(
        chat_id
    )

    cursor.execute(
        """
        INSERT INTO settlements
        (
            chat_id,
            local_number,
            from_user,
            to_user,
            amount_kopecks,
            created_by
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            settlement_number,
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