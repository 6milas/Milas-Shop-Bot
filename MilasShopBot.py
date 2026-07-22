import os
import asyncio
import aiosqlite
import logging
import re
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from telebot.async_telebot import AsyncTeleBot
from telebot import types
from telebot.asyncio_storage import StateMemoryStorage
from telebot.asyncio_handler_backends import State, StatesGroup
from telebot.asyncio_filters import StateFilter

# --- Flask Server для Render / Health Check ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# --- Конфигурация и Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8065739297:AAHHwRUAurmLeUUdxmwTE-znxFDWsB9dlE4"
if not TOKEN:
    raise ValueError("❌ Möhüm: TOKEN tapylmady! Environment variable (TOKEN) goşmagy ýatdan çykarmaň.")

SUPER_ADMIN = 7569831989  # Baş admin
BOT_USERNAME = "@MilasShopBot"

state_storage = StateMemoryStorage()
bot = AsyncTeleBot(TOKEN, state_storage=state_storage, parse_mode="HTML")

# --- Ýagdaýlar (FSM) ---
class AdminStates(StatesGroup):
    mailing = State()
    wait_bal_id = State(); wait_bal_amt = State()
    ban_id = State(); unban_id = State()
    add_admin = State(); del_admin = State()
    add_cat_name = State(); del_cat_id = State()
    add_item_name = State(); add_item_price = State()
    add_item_amount = State(); add_item_desc = State()      
    add_item_example = State(); add_item_cat = State()
    del_item_id = State()
    wait_tmt_number = State(); wait_db_file = State()
    wait_connect_user = State()

class ShopStates(StatesGroup): wait_pubg_id = State()
class TransferStates(StatesGroup): wait_target_id = State(); wait_amount = State(); confirm = State()
class TopupStates(StatesGroup): wait_amount = State(); wait_receipt = State()

# --- Baza Ulgamy we Atomar amallar (Goragly ASYNC) ---
DB_PATH = "/data/users.db" if os.path.exists("/data") else "users.db"

async def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    async with aiosqlite.connect(DB_PATH, timeout=20) as conn:
        cursor = await conn.execute(query, params)
        res = await cursor.fetchone() if fetchone else (await cursor.fetchall() if fetchall else None)
        if commit: await conn.commit()
        return res

async def change_balance(user_id, amount):
    """Atomar balans üýtgetmek (Race Conditions goragy - Async)"""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=20) as conn:
            await conn.execute("BEGIN IMMEDIATE TRANSACTION")
            cursor = await conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if not row or row[0] + amount < 0:
                await conn.rollback()
                return False
            await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            await conn.commit()
            return True
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return False

async def init_db():
    await db_query('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, is_banned INTEGER DEFAULT 0)''', commit=True)
    await db_query('''CREATE TABLE IF NOT EXISTS admins (admin_id INTEGER PRIMARY KEY)''', commit=True)
    await db_query('''CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)''', commit=True)
    await db_query('''CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, cat_id INTEGER, name TEXT, price REAL, amount TEXT, description TEXT DEFAULT 'Maglumat ýok', example_data TEXT DEFAULT 'ID ýazyň')''', commit=True)
    await db_query('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_name TEXT, amount TEXT, pubg_id TEXT, price REAL, status TEXT DEFAULT 'pending', message_id INTEGER)''', commit=True)
    await db_query('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''', commit=True)
    
    try: await db_query("ALTER TABLE users ADD COLUMN username TEXT", commit=True)
    except: pass
    try: await db_query("ALTER TABLE users ADD COLUMN joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP", commit=True)
    except: pass
    try: await db_query("ALTER TABLE orders ADD COLUMN description TEXT", commit=True)
    except: pass
    try: await db_query("ALTER TABLE orders ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP", commit=True)
    except: pass

    await db_query("INSERT OR IGNORE INTO settings (key, value) VALUES ('tmt_number', '+99360000000')", commit=True)
    await db_query("INSERT OR IGNORE INTO admins (admin_id) VALUES (?)", (SUPER_ADMIN,), commit=True)

async def is_admin(uid):
    return await db_query("SELECT admin_id FROM admins WHERE admin_id = ?", (uid,), fetchone=True) is not None

active_chats, waiting_list = {}, {}

# --- Klawiaturany Gurnamak ---
def get_main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(text="Dükana gir 🛒"))
    kb.row(types.KeyboardButton(text="Balans 💳"), types.KeyboardButton(text="Admini çagyr 👨‍💻"))
    return kb

def get_admin_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(text="📢 Habar ugrat", callback_data="adm_mail"), types.InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats"))
    kb.row(types.InlineKeyboardButton(text="💬 Ulanyja birikmek", callback_data="adm_connect_user"), types.InlineKeyboardButton(text="📦 Hemme sargytlar", callback_data="adm_orders_menu"))
    kb.row(types.InlineKeyboardButton(text="➕ Balans goş", callback_data="adm_add"), types.InlineKeyboardButton(text="➖ Balans aýyr", callback_data="adm_sub"))
    kb.row(types.InlineKeyboardButton(text="👥 Ulanyjylar", callback_data="adm_users_list_0"))
    kb.row(types.InlineKeyboardButton(text="💳 Töleg Sazlamalary", callback_data="adm_payment_set"))
    kb.row(types.InlineKeyboardButton(text="🚫 Blokla", callback_data="adm_ban"), types.InlineKeyboardButton(text="✅ Blokdan aç", callback_data="adm_unban"))
    kb.row(types.InlineKeyboardButton(text="👤 Admin goş", callback_data="adm_add_admin"), types.InlineKeyboardButton(text="🗑 Admin aýyr", callback_data="adm_del_admin"))
    kb.row(types.InlineKeyboardButton(text="📂 Kategoriýa goş", callback_data="adm_add_cat"), types.InlineKeyboardButton(text="📂 Kategoriýa aýyr", callback_data="adm_del_cat"))
    kb.row(types.InlineKeyboardButton(text="📦 Haryt goş", callback_data="adm_add_item"), types.InlineKeyboardButton(text="📦 Haryt aýyr", callback_data="adm_del_item"))
    kb.row(types.InlineKeyboardButton(text="📜 Admin sanawy", callback_data="adm_list"), types.InlineKeyboardButton(text="📝 Ban sanawy", callback_data="adm_banlist"))
    kb.row(types.InlineKeyboardButton(text="💾 BD Ýükläp Al", callback_data="adm_dl_db"), types.InlineKeyboardButton(text="📂 BD Goý", callback_data="adm_ul_db"))
    return kb

# --- Esasy Handlerlar ---
@bot.message_handler(commands=['start'])
async def start(message):
    uid = message.from_user.id
    uname = message.from_user.username or message.from_user.full_name
    
    if not await db_query("SELECT user_id FROM users WHERE user_id = ?", (uid,), fetchone=True):
        await db_query("INSERT INTO users (user_id, username) VALUES (?, ?)", (uid, uname), commit=True)
    else:
        await db_query("UPDATE users SET username = ? WHERE user_id = ?", (uname, uid), commit=True)
    
    welcome_text = (
        "Salam! Sanly dükanymyza hoş geldiňiz!\n\n"
        "Bu ýerde sanly önümleri aňsat we ygtybarly satyn alyp bilersiňiz.\n\n"
        "Balansyňyzy doldurmak we dükanda bolmadyk önümleri sargyt etmek üçin ýa-da soraglaryňyz bar bolsa \"Admini çagyr\" düwmesine basyň.\n\n"
        "Täze söwda tejribesine taýyn bolsaňyz, başlalyň! ✨"
    )
    await bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_kb())

# --- BALANS ---
@bot.message_handler(func=lambda m: m.text == "Balans 💳")
async def show_balance(message):
    uid = message.from_user.id
    user = await db_query("SELECT balance, is_banned FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if user and user[1] == 1: return 
    
    bal_tmt = user[0] if user else 0.0
    balance_text = f"<b>Balans ID:</b> <code>{uid}</code>\n<b>TMT:</b> {bal_tmt:.2f}"
    
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="💳 Balans doldurmak", callback_data="topup_start"))
    await bot.send_message(message.chat.id, balance_text, reply_markup=kb)

# --- BALANS DOLDURMAK ---
@bot.callback_query_handler(func=lambda c: c.data == "topup_start")
async def topup_start_cb(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="cancel_action"))
    await bot.edit_message_text("💰 <b>Näçe TMT doldurmak isleýärsiňiz?</b>\n\n<i>Diňe san bilen ýazyň (Meselem: 10):</i>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.set_state(c.from_user.id, TopupStates.wait_amount, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_action")
async def cancel_action_cb(c):
    await bot.delete_state(c.from_user.id, c.message.chat.id)
    await bot.delete_message(c.message.chat.id, c.message.message_id)
    await bot.answer_callback_query(c.id, "Amal ýatyryldy.")

@bot.message_handler(state=TopupStates.wait_amount, content_types=['text'])
async def topup_amount_msg(m):
    try:
        amt = float(m.text)
        if amt <= 0: raise ValueError
    except: return await bot.send_message(m.chat.id, "❌ Nädogry mukdar! Täzeden diňe san ýazyň:")
    
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        data['topup_amount'] = amt
        
    tmt_query = await db_query("SELECT value FROM settings WHERE key='tmt_number'", fetchone=True)
    tmt_num = tmt_query[0] if tmt_query else "+99360000000"
    
    text = (f"🇹🇲 <b>TMT arkaly töleg</b>\n\nŞu belgä <b>{amt:.2f} TMT</b> geçiriň:\n📞 <code>{tmt_num}</code>\n\n"
            "📸 Tölegi edeniňizden soňra, <b>TÖLEGIŇ SKRINŞOTYNY ugradyň.</b>")
    
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="cancel_action"))
    await bot.send_message(m.chat.id, text, reply_markup=kb)
    await bot.set_state(m.from_user.id, TopupStates.wait_receipt, m.chat.id)

@bot.message_handler(state=TopupStates.wait_receipt, content_types=['photo'])
async def topup_receipt_msg(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        amt = data.get("topup_amount")
        
    uid = m.from_user.id
    uname = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    
    admin_text = f"🚨 <b>Täze Balans Dolduryş Talaby!</b>\n\n👤 Ulanyjy: <code>{uid}</code> ({uname})\n💰 Mukdar: <b>{amt:.2f} TMT</b>"
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(text="✅ Tassykla", callback_data=f"adm_topup_acc_{uid}_{amt}"), types.InlineKeyboardButton(text="❌ Ret et", callback_data=f"adm_topup_rej_{uid}"))
    
    admins = await db_query("SELECT admin_id FROM admins", fetchall=True)
    if admins:
        for (a_id,) in admins:
            try: await bot.send_photo(a_id, photo=m.photo[-1].file_id, caption=admin_text, reply_markup=kb)
            except: pass
    await bot.send_message(m.chat.id, "✅ Siziň ýüzlenmäňiz kabul edildi we admine ugradyldy. Tassyklanmagyna garaşyň! ⏳")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_topup_"))
async def admin_topup_resolve(c):
    parts = c.data.split('_')
    action, uid = parts[2], int(parts[3])
    
    if action == "acc":
        amt = float(parts[4])
        await change_balance(uid, amt)
        await bot.edit_message_caption(caption=c.message.caption + "\n\n<b>✅ Tassyklanan.</b>", chat_id=c.message.chat.id, message_id=c.message.message_id)
        try: await bot.send_message(uid, f"🎉 <b>Üstünlikli!</b>\nSiziň balansyňyza <b>{amt:.2f} TMT</b> goşuldy!")
        except: pass
    elif action == "rej":
        await bot.edit_message_caption(caption=c.message.caption + "\n\n<b>❌ Ret edilen.</b>", chat_id=c.message.chat.id, message_id=c.message.message_id)
        try: await bot.send_message(uid, "❌ Siziň balans doldurmak ýüzlenmäňiz ret edildi.")
        except: pass
    await bot.answer_callback_query(c.id)

# --- PULDAN PUL GEÇIRMEK (/0804) ---
@bot.message_handler(commands=['0804'])
async def transfer_start(m):
    user_data = await db_query("SELECT is_banned FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if user_data and user_data[0] == 1: return
    
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(m.chat.id, "Kabul edijiniň balans ID-si?", reply_markup=kb)
    await bot.set_state(m.from_user.id, TransferStates.wait_target_id, m.chat.id)

@bot.message_handler(state=TransferStates.wait_target_id, content_types=['text'])
async def tr_id_check(m):
    target_id = m.text
    target_exists = await db_query("SELECT user_id FROM users WHERE user_id = ?", (target_id,), fetchone=True)
    
    if not target_id.isdigit() or int(target_id) == m.from_user.id or not target_exists:
        return await bot.send_message(m.chat.id, "Balans ID ýalňyş. Başdan synanyşyň.")
    
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        data['target_id'] = int(target_id)
        
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(m.chat.id, f"Balans ID: <code>{target_id}</code>\n Näçe TMT?", reply_markup=kb)
    await bot.set_state(m.from_user.id, TransferStates.wait_amount, m.chat.id)

@bot.message_handler(state=TransferStates.wait_amount, content_types=['text'])
async def tr_amt_check(m):
    try:
        amt = float(m.text)
        if amt <= 0: raise ValueError
    except: return await bot.send_message(m.chat.id, "Ýalňyş! Pul mukdary nädogry. Gaýtadan synanşyň.")
    
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        data['amount'] = amt
        target_id = data['target_id']
        
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(text="Ýalňyş", callback_data="cancel_action"), types.InlineKeyboardButton(text="Dogry", callback_data="confirm_transfer"))
    await bot.send_message(m.chat.id, f"Balans ID: <code>{target_id}</code>\n {amt:.2f} TMT", reply_markup=kb)
    await bot.set_state(m.from_user.id, TransferStates.confirm, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "confirm_transfer", state=TransferStates.confirm)
async def tr_final(c):
    async with bot.retrieve_data(c.from_user.id, c.message.chat.id) as data:
        target_id = data['target_id']
        amount = data['amount']
        
    sender_id = c.from_user.id
    
    if await change_balance(sender_id, -amount):
        await change_balance(target_id, amount)
        await bot.edit_message_text(f"✅ <b>Geçirim amala aşyryldy!</b>\n\nID <code>{target_id}</code> belgili ulanyja <b>{amount:.2f} TMT</b> ugradyldy.", chat_id=c.message.chat.id, message_id=c.message.message_id)
        try: await bot.send_message(target_id, f"💰 <b>Hasabyňyz dolduryldy!</b>\nSize <code>{sender_id}</code> ID-li ulanyjydan <b>{amount:.2f} TMT</b> geçirim geldi.")
        except: pass
    else:
        await bot.edit_message_text("Hasabyňyzda ýeterlik serişde ýok! Geçirim ýatyryldy.", chat_id=c.message.chat.id, message_id=c.message.message_id)
        
    await bot.delete_state(c.from_user.id, c.message.chat.id)
    await bot.answer_callback_query(c.id)

# --- DÜKAN ULGAMY ---
@bot.message_handler(func=lambda m: m.text == "Dükana gir 🛒")
async def shop_start(m):
    cats = await db_query("SELECT id, name FROM categories", fetchall=True)
    if not cats: return await bot.send_message(m.chat.id, "Dükanda häzirlikçe haryt ýok. 😔")
    kb = types.InlineKeyboardMarkup()
    for c in cats:
        kb.add(types.InlineKeyboardButton(text=c[1], callback_data=f"shopcat_{c[0]}_0"))
    await bot.send_message(m.chat.id, "Dükana girmek üçin aşaky kategoriýa saýlaň:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("shopcat_"))
async def shop_show_items(c):
    parts = c.data.split("_")
    cat_id, page = int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    items = await db_query("SELECT id, name, price FROM items WHERE cat_id = ?", (cat_id,), fetchall=True)
    
    if not items:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="shop_back"))
        await bot.edit_message_text("Bu kategoriýada haryt ýok.", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
        return await bot.answer_callback_query(c.id)
    
    ITEMS_PER_PAGE = 10
    total_pages = max(1, (len(items) - 1) // ITEMS_PER_PAGE + 1)
    page_items = items[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]
    
    kb = types.InlineKeyboardMarkup()
    for i in page_items:
        kb.add(types.InlineKeyboardButton(text=f"{i[1]} - {i[2]:.2f} TMT", callback_data=f"shopitem_{i[0]}"))
    
    nav = []
    if page > 0: nav.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"shopcat_{cat_id}_{page-1}"))
    if total_pages > 1: nav.append(types.InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1: nav.append(types.InlineKeyboardButton(text="➡️", callback_data=f"shopcat_{cat_id}_{page+1}"))
    if nav: kb.row(*nav)
        
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="shop_back"))
    await bot.edit_message_text("Satyn aljak harydyňyzy saýlaň:", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "shop_back")
async def shop_back(c):
    cats = await db_query("SELECT id, name FROM categories", fetchall=True)
    kb = types.InlineKeyboardMarkup()
    for ct in cats:
        kb.add(types.InlineKeyboardButton(text=ct[1], callback_data=f"shopcat_{ct[0]}_0"))
    await bot.edit_message_text("Dükana girmek üçin aşaky kategoriýa saýlaň:", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("shopitem_"))
async def show_item_details(c):
    item = await db_query("SELECT id, name, price, amount, description, cat_id FROM items WHERE id = ?", (int(c.data.split("_")[1]),), fetchone=True)
    if not item: return await bot.answer_callback_query(c.id, "Haryt tapylmady.", show_alert=True)
    
    text = f"<b>{item[1]}</b>\nMukdary: {item[3]}\nJemi töleg: {item[2]:.2f} TMT\nBarada: {item[4]}\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Sargyt etmek", callback_data=f"buy_{item[0]}"))
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data=f"shopcat_{item[5]}_0"))
    await bot.edit_message_text(text, chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
async def buy_item_start(c):
    item = await db_query("SELECT items.id, categories.name, items.price, items.amount, items.description, items.example_data FROM items JOIN categories ON items.cat_id = categories.id WHERE items.id = ?", (int(c.data.split("_")[1]),), fetchone=True)
    if not item: return await bot.answer_callback_query(c.id, "Haryt tapylmady.", show_alert=True)
    
    user = await db_query("SELECT balance FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not user or user[0] < item[2]: return await bot.answer_callback_query(c.id, "Hasabyňyzda ýeterlik serişde ýok! Balansyňyzy dolduryň.", show_alert=True)
    
    async with bot.retrieve_data(c.from_user.id, c.message.chat.id) as data:
        data['item_id'] = item[0]
        data['name'] = item[1]
        data['price'] = item[2]
        data['amount'] = item[3]
        data['desc'] = item[4]
        data['example'] = item[5]
        
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, f"Haryt: <b>{item[1]}</b>\nBaha: <b>{item[2]:.2f} TMT</b>\n\nNirä ugratmaly? ({item[5]}):", reply_markup=kb)
    await bot.set_state(c.from_user.id, ShopStates.wait_pubg_id, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=ShopStates.wait_pubg_id, content_types=['text'])
async def process_purchase(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        price = data['price']
        name = data['name']
        amount = data['amount']
        desc = data['desc']
        
    uid = m.from_user.id
    
    if await change_balance(uid, -price):
        await db_query("INSERT INTO orders (user_id, item_name, amount, pubg_id, price, status, description) VALUES (?, ?, ?, ?, ?, 'pending_user', ?)", 
                 (uid, name, amount, m.text, price, desc), commit=True)
        order_id = (await db_query("SELECT MAX(id) FROM orders", fetchone=True))[0]
        
        ticket_text = f"<blockquote>Sargyt ID: {order_id}</blockquote>\n<blockquote>{name}\nMukdary: {amount}\nNirä: {m.text}\nJemi töleg: {price:.2f} TMT\nBarada: {desc}</blockquote>\nEger sargyt size degişli bolsa, tassyklaň."
        
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton(text="Tassykla", callback_data=f"usrconf_{order_id}"), types.InlineKeyboardButton(text="Ret et", callback_data=f"usrcanc_{order_id}"))
        sent_msg = await bot.send_message(m.chat.id, ticket_text, reply_markup=kb)
        await db_query("UPDATE orders SET message_id = ? WHERE id = ?", (sent_msg.message_id, order_id), commit=True)
    else:
        await bot.send_message(m.chat.id, "Söhbetdeşlik wagtynda balansyňyz üýtgedi. Ýeterlik serişde ýok.")
        
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("usrcanc_"))
async def user_cancel_order(c):
    order = await db_query("SELECT user_id, price, status FROM orders WHERE id = ?", (int(c.data.split("_")[1]),), fetchone=True)
    if order and order[2] == 'pending_user':
        await change_balance(order[0], order[1])
        await db_query("UPDATE orders SET status = 'cancelled' WHERE id = ?", (int(c.data.split("_")[1]),), commit=True)
        await bot.edit_message_text("Siz bu sargydy ret etdiňiz.", chat_id=c.message.chat.id, message_id=c.message.message_id)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("usrconf_"))
async def user_confirm_order(c):
    order_id = int(c.data.split("_")[1])
    order = await db_query("SELECT orders.user_id, orders.item_name, orders.amount, orders.pubg_id, orders.price, orders.status, orders.description, users.username FROM orders LEFT JOIN users ON orders.user_id = users.user_id WHERE orders.id = ?", (order_id,), fetchone=True)
    
    if order and order[5] == 'pending_user':
        await db_query("UPDATE orders SET status = 'pending_admin' WHERE id = ?", (order_id,), commit=True)
        uname = order[7] if order[7] else "Näbelli"
        
        new_text = f"<blockquote>Sargyt ID: {order_id}</blockquote>\n<blockquote>{order[1]}\nMukdary: {order[2]}\nNirä: {order[3]}\nJemi töleg: {order[4]:.2f} TMT</blockquote>\nSiziň sargydyňyz admine iberildi."
        await bot.edit_message_text(new_text, chat_id=c.message.chat.id, message_id=c.message.message_id)
        
        adm_text = f"🚨 <b>Täze Sargyt!</b>\n\n👤 Ulanyjy: @{uname} (<code>{order[0]}</code>)\n📦 Haryt: {order[1]}\n🔢 Mukdary: {order[2]}\n📍 Nirä: {order[3]}\n💰 Töleg: {order[4]:.2f} TMT\n📝 <b>Barada:</b> {order[6]}\n🆔 Sargyt ID: {order_id}"
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton(text="Tassykla ✅", callback_data=f"conforder_{order_id}"), types.InlineKeyboardButton(text="Ret et ❌", callback_data=f"rejorder_{order_id}"))
        
        admins = await db_query("SELECT admin_id FROM admins", fetchall=True)
        if admins:
            for (a_id,) in admins:
                try: await bot.send_message(a_id, adm_text, reply_markup=kb)
                except: pass
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("conforder_"))
async def confirm_order(c):
    order_id = int(c.data.split("_")[1])
    order = await db_query("SELECT user_id, item_name, amount, pubg_id, price, status, message_id, description FROM orders WHERE id = ?", (order_id,), fetchone=True)
    
    if order and order[5] == 'pending_admin':
        await db_query("UPDATE orders SET status = 'completed' WHERE id = ?", (order_id,), commit=True)
        final_text = f"<blockquote>Sargyt ID: {order_id}</blockquote>\n<blockquote>{order[1]}\nMukdary: {order[2]}\nNirä: {order[3]}\nJemi töleg: {order[4]:.2f} TMT</blockquote>\n✅ Tabşyryldy."
        await bot.edit_message_text(final_text, chat_id=c.message.chat.id, message_id=c.message.message_id)
        try: 
            await bot.edit_message_text(chat_id=order[0], message_id=order[6], text=final_text)
            await bot.send_message(chat_id=order[0], text="📦 Sargydyňyz tabşyryldy", reply_to_message_id=order[6])
        except Exception as e: logger.error(f"Failed to edit user order msg: {e}")
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rejorder_"))
async def reject_order(c):
    order_id = int(c.data.split("_")[1])
    order = await db_query("SELECT user_id, item_name, price, status, message_id FROM orders WHERE id = ?", (order_id,), fetchone=True)
    
    if order and order[3] == 'pending_admin':
        await db_query("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,), commit=True)
        await change_balance(order[0], order[2])
        
        final_text = (c.message.text or "") + "\n\n❌ <b>Ret edildi. Pul gaýtaryldy.</b>"
        await bot.edit_message_text(final_text, chat_id=c.message.chat.id, message_id=c.message.message_id)
        try: 
            user_msg = f"❌ <b>Sargydyňyz ret edildi!</b>\nSargyt ID: {order_id}\n\n{order[2]:.2f} TMT balansyňyza yzyna gaýtaryldy."
            await bot.edit_message_text(chat_id=order[0], message_id=order[4], text=user_msg)
            await bot.send_message(chat_id=order[0], text="❌ Sargydyňyz ret edildi", reply_to_message_id=order[4])
        except Exception as e: logger.error(f"Failed to edit user order msg: {e}")
    await bot.answer_callback_query(c.id)

# --- ADMIN ÇAGYR ---
@bot.message_handler(func=lambda m: m.text == "Admini çagyr 👨‍💻")
async def call_admin(m):
    uid = m.from_user.id
    user_data = await db_query("SELECT is_banned FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if user_data and user_data[0] == 1: return

    if uid in active_chats: return await bot.send_message(m.chat.id, "Siz häzir hem admin bilen söhbetdeşlikde. Ýapmak üçin 👉 /stop 👈")
    if uid in waiting_list: return await bot.send_message(m.chat.id, "Admin söhbetdeşligi kabul etýänçä garaşyň.")

    waiting_list[uid] = True
    await bot.send_message(m.chat.id, "Admin söhbetdeşligi kabul etýänçä garaşyň. Size habar beriler.")
    
    username = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    admins = await db_query("SELECT admin_id FROM admins", fetchall=True)
    if admins:
        for (adm_id,) in admins:
            try: 
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton(text="Kabul et ✅", callback_data=f"acc_{uid}"))
                await bot.send_message(adm_id, f"🔔 <b>Täze sorag!</b>\n👤 Ulanyjy: {username}\n🆔 ID: <code>{uid}</code>", reply_markup=kb)
            except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("acc_"))
async def accept_chat(c):
    uid = int(c.data.split("_")[1])
    if uid in waiting_list:
        del waiting_list[uid]
        active_chats[uid] = c.from_user.id
        active_chats[c.from_user.id] = uid
        await bot.edit_message_text((c.message.text or "") + "\n\n✅ <b>Kabul edildi!</b>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=None)
        await bot.send_message(uid, "Söhbetdeşlik kabul edildi. Mundan beýläk ugradan zatlaryňyz admine barar. Ýapmak üçin: /stop")
    else: await bot.edit_message_text(f"⚠️ Bu sorag eýýäm kabul edildi ýa-da işjeň däl.", chat_id=c.message.chat.id, message_id=c.message.message_id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(commands=['stop'])
async def stop_chat(m):
    uid = m.from_user.id
    if uid in active_chats:
        recipient_id = active_chats.pop(uid)
        active_chats.pop(recipient_id, None)
        await bot.send_message(m.chat.id, "Söhbetdeşlik tamamlandy. ✅")
        try: await bot.send_message(recipient_id, "Söhbetdeşlik tamamlandy. 🏁")
        except: pass
    elif uid in waiting_list:
        del waiting_list[uid]
        await bot.send_message(m.chat.id, "Siziň garaşmagyňyz ýatyryldy. ❌")
    else: await bot.send_message(m.chat.id, "Siz häzir hiç hili söhbetdeşlikde däl. 🤷")

# --- ADMIN BÖLÜMI ---
@bot.message_handler(commands=['admin'])
async def admin_cmd(m):
    if await is_admin(m.from_user.id): 
        await bot.send_message(m.chat.id, f"🔧 <b>Milas Shop Admin Paneli:</b>", reply_markup=get_admin_kb())

@bot.callback_query_handler(func=lambda c: c.data == "admin_home")
async def admin_home_cb(c):
    await bot.edit_message_text("🔧 <b>Milas Shop Admin Paneli:</b>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=get_admin_kb())
    await bot.answer_callback_query(c.id)

# --- ULANYJYLAR BÖLÜMI (TÄZE) ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_users_list_"))
async def adm_users_list_cb(c):
    page = int(c.data.split("_")[3])
    users = await db_query("SELECT user_id, username FROM users ORDER BY user_id DESC", fetchall=True)

    if not users:
        return await bot.answer_callback_query(c.id, "Ulanyjy tapylmady.", show_alert=True)
    
    ITEMS_PER_PAGE = 10
    total_pages = max(1, (len(users) - 1) // ITEMS_PER_PAGE + 1)
    page_users = users[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]
    
    kb = types.InlineKeyboardMarkup()
    for u in page_users:
        uname = f"@{u[1]}" if u[1] else "Näbelli"
        kb.add(types.InlineKeyboardButton(text=f"👤 {uname} | {u[0]}", callback_data=f"adm_user_view_{u[0]}_{page}"))
    
    nav = []
    if page > 0: nav.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_list_{page-1}"))
    if total_pages > 1: nav.append(types.InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1: nav.append(types.InlineKeyboardButton(text="➡️", callback_data=f"adm_users_list_{page+1}"))
    if nav: kb.row(*nav)
    
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="admin_home"))
    await bot.edit_message_text(f"👥 <b>Ulanyjylar ({len(users)}):</b>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_user_view_"))
async def adm_user_view_cb(c):
    parts = c.data.split("_")
    uid = int(parts[3])
    page = parts[4] if len(parts) > 4 else "0"
    
    u = await db_query("SELECT user_id, username, balance FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not u:
        return await bot.answer_callback_query(c.id, "Ulanyjy tapylmady!", show_alert=True)
    
    uname = f"@{u[1]}" if u[1] else "Näbelli"
    text = (f"👤 <b>Ulanyjy Maglumaty:</b>\n\n"
            f"🆔 <b>ID:</b> <code>{u[0]}</code>\n"
            f"📝 <b>Username:</b> {uname}\n"
            f"💰 <b>Balans:</b> {u[2]:.2f} TMT")
    
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(text="➕ Balans goşmak", callback_data=f"adm_user_addbal_{u[0]}_{page}"),
           types.InlineKeyboardButton(text="➖ Balans aýyrmak", callback_data=f"adm_user_subbal_{u[0]}_{page}"))
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data=f"adm_users_list_{page}"))
    
    await bot.edit_message_text(text, chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_user_addbal_") or c.data.startswith("adm_user_subbal_"))
async def adm_user_quickbal_cb(c):
    parts = c.data.split("_")
    op_type = parts[2]
    uid = int(parts[3])
    
    async with bot.retrieve_data(c.from_user.id, c.message.chat.id) as data:
        data['bal_op'] = "+" if op_type == "addbal" else "-"
        data['tid'] = uid
        
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    
    op_text = "goşmak" if op_type == "addbal" else "aýyrmak"
    await bot.send_message(c.message.chat.id, f"ID <code>{uid}</code> üçin balans {op_text}.\n\nMukdar (TMT)?", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.wait_bal_amt, c.message.chat.id)
    await bot.answer_callback_query(c.id)

# --- ULANYJA GÖNI BIRIKMEK ---
@bot.callback_query_handler(func=lambda c: c.data == "adm_connect_user")
async def adm_connect_start(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr ❌", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "💬 <b>Ulanyja birikmek</b>\n\nUlanyjynyň ID-sini ýa-da @ýüzüneýmini (username) ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.wait_connect_user, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.wait_connect_user, content_types=['text'])
async def adm_connect_fin(m):
    query = m.text.strip()
    match = re.search(r'@?([a-zA-Z0-9_]+)', query)
    if not match:
        return await bot.send_message(m.chat.id, "❌ <b>Nädogry format!</b> Täzeden ID ýa-da username ýazyň:")
    
    clean_query = match.group(1)
    
    if clean_query.isdigit():
        user = await db_query("SELECT user_id FROM users WHERE user_id = ?", (int(clean_query),), fetchone=True)
    else:
        user = await db_query("SELECT user_id FROM users WHERE username = ?", (clean_query,), fetchone=True)
    
    if not user:
        return await bot.send_message(m.chat.id, "❌ <b>Ulanyjy tapylmady!</b> Täzeden ID ýa-da username ýazyň:")
    
    uid = user[0]
    admin_id = m.from_user.id
    
    if admin_id in active_chats:
        return await bot.send_message(m.chat.id, "⚠️ <b>Siz eýýäm söhbetdeşlikde!</b>\nÖňküsini ýapmak üçin 👉 /stop ýazyň.")
    if uid in active_chats:
        return await bot.send_message(m.chat.id, "⚠️ <b>Bu ulanyjy häzirki wagtda başga admin bilen gürleşýär!</b>")
    
    active_chats[admin_id] = uid
    active_chats[uid] = admin_id
    if uid in waiting_list: del waiting_list[uid]
    
    await bot.send_message(m.chat.id, f"✅ <b>Üstünlikli!</b> Siz <code>{uid}</code> ID-li ulanyjy bilen birikdiňiz! 💬\n\nIndi ýazan hatlaryňyz göni ulanyja barar. Ýapmak üçin: /stop")
    try: 
        await bot.send_message(uid, "👨‍💻 <b>Admin size birikdi!</b> 🤝\n\nSoraglaryňyzy arkaýyn ýazyp bilersiňiz. 💬\nSöhbetdeşligi ýapmak üçin: /stop")
    except: 
        await bot.send_message(m.chat.id, "⚠️ Ulanyjy boty bloklapdyr ýa-da işjeň däl.")
        
    await bot.delete_state(m.from_user.id, m.chat.id)

# --- HEMME SARGYTLAR BÖLÜMI ---
@bot.callback_query_handler(func=lambda c: c.data == "adm_orders_menu")
async def adm_orders_menu(c):
    succ = (await db_query("SELECT COUNT(*) FROM orders WHERE status='completed'", fetchone=True))[0]
    fail = (await db_query("SELECT COUNT(*) FROM orders WHERE status='cancelled'", fetchone=True))[0]
    pend = (await db_query("SELECT COUNT(*) FROM orders WHERE status='pending_admin'", fetchone=True))[0]
    tot = (await db_query("SELECT COUNT(*) FROM orders", fetchone=True))[0]
    
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text=f"Hemme sargytlar ({tot})", callback_data="adm_ords_all_0"))
    kb.row(types.InlineKeyboardButton(text=f"Üstünlikli ✅ ({succ})", callback_data="adm_ords_succ_0"), types.InlineKeyboardButton(text=f"Garaşylýar ⏳ ({pend})", callback_data="adm_ords_pend_0"))
    kb.add(types.InlineKeyboardButton(text=f"Şowsuz ❌ ({fail})", callback_data="adm_ords_fail_0"))
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="admin_home"))
    
    await bot.edit_message_text("📦 <b>Sargytlar Bölümi:</b>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ords_"))
async def adm_ords_list(c):
    otype, page = c.data.split('_')[2], int(c.data.split('_')[3])
    
    where = ""
    if otype == "succ": where = "WHERE status = 'completed'"
    elif otype == "fail": where = "WHERE status = 'cancelled'"
    elif otype == "pend": where = "WHERE status = 'pending_admin'"
    
    ords = await db_query(f"SELECT id, item_name, price, status FROM orders {where} ORDER BY id DESC", fetchall=True) or []
    
    ITEMS_PER_PAGE = 10
    total_pages = max(1, (len(ords) - 1) // ITEMS_PER_PAGE + 1)
    page_ords = ords[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]
    
    kb = types.InlineKeyboardMarkup()
    for o in page_ords:
        st_emoji = "✅" if o[3] == 'completed' else ("❌" if o[3] == 'cancelled' else "⏳")
        kb.add(types.InlineKeyboardButton(text=f"ID:{o[0]} | {o[1]} - {st_emoji}", callback_data=f"adm_ord_{o[0]}_{otype}_{page}"))
    
    nav = []
    if page > 0: nav.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"adm_ords_{otype}_{page-1}"))
    if total_pages > 1: nav.append(types.InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1: nav.append(types.InlineKeyboardButton(text="➡️", callback_data=f"adm_ords_{otype}_{page+1}"))
    if nav: kb.row(*nav)
    
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data="adm_orders_menu"))
    await bot.edit_message_text(f"📦 <b>Sargytlar ({otype}):</b>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ord_"))
async def adm_ord_detail(c):
    parts = c.data.split('_')
    oid, otype, page = int(parts[2]), parts[3] if len(parts) > 3 else "all", parts[4] if len(parts) > 4 else "0"
    
    o = await db_query("SELECT orders.id, orders.user_id, orders.item_name, orders.amount, orders.pubg_id, orders.price, orders.status, orders.description, users.username FROM orders LEFT JOIN users ON orders.user_id = users.user_id WHERE orders.id = ?", (oid,), fetchone=True)
    if not o: return await bot.answer_callback_query(c.id, "Tapylmady!")
    
    st_emoji = "✅" if o[6] == 'completed' else ("❌" if o[6] == 'cancelled' else "⏳")
    uname = o[8] if o[8] else "Näbelli"
    
    text = (f"📦 <b>Sargyt ID:</b> {o[0]}\n"
            f"👤 <b>Ulanyjy:</b> @{uname} (<code>{o[1]}</code>)\n"
            f"🛒 <b>Haryt:</b> {o[2]}\n"
            f"🔢 <b>Mukdar:</b> {o[3]}\n"
            f"📍 <b>Nirä:</b> {o[4]}\n"
            f"💰 <b>Baha:</b> {o[5]:.2f} TMT\n"
            f"ℹ️ <b>Barada:</b> {o[7]}\n"
            f"📊 <b>Ýagdaýy:</b> {st_emoji} ({o[6]})")
    
    kb = types.InlineKeyboardMarkup()
    if o[6] == 'pending_admin':
        kb.row(types.InlineKeyboardButton(text="Tassykla ✅", callback_data=f"conforder_{o[0]}"), types.InlineKeyboardButton(text="Ret et ❌", callback_data=f"rejorder_{o[0]}"))
    
    kb.add(types.InlineKeyboardButton(text="⬅️ Yza", callback_data=f"adm_ords_{otype}_{page}"))
    await bot.edit_message_text(text, chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_stats")
async def show_stats(c):
    users = (await db_query("SELECT COUNT(*) FROM users", fetchone=True))[0]
    bal_res = await db_query("SELECT SUM(balance) FROM users", fetchone=True)
    bal = bal_res[0] if bal_res and bal_res[0] else 0
    orders = (await db_query("SELECT COUNT(*) FROM orders WHERE status = 'completed'", fetchone=True))[0]
    spent_res = await db_query("SELECT SUM(price) FROM orders WHERE status = 'completed'", fetchone=True)
    spent = spent_res[0] if spent_res and spent_res[0] else 0
    await bot.send_message(c.message.chat.id, f"📊 <b>Bot Statistikasy:</b>\n\n👥 Ulanyjy: <b>{users}</b>\n💰 Jemi balans: <b>{bal:.2f} TMT</b>\n📦 Satylan haryt: <b>{orders}</b>\n💸 Dolanyşyk: <b>{spent:.2f} TMT</b>")
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_mail")
async def mail_start(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Rassylka üçin habaryňyzy ugradyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.mailing, c.message.chat.id)
    await bot.answer_callback_query(c.id)

# --- FINISH MAILING HANDLER (TÄZE/FIX) ---
@bot.message_handler(state=AdminStates.mailing, content_types=['text', 'photo', 'video', 'document', 'sticker'])
async def mail_fin(m):
    await bot.send_message(m.chat.id, "📢 Habar ugradylýar, garaşyň...")
    users = await db_query("SELECT user_id FROM users", fetchall=True)
    success = 0
    if users:
        for (u_id,) in users:
            try:
                if m.text:
                    await bot.send_message(u_id, m.text)
                else:
                    await bot.copy_message(u_id, m.chat.id, m.message_id)
                success += 1
            except:
                pass
    await bot.send_message(m.chat.id, f"✅ Habar {success} ulanyja üstünlikli ugradyldy!")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data in ["adm_add", "adm_sub"])
async def bal_start(c):
    async with bot.retrieve_data(c.from_user.id, c.message.chat.id) as data:
        data['bal_op'] = "+" if c.data == "adm_add" else "-"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Ulanyjy ID ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.wait_bal_id, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.wait_bal_id, content_types=['text'])
async def bal_id(m):
    if not m.text.isdigit(): return await bot.send_message(m.chat.id, "Nädogry! Diňe san ýazyň:")
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        data["tid"] = int(m.text)
    await bot.send_message(m.chat.id, "Mukdar (TMT)?")
    await bot.set_state(m.from_user.id, AdminStates.wait_bal_amt, m.chat.id)

@bot.message_handler(state=AdminStates.wait_bal_amt, content_types=['text'])
async def bal_fin(m):
    try: amt = float(m.text)
    except: return await bot.send_message(m.chat.id, "Nädogry baha. Diňe san ýazyň:")
    
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        uid, op = data.get("tid"), data.get("bal_op")
        
    if uid is None or op is None:
        return await bot.send_message(m.chat.id, "Ýalňyşlyk ýüze çykdy. Başdan synanyşyň.")
        
    amount_to_change = amt if op == "+" else -amt
    
    if await change_balance(uid, amount_to_change):
        if op == "+":
            try: await bot.send_message(uid, f"<blockquote>Hasabyňyz {amt:.2f} TMT köpeldi.</blockquote>")
            except: pass
        await bot.send_message(m.chat.id, f"Balans üýtgedildi: {op}{amt:.2f} TMT (ID: {uid})")
    else: await bot.send_message(m.chat.id, "Ýalňyşlyk: Ulanyjy tapylmady ýa-da balansy minusa gidýär.")
    await bot.delete_state(m.from_user.id, m.chat.id)

# --- Ban / Unban ---
@bot.callback_query_handler(func=lambda c: c.data in ["adm_ban", "adm_unban"])
async def ban_start(c):
    async with bot.retrieve_data(c.from_user.id, c.message.chat.id) as data:
        data["ban_op"] = "ban" if c.data == "adm_ban" else "unban"
    st = AdminStates.ban_id if c.data == "adm_ban" else AdminStates.unban_id
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Ulanyjy ID ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, st, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=[AdminStates.ban_id, AdminStates.unban_id], content_types=['text'])
async def ban_fin(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data:
        op = data.get("ban_op")
    target_id = m.text
    
    if op == "ban":
        await db_query("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_id,), commit=True)
        try: await bot.send_message(target_id, "<b>Siz bloklandyňyz! 🚫</b>\n\nSebäbi: Düzgünleri bozmak.")
        except: pass
    else:
        await db_query("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_id,), commit=True)
        try: await bot.send_message(target_id, "<b>Siziň bloguňyz açyldy! ✅</b>\n\nIndi dükany ulanyp bilersiňiz.")
        except: pass
    await bot.send_message(m.chat.id, "Amal ýerine ýetirildi.")
    await bot.delete_state(m.from_user.id, m.chat.id)

# --- Adminleri dolandyrmak ---
@bot.callback_query_handler(func=lambda c: c.data == "adm_add_admin")
async def add_adm_start(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Тäze admin ID ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.add_admin, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.add_admin, content_types=['text'])
async def add_adm_fin(m):
    await db_query("INSERT OR IGNORE INTO admins (admin_id) VALUES (?)", (m.text,), commit=True)
    await bot.send_message(m.chat.id, "Admin goşuldy! ✅")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_del_admin")
async def del_adm_start(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Aýyrjak admin ID ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.del_admin, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.del_admin, content_types=['text'])
async def del_adm_fin(m):
    if int(m.text) == SUPER_ADMIN: await bot.send_message(m.chat.id, "Esasy admini aýyryp bolmaýar! ❌")
    else:
        await db_query("DELETE FROM admins WHERE admin_id = ?", (m.text,), commit=True)
        await bot.send_message(m.chat.id, "Admin aýryldy! 🗑")
    await bot.delete_state(m.from_user.id, m.chat.id)

# --- Sanawlar ---
@bot.callback_query_handler(func=lambda c: c.data == "adm_list")
async def show_admins(c):
    adms = await db_query("SELECT a.admin_id, u.username FROM admins a LEFT JOIN users u ON a.admin_id = u.user_id", fetchall=True)
    text = "👤 <b>Adminler sanawy:</b>\n\n"
    if adms:
        for a in adms:
            uname = f"@{a[1]}" if a[1] else "Näbelli"
            text += f"🆔 <code>{a[0]}</code> — {uname}\n"
    await bot.send_message(c.message.chat.id, text)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_banlist")
async def show_banlist(c):
    banned = await db_query("SELECT user_id, username FROM users WHERE is_banned = 1", fetchall=True)
    if not banned:
        text = "Ban sanawy boş. ✅"
    else:
        text = "🚫 <b>Bloklananlar:</b>\n\n"
        for b in banned:
            uname = f"@{b[1]}" if b[1] else "Näbelli"
            text += f"🆔 <code>{b[0]}</code> — {uname}\n"
    await bot.send_message(c.message.chat.id, text)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_payment_set")
async def adm_pay_set(c):
    tmt_query = await db_query("SELECT value FROM settings WHERE key='tmt_number'", fetchone=True)
    tmt = tmt_query[0] if tmt_query else "+99360000000"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="TMT Belgi üýtget", callback_data="set_tmt_num"))
    await bot.edit_message_text(f"💳 <b>Töleg Rekwizitleri:</b>\n\n🇹🇲 TMT: <code>{tmt}</code>", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb)
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "set_tmt_num")
async def ask_tmt(c):
    await bot.send_message(c.message.chat.id, "Täze TMT belgisini ýazyň:")
    await bot.set_state(c.from_user.id, AdminStates.wait_tmt_number, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.wait_tmt_number, content_types=['text'])
async def save_tmt(m):
    await db_query("UPDATE settings SET value = ? WHERE key = 'tmt_number'", (m.text,), commit=True)
    await bot.send_message(m.chat.id, "✅ TMT belgi üýtgedildi!")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_add_cat")
async def add_cat_start(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Täze kategoriýanyň adyny ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.add_cat_name, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.add_cat_name, content_types=['text'])
async def add_cat_fin(m):
    await db_query("INSERT INTO categories (name) VALUES (?)", (m.text,), commit=True)
    await bot.send_message(m.chat.id, "Kategoriýa goşuldy! ✅")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_del_cat")
async def del_cat_start(c):
    cats = await db_query("SELECT id, name FROM categories", fetchall=True)
    if not cats: return await bot.send_message(c.message.chat.id, "Kategoriýa ýok.")
    t = "Aýyrmak isleýän kategoriýaňyzyň ID belgisini ýazyň:\n\n" + "\n".join([f"ID: {ct[0]} - {ct[1]}" for ct in cats])
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, t, reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.del_cat_id, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.del_cat_id, content_types=['text'])
async def del_cat_fin(m):
    await db_query("DELETE FROM categories WHERE id = ?", (m.text,), commit=True)
    await db_query("DELETE FROM items WHERE cat_id = ?", (m.text,), commit=True)
    await bot.send_message(m.chat.id, "Kategoriýa we içindäki harytlar aýyryldy! 🗑")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_add_item")
async def add_item_1(c):
    if not await db_query("SELECT id FROM categories", fetchone=True):
        return await bot.send_message(c.message.chat.id, "Ilki bilen kategoriýa goşuň!")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "Harydyň adyny ýazyň:", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.add_item_name, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.add_item_name, content_types=['text'])
async def add_item_2(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data: data['i_name'] = m.text
    await bot.send_message(m.chat.id, "Harydyň bahasyny ýazyň (TMT - diňe san):")
    await bot.set_state(m.from_user.id, AdminStates.add_item_price, m.chat.id)

@bot.message_handler(state=AdminStates.add_item_price, content_types=['text'])
async def add_item_3(m):
    try: price = float(m.text)
    except: return await bot.send_message(m.chat.id, "Nädogry baha. Täzeden san görnüşinde ýazyň:")
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data: data['i_price'] = price
    await bot.send_message(m.chat.id, "Mukdaryny ýazyň (ýok bolsa '-' goýuň):")
    await bot.set_state(m.from_user.id, AdminStates.add_item_amount, m.chat.id)

@bot.message_handler(state=AdminStates.add_item_amount, content_types=['text'])
async def add_item_4(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data: data['i_amount'] = m.text
    await bot.send_message(m.chat.id, "Haryt barada giňişleýin maglumat ýazyň (Barada):")
    await bot.set_state(m.from_user.id, AdminStates.add_item_desc, m.chat.id)

@bot.message_handler(state=AdminStates.add_item_desc, content_types=['text'])
async def add_item_5(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data: data['i_desc'] = m.text
    await bot.send_message(m.chat.id, "Nirä ugratmaly? Üçin mysal maglumat ýazyň\n(Meselem: 'Oýun ID-ňizi ýazyň' ýa-da 'Telefon belgiňizi ýazyň'):")
    await bot.set_state(m.from_user.id, AdminStates.add_item_example, m.chat.id)

@bot.message_handler(state=AdminStates.add_item_example, content_types=['text'])
async def add_item_6(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as data: data['i_example'] = m.text
    cats = await db_query("SELECT id, name FROM categories", fetchall=True)
    if cats:
        t = "Haýsy kategoriýa goşmaly? Kategoriýa ID-sini ýazyň:\n\n" + "\n".join([f"ID: {ct[0]} - {ct[1]}" for ct in cats])
        await bot.send_message(m.chat.id, t)
    await bot.set_state(m.from_user.id, AdminStates.add_item_cat, m.chat.id)

@bot.message_handler(state=AdminStates.add_item_cat, content_types=['text'])
async def add_item_fin(m):
    async with bot.retrieve_data(m.from_user.id, m.chat.id) as d:
        await db_query("INSERT INTO items (cat_id, name, price, amount, description, example_data) VALUES (?, ?, ?, ?, ?, ?)", 
                 (m.text, d['i_name'], d['i_price'], d['i_amount'], d['i_desc'], d['i_example']), commit=True)
    await bot.send_message(m.chat.id, "Haryt üstünlikli goşuldy! ✅")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_del_item")
async def del_item_start(c):
    items = await db_query("SELECT id, name, price FROM items", fetchall=True)
    if not items: return await bot.send_message(c.message.chat.id, "Dükanda haryt ýok.")
    t = "Aýyrmak isleýän harydyňyzyň ID belgisini ýazyň:\n\n" + "\n".join([f"ID: {i[0]} - {i[1]} ({i[2]} TMT)" for i in items])
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, t, reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.del_item_id, c.message.chat.id)
    await bot.answer_callback_query(c.id)

# --- FINISH DELETE ITEM HANDLER (TÄZE/FIX) ---
@bot.message_handler(state=AdminStates.del_item_id, content_types=['text'])
async def del_item_fin(m):
    item_id = m.text.strip()
    if not item_id.isdigit():
        return await bot.send_message(m.chat.id, "❌ Nädogry ID! Diňe san ýazyň:")
    
    exists = await db_query("SELECT id FROM items WHERE id = ?", (int(item_id),), fetchone=True)
    if not exists:
        return await bot.send_message(m.chat.id, "❌ Haryt tapylmady! Täzeden ID ýazyň:")
        
    await db_query("DELETE FROM items WHERE id = ?", (int(item_id),), commit=True)
    await bot.send_message(m.chat.id, "Haryt aýryldy! 🗑")
    await bot.delete_state(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_dl_db")
async def dl_db_cb(c):
    if os.path.exists(DB_PATH):
        with open(DB_PATH, 'rb') as f:
            await bot.send_document(c.from_user.id, f, caption="💾 Siziň Baza Maglumatlaryňyz (users.db)")
    await bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "adm_ul_db")
async def ul_db_start(c):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Ýatyr", callback_data="cancel_action"))
    await bot.send_message(c.message.chat.id, "📂 Täze <b>users.db</b> faýlyny maňa ugradyň (Diňe baza dikeltmek üçin):", reply_markup=kb)
    await bot.set_state(c.from_user.id, AdminStates.wait_db_file, c.message.chat.id)
    await bot.answer_callback_query(c.id)

@bot.message_handler(state=AdminStates.wait_db_file, content_types=['document'])
async def ul_db_fin(m):
    if m.document.file_name.endswith(".db"):
        file_info = await bot.get_file(m.document.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        with open(DB_PATH, 'wb') as new_file:
            new_file.write(downloaded_file)
        await bot.send_message(m.chat.id, "✅ Baza Maglumatlary üstünlikli çalşyryldy we dikeldildi!")
        await init_db()
    else:
        await bot.send_message(m.chat.id, "❌ Diňe '.db' faýlyny ugradyň.")
    await bot.delete_state(m.from_user.id, m.chat.id)

# --- FORWARDER (Maglumat ýalňyş girmeginiň öňüni almak we Forward etmek) ---
@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'video', 'document', 'sticker'])
async def forwarder(m):
    state = await bot.get_state(m.from_user.id, m.chat.id)
    if state is not None: 
        return await bot.send_message(m.chat.id, "Doly we dogry maglumat ugradyň (Diňe tekst ýa-da san ýazyň). ✍️")
        
    uid = m.from_user.id
    user_data = await db_query("SELECT is_banned FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if user_data and user_data[0] == 1: return
    
    if uid in active_chats:
        if m.text and m.text.startswith("/"): return
        recipient_id = active_chats[uid]
        
        if m.text:
            prefix = "👨‍💼 <b>Admin:</b>\n" if await is_admin(uid) else "🧑‍💻 <b>Müşderi:</b>\n"
            await bot.send_message(recipient_id, prefix + m.text)
        else:
            await bot.copy_message(recipient_id, m.chat.id, m.message_id)

async def main():
    await init_db()
    bot.add_custom_filter(StateFilter(bot))
    logger.info("Bot started!")
    keep_alive()  # Запуск Flask-сервера
    await bot.infinity_polling()

if __name__ == "__main__":
    asyncio.run(main())
