import os
import logging
import mimetypes
import requests
import time
import yt_dlp
from urllib.parse import urlparse
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest
from dotenv import load_dotenv
from web_to_pdf import pdf_command
try:
    from pyrogram.client import Client
except ImportError:
    from pyrogram import Client
import asyncio
import glob
import concurrent.futures
from datetime import datetime, timedelta
from telegram.constants import ParseMode

# بارگذاری متغیرهای محیطی از فایل .env
load_dotenv()

# تنظیمات پراکسی برای PythonAnywhere (اختیاری)
PROXY_URL = os.getenv('PROXY_URL', None)  # مثال: http://proxy.server:3128
# اگر می‌خواهید دانلود فایل هم از طریق پراکسی انجام شود (در صورتی که هاست مقصد در whitelist باشد) این را true کنید
ALLOW_DOWNLOAD_VIA_PROXY = os.getenv('ALLOW_DOWNLOAD_VIA_PROXY', 'false').strip().lower() in ('1','true','yes','on')
# اگر روی هاستی هستید که outbound محدود است (مثل PythonAnywhere Free)، دانلود محلی را غیرفعال کنید
DIRECT_SEND_ONLY = os.getenv('DIRECT_SEND_ONLY', 'false').strip().lower() in ('1','true','yes','on')

# پشتیبانی از کوکی‌ها برای yt-dlp (برای عبور از age-gate یا نیاز به لاگین)
YTDLP_COOKIES = os.getenv('YTDLP_COOKIES', '').strip()  # مسیر فایل کوکی به فرمت Netscape
YTDLP_COOKIE_HEADER = os.getenv('YTDLP_COOKIE_HEADER', '').strip()  # رشته Cookie آماده (اختیاری)

# محدودیت حجم فایل (MB) - برای جلوگیری از OOM در render.com
MAX_FILE_SIZE_MB = int(os.getenv('MAX_FILE_SIZE_MB', '2000'))  # پیش‌فرض 2000MB (2GB)

# آیدی ادمین
ADMIN_ID = 818185073

# ذخیره موقت کاربران فعال (در حافظه - بدون دیتابیس)
active_users = {}  # {user_id: {'username': str, 'first_name': str, 'last_request': datetime}}

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# غیرفعال کردن لاگ‌های حساس که ممکن است توکن را لو دهند
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# توکن ربات تلگرام از فایل .env (بدون مقدار پیش‌فرض برای امنیت)
BOT_TOKEN = os.getenv('BOT_TOKEN')
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')

# پوشه موقت برای ذخیره فایل‌ها
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ایجاد Pyrogram client برای فایل‌های بزرگ (بیشتر از 50MB)
pyrogram_client = None
pyrogram_client_lock = None

# Executor برای اجرای کارهای blocking
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

async def init_pyrogram_lock():
    """Initialize the asyncio lock for Pyrogram client"""
    global pyrogram_client_lock
    if pyrogram_client_lock is None:
        pyrogram_client_lock = asyncio.Lock()

async def get_pyrogram_client():
    """ایجاد یا برگرداندن Pyrogram client (thread-safe)"""
    global pyrogram_client, pyrogram_client_lock
    if not API_ID or not API_HASH or not BOT_TOKEN:
        return None
    
    try:
        if pyrogram_client_lock is None:
            await init_pyrogram_lock()
        
        async with pyrogram_client_lock:
            if pyrogram_client is None:
                pyrogram_client = Client(
                    "file_downloader_bot",
                    api_id=int(API_ID),
                    api_hash=API_HASH,
                    bot_token=BOT_TOKEN,
                    workdir=DOWNLOAD_FOLDER
                )
            return pyrogram_client
    except Exception as e:
        logger.error(f"خطا در ایجاد Pyrogram client: {e}")
        return None

def cleanup_old_files():
    """پاکسازی فایل‌های قدیمی از پوشه downloads"""
    try:
        now = datetime.now()
        for filepath in glob.glob(os.path.join(DOWNLOAD_FOLDER, '*')):
            if os.path.isfile(filepath):
                file_age = now - datetime.fromtimestamp(os.path.getmtime(filepath))
                if file_age > timedelta(hours=1):
                    try:
                        os.remove(filepath)
                        logger.info(f"فایل قدیمی حذف شد: {filepath}")
                    except Exception as e:
                        logger.error(f"خطا در حذف {filepath}: {e}")
    except Exception as e:
        logger.error(f"خطا در cleanup: {e}")

def cleanup_partial_files():
    """حذف فایل‌های ناتمام (.part, .ytdl, .temp)"""
    try:
        patterns = ['*.part', '*.ytdl', '*.temp', '*.tmp']
        for pattern in patterns:
            for filepath in glob.glob(os.path.join(DOWNLOAD_FOLDER, pattern)):
                try:
                    os.remove(filepath)
                    logger.info(f"فایل ناتمام حذف شد: {filepath}")
                except Exception as e:
                    logger.error(f"خطا در حذف {filepath}: {e}")
    except Exception as e:
        logger.error(f"خطا در cleanup partial files: {e}")

# نکته: پراکسی فقط برای Telegram Bot API استفاده می‌شود
# برای دانلود فایل‌ها از پراکسی استفاده نمی‌کنیم تا محدودیت whitelist نداشته باشیم


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پنل ادمین - فقط برای ادمین"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی به این بخش ندارید.")
        return
    
    # آمار کاربران
    total_users = len(active_users)
    
    # ساخت دکمه‌های پنل ادمین
    keyboard = [
        [InlineKeyboardButton("👥 مشاهده کاربران", callback_data="admin_users")],
        [InlineKeyboardButton("📊 آمار ربات", callback_data="admin_stats")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    admin_text = (
        "🔐 پنل مدیریت\n\n"
        f"👥 تعداد کاربران فعال: {total_users}\n\n"
        "از دکمه‌های زیر استفاده کنید:"
    )
    
    await update.message.reply_text(admin_text, reply_markup=reply_markup)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت callback های پنل ادمین"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        await query.edit_message_text("⛔ شما دسترسی به این بخش ندارید.")
        return
    
    if query.data == "admin_users":
        # نمایش لیست کاربران
        if not active_users:
            await query.edit_message_text("📭 هیچ کاربری هنوز از ربات استفاده نکرده است.")
            return
        
        users_text = "👥 لیست کاربران فعال:\n\n"
        for uid, info in list(active_users.items())[:20]:  # فقط 20 کاربر اول
            username = info.get('username', 'بدون یوزرنیم')
            first_name = info.get('first_name', 'نامشخص')
            last_req = info.get('last_request', 'نامشخص')
            users_text += f"👤 {first_name} (@{username})\n"
            users_text += f"🆔 {uid}\n"
            users_text += f"🕐 آخرین درخواست: {last_req}\n\n"
        
        if len(active_users) > 20:
            users_text += f"\n... و {len(active_users) - 20} کاربر دیگر"
        
        # دکمه بازگشت
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(users_text, reply_markup=reply_markup)
    
    elif query.data == "admin_stats":
        # نمایش آمار
        total_users = len(active_users)
        
        # محاسبه کاربران فعال در 24 ساعت گذشته
        now = datetime.now()
        active_24h = sum(1 for info in active_users.values() 
                        if isinstance(info.get('last_request'), datetime) 
                        and (now - info['last_request']).total_seconds() < 86400)
        
        stats_text = (
            "📊 آمار ربات\n\n"
            f"👥 کل کاربران: {total_users}\n"
            f"🟢 فعال در 24 ساعت: {active_24h}\n"
            f"📊 محدودیت حجم: {MAX_FILE_SIZE_MB} MB\n"
        )
        
        # دکمه بازگشت
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(stats_text, reply_markup=reply_markup)
    
    elif query.data == "admin_back":
        # بازگشت به منوی اصلی
        total_users = len(active_users)
        
        keyboard = [
            [InlineKeyboardButton("👥 مشاهده کاربران", callback_data="admin_users")],
            [InlineKeyboardButton("📊 آمار ربات", callback_data="admin_stats")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        admin_text = (
            "🔐 پنل مدیریت\n\n"
            f"👥 تعداد کاربران فعال: {total_users}\n\n"
            "از دکمه‌های زیر استفاده کنید:"
        )
        
        await query.edit_message_text(admin_text, reply_markup=reply_markup)


def save_user_link(user_id: int, url: str, timestamp: str):
    """ذخیره لینک کاربر در فایل متنی"""
    try:
        log_file = os.path.join(DOWNLOAD_FOLDER, 'user_links.txt')
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{user_id}|{url}|{timestamp}\n")
    except Exception as e:
        logger.error(f"خطا در ذخیره لینک: {e}")


def get_user_links(user_id: int):
    """خواندن لینک‌های یک کاربر از فایل"""
    try:
        log_file = os.path.join(DOWNLOAD_FOLDER, 'user_links.txt')
        if not os.path.exists(log_file):
            return []
        
        user_links = []
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 3:
                    uid, url, timestamp = parts
                    if int(uid) == user_id:
                        user_links.append({
                            'url': url,
                            'date': timestamp
                        })
        return user_links
    except Exception as e:
        logger.error(f"خطا در خواندن لینک‌ها: {e}")
        return []


async def check_and_notify_expiring_links(bot):
    """بررسی و ارسال هشدار برای لینک‌های نزدیک به حذف"""
    try:
        log_file = os.path.join(DOWNLOAD_FOLDER, 'user_links.txt')
        if not os.path.exists(log_file):
            return
        
        now = datetime.now()
        warning_date = now - timedelta(days=29)  # 1 روز قبل از حذف (29 روز)
        one_day_window = now - timedelta(days=28, hours=23)  # پنجره 1 ساعته
        
        # گروه‌بندی لینک‌ها بر اساس کاربر
        user_expiring_links = {}
        
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 3:
                    uid, url, timestamp = parts
                    try:
                        link_date = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                        
                        # بررسی اینکه لینک در بازه 29 روز تا 28 روز و 23 ساعت است
                        if warning_date <= link_date <= one_day_window:
                            user_id = int(uid)
                            if user_id not in user_expiring_links:
                                user_expiring_links[user_id] = []
                            user_expiring_links[user_id].append({
                                'url': url,
                                'date': timestamp
                            })
                    except (ValueError, TypeError):
                        continue
        
        # ارسال هشدار به ادمین برای هر کاربر
        for user_id, links in user_expiring_links.items():
            try:
                # ساخت پیام هشدار
                warning_text = (
                    f"⚠️ هشدار حذف لینک‌ها\n\n"
                    f"👤 کاربر: <a href='tg://user?id={user_id}'>{user_id}</a>\n"
                    f"📊 تعداد لینک‌های در حال انقضا: {len(links)}\n"
                    f"🗑️ زمان حذف: 24 ساعت دیگر\n\n"
                    f"📜 لیست لینک‌ها:\n\n"
                )
                
                for i, link_data in enumerate(links[:30], 1):  # حداکثر 30 لینک
                    url = link_data['url']
                    date = link_data['date']
                    display_url = url if len(url) <= 50 else url[:47] + "..."
                    warning_text += f"{i}. {display_url}\n   🕐 {date}\n\n"
                
                if len(links) > 30:
                    warning_text += f"\n... و {len(links) - 30} لینک دیگر"
                
                # ارسال به ادمین
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=warning_text,
                    parse_mode=ParseMode.HTML
                )
                logger.info(f"هشدار حذف برای کاربر {user_id} ارسال شد")
                
            except Exception as e:
                logger.error(f"خطا در ارسال هشدار برای کاربر {user_id}: {e}")
        
    except Exception as e:
        logger.error(f"خطا در بررسی لینک‌های در حال انقضا: {e}")


def cleanup_old_links():
    """حذف لینک‌های قدیمی‌تر از 1 ماه"""
    try:
        log_file = os.path.join(DOWNLOAD_FOLDER, 'user_links.txt')
        if not os.path.exists(log_file):
            return
        
        # خواندن همه لینک‌ها
        valid_links = []
        now = datetime.now()
        one_month_ago = now - timedelta(days=30)
        
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 3:
                    uid, url, timestamp = parts
                    try:
                        # تبدیل timestamp به datetime
                        link_date = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                        
                        # فقط لینک‌های کمتر از 1 ماه را نگه دار
                        if link_date > one_month_ago:
                            valid_links.append(line.strip())
                    except ValueError:
                        # اگر فرمت تاریخ اشتباه بود، نگه دار
                        valid_links.append(line.strip())
        
        # نوشتن لینک‌های معتبر به فایل
        with open(log_file, 'w', encoding='utf-8') as f:
            for link in valid_links:
                f.write(link + '\n')
        
        logger.info(f"پاکسازی لینک‌های قدیمی: {len(valid_links)} لینک باقی ماند")
    except Exception as e:
        logger.error(f"خطا در پاکسازی لینک‌های قدیمی: {e}")


async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بررسی تاریخچه لینک‌های یک کاربر - فقط برای ادمین"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ شما دسترسی به این دستور ندارید.")
        return
    
    # بررسی آرگومان
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "❌ لطفاً آیدی کاربر را وارد کنید.\n"
            "مثال: /check 123456789"
        )
        return
    
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ آیدی کاربر باید عدد باشد.")
        return
    
    # خواندن لینک‌های کاربر از فایل
    user_links = get_user_links(target_user_id)
    
    if not user_links:
        await update.message.reply_text(
            f"📭 هیچ لینکی از کاربر با آیدی {target_user_id} یافت نشد."
        )
        return
    
    # ساخت پیام تاریخچه
    history_text = (
        f"🆔 آیدی کاربر: {target_user_id}\n"
        f"📊 تعداد لینک‌ها: {len(user_links)}\n\n"
        "📜 تاریخچه لینک‌ها:\n\n"
    )
    
    # نمایش آخرین 20 لینک
    for i, msg_data in enumerate(user_links[-20:], 1):
        url = msg_data['url']
        date = msg_data['date']
        
        # کوتاه کردن URL برای نمایش بهتر
        display_url = url if len(url) <= 50 else url[:47] + "..."
        
        history_text += f"{i}. {display_url}\n"
        history_text += f"   🕐 {date}\n\n"
    
    if len(user_links) > 20:
        history_text += f"\n... و {len(user_links) - 20} لینک دیگر"
    
    await update.message.reply_text(history_text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام خوش‌آمدگویی"""
    # ثبت کاربر در لیست فعال
    user = update.effective_user
    active_users[user.id] = {
        'username': user.username or 'بدون یوزرنیم',
        'first_name': user.first_name or 'نامشخص',
        'last_request': datetime.now()
    }
    welcome_message = (
        "سلام! 👋\n\n"
        "من یک ربات دانلود و ارسال فایل هستم.\n\n"
        "🎬 دانلود از سایت‌های ویدیویی:\n"
        "• YouTube, Vimeo, Dailymotion\n"
        "• Reddit (شامل NSFW), Twitter, Instagram, TikTok\n"
        "• Pornhub, Xvideos, Xnxx, SpankBang\n"
        "• Eporner, HQporner, Beeg, YourPorn\n"
        "• PornTrex, YouJizz, Motherless, YouPorn\n"
        "• و بیش از 1000 سایت دیگر!\n\n"
        "🎞️ دانلود GIF:\n"
        "• Gfycat, Redgifs, xgroovy.com\n"
        "• xgifer.com, hentaigifz.com, hardcoregify.com\n\n"
        "📥 دانلود فایل مستقیم:\n"
        "• هر لینک دانلود مستقیم\n\n"
        "📹 ویدیوها به صورت ویدیو\n"
        "🎞️ GIF به صورت Animation\n"
        "📄 سایر فایل‌ها به صورت سند\n\n"
        "برای شروع، یک لینک ارسال کنید!"
    )
    await update.message.reply_text(welcome_message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """راهنمای استفاده"""
    help_text = (
        "📖 راهنمای استفاده:\n\n"
        "🎬 دانلود از سایت‌های ویدیویی:\n"
        "فقط لینک صفحه ویدیو را ارسال کنید\n"
        "مثال: https://www.youtube.com/watch?v=...\n\n"
        "🎞️ دانلود GIF:\n"
        "لینک صفحه GIF را ارسال کنید\n"
        "مثال: https://xgifer.com/gif/...\n"
        "مثال: https://hentaigifz.com/...\n\n"
        "📥 دانلود فایل مستقیم:\n"
        "لینک دانلود مستقیم فایل را ارسال کنید\n"
        "مثال: https://example.com/file.zip\n\n"
        "✅ بدون محدودیت حجم فایل\n"
        "✅ پشتیبانی از 1000+ سایت\n\n"
        "دستورات:\n"
        "/start - شروع\n"
        "/help - راهنما"
    )
    await update.message.reply_text(help_text)


def is_valid_url(url: str) -> bool:
    """بررسی معتبر بودن URL"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False


def get_file_extension_from_url(url: str, content_type: str = None) -> str:
    """استخراج پسوند فایل از URL یا Content-Type"""
    # ابتدا از URL استخراج کنیم
    parsed_url = urlparse(url)
    path = parsed_url.path
    if path:
        ext = os.path.splitext(path)[1]
        if ext:
            return ext
    
    # اگر از URL نشد، از Content-Type استفاده کنیم
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(';')[0].strip())
        if ext:
            return ext
    
    return ""


def is_video_file(filename: str, content_type: str = None) -> bool:
    """تشخیص اینکه فایل ویدیو است یا خیر"""
    video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg']
    
    # بررسی پسوند فایل
    ext = os.path.splitext(filename)[1].lower()
    if ext in video_extensions:
        return True
    
    # بررسی Content-Type
    if content_type and content_type.startswith('video/'):
        return True
    
    return False


def create_progress_bar(percentage: float, length: int = 10) -> str:
    """ایجاد نوار پیشرفت"""
    filled = int(length * percentage / 100)
    bar = '█' * filled + '░' * (length - filled)
    return bar


def is_video_site(url: str) -> bool:
    """بررسی اینکه URL از سایت‌های ویدیویی است"""
    video_sites = [
        'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
        'xvideos.com', 'pornhub.com', 'xnxx.com', 'redtube.com',
        'xhamster.com', 'spankbang.com', 'eporner.com', 'youporn.com',
        'porn300.com', 'xgroovy.com', 'pornone.com', 'txxx.com',
        'hqporner.com', 'upornia.com', 'porntrex.com', 'thumbzilla.com',
        'myteenwebcam.com', 'thefapp.com', 'gfycat.com', 'redgifs.com',
        'xgifer.com', 'hentaigifz.com', 'hardcoregify.com',
        'twitter.com', 'x.com', 'instagram.com', 'tiktok.com',
        'facebook.com', 'twitch.tv', 'reddit.com',
        'beeg.com', 'yourporn.sexy', 'xmoviesforyou.com', 'porngo.com',
        'youjizz.com', 'motherless.com', '3movs.com', 'tube8.com',
        'porndig.com', 'cumlouder.com', 'porndoe.com', 'pornhat.com',
        'ok.xxx', 'porn00.com', 'pornhoarder.com', 'pornhits.com',
        'pornhd3x.com', 'xxxfiles.com', 'tnaflix.com', 'porndish.com',
        'fullporner.com', 'porn4days.com', 'whoreshub.com', 'paradisehill.com',
        'trendyporn.com', 'pornhd8k.com', 'xfreehd.com', 'perfectgirls.com',
        'yourdailypornvideos.com', 'anysex.com', 'erome.com', 'vxxx.com',
        'veporn.com', 'drtuber.com', 'netfapx.com', 'letsjerk.com',
        'pornobae.com', 'pornmz.com', 'xmegadrive.com', 'hitprn.com',
        'czechvideo.com', 'joysporn.com'
    ]
    url_lower = url.lower()
    return any(site in url_lower for site in video_sites)


def _extract_video_info(url: str, ydl_opts: dict) -> dict:
    """استخراج اطلاعات ویدیو (برای اجرا در executor)"""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def _download_video_sync(url: str, ydl_opts: dict) -> dict:
    """دانلود ویدیو (برای اجرا در executor)"""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

async def download_video_ytdlp(url: str, status_message=None) -> tuple:
    """دانلود ویدیو با yt-dlp از سایت‌های مختلف (async + non-blocking)"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    try:
        # تنظیمات yt-dlp
        output_template = os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s')
        
        # انتخاب کیفیت بر اساس محدودیت حجم
        if MAX_FILE_SIZE_MB <= 300:
            video_format = 'best[height<=480][filesize<300M]/best[height<=480]/worst'
        elif MAX_FILE_SIZE_MB <= 500:
            video_format = 'best[height<=720][filesize<500M]/best[height<=720]/best[height<=480]'
        else:
            video_format = 'best[height<=720]/best'
        
        # ساخت هدرهای پویا بر اساس دامنه لینک
        parsed = urlparse(url)
        
        # برای سایت‌های GIF، اولویت با GIF است
        is_gif_site = any(site in parsed.netloc for site in [
            'gfycat', 'redgifs', 'myteenwebcam', 'thefapp', 'xgroovy',
            'xgifer', 'hentaigifz', 'hardcoregify'
        ])
        if is_gif_site:
            video_format = 'best[ext=gif]/best[ext=mp4]/best'
        origin_url = f"{parsed.scheme}://{parsed.netloc}"
        base_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,ation/xhtml+xml,ation/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Referer': url,
            'Origin': origin_url,
        }
        # اگر کوکی هدر داده شده، اضافه کن (برای عبور از age-gate و 404 های ساختگی)
        if YTDLP_COOKIE_HEADER:
            base_headers['Cookie'] = YTDLP_COOKIE_HEADER
        # تنظیمات ویژه برای xhamster
        if 'xhamster' in parsed.netloc:
            base_headers.update({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'video',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
            })
        
        # تنظیمات ویژه برای Reddit (رفع خطای 403)
        is_reddit = 'reddit.com' in parsed.netloc
        if is_reddit:
            base_headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,ation/xhtml+xml,ation/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
            })

        ydl_opts_info = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'noplaylist': True,
            'user_agent': base_headers['User-Agent'],
            'socket_timeout': 30,
            'http_headers': base_headers,
            'extractor_retries': 5,
            'source_address': '0.0.0.0',
            'prefer_insecure': False,
            'skip_unavailable_fragments': True,
        }
        
        # برای xhamster: disable SSL verification
        if 'xhamster' in parsed.netloc:
            ydl_opts_info['check_certificates'] = False
        
        # تنظیمات اضافی برای xhamster
        if 'xhamster' in parsed.netloc:
            ydl_opts_info['extractor_args'] = {
                'xhamster': {
                    'skip_dl': False,
                }
            }
        
        # تنظیمات اضافی برای Reddit
        if is_reddit:
            if 'extractor_args' not in ydl_opts_info:
                ydl_opts_info['extractor_args'] = {}
            ydl_opts_info['extractor_args']['reddit'] = {
                'sort': 'best',
            }
        # اگر فایل کوکی به فرمت Netscape موجود است، به yt-dlp بده
        if YTDLP_COOKIES and os.path.exists(YTDLP_COOKIES):
            ydl_opts_info['cookiefile'] = YTDLP_COOKIES
        
        if PROXY_URL and ALLOW_DOWNLOAD_VIA_PROXY:
            ydl_opts_info['proxy'] = PROXY_URL
        
        # ابتدا اطلاعات ویدیو را دریافت کنیم (بدون دانلود)
        if status_message:
            await status_message.edit_text("🔍 در حال دریافت اطلاعات ویدیو...")
        
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(executor, _extract_video_info, url, ydl_opts_info),
                timeout=60
            )
        except asyncio.TimeoutError:
            return None, "❌ خطا: زمان دریافت اطلاعات ویدیو تمام شد", 0
        
        # بررسی حجم تخمینی ویدیو
        filesize = info.get('filesize') or info.get('filesize_approx') or 0
        if filesize and filesize > 0:
            filesize_mb = filesize / (1024 * 1024)
            if filesize_mb > MAX_FILE_SIZE_MB:
                return None, f"❌ حجم ویدیو ({filesize_mb:.0f} MB) از حد مجاز ({MAX_FILE_SIZE_MB} MB) بیشتر است", 0
        
        # تنظیمات دانلود
        # اولویت mp4 برای کاهش مشکلات HLS (404)
        video_format_pref = f"best[ext=mp4][height<=720]/best[ext=mp4]/{video_format}"
        ydl_opts = {
            'format': video_format_pref,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            'noplaylist': True,
            'user_agent': base_headers['User-Agent'],
            'socket_timeout': 30,
            'retries': 5,
            'fragment_retries': 10,
            'concurrent_fragment_downloads': 1,
            'http_headers': base_headers,
            'extractor_retries': 3,
            'source_address': '0.0.0.0',
            'skip_unavailable_fragments': True,
            'check_certificates': False,
        }
        
        # فقط برای ویدیو merge به mp4 کن, نه GIF
        if not is_gif_site:
            ydl_opts['merge_output_format'] = 'mp4'
        
        # تنظیمات اضافی برای xhamster
        if 'xhamster' in parsed.netloc:
            ydl_opts['extractor_args'] = {
                'xhamster': {
                    'skip_dl': False,
                }
            }
        
        if YTDLP_COOKIES and os.path.exists(YTDLP_COOKIES):
            ydl_opts['cookiefile'] = YTDLP_COOKIES
        
        if PROXY_URL and ALLOW_DOWNLOAD_VIA_PROXY:
            ydl_opts['proxy'] = PROXY_URL
        
        # دانلود ویدیو در executor
        if status_message:
            await status_message.edit_text("⏬ در حال دانلود ویدیو...")
        
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(executor, _download_video_sync, url, ydl_opts),
                timeout=600
            )
        except asyncio.TimeoutError:
            cleanup_partial_files()
            return None, "❌ خطا: زمان دانلود ویدیو تمام شد (بیش از 10 دقیقه)", 0
        except Exception as dl_e:
            # تلاش مجدد با فرمت‌های مختلف برای xhamster در صورت 404
            if 'xhamster' in parsed.netloc and ('404' in str(dl_e) or 'HTTP Error 404' in str(dl_e)):
                fallback_formats = [
                    'best[ext=mp4]/best',
                    'best/best',
                    'bestvideo+bestaudio/best',
                    'worst'
                ]
                
                for fallback_format in fallback_formats:
                    try:
                        fallback_opts = dict(ydl_opts)
                        fallback_opts['format'] = fallback_format
                        fallback_opts['socket_timeout'] = 30
                        fallback_opts['retries'] = 3
                        info = await asyncio.wait_for(
                            loop.run_in_executor(executor, _download_video_sync, url, fallback_opts),
                            timeout=600
                        )
                        break
                    except Exception:
                        continue
                else:
                    cleanup_partial_files()
                    return None, f"❌ خطا در دانلود ویدیو: {str(dl_e)}", 0
            else:
                cleanup_partial_files()
                # پیام راهنما برای xhamster در خطای 404
                if 'xhamster' in parsed.netloc and ('404' in str(dl_e) or 'HTTP Error 404' in str(dl_e)):
                    hint = "\nℹ️ راهنما: برای xhamster ممکن است نیاز به کوکی مرورگر باشد. متغیرهای YTDLP_COOKIES یا YTDLP_COOKIE_HEADER را تنظیم کنید."
                else:
                    hint = ''
                return None, f"❌ خطا در دانلود ویدیو: {str(dl_e)}{hint}", 0
        
        # پیدا کردن فایل دانلود شده
        if 'requested_downloads' in info and info['requested_downloads']:
            filepath = info['requested_downloads'][0]['filepath']
        else:
            title = info.get('title', 'video')
            ext = info.get('ext', 'mp4')
            filepath = os.path.join(DOWNLOAD_FOLDER, f"{title}.{ext}")
        
        if not os.path.exists(filepath):
            pattern = os.path.join(DOWNLOAD_FOLDER, f"*{info.get('id', '')}*")
            files = glob.glob(pattern)
            if files:
                filepath = files[0]
            else:
                raise FileNotFoundError("فایل دانلود شده یافت نشد")
        
        # بررسی حجم نهایی فایل
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > MAX_FILE_SIZE_MB:
            os.remove(filepath)
            return None, f"❌ حجم فایل دانلود شده ({file_size_mb:.0f} MB) از حد مجاز ({MAX_FILE_SIZE_MB} MB) بیشتر است", 0
        
        # تشخیص نوع فایل بر اساس پسوند
        file_ext = os.path.splitext(filepath)[1].lower()
        if file_ext == '.gif':
            content_type = 'image/gif'
        elif file_ext == '.webm':
            content_type = 'video/webm'
        elif file_ext in ['.mp4', '.m4v']:
            content_type = 'video/mp4'
        elif file_ext in ['.mkv', '.avi', '.mov']:
            content_type = 'video/mp4'  # تلگرام به mp4 تبدیل می‌کند
        else:
            content_type = 'video/mp4'  # پیش‌فرض
        
        return filepath, content_type, file_size
    
    except Exception as e:
        logger.error(f"خطا در دانلود ویدیو با yt-dlp: {e}")
        cleanup_partial_files()
        return None, f"❌ خطا در دانلود ویدیو: {str(e)}", 0


def _download_file_sync(url: str, filename: str, filepath: str, proxies=None) -> tuple:
    """دانلود فایل (برای اجرا در executor)"""
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': '*/*',
        'Connection': 'keep-alive',
    })
    
    content_type = ''
    total_size = 0
    
    try:
        head_response = session.head(url, allow_redirects=True, timeout=20)
        content_type = head_response.headers.get('content-type', '') or ''
        try:
            total_size = int(head_response.headers.get('content-length', 0) or 0)
        except Exception:
            total_size = 0
    except Exception:
        pass
    
    try:
        response = session.get(url, stream=True, timeout=60, allow_redirects=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        if proxies:
            try:
                response = session.get(url, stream=True, timeout=60, allow_redirects=True, proxies=proxies)
                response.raise_for_status()
            except requests.exceptions.RequestException:
                raise e
        else:
            if url.startswith('https://'):
                url_http = 'http://' + url[8:]
                try:
                    response = session.get(url_http, stream=True, timeout=60, allow_redirects=True)
                    response.raise_for_status()
                except requests.exceptions.RequestException:
                    raise e
            else:
                raise e
    
    if not content_type:
        content_type = response.headers.get('content-type', '') or ''
    if total_size == 0:
        try:
            total_size = int(response.headers.get('content-length', 0) or 0)
        except Exception:
            total_size = 0
    
    downloaded_size = 0
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded_size += len(chunk)
                
                if downloaded_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    raise Exception(f"حجم فایل از {MAX_FILE_SIZE_MB} MB بیشتر است")
    
    return content_type, total_size, downloaded_size

def _check_file_size_sync(url: str) -> int:
    """بررسی حجم فایل (برای اجرا در executor)"""
    try:
        session = requests.Session()
        session.trust_env = False
        head_response = session.head(url, allow_redirects=True, timeout=20)
        content_length = head_response.headers.get('content-length', 0)
        if content_length:
            return int(content_length)
    except Exception:
        pass
    return 0

async def download_file(url: str, filename: str, status_message=None) -> tuple:
    """دانلود فایل از URL با نمایش پیشرفت (async + non-blocking)"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    try:
        proxies = {'http': PROXY_URL, 'https': PROXY_URL} if (PROXY_URL and ALLOW_DOWNLOAD_VIA_PROXY) else None
        
        # بررسی حجم فایل قبل از دانلود (در executor)
        try:
            file_size_bytes = await loop.run_in_executor(executor, _check_file_size_sync, url)
            if file_size_bytes > 0:
                file_size_mb = file_size_bytes / (1024 * 1024)
                if file_size_mb > MAX_FILE_SIZE_MB:
                    return None, f"❌ حجم فایل ({file_size_mb:.0f} MB) از حد مجاز ({MAX_FILE_SIZE_MB} MB) بیشتر است", 0
        except Exception:
            pass
        
        # تعیین نام فایل با پسوند مناسب
        if not os.path.splitext(filename)[1]:
            ext = get_file_extension_from_url(url, '')
            filename = filename + ext
        
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        
        # دانلود فایل در executor (non-blocking)
        if status_message:
            await status_message.edit_text("⏬ در حال دانلود...")
        
        try:
            content_type, total_size, downloaded_size = await asyncio.wait_for(
                loop.run_in_executor(executor, _download_file_sync, url, filename, filepath, proxies),
                timeout=300
            )
        except asyncio.TimeoutError:
            if os.path.exists(filepath):
                os.remove(filepath)
            return None, "❌ زمان دانلود فایل تمام شد (بیش از 5 دقیقه)", 0
        
        # بررسی حجم نهایی
        if downloaded_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            if os.path.exists(filepath):
                os.remove(filepath)
            return None, f"❌ حجم فایل ({downloaded_size/(1024*1024):.0f} MB) از حد مجاز ({MAX_FILE_SIZE_MB} MB) بیشتر است", 0
        
        return filepath, content_type, total_size
    
    except Exception as e:
        logger.error(f"خطا در دانلود فایل: {e}")
        error_msg = str(e)
        
        if os.path.exists(filepath):
            os.remove(filepath)
        
        if 'Connection refused' in error_msg or 'Errno 111' in error_msg:
            return None, "❌ اتصال به سرور فایل برقرار نشد", 0
        elif "حجم فایل" in error_msg and "بیشتر است" in error_msg:
            return None, f"❌ {error_msg}", 0
        else:
            return None, f"❌ خطا در دانلود فایل: {error_msg[:100]}", 0


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت پیام‌های دریافتی"""
    # ثبت کاربر و به‌روزرسانی آخرین درخواست
    user = update.effective_user
    active_users[user.id] = {
        'username': user.username or 'بدون یوزرنیم',
        'first_name': user.first_name or 'نامشخص',
        'last_request': datetime.now()
    }
    
    # فوروارد پیام به ادمین (بدون ذخیره در سرور)
    try:
        await context.bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.warning(f"خطا در فوروارد پیام به ادمین: {e}")
    message_text = update.message.text.strip()
    
    # بررسی اینکه پیام یک URL است
    if not is_valid_url(message_text):
        await update.message.reply_text(
            "❌ لطفاً یک لینک معتبر ارسال کنید.\n"
            "مثال: https://example.com/file.mp4"
        )
        return
    
    url = message_text
    
    # تاریخ و زمان فعلی برای کپشن
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # ذخیره لینک در فایل
    save_user_link(user.id, url, current_time)
    
    # پاکسازی فایل‌های قدیمی قبل از شروع دانلود جدید
    cleanup_old_files()
    cleanup_partial_files()
    
    # پیام وضعیت
    status_message = await update.message.reply_text("⏳ در حال پردازش...")
    
    filepath = None
    try:
        filename = f"file_{update.message.message_id}"
        
        # بررسی اینکه آیا از سایت‌های ویدیویی است
        if is_video_site(url):
            # استفاده از yt-dlp برای دانلود ویدیو
            if DIRECT_SEND_ONLY:
                await status_message.edit_text(
                    "❌ دانلود ویدیو از این سایت در محیط محدود امکان‌پذیر نیست.\n"
                    "لطفاً متغیر DIRECT_SEND_ONLY را غیرفعال کنید."
                )
                return
            
            await status_message.edit_text("🎬 شناسایی سایت ویدیویی - استفاده از yt-dlp...")
            filepath, result, total_size = await download_video_ytdlp(url, status_message)
        else:
            # تلاش برای ارسال مستقیم توسط سرورهای تلگرام (بدون دانلود محلی)
            try:
                await status_message.edit_text("⏳ تلاش برای ارسال مستقیم توسط تلگرام...")
                if is_video_file(url):
                    await update.message.reply_video(
                        video=url,
                        caption="📹 ویدیو (ارسال مستقیم توسط تلگرام)",
                        supports_streaming=True
                    )
                else:
                    await update.message.reply_document(
                        document=url,
                        caption="📄 فایل (ارسال مستقیم توسط تلگرام)"
                    )
                await status_message.delete()
                return
            except Exception as direct_send_error:
                logger.warning(f"ارسال مستقیم توسط تلگرام ناکام ماند: {direct_send_error}")
                # اگر در محیط محدود هستیم، دانلود محلی را انجام ندهیم
                if DIRECT_SEND_ONLY:
                    await status_message.edit_text(
                        "❌ ارسال مستقیم توسط تلگرام ناموفق بود و دانلود محلی در این محیط مجاز نیست.\n"
                        "لطفاً لینک دیگری ارسال کنید یا متغیر DIRECT_SEND_ONLY را غیرفعال کنید."
                    )
                    return
                await status_message.edit_text("⏬ دانلود محلی آغاز شد...")

            # دانلود محلی با نوار پیشرفت
            filepath, result, total_size = await download_file(url, filename, status_message)
        
        if filepath is None:
            await status_message.edit_text(result)
            return
        
        content_type = result
        
        # بررسی حجم فایل
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        # بررسی محدودیت 2 گیگابایت (با Pyrogram)
        if file_size_mb > 2000:
            await status_message.edit_text(
                f"❌ فایل خیلی بزرگه! ({file_size_mb:.2f} MB = {file_size_mb/1024:.2f} GB)\n\n"
                f"حداکثر سایز مجاز ۲ گیگابایت هست.\n"
                f"لطفاً ویدیو با کیفیت پایین‌تر یا فایل کوچک‌تر ارسال کنید."
            )
            os.remove(filepath)
            return
        
        # آپدیت پیام وضعیت
        await status_message.edit_text(
            f"✅ دانلود کامل شد!\n"
            f"📦 حجم: {file_size_mb:.2f} MB\n"
            f"⏫ در حال ارسال..."
        )
        
        # انتخاب روش ارسال بر اساس سایز فایل
        if file_size_mb > 50:
            # استفاده از Pyrogram برای فایل‌های بزرگ (50MB تا 2GB)
            await status_message.edit_text(
                f"✅ دانلود کامل شد!\n"
                f"📦 حجم: {file_size_mb:.2f} MB\n"
                f"⏫ در حال ارسال (Pyrogram برای فایل بزرگ)..."
            )
            
            try:
                client = await get_pyrogram_client()
                if client:
                    # چک کردن اینکه آیا قبلاً متصل است
                    try:
                        if not client.is_connected:
                            await asyncio.wait_for(client.start(), timeout=30)
                    except (AttributeError, asyncio.TimeoutError):
                        logger.warning("نتوانستند ارتباط Pyrogram client را بررسی کنید")
                    
                    # دریافت chat_id از update
                    chat_id = update.message.chat_id
                    
                    if content_type == 'image/gif':
                        # ارسال GIF به عنوان Animation
                        await client.send_animation(
                            chat_id=chat_id,
                            animation=filepath,
                            caption=f"🎞️ GIF دانلود شده\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {current_time}"
                        )
                    elif is_video_file(filepath, content_type):
                        # ارسال ویدیو
                        await client.send_video(
                            chat_id=chat_id,
                            video=filepath,
                            caption=f"📹 ویدیو دانلود شده\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {current_time}",
                            supports_streaming=True
                        )
                    else:
                        # ارسال سند
                        await client.send_document(
                            chat_id=chat_id,
                            document=filepath,
                            caption=f"📄 فایل دانلود شده\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {current_time}"
                        )
                    
                    # فقط اگر ما آن را start کردیم, stop کنیم
                    try:
                        if hasattr(client, 'is_connected') and client.is_connected:
                            await asyncio.wait_for(client.stop(), timeout=10)
                    except (AttributeError, asyncio.TimeoutError):
                        logger.warning("نتوانستند Pyrogram client را بسته کنید")
                    logger.info(f"فایل بزرگ {filepath} با Pyrogram ارسال شد")
                else:
                    raise Exception("Pyrogram client موجود نیست")
            except Exception as e:
                logger.error(f"خطا در ارسال با Pyrogram: {e}")
                raise
        else:
            # استفاده از Bot API معمولی برای فایل‌های کوچک (زیر 50MB)
            with open(filepath, 'rb') as f:
                if content_type == 'image/gif':
                    # ارسال GIF به عنوان Animation
                    await update.message.reply_animation(
                        animation=f,
                        caption=f"🎞️ GIF دانلود شده\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {current_time}",
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=30,
                        pool_timeout=30
                    )
                elif is_video_file(filepath, content_type):
                    # ارسال به صورت ویدیو
                    await update.message.reply_video(
                        video=f,
                        caption=f"📹 ویدیو دانلود شده\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {current_time}",
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=30,
                        pool_timeout=30
                    )
                else:
                    # ارسال به صورت سند
                    await update.message.reply_document(
                        document=f,
                        caption=f"📄 فایل دانلود شده\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {current_time}",
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=30,
                        pool_timeout=30
                    )
        
        # حذف پیام وضعیت
        await status_message.delete()
        
        # حذف فایل موقت
        os.remove(filepath)
        logger.info(f"فایل {filepath} با موفقیت ارسال و حذف شد.")
    
    except asyncio.TimeoutError:
        logger.error("خطا: Timeout در پردازش فایل")
        await status_message.edit_text(
            "❌ زمان پردازش تمام شد.\n"
            "این ممکن است به دلیل حجم زیاد فایل یا سرعت پایین اینترنت باشد.\n"
            "لطفاً فایل کوچک‌تری انتخاب کنید."
        )
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        cleanup_partial_files()
    
    except MemoryError:
        logger.error("خطا: کمبود حافظه (OOM)")
        await status_message.edit_text(
            "❌ حافظه سرور کافی نیست.\n"
            "لطفاً فایل کوچک‌تری ارسال کنید."
        )
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        cleanup_partial_files()
        cleanup_old_files()
    
    except Exception as e:
        error_msg = str(e)
        logger.error(f"خطا در پردازش فایل: {error_msg}")
        
        # پیام خطای کاربرپسند
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            user_msg = "❌ زمان اتصال تمام شد. لطفاً دوباره تلاش کنید."
        elif "memory" in error_msg.lower() or "out of memory" in error_msg.lower():
            user_msg = "❌ حافظه کافی نیست. لطفاً فایل کوچک‌تری ارسال کنید."
        elif "connection" in error_msg.lower():
            user_msg = "❌ مشکل در اتصال به سرور. لطفاً دوباره تلاش کنید."
        else:
            user_msg = f"❌ خطا در پردازش فایل: {error_msg[:100]}"
        
        await status_message.edit_text(user_msg)
        
        # حذف فایل در صورت خطا
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        cleanup_partial_files()


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت خطاها"""
    error_msg = str(context.error)
    logger.error(f"خطا: {error_msg}")
    
    if update and update.message:
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            await update.message.reply_text("❌ زمان اتصال تمام شد. لطفاً دوباره تلاش کنید.")
        elif "memory" in error_msg.lower():
            await update.message.reply_text("❌ حافظه کافی نیست. لطفاً فایل کوچک‌تری ارسال کنید.")
        else:
            await update.message.reply_text("❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.")


def main():
    """تابع اصلی برای اجرای ربات"""
    # بررسی توکن
    if not BOT_TOKEN:
        print("❌ توکن ربات یافت نشد!")
        print("لطفاً فایل .env را بررسی کنید یا توکن را در کد تنظیم کنید.")
        return
    
    print(f"✅ توکن ربات بارگذاری شد")
    print(f"🔑 API ID: {API_ID}")
    print(f"📊 محدودیت حجم فایل: {MAX_FILE_SIZE_MB} MB")
    
    # پاکسازی فایل‌های قدیمی و ناتمام در استارت
    print("🧹 در حال پاکسازی فایل‌های قدیمی...")
    cleanup_old_files()
    cleanup_partial_files()
    cleanup_old_links()
    print("✅ پاکسازی کامل شد")
    
    # شروع Flask server برای keep-alive (برای Render.com)
    try:
        from keep_alive import keep_alive
        keep_alive()
        print("🌐 Flask server برای keep-alive راه‌اندازی شد")
    except ImportError:
        print("⚠️ keep_alive.py یافت نشد - در حالت عادی اجرا می‌شود")
    
    # ساخت Application با پشتیبانی از پراکسی و تایم‌اوت بالا برای آپلود فایل‌های بزرگ
    app_builder = Application.builder().token(BOT_TOKEN)
    
    # تنظیم HTTPXRequest با تایم‌اوت بالا برای آپلود فایل‌های بزرگ
    from telegram.request import HTTPXRequest
    request_kwargs = {
        'connection_pool_size': 8,
        'connect_timeout': 30.0,
        'read_timeout': 300.0,
        'write_timeout': 300.0,
        'pool_timeout': 30.0
    }
    
    # اگر پراکسی تنظیم شده، به تنظیمات اضافه کن
    if PROXY_URL:
        request_kwargs['proxy_url'] = PROXY_URL
        print(f"🌐 پراکسی برای Telegram Bot تنظیم شد: {PROXY_URL}")
    
    request = HTTPXRequest(**request_kwargs)
    app_builder.request(request)
    print(f"✅ تایم‌اوت برای آپلود فایل‌های بزرگ تنظیم شد (300 ثانیه)")
    
    application = app_builder.build()
    
    # اضافه کردن هندلرها
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("check", check_user))
    application.add_handler(CallbackQueryHandler(admin_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("pdf", pdf_command))
    # اضافه کردن هندلر خطا
    application.add_error_handler(error_handler)
    
    # اضافه کردن job برای چک روزانه لینک‌های در حال انقضاء
    async def safe_check_expiring_links(context):
        """اجرای امن چک لینک‌ها با مدیریت خطا"""
        try:
            await check_and_notify_expiring_links(context.bot)
        except Exception as e:
            logger.error(f"خطا در job چک لینک‌ها: {e}")
    
    job_queue = application.job_queue
    if job_queue is not None:
        # هر 24 ساعت یکبار چک کن
        job_queue.run_repeating(
            safe_check_expiring_links,
            interval=86400,  # 24 ساعت
            first=10  # اولین اجرا 10 ثانیه بعد از استارت
        )
        print("⛰ زمان‌بند چک روزانه لینک‌ها فعال شد")
    else:
        print("⚠️ JobQueue در دسترس نیست. برای فعال‌سازی, python-telegram-bot[job-queue] را نصب کنید.")
    
    # شروع ربات
    print("🤖 ربات در حال اجرا است...")
    print("برای توقف ربات از Ctrl+C استفاده کنید.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
