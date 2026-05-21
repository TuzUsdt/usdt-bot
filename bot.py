"""
Telegram-бот для учёта сделок USDT.
Понимает свободные сообщения вида:
    Продал Гиге 475600/76.262
    Продал Адахану 6500*76.5
    Купил у Стефа 1020*74
    Купил у Жени 300000/74.55
и сам считает кассу, баланс USDT, средние курсы покупки/продажи.
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
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                ts            TEXT    NOT NULL,
                kind          TEXT    NOT NULL,   -- 'sell' / 'buy'
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
                extra_rubles   REAL DEFAULT 0     -- "Лишние"
            );
            CREATE INDEX IF NOT EXISTS idx_trades_chat ON trades(chat_id, ts);
        """)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_state(chat_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM state WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO state (chat_id) VALUES (?)", (chat_id,))
            return {"ruble_balance": 0.0, "usdt_balance": 0.0, "extra_rubles": 0.0}
        return dict(row)


def update_state(chat_id: int, *, d_rubles: float = 0, d_usdt: float = 0):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO state (chat_id) VALUES (?)", (chat_id,))
        conn.execute(
            "UPDATE state SET ruble_balance = ruble_balance + ?, "
            "usdt_balance = usdt_balance + ? WHERE chat_id = ?",
            (d_rubles, d_usdt, chat_id),
        )


def save_trade(chat_id, kind, counterparty, usdt, rate, rubles, raw):
    with db() as conn:
        conn.execute(
            "INSERT INTO trades (chat_id, ts, kind, counterparty, usdt, rate, rubles, raw_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, datetime.utcnow().isoformat(), kind, counterparty, usdt, rate, rubles, raw),
        )


def get_avg_rates(chat_id: int) -> dict:
    with db() as conn:
        sell = conn.execute(
            "SELECT COALESCE(SUM(rubles),0) r, COALESCE(SUM(usdt),0) u FROM trades "
            "WHERE chat_id = ? AND kind = 'sell'", (chat_id,)).fetchone()
        buy = conn.execute(
            "SELECT COALESCE(SUM(rubles),0) r, COALESCE(SUM(usdt),0) u FROM trades "
            "WHERE chat_id = ? AND kind = 'buy'", (chat_id,)).fetchone()
    return {
        "avg_sell":         (sell["r"] / sell["u"]) if sell["u"] else None,
        "avg_buy":          (buy["r"]  / buy["u"])  if buy["u"]  else None,
        "total_sold_usdt":  sell["u"],
        "total_bought_usdt":buy["u"],
        "total_received":   sell["r"],
        "total_spent":      buy["r"],
    }


# ────────────────── ПАРСИНГ ──────────────────
NUM = r"[\d][\d\s.,]*"

def parse_number(s: str) -> float:
    """Превращает '6 236,3' / '76.262' / '475600' в float."""
    s = s.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    return float(s)


SELL_VERBS = ("продал", "продала", "продали", "отдал", "отдала", "отдали")
BUY_VERBS = ("купил", "купила", "купили", "откупил", "откупила", "откупили",
             "забрал", "забрала", "забрали")


def _find_verb(lower: str):
    """Ищем глагол sell/buy в первых ~30 символах. Возвращаем (kind, end_idx) или (None, None)."""
    best = None
    for kind, verbs in (("sell", SELL_VERBS), ("buy", BUY_VERBS)):
        for v in verbs:
            i = lower.find(v)
            if 0 <= i <= 30:
                before_ok = (i == 0) or not lower[i - 1].isalpha()
                end = i + len(v)
                after_ok = (end == len(lower)) or not lower[end].isalpha()
                if before_ok and after_ok and (best is None or i < best[0]):
                    best = (i, end, kind)
    return (best[2], best[1]) if best else (None, None)


def parse_trade(text: str):
    """
    Возвращает dict {kind, counterparty, usdt, rate, rubles} или None.
    Поддерживает форматы:
        Продал Гиге 475600/76.262
        Купил у Стефа 1020*74
        Влад продал Клиенту 96000/75.7
        Влад купил у клиента 15000/72.1
        Откупили у Москвы 2375700/73.551
    """
    text = text.strip()
    lower = text.lower()

    kind, verb_end = _find_verb(lower)
    if not kind:
        return None

    rest = text[verb_end:].lstrip()
    for prefix in ("у ", "у\u00a0", "от ", "от\u00a0"):
        if rest.lower().startswith(prefix):
            rest = rest[len(prefix):].lstrip()
            break

    # Находим первое выражение вида X*Y или X/Y
    m = re.search(rf"({NUM})\s*([*x×/÷:])\s*({NUM})", rest)
    if not m:
        return None

    counterparty = rest[:m.start()].strip().rstrip("—-:,.").strip() or "—"
    try:
        a = parse_number(m.group(1))
        b = parse_number(m.group(3))
    except ValueError:
        return None
    op = m.group(2)

    if op in "*x×":
        # X * Y :  X — USDT, Y — курс, результат — рубли
        usdt, rate, rubles = a, b, a * b
    else:
        # X / Y :  X — рубли, Y — курс, результат — USDT
        rubles, rate, usdt = a, b, a / b

    return {
        "kind": kind,
        "counterparty": counterparty,
        "usdt": round(usdt, 4),
        "rate": rate,
        "rubles": round(rubles, 2),
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


# ────────────────── БОТ ──────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


HELP_TEXT = (
    "👋 Я считаю твои сделки USDT.\n\n"
    "📝 *Пиши обычным текстом:*\n"
    "• `Продал Гиге 475600/76.262`\n"
    "• `Купил у Стефа 1020*74`\n"
    "• `Влад продал Клиенту 96000/75.7`\n"
    "• `Откупили у Москвы 2375700/73.551`\n\n"
    "Правило простое:\n"
    "• `*` — слева USDT, справа курс\n"
    "• `/` — слева рубли, справа курс\n\n"
    "📊 *Команды:*\n"
    "/balance — текущая касса и USDT\n"
    "/stats — средние курсы, спред, прибыль\n"
    "/history — последние 10 сделок\n"
    "/undo — отменить последнюю сделку\n"
    "/setcash 4348000 1500 — задать стартовую кассу (₽ и USDT)\n"
    "/extra 394400 — записать «Лишние ₽»\n"
    "/reset — обнулить всё"
)


@dp.message(CommandStart())
@dp.message(Command("help"))
async def on_start(message: Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@dp.message(Command("balance"))
async def on_balance(message: Message):
    s = get_state(message.chat.id)
    text = (
        f"💰 *Касса:* {fmt_rub(s['ruble_balance'])}\n"
        f"💵 *Кошелёк:* {fmt_usdt(s['usdt_balance'])}"
    )
    if s["extra_rubles"]:
        text += f"\n♻️ *Лишние:* {fmt_rub(s['extra_rubles'])}"
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("stats"))
async def on_stats(message: Message):
    r = get_avg_rates(message.chat.id)
    s = get_state(message.chat.id)

    if not r["avg_sell"] and not r["avg_buy"]:
        await message.answer("Сделок ещё нет. Напиши, например: `Продал Васе 1000*76`",
                             parse_mode="Markdown")
        return

    lines = ["📊 *Статистика*\n"]

    if r["avg_sell"]:
        lines.append(f"📈 Средний курс ПРОДАЖИ: *{fmt_rate(r['avg_sell'])}*")
        lines.append(f"   Всего продано: {fmt_usdt(r['total_sold_usdt'])} → {fmt_rub(r['total_received'])}")
    if r["avg_buy"]:
        lines.append(f"📉 Средний курс ПОКУПКИ: *{fmt_rate(r['avg_buy'])}*")
        lines.append(f"   Всего куплено: {fmt_usdt(r['total_bought_usdt'])} за {fmt_rub(r['total_spent'])}")

    if r["avg_sell"] and r["avg_buy"]:
        spread = r["avg_sell"] - r["avg_buy"]
        # реализованная прибыль = на проданном объёме разница со средней покупкой
        realized = (r["avg_sell"] - r["avg_buy"]) * min(r["total_sold_usdt"], r["total_bought_usdt"])
        lines.append(f"\n💹 Спред: *{spread:+.3f}* ₽/USDT")
        lines.append(f"💵 Реализованная прибыль ≈ *{fmt_rub(realized)}*")
        lines.append("\n⚠️ Чтобы НЕ уйти в минус:")
        lines.append(f"   • Покупай НЕ дороже *{fmt_rate(r['avg_sell'])}*")
        lines.append(f"   • Продавай НЕ дешевле *{fmt_rate(r['avg_buy'])}*")

    lines.append(f"\n💰 Касса: {fmt_rub(s['ruble_balance'])}")
    lines.append(f"💵 Кошелёк: {fmt_usdt(s['usdt_balance'])}")
    if s["extra_rubles"]:
        lines.append(f"♻️ Лишние: {fmt_rub(s['extra_rubles'])}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("history"))
async def on_history(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE chat_id = ? ORDER BY id DESC LIMIT 10",
            (message.chat.id,)).fetchall()
    if not rows:
        await message.answer("Сделок ещё нет.")
        return
    lines = ["📜 *Последние сделки:*\n"]
    for r in rows:
        emoji = "📤" if r["kind"] == "sell" else "📥"
        verb = "Продал" if r["kind"] == "sell" else "Купил у"
        lines.append(
            f"{emoji} {verb} {r['counterparty']}: "
            f"{fmt_usdt(r['usdt'])} × {fmt_rate(r['rate'])} = {fmt_rub(r['rubles'])}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("undo"))
async def on_undo(message: Message):
    chat_id = message.chat.id
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,)).fetchone()
        if not row:
            await message.answer("Отменять нечего.")
            return
        conn.execute("DELETE FROM trades WHERE id = ?", (row["id"],))

    if row["kind"] == "sell":
        update_state(chat_id, d_rubles=-row["rubles"], d_usdt=row["usdt"])
    else:
        update_state(chat_id, d_rubles=row["rubles"], d_usdt=-row["usdt"])

    await message.answer(f"↩️ Отменил: `{row['raw_text']}`", parse_mode="Markdown")


@dp.message(Command("reset"))
async def on_reset(message: Message):
    with db() as conn:
        conn.execute("DELETE FROM trades WHERE chat_id = ?", (message.chat.id,))
        conn.execute(
            "UPDATE state SET ruble_balance=0, usdt_balance=0, extra_rubles=0 WHERE chat_id = ?",
            (message.chat.id,))
    await message.answer("🗑 Всё обнулил.")


@dp.message(Command("setcash"))
async def on_setcash(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Использование: `/setcash <рубли> [USDT]`\n"
            "Например: `/setcash 4348000 1500`",
            parse_mode="Markdown")
        return
    try:
        rub = parse_number(parts[1])
        usdt = parse_number(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        await message.answer("Не понял числа. Пример: `/setcash 4348000 1500`",
                             parse_mode="Markdown")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO state (chat_id, ruble_balance, usdt_balance) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET ruble_balance=excluded.ruble_balance, "
            "usdt_balance=excluded.usdt_balance",
            (message.chat.id, rub, usdt))
    await message.answer(f"✅ Касса: {fmt_rub(rub)}\n💵 Кошелёк: {fmt_usdt(usdt)}")


@dp.message(Command("extra"))
async def on_extra(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: `/extra <сумма_в_рублях>`", parse_mode="Markdown")
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
    parsed = parse_trade(message.text)
    if not parsed:
        # Если человек явно начал с «продал»/«купил», но формат сломан — подсказать.
        low = message.text.lower().strip()
        if any(v in low for v in ("продал", "купил", "откупил", "забрал")):
            await message.answer(
                "Не понял формат. Используй:\n"
                "`Продал [имя] [usdt]*[курс]`  → `Продал Васе 1000*76`\n"
                "`Купил у [имя] [рубли]/[курс]` → `Купил у Васи 75000/74.5`\n"
                "`Влад продал [имя] [рубли]/[курс]` тоже работает",
                parse_mode="Markdown")
        return

    chat_id = message.chat.id
    old = get_state(chat_id)

    save_trade(chat_id, parsed["kind"], parsed["counterparty"],
               parsed["usdt"], parsed["rate"], parsed["rubles"], message.text)

    if parsed["kind"] == "sell":
        update_state(chat_id, d_rubles=parsed["rubles"], d_usdt=-parsed["usdt"])
        op_sign = "+"
    else:
        update_state(chat_id, d_rubles=-parsed["rubles"], d_usdt=parsed["usdt"])
        op_sign = "−"

    new = get_state(chat_id)
    avgs = get_avg_rates(chat_id)
    verb = "Продал" if parsed["kind"] == "sell" else "Купил у"

    reply = [
        f"✅ {verb} *{parsed['counterparty']}*",
        f"   {fmt_usdt(parsed['usdt'])} × {fmt_rate(parsed['rate'])} = {fmt_rub(parsed['rubles'])}",
        "",
        f"💰 Касса: {fmt_rub(old['ruble_balance'])} {op_sign} {fmt_rub(parsed['rubles'])} = *{fmt_rub(new['ruble_balance'])}*",
        f"💵 Кошелёк: *{fmt_usdt(new['usdt_balance'])}*",
    ]

    # Подсказка по выгодности
    if parsed["kind"] == "buy" and avgs["avg_sell"]:
        if parsed["rate"] >= avgs["avg_sell"]:
            reply.append(
                f"\n⚠️ Купил по {fmt_rate(parsed['rate'])} ≥ среднего продажи "
                f"{fmt_rate(avgs['avg_sell'])} — на этом объёме будет минус!")
        else:
            margin = avgs["avg_sell"] - parsed["rate"]
            reply.append(
                f"\n👍 Курс ниже среднего продажи на {margin:.2f}₽ — "
                f"если продашь по среднему, профит ≈ *{fmt_rub(margin * parsed['usdt'])}*")
    if parsed["kind"] == "sell" and avgs["avg_buy"]:
        if parsed["rate"] <= avgs["avg_buy"]:
            reply.append(
                f"\n⚠️ Продал по {fmt_rate(parsed['rate'])} ≤ среднего покупки "
                f"{fmt_rate(avgs['avg_buy'])} — это минус!")
        else:
            margin = parsed["rate"] - avgs["avg_buy"]
            reply.append(
                f"\n👍 Курс выше среднего покупки на {margin:.2f}₽ — "
                f"профит на сделке ≈ *{fmt_rub(margin * parsed['usdt'])}*")

    await message.answer("\n".join(reply), parse_mode="Markdown")


# ────────────────── ЗАПУСК ──────────────────
async def main():
    init_db()
    log.info("Бот запущен. Жду сообщений…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
