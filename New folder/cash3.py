import os
import re
import sqlite3
import logging
import asyncio
import requests
from datetime import datetime
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.request import HTTPXRequest

# ==================== الإعدادات من البيئة ====================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set in environment")

OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY")
if not OCR_SPACE_API_KEY:
    raise ValueError("OCR_SPACE_API_KEY not set in environment")

OCR_API_URL = 'https://api.ocr.space/parse/image'
ADMIN_ID = int(os.environ.get("ADMIN_ID", 1026212735))  # خلي رقمك أو حطه في البيئة

# حالات المحادثات (نفس السابق)
AWAITING_USER_ID, AWAITING_AMOUNT, AWAITING_RESET_USER_ID, REQUESTING_AMOUNT, AWAITING_REJECT_REASON, AWAITING_EDIT_AMOUNT, AWAITING_ADMIN_HISTORY_ID, AWAITING_ADMIN_CLEAR_ID = range(8)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== قاعدة البيانات ====================
DB_PATH = 'codes_global.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS global_codes (
            code TEXT PRIMARY KEY,
            amount TEXT,
            first_user_id INTEGER,
            first_time TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_codes (
            user_id INTEGER,
            code TEXT,
            amount TEXT,
            accepted INTEGER DEFAULT 1,
            sent_time TIMESTAMP,
            PRIMARY KEY (user_id, code)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    c.execute('INSERT OR IGNORE INTO users (user_id, balance, is_admin) VALUES (?, 0, 1)', (ADMIN_ID,))
    conn.commit()
    conn.close()

init_db()

# ==================== دوال الرصيد (نفس السابق) ====================
def get_user_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT balance FROM users WHERE user_id=?', (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute('INSERT INTO users (user_id, balance, is_admin) VALUES (?, 0, 0)', (user_id,))
        conn.commit()
        conn.close()
        return 0.0
    conn.close()
    return row[0]

def update_user_balance(user_id, new_balance):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO users (user_id, balance, is_admin)
        VALUES (?, ?, COALESCE((SELECT is_admin FROM users WHERE user_id=?), 0))
    ''', (user_id, new_balance, user_id))
    conn.commit()
    conn.close()

def deduct_balance(user_id, amount):
    current = get_user_balance(user_id)
    update_user_balance(user_id, current - amount)
    return True

def add_balance(user_id, amount):
    current = get_user_balance(user_id)
    if current < 0:
        debt = -current
        if amount >= debt:
            new_balance = amount - debt
        else:
            new_balance = current + amount
    else:
        new_balance = current + amount
    update_user_balance(user_id, new_balance)

def reset_balance(user_id):
    update_user_balance(user_id, 0)

def reset_all_balances():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE users SET balance = 0')
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id, balance FROM users')
    rows = c.fetchall()
    conn.close()
    return rows

def is_admin(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT is_admin FROM users WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] == 1 if row else False

# ==================== دوال الأكواد العالمية (نفس السابق) ====================
def is_global_duplicate(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM global_codes WHERE code=?', (code,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def add_global_code(code, amount, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT OR IGNORE INTO global_codes (code, amount, first_user_id, first_time)
        VALUES (?, ?, ?, ?)
    ''', (code, amount, user_id, now))
    conn.commit()
    conn.close()

def add_user_code(user_id, code, amount, accepted=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT OR REPLACE INTO user_codes (user_id, code, amount, accepted, sent_time)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, code, amount, accepted, now))
    conn.commit()
    conn.close()

def get_user_codes(user_id, only_accepted=True):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if only_accepted:
        c.execute('SELECT code, amount FROM user_codes WHERE user_id=? AND accepted=1', (user_id,))
        rows = c.fetchall()
        conn.close()
        return {code: amount for code, amount in rows}
    else:
        c.execute('SELECT code, amount, accepted FROM user_codes WHERE user_id=?', (user_id,))
        rows = c.fetchall()
        conn.close()
        return rows

def clear_user_codes(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_codes WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

def get_all_users_with_codes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT DISTINCT user_id FROM user_codes')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def delete_all_users_codes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_codes')
    conn.commit()
    conn.close()

# ==================== استخراج الأكواد وقيم الشحن (نفس السابق) ====================
def extract_codes(text):
    pattern = r'\b\d(?:[A-Z0-9]\s*){20}\b'
    matches = re.findall(pattern, text)
    codes = [re.sub(r'\s+', '', m) for m in matches if len(re.sub(r'\s+', '', m)) == 21]
    return codes

def extract_amounts(text):
    amounts = []
    patterns_with_symbol = [
        r'USD\s*(\d{1,3}(?:\.\d{2})?)',
        r'(\d{1,3}(?:\.\d{2})?)\s*USD',
        r'دولار\s*امريكي\s*(\d{1,3}(?:\.\d{2})?)',
        r'(\d{1,3}(?:\.\d{2})?)\s*دولار\s*امريكي',
        r'\$(\d{1,3}(?:\.\d{2})?)',
        r'(\d{1,3}(?:\.\d{2})?)\s*\$',
        r'(\d{1,3}(?:\.\d{2})?)\s*@',
    ]
    for pat in patterns_with_symbol:
        for match in re.finditer(pat, text):
            num = match.group(1)
            if len(num) <= 6:
                amounts.append(num)
    if not amounts:
        typical_values = ['5', '10', '15', '20', '25', '30', '50', '75', '100', '150', '200', '250', '300', '500', '600']
        for val in typical_values:
            if re.search(rf'\b{val}(?:\.00)?\b', text):
                amounts.append(val)
    if not amounts:
        for match in re.finditer(r'\b([5-9]|[1-5][0-9]|[1-6][0-9][0-9])(?:\.00)?\b', text):
            num = match.group(1)
            if 5 <= int(num) <= 600:
                amounts.append(num)
    unique = []
    for a in amounts:
        clean = a.replace('.00', '')
        if clean not in unique:
            unique.append(clean)
    return unique

def compress_image_fast(image_path):
    try:
        size = os.path.getsize(image_path)
        if size < 500 * 1024:
            return
        img = Image.open(image_path)
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        max_dim = 1024
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim))
        img.save(image_path, 'JPEG', quality=70, optimize=True)
    except Exception as e:
        logging.warning(f"فشل ضغط سريع: {e}")

async def extract_from_ocr_fast(image_path, engine=2):
    try:
        with open(image_path, 'rb') as f:
            response = requests.post(
                OCR_API_URL,
                files={'file': f},
                data={'apikey': OCR_SPACE_API_KEY, 'language': 'eng', 'OCREngine': engine},
                timeout=25
            )
        result = response.json()
        if result.get('ParsedResults'):
            return result['ParsedResults'][0]['ParsedText']
        return ""
    except Exception as e:
        logging.warning(f"OCR فشل: {e}")
        return ""

async def extract_info_from_image_fast(image_path: str):
    try:
        compress_image_fast(image_path)
        text = await extract_from_ocr_fast(image_path, engine=2)
        codes = extract_codes(text)
        amounts = extract_amounts(text)
        if not amounts and not codes:
            text2 = await extract_from_ocr_fast(image_path, engine=1)
            codes = extract_codes(text2)
            amounts = extract_amounts(text2)
        if codes or amounts:
            return codes, amounts, None
        else:
            return [], [], "لم يتم العثور على كود أو قيمة شحن."
    except Exception as e:
        logging.exception(e)
        return [], [], f"خطأ تقني: {str(e)}"

async def download_fast(file, path):
    try:
        await file.download_to_drive(path)
        return True
    except Exception as e:
        logging.error(f"فشل التحميل: {e}")
        return False

# ==================== القوائم والأزرار (تم إضافة الزر الجديد) ====================
async def main_menu(user_id):
    if is_admin(user_id):
        # قائمة الأدمن: تحتوي على كل الأزرار بما فيها "تصفير رصيد مستخدم"
        keyboard = [
            [InlineKeyboardButton("📸 فحص صورة جديدة", callback_data="scan_new")],
            [InlineKeyboardButton("💰 رصيدي", callback_data="my_balance"),
             InlineKeyboardButton("📜 تاريخي", callback_data="my_history")],
            [InlineKeyboardButton("📨 طلب شحن رصيد", callback_data="request_balance"),
             InlineKeyboardButton("🗑️ مسح سجلي", callback_data="reset_me")],
            [InlineKeyboardButton("📜 تاريخ أي مستخدم", callback_data="admin_history")],
            [InlineKeyboardButton("🗑️ مسح سجل مستخدم", callback_data="admin_clear_user"),
             InlineKeyboardButton("🗑️ مسح سجل الكل", callback_data="admin_clear_all")],
            [InlineKeyboardButton("🔄 تصفير رصيد مستخدم", callback_data="admin_reset_balance_user")]  # الزر الجديد
        ]
    else:
        # قائمة المستخدم العادي: بدون تغيير
        keyboard = [
            [InlineKeyboardButton("📸 فحص صورة جديدة", callback_data="scan_new")],
            [InlineKeyboardButton("💰 رصيدي", callback_data="my_balance")],
            [InlineKeyboardButton("📨 طلب شحن رصيد", callback_data="request_balance")]
        ]
    return InlineKeyboardMarkup(keyboard)

# ==================== دوال معالجة الصور (نفس السابق) ====================
async def handle_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo_file = await update.message.photo[-1].get_file()
    msg = await update.message.reply_text("📥 جاري التحليل...")
    os.makedirs("./downloads", exist_ok=True)
    photo_path = f"./downloads/{photo_file.file_id}.jpg"
    if not await download_fast(photo_file, photo_path):
        await msg.edit_text("❌ فشل تحميل الصورة")
        return
    codes, amounts, error = await extract_info_from_image_fast(photo_path)
    try:
        os.remove(photo_path)
    except:
        pass
    if error:
        await msg.edit_text(f"❌ {error}")
        return
    if not codes or not amounts:
        await msg.edit_text("❌ لم يتم العثور على كود أو قيمة شحن.")
        return
    code = codes[0]
    amount = float(amounts[0])
    global_dup = is_global_duplicate(code)
    if global_dup:
        add_user_code(user_id, code, amount, accepted=0)
        reply = f"⚠️ كود مكرر\n🔑 {code}\n💰 {amount}\nلم يتم خصم أي مبلغ."
    else:
        deduct_balance(user_id, amount)
        add_global_code(code, amount, user_id)
        add_user_code(user_id, code, amount, accepted=1)
        reply = f"✅ كود جديد\n🔑 {code}\n💰 {amount}\nتم خصم {amount} من رصيدك."
    new_balance = get_user_balance(user_id)
    reply += f"\n💰 رصيدك الحالي: {new_balance:.2f}"
    await msg.edit_text(reply)
    await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(user_id))

pending_albums = {}
async def handle_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    media_group_id = update.message.media_group_id
    if not media_group_id:
        return
    photo_file = await update.message.photo[-1].get_file()
    if media_group_id not in pending_albums:
        pending_albums[media_group_id] = {
            'photos': [],
            'timer': None,
            'chat_id': update.effective_chat.id,
            'user_id': update.effective_user.id,
        }
    pending_albums[media_group_id]['photos'].append(photo_file)
    if pending_albums[media_group_id]['timer']:
        pending_albums[media_group_id]['timer'].cancel()
    loop = asyncio.get_running_loop()
    timer = loop.call_later(0.8, lambda: asyncio.create_task(process_album_parallel(media_group_id, context)))
    pending_albums[media_group_id]['timer'] = timer

async def process_album_parallel(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    album_data = pending_albums.pop(media_group_id, None)
    if not album_data:
        return
    photos = album_data['photos']
    chat_id = album_data['chat_id']
    user_id = album_data['user_id']
    await context.bot.send_message(chat_id=chat_id, text=f"📸 {len(photos)} صورة، جاري المعالجة...")
    semaphore = asyncio.Semaphore(3)
    async def process_one(pf):
        async with semaphore:
            photo_path = f"./downloads/{pf.file_id}.jpg"
            try:
                if await download_fast(pf, photo_path):
                    codes, amounts, _ = await extract_info_from_image_fast(photo_path)
                    if codes and amounts:
                        return codes[0], float(amounts[0])
            except:
                pass
            finally:
                try:
                    os.remove(photo_path)
                except:
                    pass
            return None, None
    tasks = [process_one(pf) for pf in photos]
    results = await asyncio.gather(*tasks)
    unique_in_album = {}
    for code, amount in results:
        if code and amount and code not in unique_in_album:
            unique_in_album[code] = amount
    new_codes = []
    duplicate_global = []
    for code, amount in unique_in_album.items():
        if not is_global_duplicate(code):
            new_codes.append((code, amount))
        else:
            duplicate_global.append((code, amount))
    total_to_deduct = sum(amt for _, amt in new_codes)
    if total_to_deduct > 0:
        for code, amount in new_codes:
            deduct_balance(user_id, amount)
            add_global_code(code, amount, user_id)
            add_user_code(user_id, code, amount, accepted=1)
        success_msg = f"✅ تم خصم {total_to_deduct} من رصيدك."
    else:
        success_msg = "ℹ️ لا توجد أكواد جديدة."
    for code, amount in duplicate_global:
        add_user_code(user_id, code, amount, accepted=0)
    lines = ["📊 **نتيجة الألبوم:**"]
    if new_codes:
        lines.append("✅ الأكواد الجديدة (تم خصمها):")
        for c, a in new_codes:
            lines.append(f"🔑 {c} → {a}")
    if duplicate_global:
        lines.append("⚠️ الأكواد المكررة (لم تُخصم):")
        for c, a in duplicate_global:
            lines.append(f"🔑 {c} → {a}")
    lines.append(success_msg)
    new_balance = get_user_balance(user_id)
    lines.append(f"💰 رصيدك الحالي: {new_balance:.2f}")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    await context.bot.send_message(chat_id=chat_id, text="اختر من القائمة:", reply_markup=await main_menu(user_id))

async def handle_any_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        await handle_album(update, context)
    else:
        await handle_single_photo(update, context)

# ==================== الأزرار الأساسية والأوامر (تم إضافة معالج الزر الجديد) ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user_balance(user_id)
    await update.message.reply_text(
        "مرحبًا! 👋\nأنا بوت استخراج أكواد Razer Gold.\n\n"
        "✨ **الميزات:**\n"
        "- فحص الصور المفردة والمجموعات\n"
        "- خصم قيمة الشحن من رصيدك (يسمح بالسالب)\n"
        "- طلب شحن الرصيد (سيتم إرسال طلبك للأدمن)\n"
        "- إدارة رصيدك عبر الأزرار\n\n"
        "👇 اختر من القائمة:",
        reply_markup=await main_menu(user_id)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "scan_new":
        await query.edit_message_text("📸 أرسل لي صورة (أو عدة صور دفعة واحدة).")
        context.user_data['awaiting_photo'] = True
    elif data == "my_balance":
        balance = get_user_balance(user_id)
        await query.edit_message_text(f"💰 **رصيدك الحالي:** {balance:.2f} دولار", reply_markup=await main_menu(user_id))
    elif data == "my_history":
        rows = get_user_codes(user_id, only_accepted=False)
        if not rows:
            text = "📜 لم تقم باستخراج أي كود بعد."
        else:
            lines = ["📜 **الأكواد التي استخرجتها:**\n"]
            for code, amount, accepted in rows:
                status = "✅" if accepted else "❌ (مكرر)"
                lines.append(f"{status} {code} → {amount}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=await main_menu(user_id))
    elif data == "reset_me":
        if is_admin(user_id):
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ مسح سجلي أنا", callback_data="clear_my_own")],
                [InlineKeyboardButton("🗑️ مسح سجل مستخدم آخر", callback_data="clear_other_user")],
                [InlineKeyboardButton("🗑️ مسح سجل جميع المستخدمين", callback_data="clear_all_users")]
            ])
            await query.edit_message_text("اختر نوع المسح:", reply_markup=keyboard)
        else:
            clear_user_codes(user_id)
            await query.edit_message_text("🗑️ تم مسح سجل الأكواد الخاص بك.", reply_markup=await main_menu(user_id))
        return
    elif data == "admin_history":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        await query.edit_message_text("👤 أرسل معرف المستخدم (user_id) لعرض تاريخه:")
        return AWAITING_ADMIN_HISTORY_ID
    elif data == "admin_clear_user":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        users = get_all_users_with_codes()
        if not users:
            await query.edit_message_text("لا يوجد أي مستخدم لديه سجل أكواد.")
            return
        keyboard = []
        for uid in users:
            keyboard.append([InlineKeyboardButton(f"👤 {uid}", callback_data=f"clear_user_{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_menu")])
        await query.edit_message_text("اختر المستخدم الذي تريد مسح سجله:", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    elif data == "admin_clear_all":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، تأكيد", callback_data="confirm_clear_all"),
             InlineKeyboardButton("❌ إلغاء", callback_data="back_to_menu")]
        ])
        await query.edit_message_text("⚠️ **تحذير:** أنت على وشك مسح سجل جميع المستخدمين. لا يمكن التراجع.\nهل أنت متأكد؟", reply_markup=keyboard)
        return
    elif data == "clear_my_own":
        clear_user_codes(user_id)
        await query.edit_message_text("🗑️ تم مسح سجلك الشخصي.", reply_markup=await main_menu(user_id))
        return
    elif data == "clear_other_user":
        await query.edit_message_text("👤 أرسل معرف المستخدم (user_id) الذي تريد مسح سجله:")
        return AWAITING_ADMIN_CLEAR_ID
    elif data == "clear_all_users":
        delete_all_users_codes()
        await query.edit_message_text("🗑️ تم مسح سجل جميع المستخدمين.", reply_markup=await main_menu(user_id))
        return
    elif data == "confirm_clear_all":
        delete_all_users_codes()
        await query.edit_message_text("🗑️ تم مسح سجل جميع المستخدمين.", reply_markup=await main_menu(user_id))
        return
    elif data.startswith("clear_user_"):
        target_id = int(data.split('_')[2])
        clear_user_codes(target_id)
        await query.edit_message_text(f"✅ تم مسح سجل المستخدم `{target_id}`.", reply_markup=await main_menu(user_id))
        return
    elif data == "back_to_menu":
        await query.edit_message_text("اختر من القائمة:", reply_markup=await main_menu(user_id))
        return
    elif data.startswith("approve_req"):
        parts = data.split('_')
        target_id = int(parts[2])
        amount = float(parts[3])
        add_balance(target_id, amount)
        new_balance = get_user_balance(target_id)
        await query.edit_message_text(f"✅ تمت الموافقة وإضافة {amount} إلى رصيد المستخدم `{target_id}`.\n💰 الرصيد الجديد: {new_balance:.2f}")
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🎉 تم شحن رصيدك بمبلغ {amount}.\n💰 رصيدك الحالي: {new_balance:.2f}\nاختر من القائمة:",
            reply_markup=await main_menu(target_id)
        )
        return ConversationHandler.END
    elif data.startswith("reject_req"):
        parts = data.split('_')
        target_id = int(parts[2])
        context.user_data['reject_target_id'] = target_id
        await query.edit_message_text("✏️ أرسل سبب الرفض (نص قصير):")
        return AWAITING_REJECT_REASON
    elif data.startswith("edit_req"):
        parts = data.split('_')
        target_id = int(parts[2])
        context.user_data['editing_target_id'] = target_id
        await query.edit_message_text(f"✏️ أرسل المبلغ الجديد للمستخدم `{target_id}` (رقم فقط):")
        return AWAITING_EDIT_AMOUNT
    elif data == "request_balance":
        context.user_data['requesting_user_id'] = user_id
        await query.edit_message_text("✏️ أرسل المبلغ الذي تريد شحنه (رقم فقط):")
        return REQUESTING_AMOUNT
    elif data == "add_balance_admin":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        await query.edit_message_text("👤 أرسل معرف المستخدم الذي تريد إضافة رصيد له:")
        return AWAITING_USER_ID
    elif data == "reset_balance_admin":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        await query.edit_message_text("👤 أرسل معرف المستخدم الذي تريد تصفير رصيده:")
        return AWAITING_RESET_USER_ID
    # ==================== الزر الجديد: تصفير رصيد مستخدم ====================
    elif data == "admin_reset_balance_user":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        # عرض قائمة بجميع المستخدمين المسجلين في قاعدة البيانات (users)
        users = get_all_users()
        if not users:
            await query.edit_message_text("لا يوجد أي مستخدم مسجل في النظام.")
            return
        keyboard = []
        for uid, bal in users:
            keyboard.append([InlineKeyboardButton(f"👤 {uid} (رصيد: {bal:.2f})", callback_data=f"reset_balance_user_{uid}")])
        keyboard.append([InlineKeyboardButton("🔄 تصفير رصيد جميع المستخدمين", callback_data="reset_all_balances")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_menu")])
        await query.edit_message_text("اختر المستخدم الذي تريد تصفير رصيده (جعله صفر):", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    elif data.startswith("reset_balance_user_"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        target_id = int(data.split('_')[3])
        reset_balance(target_id)
        new_balance = get_user_balance(target_id)
        await query.edit_message_text(f"🔄 تم تصفير رصيد المستخدم `{target_id}`.\n💰 الرصيد الآن: {new_balance:.2f}", reply_markup=await main_menu(user_id))
        return
    elif data == "reset_all_balances":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        reset_all_balances()
        await query.edit_message_text("🔄 تم تصفير رصيد **جميع** المستخدمين إلى صفر.", reply_markup=await main_menu(user_id))
        return

# ==================== بقية المحادثات (نفس السابق تماماً) ====================
async def receive_admin_history_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        target_id = int(text)
        rows = get_user_codes(target_id, only_accepted=False)
        if not rows:
            await update.message.reply_text(f"📜 المستخدم `{target_id}` ليس لديه أي سجل أكواد.")
        else:
            lines = [f"📜 **تاريخ المستخدم {target_id}:**\n"]
            for code, amount, accepted in rows:
                status = "✅" if accepted else "❌ (مكرر)"
                lines.append(f"{status} {code} → {amount}")
            await update.message.reply_text("\n".join(lines))
    except ValueError:
        await update.message.reply_text("❌ معرف المستخدم يجب أن يكون رقماً.")
    await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(update.effective_user.id))
    return ConversationHandler.END

async def receive_admin_clear_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        target_id = int(text)
        clear_user_codes(target_id)
        await update.message.reply_text(f"✅ تم مسح سجل المستخدم `{target_id}`.")
    except ValueError:
        await update.message.reply_text("❌ معرف المستخدم غير صحيح.")
    await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(update.effective_user.id))
    return ConversationHandler.END

async def receive_request_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount = float(text)
        if amount <= 0:
            await update.message.reply_text("⚠️ المبلغ يجب أن يكون أكبر من صفر. حاول مرة أخرى.")
            return REQUESTING_AMOUNT
        user_id = context.user_data.get('requesting_user_id')
        if not user_id:
            await update.message.reply_text("حدث خطأ، حاول مرة أخرى.")
            return ConversationHandler.END
        admin_id = ADMIN_ID
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ موافقة", callback_data=f"approve_req_{user_id}_{amount}"),
             InlineKeyboardButton("❌ رفض", callback_data=f"reject_req_{user_id}"),
             InlineKeyboardButton("✏️ تعديل المبلغ", callback_data=f"edit_req_{user_id}")]
        ])
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"📨 **طلب شحن رصيد جديد**\n\n👤 المستخدم: `{user_id}`\n💰 المبلغ المطلوب: {amount}\n\nاختر الإجراء:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ تم إرسال طلبك إلى الأدمن. سيتم إشعارك عند الموافقة أو الرفض.")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ المبلغ غير صحيح. أرسل رقماً فقط (مثال: 50).")
        return REQUESTING_AMOUNT

async def receive_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text("⚠️ الرجاء كتابة سبب الرفض (لا يمكن تركه فارغاً).")
        return AWAITING_REJECT_REASON
    target_id = context.user_data.get('reject_target_id')
    if not target_id:
        await update.message.reply_text("حدث خطأ، حاول مرة أخرى.")
        return ConversationHandler.END
    await context.bot.send_message(
        chat_id=target_id,
        text=f"❌ **تم رفض طلب شحن الرصيد**\n\nالسبب: {reason}\n\nإذا كان لديك استفسار، تواصل مع الدعم.",
        parse_mode="Markdown",
        reply_markup=await main_menu(target_id)
    )
    await update.message.reply_text(f"✅ تم إرسال رسالة الرفض إلى المستخدم `{target_id}` مع السبب: {reason}")
    await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(update.effective_user.id))
    context.user_data.pop('reject_target_id', None)
    return ConversationHandler.END

async def receive_edit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount = float(text)
        if amount <= 0:
            await update.message.reply_text("⚠️ المبلغ يجب أن يكون أكبر من صفر.")
            return AWAITING_EDIT_AMOUNT
        target_id = context.user_data.get('editing_target_id')
        if not target_id:
            await update.message.reply_text("حدث خطأ، حاول مرة أخرى.")
            return ConversationHandler.END
        add_balance(target_id, amount)
        new_balance = get_user_balance(target_id)
        await update.message.reply_text(
            f"✅ تم تعديل المبلغ وإضافة {amount} إلى رصيد المستخدم `{target_id}`.\n💰 الرصيد الجديد: {new_balance:.2f}"
        )
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🎉 تم شحن رصيدك بمبلغ {amount} (بعد تعديل المبلغ).\n💰 رصيدك الحالي: {new_balance:.2f}\nاختر من القائمة:",
                reply_markup=await main_menu(target_id)
            )
        except Exception as e:
            logging.warning(f"فشل إرسال إشعار للمستخدم {target_id}: {e}")
        await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(update.effective_user.id))
        context.user_data.pop('editing_target_id', None)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ المبلغ غير صحيح. أرسل رقماً فقط (مثال: 50).")
        return AWAITING_EDIT_AMOUNT

async def receive_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        target_id = int(text)
        get_user_balance(target_id)
        context.user_data['target_id'] = target_id
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("10", callback_data="amt_10"),
             InlineKeyboardButton("20", callback_data="amt_20"),
             InlineKeyboardButton("50", callback_data="amt_50")],
            [InlineKeyboardButton("100", callback_data="amt_100"),
             InlineKeyboardButton("200", callback_data="amt_200"),
             InlineKeyboardButton("500", callback_data="amt_500")],
            [InlineKeyboardButton("📝 إدخال مبلغ مخصص", callback_data="amt_custom")]
        ])
        await update.message.reply_text("💰 اختر المبلغ:", reply_markup=keyboard)
        return AWAITING_AMOUNT
    except ValueError:
        await update.message.reply_text("❌ معرف المستخدم يجب أن يكون رقماً. حاول مرة أخرى.")
        return AWAITING_USER_ID

async def amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    target_id = context.user_data.get('target_id')
    if not target_id:
        await query.edit_message_text("حدث خطأ، حاول مرة أخرى.")
        return ConversationHandler.END
    if data == "amt_custom":
        await query.edit_message_text("✏️ أرسل المبلغ (رقم فقط):")
        return AWAITING_AMOUNT
    amount = float(data.split('_')[1])
    add_balance(target_id, amount)
    new_balance = get_user_balance(target_id)
    await query.edit_message_text(
        f"✅ تم إضافة {amount} إلى رصيد المستخدم `{target_id}`.\n💰 الرصيد الجديد: {new_balance:.2f}"
    )
    await query.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(query.from_user.id))
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🎉 تم شحن رصيدك بمبلغ {amount}.\n💰 رصيدك الحالي: {new_balance:.2f}\nاختر من القائمة:",
            reply_markup=await main_menu(target_id)
        )
    except Exception as e:
        logging.warning(f"فشل إرسال إشعار للمستخدم {target_id}: {e}")
    context.user_data.pop('target_id', None)
    return ConversationHandler.END

async def receive_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount = float(text)
        if amount <= 0:
            await update.message.reply_text("⚠️ المبلغ يجب أن يكون أكبر من صفر.")
            return AWAITING_AMOUNT
        target_id = context.user_data.get('target_id')
        if target_id:
            add_balance(target_id, amount)
            new_balance = get_user_balance(target_id)
            await update.message.reply_text(
                f"✅ تم إضافة {amount} إلى رصيد المستخدم `{target_id}`.\n💰 الرصيد الجديد: {new_balance:.2f}"
            )
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=f"🎉 تم شحن رصيدك بمبلغ {amount}.\n💰 رصيدك الحالي: {new_balance:.2f}\nاختر من القائمة:",
                    reply_markup=await main_menu(target_id)
                )
            except Exception as e:
                logging.warning(f"فشل إرسال إشعار للمستخدم {target_id}: {e}")
            await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(update.effective_user.id))
            context.user_data.pop('target_id', None)
            return ConversationHandler.END
        else:
            await update.message.reply_text("حدث خطأ: لم يتم العثور على المستخدم المستهدف.")
            return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ المبلغ غير صحيح. أرسل رقماً فقط (مثال: 50).")
        return AWAITING_AMOUNT

async def reset_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return ConversationHandler.END
    await query.edit_message_text("👤 أرسل معرف المستخدم الذي تريد تصفير رصيده:")
    return AWAITING_RESET_USER_ID

async def receive_reset_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        target_id = int(text)
        reset_balance(target_id)
        new_balance = get_user_balance(target_id)
        await update.message.reply_text(
            f"🔄 تم تصفير رصيد المستخدم `{target_id}`.\n💰 الرصيد الآن: {new_balance:.2f}"
        )
        await update.message.reply_text("اختر من القائمة:", reply_markup=await main_menu(update.effective_user.id))
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ معرف المستخدم غير صحيح. أرسل رقماً.")
        return AWAITING_RESET_USER_ID

# ==================== التشغيل الرئيسي ====================
def main():
    request = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)
    app = Application.builder().token(TOKEN).request(request).build()

    # محادثة إضافة الرصيد (يدوي)
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^add_balance_admin$")],
        states={
            AWAITING_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_user_id)],
            AWAITING_AMOUNT: [CallbackQueryHandler(amount_callback, pattern="^amt_"),
                              MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_amount)]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    # محادثة تصفير الرصيد (الطريقة القديمة بإدخال ID)
    reset_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^reset_balance_admin$")],
        states={AWAITING_RESET_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reset_user_id)]},
        fallbacks=[CommandHandler("start", start)]
    )
    # محادثة طلب الشحن
    request_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^request_balance$")],
        states={REQUESTING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_request_amount)]},
        fallbacks=[CommandHandler("start", start)]
    )
    # محادثة رفض الطلب
    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^reject_req_")],
        states={AWAITING_REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reject_reason)]},
        fallbacks=[CommandHandler("start", start)]
    )
    # محادثة تعديل المبلغ
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^edit_req_")],
        states={AWAITING_EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_amount)]},
        fallbacks=[CommandHandler("start", start)]
    )
    # محادثة عرض تاريخ أي مستخدم (للأدمن)
    admin_history_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^admin_history$")],
        states={AWAITING_ADMIN_HISTORY_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_history_id)]},
        fallbacks=[CommandHandler("start", start)]
    )
    # محادثة مسح سجل مستخدم آخر (للأدمن)
    admin_clear_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^clear_other_user$")],
        states={AWAITING_ADMIN_CLEAR_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_clear_id)]},
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(add_conv)
    app.add_handler(reset_conv)
    app.add_handler(request_conv)
    app.add_handler(reject_conv)
    app.add_handler(edit_conv)
    app.add_handler(admin_history_conv)
    app.add_handler(admin_clear_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_any_photo))

    print("✅ البوت يعمل مع جميع الميزات (تم إضافة زر تصفير رصيد المستخدم)...")
    app.run_polling()

if __name__ == '__main__':
    main()