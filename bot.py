"""
Telegram-бот для учёта USDT-сделок, кэш-движений и арбитражных сделок.

УМЕЕТ ПОНИМАТЬ:
  USDT-сделки:
    Продал Гиге 475600/76.262
    Купил у Стефа 1020*74
    Влад продал Клиенту 96000/75.7

  Кэш-движения (только рубли):
    Выдал Владу 72 600
    Принял от Адахана 729 100
    + 96000 от клиента
    - 72600 Владу

  Арбитраж (сделка без своих средств):
    Сделка без моих средств
    Купил у Германа 196500/75=2620
    Продал Стефу 196500/75.5=2602,649
  → Профит +17.35 USDT, касса не меняется.

ПОДТВЕРЖДЕНИЕ:
  После сделки бот показывает кнопки [✅ Записать] [❌ Отмена].
  По умолчанию подтверждаем USDT-сделки и арбитраж.
  Кэш-движения по умолчанию записываются сразу (можно изменить через /confirm).

ТИПЫ ЧАТОВ (/settype):
  main   — личный чат: все операции
  field  — чат сотрудника (Влада): сделки в поле
  common — общая касса: приходы/расходы
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
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

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

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id        INTEGER NOT NULL,
                ts             TEXT    NOT NULL,
                type           TEXT    NOT NULL,
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

        # v2: миграция из старой trades
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

        # v3: добавляем status, batch_id, и поля для арбитража
        if version < 3:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
            if "status" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN status TEXT DEFAULT 'confirmed'")
            if "batch_id" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN batch_id INTEGER")
            # Поля для арбитража: что получили / что отдали в USDT
            if "usdt_in" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN usdt_in REAL DEFAULT 0")
            if "usdt_out" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN usdt_out REAL DEFAULT 0")
            if "partner_in" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN partner_in TEXT")
            if "partner_out" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN partner_out TEXT")

            # Все старые транзакции — confirmed
            conn.execute("UPDATE transactions SET status = 'confirmed' WHERE status IS NULL")
            conn.execute("PRAGMA user_version = 3")
            log.info("Применил миграцию v3 (status, batch_id, поля арбитража).")

        # v4: типы долгов добавлены в код (loan_out, loan_in, debt_repay_in, debt_repay_out),
        # отдельной схемы не нужно — используется та же transactions с новыми type.
        if version < 4:
            conn.execute("PRAGMA user_version = 4")
            log.info("Применил миграцию v4 (типы долгов).")

        # Настройки чата (тип, режим подтверждения)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id       INTEGER PRIMARY KEY,
                chat_type     TEXT DEFAULT 'main',     -- 'main' | 'field' | 'common'
                confirm_mode  TEXT DEFAULT 'trades'    -- 'all' | 'trades' | 'big' | 'off'
            );
        """)


# ───── Состояние кассы/кошелька (только для confirmed транзакций) ─────
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


# ───── Настройки чата ─────
def get_chat_settings(chat_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
            return {"chat_id": chat_id, "chat_type": "main", "confirm_mode": "trades"}
        return dict(row)


def set_chat_setting(chat_id: int, **kw):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
        for k, v in kw.items():
            if k in ("chat_type", "confirm_mode"):
                conn.execute(f"UPDATE chat_settings SET {k} = ? WHERE chat_id = ?", (v, chat_id))


# ───── Сохранение транзакций ─────
def save_tx(chat_id, type_, counterparty, rubles, *, usdt=0, rate=0, raw="",
            doer=None, status="confirmed", batch_id=None,
            usdt_in=0, usdt_out=0, partner_in=None, partner_out=None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO transactions "
            "(chat_id, ts, type, counterparty, doer, usdt, rate, rubles, raw_text, "
            " status, batch_id, usdt_in, usdt_out, partner_in, partner_out) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, datetime.utcnow().isoformat(), type_, counterparty, doer,
             usdt, rate, rubles, raw, status, batch_id,
             usdt_in, usdt_out, partner_in, partner_out)
        )
        return cur.lastrowid


def get_avg_rates(chat_id: int) -> dict:
    """Средневзвешенные курсы — только по confirmed транзакциям."""
    with db() as conn:
        sell = conn.execute(
            "SELECT COALESCE(SUM(rubles),0) r, COALESCE(SUM(usdt),0) u "
            "FROM transactions WHERE chat_id = ? AND type = 'sell' AND status = 'confirmed'",
            (chat_id,)).fetchone()
        buy = conn.execute(
            "SELECT COALESCE(SUM(rubles),0) r, COALESCE(SUM(usdt),0) u "
            "FROM transactions WHERE chat_id = ? AND type = 'buy' AND status = 'confirmed'",
            (chat_id,)).fetchone()
        arb_profit = conn.execute(
            "SELECT COALESCE(SUM(usdt),0) p FROM transactions "
            "WHERE chat_id = ? AND type = 'arb' AND status = 'confirmed'",
            (chat_id,)).fetchone()
    return {
        "avg_sell":          (sell["r"] / sell["u"]) if sell["u"] else None,
        "avg_buy":           (buy["r"]  / buy["u"])  if buy["u"]  else None,
        "total_sold_usdt":   sell["u"],
        "total_bought_usdt": buy["u"],
        "total_received":    sell["r"],
        "total_spent":       buy["r"],
        "arb_profit_usdt":   arb_profit["p"],
    }


# ───── Долги ─────
def name_root(name: str) -> str:
    """
    Возвращает нормализованный «корень» имени для группировки долгов.
    Русские имена меняются по падежам («Михаил» → «Михаилу» → «Михаила»),
    бот считал бы их разными людьми. Решение — брать первые 3 буквы.

    Стратегия:
      • Латиница / цифры — возвращаем как есть в lowercase (MTX, Mtex).
      • Русское слово — первые 3 буквы в lowercase.
        («Михаил» / «Михаила» / «Михаилу» → "мих";
         «Вася»   / «Васи»    / «Васе»    → "вас";
         «Анна»   / «Анной»   / «Анне»    → "анн";
         «Юра»    / «Юры»     / «Юре»     → "юра".)

    Компромисс: разные имена с одинаковыми первыми 3 буквами схлопнутся
    («Михаил» и «Михей» оба → «мих», «Иван» и «Ивановский» оба → «ива»).
    Если такое случается на практике — увидишь в /debts и переименуешь одного
    контрагента, например на «Михей-Москва» — корень станет другим.
    """
    if not name:
        return ""
    first_word = re.split(r"[\s\(\),]+", name.strip())[0]
    if not first_word:
        return ""
    lower = first_word.lower()

    # Латиница/смешанное — не трогаем
    if not any("а" <= ch <= "я" or ch == "ё" for ch in lower):
        return lower

    # Русское — первые 3 буквы (или всё слово если оно короче)
    return lower[:3]


def get_debts(chat_id: int) -> dict:
    """
    Считает открытые долги по этому чату.
    Имена группируются через name_root, чтобы «Михаил» и «Михаилу» считались
    одним человеком.

    Возвращает {
        "owed_to_us": [(name, amount), ...],
        "we_owe":     [(name, amount), ...],
        "total_owed_to_us": float,
        "total_we_owe": float,
    }
    """
    with db() as conn:
        # Все долговые транзакции
        all_loan_rows = conn.execute(
            "SELECT id, type, counterparty, rubles FROM transactions "
            "WHERE chat_id = ? AND status = 'confirmed' "
            "AND type IN ('loan_out', 'loan_in', 'debt_repay_in', 'debt_repay_out') "
            "ORDER BY id",
            (chat_id,)).fetchall()

    # Агрегируем по name_root
    owed_acc = {}  # root → {"bal": float, "display_name": str}
    we_owe_acc = {}
    for r in all_loan_rows:
        cp = r["counterparty"] or "—"
        root = name_root(cp)
        if r["type"] == "loan_out":
            d = owed_acc.setdefault(root, {"bal": 0.0, "display_name": cp})
            d["bal"] += r["rubles"]; d["display_name"] = cp
        elif r["type"] == "debt_repay_in":
            d = owed_acc.setdefault(root, {"bal": 0.0, "display_name": cp})
            d["bal"] -= r["rubles"]
        elif r["type"] == "loan_in":
            d = we_owe_acc.setdefault(root, {"bal": 0.0, "display_name": cp})
            d["bal"] += r["rubles"]; d["display_name"] = cp
        elif r["type"] == "debt_repay_out":
            d = we_owe_acc.setdefault(root, {"bal": 0.0, "display_name": cp})
            d["bal"] -= r["rubles"]

    owed_to_us = [(v["display_name"], round(v["bal"], 2))
                  for v in owed_acc.values() if v["bal"] > 0.01]
    we_owe = [(v["display_name"], round(v["bal"], 2))
              for v in we_owe_acc.values() if v["bal"] > 0.01]

    owed_to_us.sort(key=lambda x: -x[1])
    we_owe.sort(key=lambda x: -x[1])

    return {
        "owed_to_us":         owed_to_us,
        "we_owe":             we_owe,
        "total_owed_to_us":   sum(a for _, a in owed_to_us),
        "total_we_owe":       sum(a for _, a in we_owe),
    }


def get_debt_history(chat_id: int, name: str) -> list:
    """История долговых операций по корню имени (регистронезависимо, через падежи)."""
    target_root = name_root(name)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? AND status = 'confirmed' "
            "AND type IN ('loan_out', 'loan_in', 'debt_repay_in', 'debt_repay_out') "
            "ORDER BY id",
            (chat_id,)).fetchall()
    # Фильтруем по корню
    return [r for r in rows if name_root(r["counterparty"] or "") == target_root]


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

ARB_MARKERS = (
    "сделка без моих средств",
    "сделка без своих средств",
    "арбитраж",
    "партнёрская сделка",
    "партнерская сделка",
)


def parse_number(s: str, prefer_decimal: bool = False) -> float:
    """
    Парсит '72 600' → 72600, '85.000' → 85000, '73.5' → 73.5,
    '1 142 400' → 1142400, '75,6' → 75.6, '1.000.000' → 1000000.

    prefer_decimal=True — для курсов: единственный разделитель всегда десятичный.
    """
    s = s.strip().replace(" ", "").replace("\u00a0", "")
    s = s.rstrip("₽рp$ ").strip()
    if not s:
        raise ValueError("empty")

    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif prefer_decimal:
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
        a = parse_number(m.group(1))
        b = parse_number(m.group(3), prefer_decimal=True)
        if op in "*x×":
            usdt, rate, rubles = a, b, a * b
        else:
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

    direction = kind
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


def parse_loan(text: str):
    """
    Долговые операции. Возвращает dict или None.

    Шаблоны:
      Дал в долг Михаилу 90000        → loan_out  (касса -, должник Михаил +)
      Занял у Васи 200000             → loan_in   (касса +, кредитор Вася +)
      Михаил вернул 50000             → debt_repay_in  (касса +, долг Михаила -)
      Вернул Васе 100000              → debt_repay_out (касса -, наш долг Васе -)
    """
    t = text.strip().lstrip("+-—– ").strip()
    lower = t.lower()

    # Дал в долг X сумма
    m = re.match(
        r"^(?:дал|дала|дали|выдал|выдала|выдали|занял\s+(?!у\b))\s+в\s+долг\s+(.+?)\s+(" + NUM_PATTERN + r")\s*₽?\s*$",
        t, re.IGNORECASE)
    if m:
        try:
            amt = parse_number(m.group(2))
        except ValueError:
            return None
        if amt <= 0:
            return None
        return {"type": "loan_out", "counterparty": m.group(1).strip(),
                "amount": amt, "doer": None}

    # Занял у X сумма (без "в долг")
    m = re.match(
        r"^(?:занял|заняла|заняли|взял\s+в\s+долг|взяли\s+в\s+долг)\s+(?:у\s+)?(.+?)\s+(" + NUM_PATTERN + r")\s*₽?\s*$",
        t, re.IGNORECASE)
    if m:
        try:
            amt = parse_number(m.group(2))
        except ValueError:
            return None
        if amt <= 0:
            return None
        return {"type": "loan_in", "counterparty": m.group(1).strip(),
                "amount": amt, "doer": None}

    # X вернул [нам] сумма
    m = re.match(
        r"^(.+?)\s+(?:вернул|вернула|вернули|отдал|отдала|отдали|погасил|погасила|погасили)"
        r"(?:\s+(?:нам|мне|долг))?\s+(" + NUM_PATTERN + r")\s*₽?\s*$",
        t, re.IGNORECASE)
    if m:
        try:
            amt = parse_number(m.group(2))
        except ValueError:
            return None
        if amt <= 0:
            return None
        return {"type": "debt_repay_in", "counterparty": m.group(1).strip(),
                "amount": amt, "doer": None}

    # Вернул/Отдал X сумма (мы отдаём свой долг)
    m = re.match(
        r"^(?:вернул|вернула|вернули|отдал\s+долг|отдала\s+долг|отдали\s+долг|погасил|погасила|погасили)"
        r"\s+(.+?)\s+(" + NUM_PATTERN + r")\s*₽?\s*$",
        t, re.IGNORECASE)
    if m:
        try:
            amt = parse_number(m.group(2))
        except ValueError:
            return None
        if amt <= 0:
            return None
        return {"type": "debt_repay_out", "counterparty": m.group(1).strip(),
                "amount": amt, "doer": None}

    return None


def parse_message_line(line: str):
    """Парсит одну строку: сначала сделку, потом долг, потом кэш-движение."""
    line = line.strip()
    line = re.sub(r"^\d+\s*[\.\)]\s*", "", line).strip()
    if not line:
        return None
    t = parse_trade(line)
    if t:
        return t
    l = parse_loan(line)
    if l:
        return l
    c = parse_cash_flow(line)
    if c:
        return c
    return None


def parse_arbitrage_message(text: str):
    """
    Арбитраж: маркер + 2 trade-формат блока.
    Касса не меняется, в кошелёк падает разница USDT (профит).

    Пример:
        Сделка без моих средств

        Продал Стефу
        196500/75.5=2 602,649

        Купил у Германа
        196500/75=2 620

    Возвращает dict или None.
    """
    lower_full = text.lower()
    if not any(m in lower_full for m in ARB_MARKERS):
        return None

    # Делим сообщение на блоки по пустым строкам
    blocks = re.split(r"\n\s*\n", text)

    # В каждом блоке склеиваем строки в одну, пропуская маркер
    trades = []
    for block in blocks:
        lines = []
        for line in block.split("\n"):
            line = re.sub(r"^\d+\s*[\.\)]\s*", "", line.strip()).strip()
            if not line:
                continue
            if any(m in line.lower() for m in ARB_MARKERS):
                continue
            lines.append(line)
        if not lines:
            continue
        joined = " ".join(lines)
        t = parse_trade(joined)
        if t:
            trades.append((joined, t))

    if len(trades) < 2:
        return None

    # Берём первые 2 сделки
    (raw_a, a), (raw_b, b) = trades[0], trades[1]

    # Та, что больше USDT — это что мы получили; меньшая — что отдали.
    if a["usdt"] >= b["usdt"]:
        got, sent = a, b
    else:
        got, sent = b, a

    profit = round(got["usdt"] - sent["usdt"], 4)

    # Sanity check: профит не должен быть нулевым/отрицательным и не больше 30% от объёма
    if profit <= 0 or profit > got["usdt"] * 0.30:
        return None

    return {
        "type": "arb",
        "partner_in": got["counterparty"],
        "partner_out": sent["counterparty"],
        "usdt_in": got["usdt"],
        "usdt_out": sent["usdt"],
        "rub_amount": round(max(got["rubles"], sent["rubles"]), 2),
        "rate_in": got["rate"],
        "rate_out": sent["rate"],
        "profit": profit,
    }


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


def format_debt_status(d: dict, focus_name: str = None, max_items: int = 4) -> str:
    """
    Возвращает строку-напоминание о долгах для добавления к ответу бота.
    focus_name — если указан, выводит ТОЛЬКО детали по этому контрагенту.
    Возвращает '' если долгов нет.
    """
    lines = []
    if focus_name:
        focus_lower = focus_name.lower()
        # Ищем должника
        for name, amt in d.get("owed_to_us", []):
            if name.lower() == focus_lower or focus_lower in name.lower():
                lines.append(f"   📥 *{name}* теперь должен нам *{fmt_rub(amt)}*")
                break
        for name, amt in d.get("we_owe", []):
            if name.lower() == focus_lower or focus_lower in name.lower():
                lines.append(f"   📤 *Мы должны {name}: {fmt_rub(amt)}*")
                break
        return "\n".join(lines) if lines else ""

    # Общий вид (для напоминания после сделки)
    owed = d.get("owed_to_us", [])
    we_owe = d.get("we_owe", [])
    if not owed and not we_owe:
        return ""

    if owed:
        names = ", ".join(f"{n} {fmt_rub(a)}" for n, a in owed[:max_items])
        if len(owed) > max_items:
            names += f" + ещё {len(owed) - max_items}"
        lines.append(f"⚠️ Нам должны *{fmt_rub(d['total_owed_to_us'])}*: {names}")
    if we_owe:
        names = ", ".join(f"{n} {fmt_rub(a)}" for n, a in we_owe[:max_items])
        if len(we_owe) > max_items:
            names += f" + ещё {len(we_owe) - max_items}"
        lines.append(f"⚠️ Мы должны *{fmt_rub(d['total_we_owe'])}*: {names}")
    return "\n" + "\n".join(lines)


def fmt_tx_short(r) -> str:
    tid = fmt_id(r["id"])
    cp = r["counterparty"] or "—"
    t = r["type"]
    pending = " ⏳" if (r["status"] if "status" in r.keys() else "confirmed") == "pending" else ""
    if t == "sell":
        return f"{tid}{pending} 📤 Продал {cp}: {fmt_usdt(r['usdt'])} × {fmt_rate(r['rate'])} = {fmt_rub(r['rubles'])}"
    if t == "buy":
        return f"{tid}{pending} 📥 Купил у {cp}: {fmt_usdt(r['usdt'])} × {fmt_rate(r['rate'])} = {fmt_rub(r['rubles'])}"
    if t == "cash_out":
        return f"{tid}{pending} 💸 Выдал {cp}: −{fmt_rub(r['rubles'])}"
    if t == "cash_in":
        return f"{tid}{pending} 💰 Принял {cp}: +{fmt_rub(r['rubles'])}"
    if t == "arb":
        pin = r["partner_in"] if "partner_in" in r.keys() else "?"
        pout = r["partner_out"] if "partner_out" in r.keys() else "?"
        return f"{tid}{pending} 🔄 Арбитраж {pin}→{pout}: +{fmt_usdt(r['usdt'])} профит"
    if t == "loan_out":
        return f"{tid}{pending} 📤💳 Дал в долг {cp}: −{fmt_rub(r['rubles'])}"
    if t == "loan_in":
        return f"{tid}{pending} 📥💳 Занял у {cp}: +{fmt_rub(r['rubles'])}"
    if t == "debt_repay_in":
        return f"{tid}{pending} ↩️💰 {cp} вернул долг: +{fmt_rub(r['rubles'])}"
    if t == "debt_repay_out":
        return f"{tid}{pending} ↩️💸 Вернул {cp}: −{fmt_rub(r['rubles'])}"
    return f"{tid}{pending} {t}: {fmt_rub(r['rubles'])}"


# ────────────────── ЛОГИКА ПРИМЕНЕНИЯ БАЛАНСА ──────────────────
def apply_balance(chat_id: int, tx_row) -> None:
    """Применяет влияние подтверждённой транзакции на кассу/кошелёк."""
    t = tx_row["type"]
    if t == "sell":
        update_state(chat_id, d_rubles=tx_row["rubles"], d_usdt=-tx_row["usdt"])
    elif t == "buy":
        update_state(chat_id, d_rubles=-tx_row["rubles"], d_usdt=tx_row["usdt"])
    elif t == "cash_in":
        update_state(chat_id, d_rubles=tx_row["rubles"])
    elif t == "cash_out":
        update_state(chat_id, d_rubles=-tx_row["rubles"])
    elif t == "arb":
        # Касса не меняется, в кошелёк падает только разница (profit, лежит в поле usdt)
        update_state(chat_id, d_usdt=tx_row["usdt"])
    elif t == "loan_out":          # дали в долг → касса -
        update_state(chat_id, d_rubles=-tx_row["rubles"])
    elif t == "loan_in":           # заняли → касса +
        update_state(chat_id, d_rubles=tx_row["rubles"])
    elif t == "debt_repay_in":     # нам вернули → касса +
        update_state(chat_id, d_rubles=tx_row["rubles"])
    elif t == "debt_repay_out":    # мы вернули → касса -
        update_state(chat_id, d_rubles=-tx_row["rubles"])


def reverse_balance(chat_id: int, tx_row) -> None:
    """Откатывает влияние транзакции."""
    t = tx_row["type"]
    if t == "sell":
        update_state(chat_id, d_rubles=-tx_row["rubles"], d_usdt=tx_row["usdt"])
    elif t == "buy":
        update_state(chat_id, d_rubles=tx_row["rubles"], d_usdt=-tx_row["usdt"])
    elif t == "cash_in":
        update_state(chat_id, d_rubles=-tx_row["rubles"])
    elif t == "cash_out":
        update_state(chat_id, d_rubles=tx_row["rubles"])
    elif t == "arb":
        update_state(chat_id, d_usdt=-tx_row["usdt"])
    elif t == "loan_out":
        update_state(chat_id, d_rubles=tx_row["rubles"])
    elif t == "loan_in":
        update_state(chat_id, d_rubles=-tx_row["rubles"])
    elif t == "debt_repay_in":
        update_state(chat_id, d_rubles=-tx_row["rubles"])
    elif t == "debt_repay_out":
        update_state(chat_id, d_rubles=tx_row["rubles"])


def should_confirm(parsed: dict, settings: dict) -> bool:
    """Нужно ли подтверждать через кнопки?"""
    mode = settings.get("confirm_mode", "trades")
    if mode == "off":
        return False
    if mode == "all":
        return True
    t = parsed["type"]
    if mode == "trades":
        # Подтверждаем только сделки и арбитраж. Кэш и долги — сразу.
        return t in ("sell", "buy", "arb")
    if mode == "big":
        amt = parsed.get("rubles") or parsed.get("amount") or parsed.get("rub_amount") or 0
        return amt >= 100000
    return False


# ────────────────── БОТ ──────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


HELP_MAIN = (
    "👋 Я веду учёт USDT-сделок, кэш-движений, долгов и арбитража.\n"
    "_Тип чата:_ *main* (твой личный — пишешь все операции)\n\n"
    "📝 *USDT-сделки* (меняют ₽ и USDT, требуют подтверждения):\n"
    "`Продал Гиге 475600/76.262`\n"
    "`Купил у Стефа 1020*74`\n"
    "`Влад продал Клиенту 96000/75.7`\n"
    "Правило: `*` слева USDT, `/` слева рубли.\n\n"
    "💵 *Кэш-движения* (меняют только ₽):\n"
    "`Выдал Владу 72 600`\n"
    "`Принял от Адахана 729 100`\n"
    "`+ 96000 от клиента`\n\n"
    "💳 *Долги:*\n"
    "`Дал в долг Михаилу 90000`\n"
    "`Занял у Васи 200000`\n"
    "`Михаил вернул 50000`\n"
    "`Вернул Васе 100000`\n\n"
    "🔄 *Арбитраж* (касса не меняется, в кошелёк падает спред):\n"
    "```\nСделка без моих средств\n"
    "Купил у Германа 196500/75=2620\n"
    "Продал Стефу 196500/75.5=2602\n```\n"
    "📊 *Команды:*\n"
    "/balance · /stats · /history · /cashflow · /debts · /debt\n"
    "/find · /undo · /pending · /setcash · /settype · /confirm · /help"
)

HELP_FIELD = (
    "👋 Это рабочий чат сотрудника (поле).\n"
    "_Тип чата:_ *field* — операции сотрудника\n\n"
    "📝 *Сделки в поле:*\n"
    "`Влад купил у клиента 78200/73.5`\n"
    "`Влад продал клиенту 96000/75.7`\n\n"
    "💵 *Кэш на руках:*\n"
    "`Выдали Владу 72 600` _(из общей кассы → на руки Владу)_\n"
    "`Принял от клиента 96 000` _(клиент дал нал)_\n"
    "`Влад выдал клиенту 30 000`\n\n"
    "💳 *Долги клиентов:*\n"
    "`Дал в долг Серёге 50000`\n"
    "`Серёга вернул 50000`\n\n"
    "📊 *Команды:*\n"
    "/balance · /history · /cashflow · /debts · /find · /undo · /pending · /settype · /help"
)

HELP_COMMON = (
    "👋 Это чат общей кассы.\n"
    "_Тип чата:_ *common* — приходы и расходы общего котла\n\n"
    "💵 *Шаблоны:*\n"
    "`Принял от Влада 500 000` _(Влад сдал в кассу)_\n"
    "`Принял от Ивана 800 000` _(Иван внёс в кассу)_\n"
    "`Выдал Владу 100 000` _(касса → Владу на оборотку)_\n"
    "`Выдал на расходы 30 000`\n\n"
    "💳 *Долги партнёров:*\n"
    "`Дал в долг MTX 1 000 000`\n"
    "`MTX вернул 500 000`\n\n"
    "📊 *Команды:*\n"
    "/balance · /cashflow · /debts · /find · /undo · /history · /settype · /help"
)


def help_text(chat_id: int) -> str:
    s = get_chat_settings(chat_id)
    if s["chat_type"] == "field":
        return HELP_FIELD
    if s["chat_type"] == "common":
        return HELP_COMMON
    return HELP_MAIN


def confirm_kb(batch_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записать", callback_data=f"c:{batch_id}"),
        InlineKeyboardButton(text="❌ Отмена",   callback_data=f"x:{batch_id}"),
    ]])


@dp.message(CommandStart())
@dp.message(Command("help"))
async def on_start(message: Message):
    await message.answer(help_text(message.chat.id), parse_mode="Markdown")


@dp.message(Command("settype"))
async def on_settype(message: Message):
    parts = message.text.split(maxsplit=1)
    s = get_chat_settings(message.chat.id)
    if len(parts) < 2:
        await message.answer(
            f"Сейчас тип чата: *{s['chat_type']}*\n\n"
            "Доступные:\n"
            "• `/settype main` — твой личный чат (все операции)\n"
            "• `/settype field` — чат сотрудника в поле\n"
            "• `/settype common` — общая касса\n\n"
            "Тип меняет только подсказки и шаблоны в `/help`, а команды и парсер работают одинаково.",
            parse_mode="Markdown")
        return
    new_type = parts[1].strip().lower()
    if new_type not in ("main", "field", "common"):
        await message.answer("Тип должен быть main, field или common.")
        return
    set_chat_setting(message.chat.id, chat_type=new_type)
    await message.answer(f"✅ Тип чата: *{new_type}*\n\nНовая памятка — /help", parse_mode="Markdown")


@dp.message(Command("confirm"))
async def on_confirm_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    s = get_chat_settings(message.chat.id)
    if len(parts) < 2:
        await message.answer(
            f"Сейчас режим подтверждения: *{s['confirm_mode']}*\n\n"
            "Доступные:\n"
            "• `/confirm trades` — кнопки только для USDT-сделок и арбитража _(по умолчанию)_\n"
            "• `/confirm all` — кнопки для всего, включая кэш\n"
            "• `/confirm big` — кнопки только для сделок от 100 000₽\n"
            "• `/confirm off` — без кнопок, записывать сразу",
            parse_mode="Markdown")
        return
    mode = parts[1].strip().lower()
    if mode not in ("all", "trades", "big", "off"):
        await message.answer("Режим должен быть all, trades, big или off.")
        return
    set_chat_setting(message.chat.id, confirm_mode=mode)
    await message.answer(f"✅ Подтверждение: *{mode}*", parse_mode="Markdown")


@dp.message(Command("balance"))
async def on_balance(message: Message):
    s = get_state(message.chat.id)
    text = f"💰 *Касса:* {fmt_rub(s['ruble_balance'])}\n💵 *Кошелёк:* {fmt_usdt(s['usdt_balance'])}"
    if s["extra_rubles"]:
        text += f"\n♻️ *Лишние:* {fmt_rub(s['extra_rubles'])}"
    # Сколько pending
    with db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE chat_id = ? AND status = 'pending'",
            (message.chat.id,)).fetchone()["c"]
    if pending:
        text += f"\n⏳ Не подтверждено: *{pending}* (см. /pending)"
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("stats"))
async def on_stats(message: Message):
    r = get_avg_rates(message.chat.id)
    s = get_state(message.chat.id)

    if not r["avg_sell"] and not r["avg_buy"] and not r["arb_profit_usdt"]:
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

    if r["arb_profit_usdt"]:
        lines.append(f"\n🔄 Профит от арбитража: *+{fmt_usdt(r['arb_profit_usdt'])}*")

    lines.append(f"\n💰 Касса: {fmt_rub(s['ruble_balance'])}")
    lines.append(f"💵 Кошелёк: {fmt_usdt(s['usdt_balance'])}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("history"))
async def on_history(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? AND status = 'confirmed' "
            "ORDER BY id DESC LIMIT 15",
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
            "AND type IN ('cash_in', 'cash_out') AND status = 'confirmed' "
            "ORDER BY id DESC LIMIT 20",
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


@dp.message(Command("debts"))
async def on_debts(message: Message):
    d = get_debts(message.chat.id)
    lines = ["💳 *Долги*\n"]

    if d["owed_to_us"]:
        lines.append(f"📥 *Нам должны:* {fmt_rub(d['total_owed_to_us'])}")
        for name, amt in d["owed_to_us"]:
            lines.append(f"   • {name}: {fmt_rub(amt)}")
    else:
        lines.append("📥 *Нам никто не должен.*")

    lines.append("")

    if d["we_owe"]:
        lines.append(f"📤 *Мы должны:* {fmt_rub(d['total_we_owe'])}")
        for name, amt in d["we_owe"]:
            lines.append(f"   • {name}: {fmt_rub(amt)}")
    else:
        lines.append("📤 *Мы никому не должны.*")

    net = d["total_owed_to_us"] - d["total_we_owe"]
    lines.append("")
    if net > 0:
        lines.append(f"📊 *Чистый баланс: +{fmt_rub(net)}* (нам должны больше, чем мы)")
    elif net < 0:
        lines.append(f"📊 *Чистый баланс: −{fmt_rub(abs(net))}* (мы должны больше, чем нам)")
    else:
        lines.append("📊 *Чистый баланс: 0*")

    lines.append("\nДеталь по человеку: `/debt <имя>`")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("debt"))
async def on_debt(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: `/debt <имя>`\n"
            "Например: `/debt Михаил`",
            parse_mode="Markdown")
        return
    name = parts[1].strip()
    rows = get_debt_history(message.chat.id, name)
    if not rows:
        await message.answer(f"Долговых операций по «{name}» не нашёл.")
        return

    # Считаем остаток: для loan_out / debt_repay_in — нам должны
    # для loan_in / debt_repay_out — мы должны
    bal_owed = 0.0  # нам должны
    bal_owe = 0.0   # мы должны
    for r in rows:
        if r["type"] == "loan_out":
            bal_owed += r["rubles"]
        elif r["type"] == "debt_repay_in":
            bal_owed -= r["rubles"]
        elif r["type"] == "loan_in":
            bal_owe += r["rubles"]
        elif r["type"] == "debt_repay_out":
            bal_owe -= r["rubles"]

    lines = [f"💳 *История долгов по «{name}»* ({len(rows)} оп.)\n"]
    for r in rows:
        lines.append(fmt_tx_short(r))
    lines.append("")
    if abs(bal_owed) > 0.01:
        if bal_owed > 0:
            lines.append(f"📥 *Остаток: должен нам {fmt_rub(bal_owed)}*")
        else:
            lines.append(f"⚠️ Переплата: нам вернули на {fmt_rub(-bal_owed)} больше")
    if abs(bal_owe) > 0.01:
        if bal_owe > 0:
            lines.append(f"📤 *Остаток: мы должны {fmt_rub(bal_owe)}*")
        else:
            lines.append(f"⚠️ Переплата: мы вернули на {fmt_rub(-bal_owe)} больше")
    if abs(bal_owed) < 0.01 and abs(bal_owe) < 0.01:
        lines.append("✅ Расчёты закрыты.")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("pending"))
async def on_pending(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? AND status = 'pending' "
            "ORDER BY id",
            (message.chat.id,)).fetchall()
    if not rows:
        await message.answer("Нет операций, ждущих подтверждения.")
        return
    lines = [f"⏳ *Ждут подтверждения ({len(rows)}):*\n"]
    for r in rows:
        lines.append(fmt_tx_short(r))
    lines.append("\nИспользуй кнопки под нужным сообщением, или /pending_cancel чтобы удалить все.")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("pending_cancel"))
async def on_pending_cancel(message: Message):
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE chat_id = ? AND status = 'pending'",
            (message.chat.id,)).fetchone()["c"]
        conn.execute(
            "DELETE FROM transactions WHERE chat_id = ? AND status = 'pending'",
            (message.chat.id,))
    await message.answer(f"🗑 Удалил {n} неподтверждённых операций.")


@dp.message(Command("find"))
async def on_find(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: `/find <ID | имя | сумма>`\n\n"
            "`/find #142` — найти операцию по ID\n"
            "`/find Адахан` — все операции с этим контрагентом\n"
            "`/find 96000` — все операции на эту сумму",
            parse_mode="Markdown")
        return
    query = parts[1].strip()

    id_match = re.match(r"^#?(\d+)$", query.replace(" ", ""))
    if id_match:
        tx_id = int(id_match.group(1))
        with db() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not row:
            await message.answer(f"Операция #{tx_id:03d} не найдена.")
            return
        same_chat = row["chat_id"] == message.chat.id
        note = "" if same_chat else f"\n_(из другого чата, ID={row['chat_id']})_"
        await message.answer(f"🔍 *Найдено:*\n\n{fmt_tx_short(row)}{note}", parse_mode="Markdown")
        return

    try:
        amount = parse_number(query)
        if amount >= 100:
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE chat_id = ? "
                    "AND ABS(rubles - ?) < 0.5 AND status = 'confirmed' ORDER BY id DESC LIMIT 15",
                    (message.chat.id, amount)).fetchall()
            if rows:
                out = [f"🔍 *Найдено {len(rows)} операций на {fmt_rub(amount)}:*\n"]
                out.extend(fmt_tx_short(r) for r in rows)
                await message.answer("\n".join(out), parse_mode="Markdown")
                return
    except (ValueError, TypeError):
        pass

    pattern = f"%{query.lower()}%"
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? AND status = 'confirmed' "
            "AND (pylower(counterparty) LIKE ? OR pylower(partner_in) LIKE ? OR pylower(partner_out) LIKE ?) "
            "ORDER BY id DESC LIMIT 15",
            (message.chat.id, pattern, pattern, pattern)).fetchall()
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
            "SELECT * FROM transactions WHERE chat_id = ? AND status = 'confirmed' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,)).fetchone()
        if not row:
            await message.answer("Отменять нечего.")
            return
        conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))

    reverse_balance(chat_id, row)
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


# ────────────────── ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ──────────────────
@dp.message(F.text)
async def on_text(message: Message):
    chat_id = message.chat.id
    settings = get_chat_settings(chat_id)

    # 1. Сначала пробуем распознать арбитраж
    arb = parse_arbitrage_message(message.text)
    if arb:
        await handle_arbitrage(message, arb, settings)
        return

    # 2. Иначе — строка за строкой
    parsed_items = []
    for line in message.text.split("\n"):
        p = parse_message_line(line)
        if p:
            parsed_items.append((p, line.strip()))

    if not parsed_items:
        low = message.text.lower().strip()
        triggers = ("продал", "продала", "купил", "купила", "откупил",
                    "выдал", "выдала", "выдали", "отдал", "отдали",
                    "принял", "приняла", "приняли", "забрал", "забрали",
                    "занял", "заняла", "заняли", "долг", "вернул",
                    "вернула", "вернули", "погасил", "погасили")
        if any(t in low for t in triggers):
            await message.reply(
                "Не понял формат. Примеры:\n"
                "`Продал Гиге 100*76`\n"
                "`Купил у Васи 75000/74`\n"
                "`Выдал Владу 72600`\n"
                "`Принял от Адахана 96000`",
                parse_mode="Markdown")
        return

    await handle_regular_ops(message, parsed_items, settings)


async def handle_arbitrage(message: Message, arb: dict, settings: dict):
    chat_id = message.chat.id
    needs_confirm = should_confirm(arb, settings)
    status = "pending" if needs_confirm else "confirmed"

    # raw_text — оригинал сообщения для трассировки
    raw = message.text.strip()
    counterparty = f"{arb['partner_in']}→{arb['partner_out']}"

    tid = save_tx(
        chat_id, "arb", counterparty, arb["rub_amount"],
        usdt=arb["profit"],
        rate=0,
        raw=raw,
        status=status,
        usdt_in=arb["usdt_in"],
        usdt_out=arb["usdt_out"],
        partner_in=arb["partner_in"],
        partner_out=arb["partner_out"],
    )

    initial = get_state(chat_id)

    lines = [
        f"🔄 *Арбитраж* {fmt_id(tid)}{' ⏳' if needs_confirm else ''}",
        "",
        f"📥 От *{arb['partner_in']}*: +{fmt_usdt(arb['usdt_in'])} (по курсу {fmt_rate(arb['rate_in'])})",
        f"📤 К *{arb['partner_out']}*: −{fmt_usdt(arb['usdt_out'])} (по курсу {fmt_rate(arb['rate_out'])})",
        f"💰 Через касу: {fmt_rub(arb['rub_amount'])} _(не зачисляется)_",
        "",
        f"💵 *Профит: +{fmt_usdt(arb['profit'])}*",
    ]

    if needs_confirm:
        lines.append("\n⚠️ Проверь и подтверди:")
        await message.reply("\n".join(lines), parse_mode="Markdown", reply_markup=confirm_kb(tid))
    else:
        apply_balance(chat_id, {
            "type": "arb",
            "usdt": arb["profit"],
            "rubles": 0,
        })
        final = get_state(chat_id)
        lines.append(f"\n💵 Кошелёк: {fmt_usdt(initial['usdt_balance'])} +{fmt_usdt(arb['profit'])} = *{fmt_usdt(final['usdt_balance'])}*")
        debt_status = format_debt_status(get_debts(chat_id))
        if debt_status:
            lines.append(debt_status)
        await message.reply("\n".join(lines), parse_mode="Markdown")


async def handle_regular_ops(message: Message, parsed_items: list, settings: dict):
    chat_id = message.chat.id

    # Решаем, есть ли среди операций такие, что требуют подтверждения
    needs_confirm_items = [p for p, _ in parsed_items if should_confirm(p, settings)]
    immediate_items = [(p, raw) for p, raw in parsed_items if not should_confirm(p, settings)]

    # ВАРИАНТ 1: смешанный режим (часть нужна подтверждения, часть — нет)
    # Чтобы не запутать пользователя — всё подтверждать вместе, если хоть одна нужна.
    if needs_confirm_items:
        # Сохраняем ВСЕ как pending, общий batch_id = id первой
        saved = []
        first_id = None
        for p, raw in parsed_items:
            t = p["type"]
            cp = p["counterparty"]
            if t in ("sell", "buy"):
                tid = save_tx(chat_id, t, cp, p["rubles"], usdt=p["usdt"], rate=p["rate"],
                              raw=raw, doer=p.get("doer"), status="pending")
            else:
                tid = save_tx(chat_id, t, cp, p["amount"],
                              raw=raw, doer=p.get("doer"), status="pending")
            if first_id is None:
                first_id = tid
            saved.append((tid, p))

        # Привязываем все к batch_id первой операции
        with db() as conn:
            ids = [s[0] for s in saved]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE transactions SET batch_id = ? WHERE id IN ({placeholders})",
                (first_id, *ids))

        # Сборка превью
        if len(saved) == 1:
            tid, p = saved[0]
            t = p["type"]
            if t in ("sell", "buy"):
                verb = "Продал" if t == "sell" else "Купил у"
                doer_str = f" ({p['doer']})" if p.get("doer") else ""
                reply = [
                    f"{fmt_id(tid)} ⏳ {verb} *{p['counterparty']}*{doer_str}",
                    f"   {fmt_usdt(p['usdt'])} × {fmt_rate(p['rate'])} = {fmt_rub(p['rubles'])}",
                    "",
                    "⚠️ *Проверь и подтверди:*",
                ]
                if t == "sell":
                    reply.append(f"📥 Ты получил {fmt_rub(p['rubles'])}?")
                    reply.append(f"📤 Ты отправил {fmt_usdt(p['usdt'])}?")
                else:
                    reply.append(f"📥 Ты получил {fmt_usdt(p['usdt'])}?")
                    reply.append(f"📤 Ты отправил {fmt_rub(p['rubles'])}?")
                await message.reply("\n".join(reply), parse_mode="Markdown",
                                    reply_markup=confirm_kb(first_id))
            else:
                sign = "+" if t == "cash_in" else "−"
                verb = "Принял" if t == "cash_in" else "Выдал"
                doer_str = f" ({p['doer']})" if p.get("doer") else ""
                reply = [
                    f"{fmt_id(tid)} ⏳ {verb} *{p['counterparty']}*{doer_str}",
                    f"   {sign}{fmt_rub(p['amount'])}",
                    "",
                    "⚠️ *Подтверди:*",
                ]
                await message.reply("\n".join(reply), parse_mode="Markdown",
                                    reply_markup=confirm_kb(first_id))
        else:
            lines = [f"⏳ *Распарсил {len(saved)} операций — подтверди все:*\n"]
            for tid, p in saved:
                t = p["type"]
                if t == "sell":
                    lines.append(f"{fmt_id(tid)} 📤 Продал {p['counterparty']}: +{fmt_rub(p['rubles'])} / −{fmt_usdt(p['usdt'])}")
                elif t == "buy":
                    lines.append(f"{fmt_id(tid)} 📥 Купил у {p['counterparty']}: −{fmt_rub(p['rubles'])} / +{fmt_usdt(p['usdt'])}")
                elif t == "cash_in":
                    lines.append(f"{fmt_id(tid)} 💰 Принял {p['counterparty']}: +{fmt_rub(p['amount'])}")
                elif t == "cash_out":
                    lines.append(f"{fmt_id(tid)} 💸 Выдал {p['counterparty']}: −{fmt_rub(p['amount'])}")
            await message.reply("\n".join(lines), parse_mode="Markdown",
                                reply_markup=confirm_kb(first_id))
        return

    # ВАРИАНТ 2: ничего не требует подтверждения — сразу записываем
    initial = get_state(chat_id)
    saved = []
    for p, raw in parsed_items:
        t = p["type"]
        cp = p["counterparty"]
        if t in ("sell", "buy"):
            tid = save_tx(chat_id, t, cp, p["rubles"], usdt=p["usdt"], rate=p["rate"],
                          raw=raw, doer=p.get("doer"), status="confirmed")
            apply_balance(chat_id, {"type": t, "usdt": p["usdt"], "rubles": p["rubles"]})
        else:
            tid = save_tx(chat_id, t, cp, p["amount"],
                          raw=raw, doer=p.get("doer"), status="confirmed")
            apply_balance(chat_id, {"type": t, "usdt": 0, "rubles": p["amount"]})
        saved.append((tid, p))

    final = get_state(chat_id)

    if len(saved) == 1:
        tid, p = saved[0]
        t = p["type"]
        if t in ("sell", "buy"):
            verb = "Продал" if t == "sell" else "Купил у"
            op_sign = "+" if t == "sell" else "−"
            doer_str = f" ({p['doer']})" if p.get("doer") else ""
            d = get_debts(chat_id)
            debt_reminder = format_debt_status(d)
            await message.reply(
                f"{fmt_id(tid)} ✅ {verb} *{p['counterparty']}*{doer_str}\n"
                f"   {fmt_usdt(p['usdt'])} × {fmt_rate(p['rate'])} = {fmt_rub(p['rubles'])}\n\n"
                f"💰 Касса: {fmt_rub(initial['ruble_balance'])} {op_sign} {fmt_rub(p['rubles'])} = *{fmt_rub(final['ruble_balance'])}*\n"
                f"💵 Кошелёк: *{fmt_usdt(final['usdt_balance'])}*"
                f"{debt_reminder}",
                parse_mode="Markdown")
        else:
            doer_str = f" ({p['doer']})" if p.get("doer") else ""
            if t == "loan_out":
                verb_full = f"Дал в долг *{p['counterparty']}*{doer_str}"
                op_sign = "−"
                sign = "−"
            elif t == "loan_in":
                verb_full = f"Занял у *{p['counterparty']}*{doer_str}"
                op_sign = "+"
                sign = "+"
            elif t == "debt_repay_in":
                verb_full = f"*{p['counterparty']}*{doer_str} вернул долг"
                op_sign = "+"
                sign = "+"
            elif t == "debt_repay_out":
                verb_full = f"Вернул долг *{p['counterparty']}*{doer_str}"
                op_sign = "−"
                sign = "−"
            else:
                verb = "Принял" if t == "cash_in" else "Выдал"
                verb_full = f"{verb} *{p['counterparty']}*{doer_str}"
                op_sign = "+" if t == "cash_in" else "−"
                sign = op_sign

            reply_lines = [
                f"{fmt_id(tid)} ✅ {verb_full}",
                f"   {sign}{fmt_rub(p['amount'])}",
                "",
                f"💰 Касса: {fmt_rub(initial['ruble_balance'])} {op_sign} {fmt_rub(p['amount'])} = *{fmt_rub(final['ruble_balance'])}*",
            ]
            # Для долговых операций — показываем актуальное состояние долгов
            if t in ("loan_out", "loan_in", "debt_repay_in", "debt_repay_out"):
                d = get_debts(chat_id)
                debt_line = format_debt_status(d, focus_name=p["counterparty"])
                if debt_line:
                    reply_lines.append(debt_line)
            await message.reply("\n".join(reply_lines), parse_mode="Markdown")
    else:
        lines = [f"✅ *Записано {len(saved)} операций:*\n"]
        d_rub = 0.0
        d_usdt = 0.0
        has_debt_ops = False
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
            elif t == "loan_out":
                lines.append(f"{fmt_id(tid)} 📤💳 Дал в долг {p['counterparty']}: −{fmt_rub(p['amount'])}")
                d_rub -= p["amount"]; has_debt_ops = True
            elif t == "loan_in":
                lines.append(f"{fmt_id(tid)} 📥💳 Занял у {p['counterparty']}: +{fmt_rub(p['amount'])}")
                d_rub += p["amount"]; has_debt_ops = True
            elif t == "debt_repay_in":
                lines.append(f"{fmt_id(tid)} ↩️💰 {p['counterparty']} вернул: +{fmt_rub(p['amount'])}")
                d_rub += p["amount"]; has_debt_ops = True
            elif t == "debt_repay_out":
                lines.append(f"{fmt_id(tid)} ↩️💸 Вернул {p['counterparty']}: −{fmt_rub(p['amount'])}")
                d_rub -= p["amount"]; has_debt_ops = True
        lines.append(f"\n📊 Итого по ₽: {('+' if d_rub >= 0 else '−')}{fmt_rub(abs(d_rub))}")
        if abs(d_usdt) > 1e-6:
            lines.append(f"📊 Итого по USDT: {('+' if d_usdt >= 0 else '−')}{fmt_usdt(abs(d_usdt))}")
        lines.append(f"\n💰 Касса: {fmt_rub(initial['ruble_balance'])} → *{fmt_rub(final['ruble_balance'])}*")
        if abs(d_usdt) > 1e-6:
            lines.append(f"💵 Кошелёк: *{fmt_usdt(final['usdt_balance'])}*")
        if has_debt_ops:
            debt_status = format_debt_status(get_debts(chat_id))
            if debt_status:
                lines.append(debt_status)
        await message.reply("\n".join(lines), parse_mode="Markdown")


# ────────────────── ОБРАБОТЧИКИ КНОПОК ──────────────────
@dp.callback_query(F.data.startswith("c:"))
async def on_confirm_button(cq: CallbackQuery):
    try:
        batch_id = int(cq.data[2:])
    except ValueError:
        await cq.answer("Ошибка кнопки.", show_alert=True)
        return

    chat_id = cq.message.chat.id
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? AND batch_id = ? AND status = 'pending'",
            (chat_id, batch_id)).fetchall()
        # Если batch_id не выставлен (одиночная операция), пробуем по id
        if not rows:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE chat_id = ? AND id = ? AND status = 'pending'",
                (chat_id, batch_id)).fetchall()
        if not rows:
            await cq.answer("Уже подтверждено или отменено.", show_alert=True)
            try:
                await cq.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        # Подтверждаем все
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE transactions SET status = 'confirmed' WHERE id IN ({placeholders})",
            ids)

    # Применяем балансы
    for r in rows:
        apply_balance(chat_id, r)

    final = get_state(chat_id)
    n = len(rows)
    if n == 1:
        r = rows[0]
        confirm_line = f"\n\n✅ *Подтверждено* в {datetime.utcnow().strftime('%H:%M UTC')}"
    else:
        confirm_line = f"\n\n✅ *Подтверждено {n} операций* в {datetime.utcnow().strftime('%H:%M UTC')}"
    confirm_line += f"\n💰 Касса: *{fmt_rub(final['ruble_balance'])}*"
    confirm_line += f"\n💵 Кошелёк: *{fmt_usdt(final['usdt_balance'])}*"
    debt_status = format_debt_status(get_debts(chat_id))
    if debt_status:
        confirm_line += debt_status

    try:
        new_text = (cq.message.text or "") + confirm_line
        await cq.message.edit_text(new_text, parse_mode="Markdown", reply_markup=None)
    except Exception as e:
        log.warning(f"edit_text failed: {e}")
        await cq.message.answer(f"✅ Подтверждено {n} операций.{confirm_line}", parse_mode="Markdown")
    await cq.answer("Записано!")


@dp.callback_query(F.data.startswith("x:"))
async def on_cancel_button(cq: CallbackQuery):
    try:
        batch_id = int(cq.data[2:])
    except ValueError:
        await cq.answer("Ошибка кнопки.", show_alert=True)
        return

    chat_id = cq.message.chat.id
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE chat_id = ? AND batch_id = ? AND status = 'pending'",
            (chat_id, batch_id)).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE chat_id = ? AND id = ? AND status = 'pending'",
                (chat_id, batch_id)).fetchall()
        if not rows:
            await cq.answer("Уже подтверждено или отменено.", show_alert=True)
            try:
                await cq.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)

    n = len(rows)
    try:
        new_text = (cq.message.text or "") + f"\n\n❌ *Отменено* ({n} оп.)"
        await cq.message.edit_text(new_text, parse_mode="Markdown", reply_markup=None)
    except Exception:
        await cq.message.answer(f"❌ Отменено {n} операций.", parse_mode="Markdown")
    await cq.answer("Отменено")


# ────────────────── ЗАПУСК ──────────────────
async def main():
    init_db()
    log.info("Бот запущен. Жду сообщения…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
