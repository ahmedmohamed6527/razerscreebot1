import os
import re
import sqlite3
import logging
import asyncio
import requests
from datetime import datetime
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# ==================== الإعدادات ====================
import os

TOKEN = os.getenv("8288326480:AAE_mbkmBNaTi8478adhn-e2Wu8a5CqQGWY")
OCR_SPACE_API_KEY = os.getenv("K85149875988957")
OCR_API_URL = 'https://api.ocr.space/parse/image'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== قاعدة البيانات (كما هي) ====================
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
    conn.commit()
    conn.close()

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

def get_total_for_user(user_id):
    codes = get_user_codes(user_id, only_accepted=True)
    total = sum(float(amt) for amt in codes.values())
    return total

init_db()

# ==================== استخراج الأكواد وقيم الشحن (كما هي) ====================
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

# ==================== ضغط سريع للصورة ====================
def compress_image_fast(image_path):
    """ضغط سريع (جودة 70%، حد أقصى 1024 بكسل)"""
    try:
        size = os.path.getsize(image_path)
        if size < 500 * 1024:  # أقل من 500KB لا نضغط
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

# ==================== OCR مع محاولة واحدة فقط (للتسريع) ====================
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
        # إذا لم نجد قيماً، نحاول مرة واحدة بمحرك 1 (أسرع)
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

# ==================== تحميل سريع ====================
async def download_fast(file, path):
    try:
        await file.download_to_drive(path)
        return True
    except Exception as e:
        logging.error(f"فشل التحميل: {e}")
        return False

# ==================== معالجة الصورة الواحدة ====================
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
    amount = amounts[0]
    
    global_dup = is_global_duplicate(code)
    
    if not global_dup:
        add_global_code(code, amount, user_id)
        add_user_code(user_id, code, amount, accepted=1)
        reply = (f"✅ كود جديد\n🔑 {code}\n💰 {amount}\nتمت الإضافة.")
    else:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT 1 FROM user_codes WHERE user_id=? AND code=?', (user_id, code))
        user_has = c.fetchone() is not None
        conn.close()
        if not user_has:
            add_user_code(user_id, code, amount, accepted=0)
            reply = f"⚠️ كود مكرر (أرسله آخر)\n🔑 {code}\n💰 {amount}\nلم يُحتسب."
        else:
            add_user_code(user_id, code, amount, accepted=0)
            reply = f"⚠️ كود مكرر (أرسلته سابقاً)\n🔑 {code}\n💰 {amount}\nلم يُحتسب."
    
    total = get_total_for_user(user_id)
    reply += f"\n💰 رصيدك: {total:.2f}"
    await msg.edit_text(reply)

# ==================== معالجة الألبوم بالتوازي ====================
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
            'update': update
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
    
    await context.bot.send_message(chat_id=chat_id, text=f"📸 {len(photos)} صورة، جاري المعالجة المتوازية...")
    
    # دالة لمعالجة صورة واحدة
    async def process_one(photo_file):
        photo_path = f"./downloads/{photo_file.file_id}.jpg"
        try:
            if await download_fast(photo_file, photo_path):
                codes, amounts, _ = await extract_info_from_image_fast(photo_path)
                if codes and amounts:
                    return codes[0], amounts[0]
        except Exception as e:
            logging.error(e)
        finally:
            try:
                os.remove(photo_path)
            except:
                pass
        return None, None
    
    # معالجة جميع الصور بالتوازي (بحد أقصى 3 في نفس الوقت)
    semaphore = asyncio.Semaphore(3)
    
    async def limited_process(pf):
        async with semaphore:
            return await process_one(pf)
    
    tasks = [limited_process(pf) for pf in photos]
    results = await asyncio.gather(*tasks)
    
    # تجميع الأكواد الفريدة في الألبوم
    unique_in_album = {}
    for code, amount in results:
        if code and amount and code not in unique_in_album:
            unique_in_album[code] = amount
    
    # تصنيف الأكواد
    new_codes = []
    duplicate_global = []
    for code, amount in unique_in_album.items():
        if not is_global_duplicate(code):
            new_codes.append((code, amount))
            add_global_code(code, amount, user_id)
            add_user_code(user_id, code, amount, accepted=1)
        else:
            duplicate_global.append((code, amount))
            add_user_code(user_id, code, amount, accepted=0)
    
    batch_total = sum(float(amt) for _, amt in new_codes)
    
    # بناء الرسالة
    lines = ["📊 نتيجة الألبوم:"]
    if new_codes:
        lines.append("✅ أكواد جديدة:")
        for c, a in new_codes:
            lines.append(f"🔑 {c} → 💰 {a}")
        lines.append(f"💰 إجمالي الدفعة: {batch_total:.2f}")
    else:
        lines.append("❌ لا توجد أكواد جديدة.")
    if duplicate_global:
        lines.append("⚠️ مكررة (أرسلها آخرون):")
        for c, a in duplicate_global:
            lines.append(f"🔑 {c} → 💰 {a}")
    total_user = get_total_for_user(user_id)
    lines.append(f"📈 رصيدك الكلي: {total_user:.2f}")
    
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))

# ==================== التوجيه ====================
async def handle_any_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        await handle_album(update, context)
    else:
        await handle_single_photo(update, context)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحبًا! أرسل صورة أو عدة صور دفعة واحدة.\n"
        "سأستخرج الأكواد والقيم بسرعة فائقة.\n"
        "الأوامر: /reset , /history"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_user_codes(user_id)
    await update.message.reply_text("🗑️ تم مسح سجلك.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_codes(user_id, only_accepted=False)
    if not rows:
        await update.message.reply_text("لا يوجد سجل.")
        return
    lines = ["📜 تاريخك:"]
    for code, amount, accepted in rows:
        status = "✅" if accepted else "❌"
        lines.append(f"{status} {code} → {amount}")
    await update.message.reply_text("\n".join(lines))

# ==================== التشغيل ====================
def main():
    request = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)
    app = Application.builder().token(TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(MessageHandler(filters.PHOTO, handle_any_photo))
    print("✅ البوت يعمل (سريع جداً مع معالجة متوازية)...")
    app.run_polling()

if __name__ == '__main__':
    main()