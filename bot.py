import asyncio
import html
import logging
import os
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHANNEL = os.getenv("CHANNEL_USERNAME", "@bendoShop").strip()
REQUIRE_SUB = os.getenv("REQUIRE_SUBSCRIPTION", "1") == "1"
CURRENCY = os.getenv("CURRENCY", "₸")
DB_PATH = os.getenv("DB_PATH", "bendoshop.db")
COMMISSION = 30
DELIVERY_MINUTES = 60

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bendoshop")
user_router = Router()
admin_router = Router()
admin_router.message.filter(F.chat.type == "private", F.from_user.id == ADMIN_ID)
admin_router.callback_query.filter(F.from_user.id == ADMIN_ID)
SUB_OK = {"member", "administrator", "creator"}


def esc(v):
    return html.escape(str(v or ""))


def money(v):
    return f"{int(v):,}".replace(",", " ") + f" {CURRENCY}"


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [x if isinstance(x, InlineKeyboardButton) else InlineKeyboardButton(text=x[0], callback_data=x[1]) for x in row]
        for row in rows
    ])


def url_button(text, url):
    return InlineKeyboardButton(text=text, url=url)


db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys=ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT, accepted INTEGER NOT NULL DEFAULT 0,
 balance INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, country TEXT NOT NULL, registration TEXT NOT NULL,
 spamblock TEXT NOT NULL, email_status TEXT NOT NULL, status_label TEXT NOT NULL,
 price INTEGER NOT NULL, description TEXT NOT NULL, delivery TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'available', owner_id INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
 price INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', created_at INTEGER NOT NULL,
 delivered_at INTEGER, FOREIGN KEY(user_id) REFERENCES users(id),
 FOREIGN KEY(account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS inventory(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'held', acquired_at INTEGER NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS ledger(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, delta INTEGER NOT NULL,
 reason TEXT NOT NULL, ref_id INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resales(
 id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
 owner_id INTEGER NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'listed',
 created_at INTEGER NOT NULL
);
""")
db.commit()


def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    rows = cur.fetchall()
    db.commit()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def ensure_user(uid, username):
    q("INSERT INTO users(id,username,created_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username",
      (uid, username, int(time.time())))


def accepted(uid):
    u = q("SELECT accepted FROM users WHERE id=?", (uid,), True)
    return bool(u and u["accepted"])


def is_admin(uid):
    return uid == ADMIN_ID and ADMIN_ID != 0


def terms():
    p = Path(__file__).with_name("agreement.txt")
    return p.read_text(encoding="utf-8")


async def subscribed(bot, uid):
    if not REQUIRE_SUB or is_admin(uid):
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, uid)
        return member.status in SUB_OK or (member.status == "restricted" and member.is_member)
    except TelegramAPIError:
        log.exception("Не удалось проверить подписку. Проверьте CHANNEL_USERNAME и права бота.")
        return False


async def access(ev, bot):
    uid = ev.from_user.id
    if not await subscribed(bot, uid):
        text = f"Для доступа подпишитесь на канал {esc(CHANNEL)}, затем нажмите «Проверить подписку»."
        await show(ev, text, kb(
            [url_button("📢 Открыть канал", f"https://t.me/{CHANNEL.lstrip('@')}")],
            [("✅ Проверить подписку", "check_sub")]
        ))
        return False
    if not accepted(uid):
        await show(ev, terms(), kb([("✅ Принимаю соглашение", "accept")]))
        return False
    return True


async def show(ev, text, markup=None):
    try:
        if isinstance(ev, CallbackQuery):
            await ev.message.edit_text(text, reply_markup=markup)
            await ev.answer()
        else:
            await ev.answer(text, reply_markup=markup)
    except TelegramAPIError:
        if isinstance(ev, CallbackQuery):
            try:
                await ev.message.answer(text, reply_markup=markup)
                await ev.answer()
            except TelegramAPIError:
                pass


def menu_text(uid):
    u = q("SELECT balance FROM users WHERE id=?", (uid,), True) or {"balance": 0}
    return f"🏪 <b>BendoShop — магазин аккаунтов</b>\nБаланс: <b>{money(u['balance'])}</b>\n\nВыберите действие."


async def show_menu(ev):
    await show(ev, menu_text(ev.from_user.id), kb(
        [("🛍 Каталог аккаунтов", "catalog")],
        [("📦 Мой инвентарь", "inventory"), ("🧾 Мои заказы", "myorders")],
        [("💰 Баланс", "balance")],
        [("📄 Соглашение", "terms")]
    ))


@user_router.message(CommandStart())
async def start(m: Message, bot: Bot):
    ensure_user(m.from_user.id, m.from_user.username)
    if not await access(m, bot):
        return
    await show_menu(m)


@user_router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    if not await subscribed(bot, c.from_user.id):
        await show(c, "Подписка пока не найдена. Подпишитесь на канал и нажмите проверку ещё раз.",
                   kb([url_button("📢 Открыть канал", f"https://t.me/{CHANNEL.lstrip('@')}")],
                      [("🔄 Проверить", "check_sub")]))
        return
    if not accepted(c.from_user.id):
        await show(c, terms(), kb([("✅ Принимаю соглашение", "accept")]))
    else:
        await show_menu(c)


@user_router.callback_query(F.data == "accept")
async def accept(c: CallbackQuery):
    q("UPDATE users SET accepted=1 WHERE id=?", (c.from_user.id,))
    await show_menu(c)


@user_router.callback_query(F.data == "terms")
async def show_terms_cb(c: CallbackQuery):
    await show(c, terms(), kb([("⬅️ Меню", "menu")]))


@user_router.callback_query(F.data == "menu")
async def menu_cb(c: CallbackQuery, bot: Bot):
    if await access(c, bot):
        await show_menu(c)


@user_router.callback_query(F.data == "catalog")
async def catalog(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    countries = q("SELECT country,COUNT(*) n FROM accounts WHERE state='available' GROUP BY country ORDER BY country")
    if not countries:
        await show(c, "Сейчас нет аккаунтов в наличии.", kb([("⬅️ Меню", "menu")]))
        return
    await show(c, "🌍 <b>Выберите страну:</b>", kb(*[[(f"{x['country']} · {x['n']} шт.", f"country:{x['country']}")] for x in countries], [("⬅️ Меню", "menu")]))


@user_router.callback_query(F.data.startswith("country:"))
async def country(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    name = c.data.split(":", 1)[1]
    items = q("SELECT * FROM accounts WHERE country=? AND state='available' ORDER BY id", (name,))
    rows = [[(f"#{a['id']} · {money(a['price'])}", f"account:{a['id']}")] for a in items]
    rows.append([("⬅️ Страны", "catalog")])
    await show(c, f"Аккаунты: <b>{esc(name)}</b>", kb(*rows))


@user_router.callback_query(F.data.startswith("account:"))
async def account_detail(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    aid = int(c.data.split(":")[1])
    a = q("SELECT * FROM accounts WHERE id=? AND state='available'", (aid,), True)
    if not a:
        await show(c, "Этот аккаунт уже недоступен.", kb([("⬅️ Каталог", "catalog")]))
        return
    text = (f"🌎 Страна: <b>{esc(a['country'])}</b>\n"
            f"📅 Регистрация: {esc(a['registration'])}\n"
            f"🚫 Спамблок: {esc(a['spamblock'])}\n"
            f"📧 Почта: {esc(a['email_status'])}\n"
            f"📌 Статус: {esc(a['status_label'])}\n"
            f"📝 Описание: {esc(a['description'])}\n"
            f"💰 Цена: <b>{money(a['price'])}</b>")
    await show(c, text, kb([("✅ Купить", f"buy:{aid}")], [("⬅️ Назад", f"country:{a['country']}")]))


@user_router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    aid = int(c.data.split(":")[1])
    a = q("SELECT * FROM accounts WHERE id=? AND state='available'", (aid,), True)
    u = q("SELECT balance FROM users WHERE id=?", (c.from_user.id,), True)
    if not a:
        await show(c, "Аккаунт уже куплен или снят с продажи.", kb([("Каталог", "catalog")]))
        return
    if not u or u["balance"] < a["price"]:
        await show(c, "Недостаточно средств. Обратитесь к администратору для пополнения баланса.",
                   kb([("⬅️ Меню", "menu")]))
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute("UPDATE accounts SET state='reserved' WHERE id=? AND state='available'", (aid,))
        if cur.rowcount != 1:
            db.rollback()
            await show(c, "Аккаунт уже недоступен.", kb([("Каталог", "catalog")]))
            return
        cur = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (a["price"], c.from_user.id, a["price"]))
        if cur.rowcount != 1:
            db.rollback()
            await show(c, "Недостаточно средств.", kb([("Меню", "menu")]))
            return
        cur = db.execute("INSERT INTO orders(user_id,account_id,price,created_at) VALUES(?,?,?,?)",
                         (c.from_user.id, aid, a["price"], int(time.time())))
        oid = cur.lastrowid
        db.execute("INSERT INTO ledger(user_id,delta,reason,ref_id,created_at) VALUES(?,?,?,?,?)",
                   (c.from_user.id, -a["price"], "purchase", oid, int(time.time())))
        # If the admin relisted a gifted inventory item, credit 70% to its original owner.
        if a["owner_id"]:
            owner_share = a["price"] * (100 - COMMISSION) // 100
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (owner_share, a["owner_id"]))
            db.execute("INSERT INTO ledger(user_id,delta,reason,ref_id,created_at) VALUES(?,?,?,?,?)",
                       (a["owner_id"], owner_share, "resale_owner_share", oid, int(time.time())))
            db.execute("UPDATE inventory SET status='sold' WHERE account_id=? AND status='listed'", (aid,))
            db.execute("UPDATE resales SET status='sold' WHERE account_id=? AND status='listed'", (aid,))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("purchase failed")
        await show(c, "Не удалось оформить заказ. Попробуйте позже.", kb([("Меню", "menu")]))
        return
    await show(c, f"✅ Заказ <b>#{oid}</b> принят!\n\nСтрана: {esc(a['country'])}\nСтатус: ⏳ Ожидает выдачи\nОжидайте обработки в течение {DELIVERY_MINUTES} минут. После подтверждения покупки вы получите инструкцию.",
               kb([("🧾 Мои заказы", "myorders")], [("Меню", "menu")]))
    await bot.send_message(ADMIN_ID, f"🛒 Новый заказ #{oid}\nПокупатель: <code>{c.from_user.id}</code>\nАккаунт: #{aid} ({esc(a['country'])})\nЦена: {money(a['price'])}\nВыдать: <code>/deliver {oid} текст_выдачи</code>")


@user_router.callback_query(F.data == "myorders")
async def myorders(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    rows = q("SELECT o.*,a.country FROM orders o JOIN accounts a ON a.id=o.account_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10", (c.from_user.id,))
    if not rows:
        text = "У вас пока нет заказов."
    else:
        text = "\n\n".join(f"Заказ #{o['id']} · {esc(o['country'])}\nСтатус: {'✅ Выдан' if o['status']=='done' else '⏳ Ожидает выдачи'}" for o in rows)
    await show(c, text, kb([("⬅️ Меню", "menu")]))


@user_router.callback_query(F.data == "inventory")
async def inventory(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    rows = q("SELECT i.id inventory_id,a.id account_id,a.country,a.registration,a.status_label,i.status FROM inventory i JOIN accounts a ON a.id=i.account_id WHERE i.user_id=? ORDER BY i.id DESC", (c.from_user.id,))
    if not rows:
        await show(c, "Ваш инвентарь пока пуст.", kb([("⬅️ Меню", "menu")]))
        return
    text = "\n\n".join(f"🎁 Инвентарь #{r['inventory_id']} · аккаунт #{r['account_id']}\nСтрана: {esc(r['country'])}\nРегистрация: {esc(r['registration'])}\nСтатус: {esc(r['status_label'])}\nСостояние: {esc(r['status'])}" for r in rows)
    await show(c, text, kb([("⬅️ Меню", "menu")]))


@user_router.callback_query(F.data == "balance")
async def balance(c: CallbackQuery, bot: Bot):
    if not await access(c, bot):
        return
    u = q("SELECT balance FROM users WHERE id=?", (c.from_user.id,), True) or {"balance": 0}
    await show(c, f"💰 Ваш баланс: <b>{money(u['balance'])}</b>\nДля пополнения обратитесь к администратору.", kb([("⬅️ Меню", "menu")]))


# -------- Админ-команды --------

@admin_router.message(Command("admin"))
async def admin_help(m: Message):
    await m.answer(
        "<b>Админ-команды BendoShop</b>\n"
        "/addaccount страна | регистрация | спамблок | почта | статус | цена | описание | данные_выдачи\n"
        "/stock — остатки\n"
        "/orders — ожидающие заказы\n"
        "/deliver ID_заказа текст — выдать заказ\n"
        "/gift ID_покупателя | страна — подарить первый доступный аккаунт\n"
        "/resell ID_инвентаря цена — выставить подаренный аккаунт на продажу от имени магазина\n"
        "/addbalance ID сумма — пополнить баланс\n"
        "/notice текст — объявление всем в меню\n"
        "\nПокупатели не могут выставлять товары или продавать их самостоятельно."
    )


@admin_router.message(Command("addaccount"))
async def add_account(m: Message, command: CommandObject):
    args = command.args or ""
    parts = [x.strip() for x in args.split("|", 7)]
    if len(parts) != 8:
        await m.answer("Формат:\n/addaccount USA | 2012 | Нет | Привязана | Активен | 399 | Описание | Данные_для_выдачи")
        return
    country_name, reg, spam, email, status, price, desc, delivery = parts
    try:
        price = int(price)
        if price < 0:
            raise ValueError
    except ValueError:
        await m.answer("Цена должна быть целым числом не меньше 0.")
        return
    q("INSERT INTO accounts(country,registration,spamblock,email_status,status_label,price,description,delivery,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
      (country_name, reg, spam, email, status, price, desc, delivery, int(time.time())))
    new_id = q("SELECT last_insert_rowid() id", one=True)["id"]
    await m.answer(f"✅ Аккаунт добавлен. ID: <code>{new_id}</code>")


@admin_router.message(Command("stock"))
async def stock(m: Message):
    rows = q("SELECT id,country,registration,price,state FROM accounts ORDER BY id DESC LIMIT 50")
    if not rows:
        await m.answer("Склад пуст.")
        return
    await m.answer("\n".join(f"#{r['id']} · {esc(r['country'])} · {esc(r['registration'])} · {money(r['price'])} · {esc(r['state'])}" for r in rows))


@admin_router.message(Command("orders"))
async def orders(m: Message):
    rows = q("SELECT o.id,o.user_id,o.price,o.status,a.country,a.id account_id FROM orders o JOIN accounts a ON a.id=o.account_id WHERE o.status='waiting' ORDER BY o.id")
    if not rows:
        await m.answer("Ожидающих заказов нет.")
        return
    await m.answer("\n".join(f"#{r['id']} · покупатель {r['user_id']} · аккаунт #{r['account_id']} {esc(r['country'])} · {money(r['price'])}" for r in rows))


@admin_router.message(Command("deliver"))
async def deliver(m: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split(maxsplit=1)
    if len(args) != 2 or not args[0].isdigit():
        await m.answer("Формат: /deliver ID_заказа текст_выдачи")
        return
    oid, delivery_text = int(args[0]), args[1]
    o = q("SELECT o.*,a.id account_id,a.country,a.delivery FROM orders o JOIN accounts a ON a.id=o.account_id WHERE o.id=? AND o.status='waiting'", (oid,), True)
    if not o:
        await m.answer("Ожидающий заказ не найден.")
        return
    final_text = delivery_text if delivery_text != "AUTO" else o["delivery"]
    q("UPDATE orders SET status='done',delivered_at=? WHERE id=? AND status='waiting'", (int(time.time()), oid))
    q("UPDATE accounts SET state='sold' WHERE id=?", (o["account_id"],))
    await bot.send_message(o["user_id"],
        f"🎉 <b>Покупка подтверждена!</b>\nЗаказ: #{oid}\nСтрана: {esc(o['country'])}\n\n"
        f"<b>Данные выдачи:</b>\n<code>{esc(final_text)}</code>\n\n"
        "Инструкция:\n1. Смените пароль аккаунта на свой.\n2. Проверьте и привяжите свою личную почту, если это предусмотрено сервисом.\n"
        "3. Проверьте настройки безопасности и способы восстановления доступа.\n\n"
        "Не передавайте данные третьим лицам."
    )
    await m.answer(f"✅ Заказ #{oid} выдан покупателю.")


@admin_router.message(Command("gift"))
async def gift(m: Message, command: CommandObject, bot: Bot):
    parts = [x.strip() for x in (command.args or "").split("|", 1)]
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
        await m.answer("Формат: /gift ID_покупателя | USA")
        return
    uid, country_name = int(parts[0]), parts[1]
    ensure_user(uid, None)
    a = q("SELECT * FROM accounts WHERE country=? AND state='available' ORDER BY id LIMIT 1", (country_name,), True)
    if not a:
        await m.answer(f"Нет доступных аккаунтов страны {esc(country_name)}.")
        return
    q("UPDATE accounts SET state='gifted',owner_id=? WHERE id=? AND state='available'", (uid, a["id"]))
    q("INSERT INTO inventory(user_id,account_id,status,acquired_at) VALUES(?,?,?,?)", (uid, a["id"], "held", int(time.time())))
    await bot.send_message(uid, f"🎁 <b>Вам подарен аккаунт!</b>\nСтрана: {esc(a['country'])}\nРегистрация: {esc(a['registration'])}\nАккаунт добавлен в ваш инвентарь. Выставлять товары на продажу самостоятельно нельзя.")
    await m.answer(f"🎁 Аккаунт #{a['id']} добавлен в инвентарь пользователя {uid}.")


@admin_router.message(Command("resell"))
async def resell(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts):
        await m.answer("Формат: /resell ID_инвентаря цена")
        return
    iid, price = map(int, parts)
    inv = q("SELECT i.*,a.id account_id,a.state FROM inventory i JOIN accounts a ON a.id=i.account_id WHERE i.id=? AND i.status='held'", (iid,), True)
    if not inv or price <= 0:
        await m.answer("Запись инвентаря не найдена или цена некорректна.")
        return
    q("UPDATE inventory SET status='listed' WHERE id=?", (iid,))
    q("UPDATE accounts SET state='available',price=? WHERE id=?", (price, inv["account_id"]))
    q("INSERT INTO resales(inventory_id,account_id,owner_id,price,created_at) VALUES(?,?,?,?,?)",
      (iid, inv["account_id"], q("SELECT user_id FROM inventory WHERE id=?", (iid,), True)["user_id"], price, int(time.time())))
    await m.answer(f"✅ Аккаунт #{inv['account_id']} снова выставлен в каталог за {money(price)}. При успешной продаже магазин удержит 30%, владельцу будет начислено 70%.")


@admin_router.message(Command("addbalance"))
async def add_balance(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2:
        await m.answer("Формат: /addbalance ID сумма")
        return
    try:
        uid, amount = int(parts[0]), int(parts[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await m.answer("ID и сумма должны быть положительными числами.")
        return
    ensure_user(uid, None)
    q("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    q("INSERT INTO ledger(user_id,delta,reason,created_at) VALUES(?,?,?,?)", (uid, amount, "admin_topup", int(time.time())))
    await m.answer(f"Баланс пользователя {uid} пополнен на {money(amount)}.")


@admin_router.message(Command("notice"))
async def notice(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        await m.answer("Формат: /notice текст")
        return
    q("INSERT INTO settings(key,value) VALUES('notice',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (text,))
    await m.answer("Объявление сохранено.")


@user_router.message(Command("inventory"))
async def inventory_cmd(m: Message, bot: Bot):
    if not await access(m, bot):
        return
    rows = q("SELECT i.id inventory_id,a.id account_id,a.country,a.registration,a.status_label,i.status FROM inventory i JOIN accounts a ON a.id=i.account_id WHERE i.user_id=? ORDER BY i.id DESC", (m.from_user.id,))
    if not rows:
        await m.answer("Ваш инвентарь пуст.")
    else:
        await m.answer("\n\n".join(f"Инвентарь #{r['inventory_id']} · аккаунт #{r['account_id']}\n{esc(r['country'])} · {esc(r['registration'])}\n{esc(r['status_label'])}" for r in rows))


async def main():
    if not TOKEN or not ADMIN_ID:
        raise RuntimeError("Заполните 9151806238:AAGFAeL70FlOv0VQm5whKL9EMeYYeRnotk4 и 9068910605 в .env")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(user_router)
    log.info("BendoShop запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
