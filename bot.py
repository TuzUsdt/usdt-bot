"""
Telegram-бот для учёта USDT-сделок и денежных движений.
Понимает:
  Продал Гиге 475600/76.262        — USDT-сделка
  Купил у Стефа 1020*74            — USDT-сделка
  Влад продал Клиенту 96000/75.7   — USDT-сделка с указанием исполнителя
  Откупили у Москвы 2375700/73.551 — USDT-сделка

  Выдал Владу 72 600               — кэш минус
  Принял от Адахана 729 100        — кэш плюс
  Забрали у Бабая 942 000          — кэш плюс
  Влад выдал клиенту 30 000        — кэш минус
  + 96000 от клиента               — кэш плюс
  - 72600 Владу                    — кэш минус

Можно несколько операций в одном сообщении — каждая на новой строке.
Можно с номерами (1. ... 2. ...).
"""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

# ────────────────── НАСТРОЙКИ ──────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(__file__).parent / "trades.db"

if not BOT_TOKEN:
    raise SystemExit(
        "❌ Не задан BOT_TOKEN.\n"
        "Создай файл .env рядом с bot.py и положи туда: BOT_TOKEN=твой_токен"
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)


# ────────────────── БАЗА ДАННЫХ ──────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # SQLite не умеет LOWER() для не-ASCII, регистрируем Python-функцию
    conn.create_function("pylower", 1, lambda s: s.lower() if s else "")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Создаёт схему и мигрирует старые сделки в новую таблицу transactions."""
    with db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        # Старая схема — оставляем для совместимости со старыми данными
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                ts            TEXT    NOT NULL,
                kind          TEXT    NOT NULL,
                counterparty  TEXT,
                usdt          REAL    NOT NULL,
                rate          REAL    NOT NULL,
                rubles        REAL    NOT NULL,
                raw_text      TEXT
            );
            CREATE TABLE IF NOT EXISTS state (
                chat_id        INTEGER PRIMARY KEY,
                ruble_balance  REAL DEFAULT 0,
                usdt_balance   REAL DEFAULT 0,
                extra_rubles   REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_trades_chat ON trades(chat_id, ts);
        """)

        # Новая схема: единая таблица transactions с глобальными ID
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id        INTEGER NOT NULL,
                ts             TEXT    NOT NULL,
                type           TEXT    NOT NULL,  -- 'sell' | 'buy' | 'cash_in' | 'cash_out'
                counterparty   TEXT,
                doer           TEXT,
                usdt           REAL    DEFAULT 0,
                rate           REAL    DEFAULT 0,
                rubles         REAL    NOT NULL,
                raw_text       TEXT,
                photo_file_id  TEXT,
                note           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tx_chat ON transactions(chat_id, id);
            CREATE INDEX IF NOT EXISTS idx_tx_party ON transactions(counterparty);
        """)

        # Однократная миграция старых сделок
        if version < 2:
            old_count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
            if old_count > 0:
                rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO transactions (chat_id, ts, type, counterparty, usdt, rate, rubles, raw_text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["chat_id"], r["ts"], r["kind"], r["counterparty"],
                         r["usdt"], r["rate"], r["rubles"], r["raw_text"])
                    )
                log.info(f"Перенёс {old_count} старых сделок в transactions.")
            conn.execute("PRAGMA user_version = 2")


def get_state(chat_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM state WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO state (chat_id) VALUES (?)", (chat_id,))
            return {"ruble_balance": 0.0, "usdt_balance": 0.0, "extra_rubles": 0.0}
        return dict(row)


def update_state(chat_id: int, d_rubles: float = 0, d_usdt: float = 0):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO state (chat_id) VALUES (?)", (chat_id,))
        conn.execute(
            "UPDATE state SET ruble_balance = ruble_balance + ?, "
            "usdt_balance = usdt_balance + ? WHERE chat_id = ?",
            (d_rubles, d_usdt, chat_id)
        )


def save_tx(chat_id, type_, counterparty, rubles, usdt=0, rate=0, raw="", doer=None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (chat_id, ts, type, counterparty, doer, usdt, rate, rubles, raw_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, datetime.utcnow().isoformat(), type_, counterparty, doer,
             usdt, rate, rubles, raw)
        )
        return cur.lastrowid


def get_avg_rates(chat_id: int) -> dict:
    with db() as conn:
        sell = conn.execute(
            "SELECT COALESCE(SUM(rubles),0) r, COALESCE(SUM(usdt),0) u "
            "FROM transactions WHERE chat_id = ? AND type = 'sell'",
            (chat_id,)).fetchone()
        buy = conn.execute(
            "SELECT COALESCE(SUM(rubles),0) r, COALESCE(SUM(usdt),0) u "
            "FROM transactions WHERE chat_id = ? AND type = 'buy'",
            (chat_id,)).fetchone()
    return {
        "avg_sell":          (sell["r"] / sell["u"]) if sell["u"] else None,
        "avg_buy":           (buy["r"]  / buy["u"])  if buy["u"]  else None,
        "total_sold_usdt":   sell["u"],
        "total_bought_usdt": buy["u"],
        "total_received":    sell["r"],
        "total_spent":       buy["r"],
    }


# ────────────────── ПАРСИНГ ──────────────────
NUM_PATTERN = r"\d[\d\s\u00a0.,]*\d|\d"

SELL_VERBS = ("продал", "продала", "продали", "продаю", "продают")
BUY_VERBS = ("купил", "купила", "купили", "куплю", "покупаю",
             "откупил", "откупила", "откупили")

CASH_OUT_VERBS = ("выдал", "выдала", "выдали", "выдавал", "выдавали",
                  "отдал", "отдала", "отдали", "отдавал", "отдавали",
                  "перевёл", "перевел", "перевела", "перевели",
                  "потратил", "потратила", "потратили")
CASH_IN_VERBS = ("принял", "приняла", "приняли", "принимал", "принимали",
                 "забрал", "забрала", "забрали", "забирал", "забирали",
                 "получил", "получила", "получили", "получал", "получали")


def parse_number(s: str, prefer_decimal: bool = False) -> float:
    """
    Парсит '72 600' → 72600, '85.000' → 85000, '73.5' → 73.5,
    '1 142 400' → 1142400, '75,6' → 75.6, '1.000.000' → 1000000.

    prefer_decimal=True — для курсов: запятая/точка всегда десятичные
    (например, '73,551' → 73.551, а не 73551).
    """
    s = s.strip().replace(" ", "").replace("\u00a0", "")
    s = s.rstrip("₽рp$ ").strip()
    if not s:
        raise ValueError("empty")

    if "." in s and "," in s:
        # Оба разделителя: точка — тысячи, запятая — дробная часть
        s = s.replace(".", "").replace(",", ".")
    elif prefer_decimal:
        # Для курсов — единственный разделитель всегда десятичный
        s = s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if (all(len(p) == 3 and p.isdigit() for p in parts[1:])
                and 1 <= len(parts[0]) <= 3 and parts[0].isdigit()):
            s = s.replace(".", "")
    elif "," in s:
        parts = s.split(",")
        if (all(len(p) == 3 and p.isdigit() for p in parts[1:])
                and 1 <= len(parts[0]) <= 3 and parts[0].isdigit()):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")

    return float(s)


def _find_verb(lower: str, verb_groups: list) -> tuple:
    """Ищет глагол. Возвращает (kind, start, end) или (None, None, None)."""
    best = None
    for kind, verbs in verb_groups:
        for v in verbs:
            i = lower.find(v)
            if i < 0 or i > 30:
                continue
            # Префикс до глагола: либо пусто, либо одно слово (исполнитель)
            before = lower[:i].strip()
            if before and " " in before:
                continue
            end = i + len(v)
            before_ok = (i == 0) or not lower[i - 1].isalpha()
            after_ok = (end == len(lower)) or not lower[end].isalpha()
            if before_ok and after_ok:
                if best is None or i < best[1]:
                    best = (kind, i, end)
    return best if best else (None, None, None)


def parse_trade(text: str):
    """USDT-сделка. Возвращает dict или None."""
    text = text.strip().lstrip("+-—– ").strip()
    lower = text.lower()

    kind, vs, ve = _find_verb(lower, [("sell", SELL_VERBS), ("buy", BUY_VERBS)])
    if not kind:
        return None

    doer = text[:vs].strip() or None
    rest = text[ve:].lstrip()
    for prep in ("у ", "у\u00a0", "от ", "от\u00a0"):
        if rest.lower().startswith(prep):
            rest = rest[len(prep):].lstrip()
            break

    m = re.search(rf"({NUM_PATTERN})\s*([*x×/÷:])\s*({NUM_PATTERN})", rest)
    if not m:
        return None

    counterparty = rest[:m.start()].strip().rstrip("—-:,.").strip() or "—"
    try:
        op = m.group(2)
        if op in "*x×":
            # Слева USDT, справа курс. Курс — всегда десятичное.
            a = parse_number(m.group(1))
            b = parse_number(m.group(3), prefer_decimal=True)
            usdt, rate, rubles = a, b, a * b
        else:
            # Слева рубли, справа курс. Курс — всегда десятичное.
            a = parse_number(m.group(1))
            b = parse_number(m.group(3), prefer_decimal=True)
            rubles, rate, usdt = a, b, a / b
    except (ValueError, ZeroDivisionError):
        return None

    if rate <= 0 or usdt <= 0 or rubles <= 0:
        return None

    return {
        "type": kind,
        "doer": doer,
        "counterparty": counterparty,
        "usdt": round(usdt, 4),
        "rate": rate,
        "rubles": round(rubles, 2),
    }


def parse_cash_flow(text: str):
    """Кэш-движение (только рубли). Возвращает dict или None."""
    text = text.strip()

    explicit_dir = None
    m_sign = re.match(r"^([+\-—–])\s*", text)
    if m_sign:
        explicit_dir = "cash_out" if m_sign.group(1) in "-—–" else "cash_in"
        text = text[m_sign.end():].strip()

    lower = text.lower()

    kind, vs, ve = _find_verb(lower, [
        ("cash_in", CASH_IN_VERBS),
        ("cash_out", CASH_OUT_VERBS),
    ])

    if not kind:
        # Без глагола, но с явным знаком: "+ 96000 от клиента" / "- 72600 Владу"
        if not explicit_dir:
            return None
        m_amt = re.search(NUM_PATTERN, text)
        if not m_amt:
            return None
        try:
            amount = parse_number(m_amt.group())
        except ValueError:
            return None
        if amount <= 0:
            return None
        before = text[:m_amt.start()].strip(" ,:.")
        after = text[m_amt.end():].strip(" ,:.")
        for prep in ("от ", "у ", "из ", "к ", "для "):
            if after.lower().startswith(prep):
                after = after[len(prep):].strip()
                break
        counterparty = (before + " " + after).strip().strip(",:.") or "—"
        return {"type": explicit_dir, "doer": None, "counterparty": counterparty, "amount": amount}

    direction = kind  # глагол приоритетнее знака
    doer = text[:vs].strip() or None
    rest = text[ve:].lstrip()
    for prep in ("у ", "у\u00a0", "от ", "от\u00a0", "из ", "к ", "для "):
        if rest.lower().startswith(prep):
            rest = rest[len(prep):].lstrip()
            break

    matches = list(re.finditer(NUM_PATTERN, rest))
    if not matches:
        return None
    last_m = matches[-1]
    try:
        amount = parse_number(last_m.group())
    except ValueError:
        return None
    if amount <= 0:
        return None

    target = rest[:last_m.start()].strip().rstrip(",:.").rstrip().rstrip("—-").strip() or "—"
    return {"type": direction, "doer": doer, "counterparty": target, "amount": amount}


def parse_message_line(line: str):
    """Парсит одну строку. Сначала пробует сделку, потом кэш-движение."""
    line = line.strip()
    # Снять префиксную нумерацию "1." или "1)"
    line = re.sub(r"^\d+\s*[\.\)]\s*", "", line).strip()
    if not line:
        return None

    t = parse_trade(line)
    if t:
        return t
    c = parse_cash_flow(line)
    if c:
        return c
    return None


# ────────────────── ФОРМАТИРОВАНИЕ ──────────────────
def fmt_rub(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    s = f"{n:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return f"{sign}{s}₽"


def fmt_usdt(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    s = f"{n:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return f"{sign}{s} USDT"


def fmt_rate(n: float) -> str:
    s = f"{n:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def fmt_id(i: int) -> str:
    return f"#{i:03d}"


def fmt_tx_short(r) -> str:
    tid = fmt_id(r["id"])
    cp = r["counterparty"] or "—"
    t = r["type"]
    if t == "sell":
        return f"{tid} 📤 Продал {cp}: {fmt_usdt(r['usdt'])} × {fmt_rate(r['rate'])} = {fmt_rub(r['rubles'])}"
    if t == "buy":
        return f"{tid} 📥 Купил у {cp}: {fmt_usdt(r['usdt'])} × {fmt_rate(r['rate'])} = {fmt_rub(r['rubles'])}"
    if t == "cash_out":
        return f"{tid} 💸 Выдал {cp}: −{fmt_rub(r['rubles'])}"
    if t == "cash_in":
        return f"{tid} 💰 Принял {cp}: +{fmt_rub(r['rubles'])}"
    return f"{tid} {t}: {fmt_rub(r['rubles'])}"


# ────────────────── БОТ ──────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HELP_TEXT = (
    "👋 Я веду учёт USDT-сделок и денежных движений.\n\n"
    "📝 *USDT-сделки* (меняют ₽ и USDT):\n"
    "`Продал Гиге 475600/76.262`\n"
    "`Купил у Стефа 1020*74`\n"
    "`Влад продал Клиенту 96000/75.7`\n"
    "Правило: `*` слева USDT, `/` слева рубли.\n\n"
    "💵 *Кэш-движения* (меняют только ₽):\n"
    "`Выдал Владу 72 600`\n"
    "`Принял от Адахана 729 100`\n"
    "`Забрали у Бабая 942 000`\n"
    "`+ 96000 от клиента`\n"
    "`- 72600 Владу`\n\n"
    "Можно несколько операций в одном сообщении — каждая на новой строке. "
    "Бот игнорирует нумерацию `1.`/`2.` в начале.\n\n"
    "📊 *Команды:*\n"
    "/balance — касса и USDT\n"
    "/stats — средние курсы, спред, прибыль\n"
    "/history — последние 15 операций\n"
    "/cashflow — последние 20 кэш-движений\n"
    "/find — поиск по ID, имени или сумме\n"
    "/undo — отменить последнюю операцию\n"
    "/setcash — задать стартовую кассу\n"
    "/help — эта памятка"
)


@dp.message(CommandStart())
@dp.message(Command("help"))
async def on_start(message: Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@dp.message(Command("balance"))
async def on_balance(message: Message):
    s = get_state(message.chat.id)
    text = f"💰 *Касса:* {fmt_rub(s['ruble_balance'])}\n💵 *Кошелёк:* {fmt_usdt(s['usdt_balance'])}"
    if s["extra_rubles"]:
        text += f"\n♻️ *Лишние:* {fmt_rub(s['extra_rubles'])}"
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("stats"))
async def on_stats(message: Message):
    r = get_avg_rates(message.chat.id)
    s = get_state(message.chat.id)

    if not r["avg_sell"] and not r["avg_buy"]:
        await message.answer("USDT-сделок ещё нет.")
        return

    lines = ["📊 *Статистика USDT-сделок*\n"]
    if r["avg_sell"]:
        lines.append(f"📈 Средний курс ПРОДАЖИ: *{fmt_rate(r['avg_sell'])}*")
        lines.append(f"   Всего продано: {fmt_usdt(r['total_sold_usdt'])} → {fmt_rub(r['total_received'])}")
    if r["avg_buy"]:
        lines.append(f"📉 Средний курс ПОКУПКИ: *{fmt_rate(r['avg_buy'])}*")
        lines.append(f"   Всего куплено: {fmt_usdt(r['total_bought_usdt'])} за {fmt_rub(r['total_spent'])}")

    if r["avg_sell"] and r["avg_buy"]:
        spread = r["avg_sell"] - r["avg_buy"]
        realized = (r["avg_sell"] - r["avg_buy"]) * min(r["total_sold_usdt"], r["total_bought_usdt"])
        lines.append(f"\n💹 Спред: *{spread:+.3f}* ₽/USDT")
        lines.append(f"💵 Реализованная прибыль ≈ *{fmt_rub(realized)}*")
        lines.append("\n⚠️ Чтобы не уйти в минус:")
        lines.append(f"   • Покупай НЕ дороже *{fmt_rate(r['avg_sell'])}*")
        lines.append(f"   • Продавай НЕ дешевле *{fmt_rate(r['avg_buy'])}*")

    lines.append(f"\n💰 Касса: {fmt_rub(s['ruble_balance'])}")
    lines.append(f"💵 Кошелёк: {fmt_usdt(s['usdt_balance'])}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("history"))
async def on_history(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? ORDER BY id DESC LIMIT 15",
            (message.chat.id,)).fetchall()
    if not rows:
        await message.answer("Операций ещё нет.")
        return
    lines = ["📜 *Последние операции:*\n"]
    for r in rows:
        lines.append(fmt_tx_short(r))
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("cashflow"))
async def on_cashflow(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? "
            "AND type IN ('cash_in', 'cash_out') ORDER BY id DESC LIMIT 20",
            (message.chat.id,)).fetchall()
    if not rows:
        await message.answer("Кэш-движений ещё нет.")
        return
    lines = ["💵 *Последние кэш-движения:*\n"]
    for r in rows:
        sign = "−" if r["type"] == "cash_out" else "+"
        cp = r["counterparty"] or "—"
        lines.append(f"{fmt_id(r['id'])} {sign}{fmt_rub(r['rubles'])} — {cp}")
    s = get_state(message.chat.id)
    lines.append(f"\n💰 Текущая касса: *{fmt_rub(s['ruble_balance'])}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("find"))
async def on_find(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: `/find <ID | имя | сумма>`\n\n"
            "Примеры:\n"
            "`/find #142` — найти операцию по ID\n"
            "`/find Адахан` — все операции с этим контрагентом\n"
            "`/find 96000` — все операции на эту сумму",
            parse_mode="Markdown")
        return
    query = parts[1].strip()

    # Поиск по ID (глобальный, чтобы можно было сверять между чатами)
    id_match = re.match(r"^#?(\d+)$", query.replace(" ", ""))
    if id_match:
        tx_id = int(id_match.group(1))
        with db() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not row:
            await message.answer(f"Операция #{tx_id:03d} не найдена.")
            return
        same_chat = row["chat_id"] == message.chat.id
        note = "" if same_chat else f"\n_(операция из другого чата, ID={row['chat_id']})_"
        await message.answer(f"🔍 *Найдено:*\n\n{fmt_tx_short(row)}{note}", parse_mode="Markdown")
        return

    # Поиск по сумме (внутри чата)
    try:
        amount = parse_number(query)
        if amount >= 100:
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE chat_id = ? "
                    "AND ABS(rubles - ?) < 0.5 ORDER BY id DESC LIMIT 15",
                    (message.chat.id, amount)).fetchall()
            if rows:
                out = [f"🔍 *Найдено {len(rows)} операций на {fmt_rub(amount)}:*\n"]
                out.extend(fmt_tx_short(r) for r in rows)
                await message.answer("\n".join(out), parse_mode="Markdown")
                return
    except (ValueError, TypeError):
        pass

    # Поиск по контрагенту (внутри чата, регистронезависимо)
    pattern = f"%{query.lower()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? "
            "AND pylower(counterparty) LIKE ? ORDER BY id DESC LIMIT 15",
            (message.chat.id, pattern)).fetchall()
    if not rows:
        await message.answer(f"По запросу «{query}» ничего не нашёл.")
        return
    out = [f"🔍 *Найдено {len(rows)} операций по «{query}»:*\n"]
    out.extend(fmt_tx_short(r) for r in rows)
    await message.answer("\n".join(out), parse_mode="Markdown")


@dp.message(Command("undo"))
async def on_undo(message: Message):
    chat_id = message.chat.id
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,)).fetchone()
        if not row:
            await message.answer("Отменять нечего.")
            return
        conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))

    t = row["type"]
    if t == "sell":
        update_state(chat_id, d_rubles=-row["rubles"], d_usdt=row["usdt"])
    elif t == "buy":
        update_state(chat_id, d_rubles=row["rubles"], d_usdt=-row["usdt"])
    elif t == "cash_in":
        update_state(chat_id, d_rubles=-row["rubles"])
    elif t == "cash_out":
        update_state(chat_id, d_rubles=row["rubles"])

    await message.answer(f"↩️ Отменил {fmt_id(row['id'])}: `{row['raw_text']}`", parse_mode="Markdown")


@dp.message(Command("reset"))
async def on_reset(message: Message):
    with db() as conn:
        conn.execute("DELETE FROM transactions WHERE chat_id = ?", (message.chat.id,))
        conn.execute(
            "UPDATE state SET ruble_balance=0, usdt_balance=0, extra_rubles=0 WHERE chat_id = ?",
            (message.chat.id,))
    await message.answer("🗑 Всё обнулил.")


@dp.message(Command("setcash"))
async def on_setcash(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Использование: `/setcash <рубли> [USDT]`\nНапример: `/setcash 4348000 1500`",
            parse_mode="Markdown")
        return
    try:
        rub = parse_number(parts[1])
        usdt = parse_number(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        await message.answer("Не понял числа. Пример: `/setcash 4348000 1500`", parse_mode="Markdown")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO state (chat_id, ruble_balance, usdt_balance) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET ruble_balance = excluded.ruble_balance, "
            "usdt_balance = excluded.usdt_balance",
            (message.chat.id, rub, usdt))
    await message.answer(f"✅ Касса: {fmt_rub(rub)}\n💵 Кошелёк: {fmt_usdt(usdt)}")


@dp.message(Command("extra"))
async def on_extra(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: `/extra <сумма>`", parse_mode="Markdown")
        return
    try:
        val = parse_number(parts[1])
    except ValueError:
        await message.answer("Не понял число.")
        return
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO state (chat_id) VALUES (?)", (message.chat.id,))
        conn.execute("UPDATE state SET extra_rubles = ? WHERE chat_id = ?", (val, message.chat.id))
    await message.answer(f"♻️ Лишние: {fmt_rub(val)}")


@dp.message(F.text)
async def on_text(message: Message):
    chat_id = message.chat.id

    parsed_items = []
    for line in message.text.split("\n"):
        p = parse_message_line(line)
        if p:
            parsed_items.append((p, line.strip()))

    if not parsed_items:
        low = message.text.lower().strip()
        triggers = ("продал", "продала", "купил", "купила", "откупил",
                    "выдал", "выдала", "выдали", "отдал", "отдали",
                    "принял", "приняла", "приняли", "забрал", "забрали")
        if any(t in low for t in triggers):
            await message.reply(
                "Не понял формат. Примеры:\n"
                "`Продал Гиге 100*76`\n"
                "`Купил у Васи 75000/74`\n"
                "`Выдал Владу 72600`\n"
                "`Принял от Адахана 96000`",
                parse_mode="Markdown")
        return

    initial = get_state(chat_id)
    saved = []
    for p, raw in parsed_items:
        t = p["type"]
        if t == "sell":
            tid = save_tx(chat_id, "sell", p["counterparty"], p["rubles"],
                          usdt=p["usdt"], rate=p["rate"], raw=raw, doer=p.get("doer"))
            update_state(chat_id, d_rubles=p["rubles"], d_usdt=-p["usdt"])
        elif t == "buy":
            tid = save_tx(chat_id, "buy", p["counterparty"], p["rubles"],
                          usdt=p["usdt"], rate=p["rate"], raw=raw, doer=p.get("doer"))
            update_state(chat_id, d_rubles=-p["rubles"], d_usdt=p["usdt"])
        elif t == "cash_in":
            tid = save_tx(chat_id, "cash_in", p["counterparty"], p["amount"],
                          raw=raw, doer=p.get("doer"))
            update_state(chat_id, d_rubles=p["amount"])
        elif t == "cash_out":
            tid = save_tx(chat_id, "cash_out", p["counterparty"], p["amount"],
                          raw=raw, doer=p.get("doer"))
            update_state(chat_id, d_rubles=-p["amount"])
        else:
            continue
        saved.append((tid, p))

    final = get_state(chat_id)

    if len(saved) == 1:
        tid, p = saved[0]
        t = p["type"]
        if t in ("sell", "buy"):
            verb = "Продал" if t == "sell" else "Купил у"
            op_sign = "+" if t == "sell" else "−"
            doer_str = f" ({p['doer']})" if p.get("doer") else ""
            reply = [
                f"{fmt_id(tid)} ✅ {verb} *{p['counterparty']}*{doer_str}",
                f"   {fmt_usdt(p['usdt'])} × {fmt_rate(p['rate'])} = {fmt_rub(p['rubles'])}",
                "",
                f"💰 Касса: {fmt_rub(initial['ruble_balance'])} {op_sign} {fmt_rub(p['rubles'])} = *{fmt_rub(final['ruble_balance'])}*",
                f"💵 Кошелёк: *{fmt_usdt(final['usdt_balance'])}*",
            ]
            avgs = get_avg_rates(chat_id)
            if t == "buy" and avgs["avg_sell"]:
                if p["rate"] >= avgs["avg_sell"]:
                    reply.append(f"\n⚠️ Курс {fmt_rate(p['rate'])} ≥ средн. продажи {fmt_rate(avgs['avg_sell'])} — это минус!")
                else:
                    margin = avgs["avg_sell"] - p["rate"]
                    reply.append(f"\n👍 Ниже средн. продажи на {margin:.2f}₽. Профит ≈ *{fmt_rub(margin * p['usdt'])}*")
            elif t == "sell" and avgs["avg_buy"]:
                if p["rate"] <= avgs["avg_buy"]:
                    reply.append(f"\n⚠️ Курс {fmt_rate(p['rate'])} ≤ средн. покупки {fmt_rate(avgs['avg_buy'])} — это минус!")
                else:
                    margin = p["rate"] - avgs["avg_buy"]
                    reply.append(f"\n👍 Выше средн. покупки на {margin:.2f}₽. Профит ≈ *{fmt_rub(margin * p['usdt'])}*")
            await message.reply("\n".join(reply), parse_mode="Markdown")
        else:
            sign = "+" if t == "cash_in" else "−"
            verb = "Принял" if t == "cash_in" else "Выдал"
            doer_str = f" ({p['doer']})" if p.get("doer") else ""
            op_sign = "+" if t == "cash_in" else "−"
            reply = [
                f"{fmt_id(tid)} ✅ {verb} *{p['counterparty']}*{doer_str}",
                f"   {sign}{fmt_rub(p['amount'])}",
                "",
                f"💰 Касса: {fmt_rub(initial['ruble_balance'])} {op_sign} {fmt_rub(p['amount'])} = *{fmt_rub(final['ruble_balance'])}*",
            ]
            await message.reply("\n".join(reply), parse_mode="Markdown")
    else:
        # Несколько операций в одном сообщении
        lines = [f"✅ *Записано {len(saved)} операций:*\n"]
        d_rub = 0.0
        d_usdt = 0.0
        for tid, p in saved:
            t = p["type"]
            if t == "sell":
                lines.append(f"{fmt_id(tid)} 📤 Продал {p['counterparty']}: +{fmt_rub(p['rubles'])} / −{fmt_usdt(p['usdt'])}")
                d_rub += p["rubles"]; d_usdt -= p["usdt"]
            elif t == "buy":
                lines.append(f"{fmt_id(tid)} 📥 Купил у {p['counterparty']}: −{fmt_rub(p['rubles'])} / +{fmt_usdt(p['usdt'])}")
                d_rub -= p["rubles"]; d_usdt += p["usdt"]
            elif t == "cash_in":
                lines.append(f"{fmt_id(tid)} 💰 Принял {p['counterparty']}: +{fmt_rub(p['amount'])}")
                d_rub += p["amount"]
            elif t == "cash_out":
                lines.append(f"{fmt_id(tid)} 💸 Выдал {p['counterparty']}: −{fmt_rub(p['amount'])}")
                d_rub -= p["amount"]
        lines.append(f"\n📊 Итого по ₽: {('+' if d_rub >= 0 else '−')}{fmt_rub(abs(d_rub))}")
        if abs(d_usdt) > 1e-6:
            lines.append(f"📊 Итого по USDT: {('+' if d_usdt >= 0 else '−')}{fmt_usdt(abs(d_usdt))}")
        lines.append(f"\n💰 Касса: {fmt_rub(initial['ruble_balance'])} → *{fmt_rub(final['ruble_balance'])}*")
        if abs(d_usdt) > 1e-6:
            lines.append(f"💵 Кошелёк: *{fmt_usdt(final['usdt_balance'])}*")
        await message.reply("\n".join(lines), parse_mode="Markdown")


# ────────────────── ЗАПУСК ──────────────────
async def main():
    init_db()
    log.info("Бот запущен. Жду сообщения…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
