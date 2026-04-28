# web_to_pdf.py
import os
import logging
import asyncio
from datetime import datetime
from urllib.parse import urlparse
import aiohttp
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# تلاش برای import کردن ماژول‌های مورد نیاز
try:
    from pyppeteer import launch
    import nest_asyncio
    nest_asyncio.apply()
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    logger.warning("pyppeteer نصب نیست. برای نصب: pip install pyppeteer")

# پوشه ذخیره PDF
PDF_FOLDER = "pdf_output"
os.makedirs(PDF_FOLDER, exist_ok=True)

async def html_to_pdf(url: str, status_message=None) -> tuple:
    """
    تبدیل صفحه وب به PDF با حفظ تمام محتوا (عکس‌ها، GIFها، متن)
    
    Args:
        url: آدرس صفحه وب
        status_message: پیام وضعیت برای آپدیت کردن
    
    Returns:
        tuple: (filepath, error_message, file_size)
    """
    
    if not PYPPETEER_AVAILABLE:
        error_msg = "❌ ماژول pyppeteer نصب نیست. لطفاً با دستور 'pip install pyppeteer' نصب کنید."
        if status_message:
            await status_message.edit_text(error_msg)
        return None, error_msg, 0
    
    browser = None
    page = None
    
    try:
        if status_message:
            await status_message.edit_text("🌐 در حال راه‌اندازی مرورگر مجازی...")
        
        # راه‌اندازی مرورگر با تنظیمات بهینه
        browser = await launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-extensions',
                '--disable-web-security',
                '--disable-features=VizDisplayCompositor',
                '--enable-features=NetworkService,NetworkServiceInProcess',
                '--window-size=1920,1080'
            ],
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
            defaultViewport={'width': 1920, 'height': 1080}
        )
        
        if status_message:
            await status_message.edit_text("📄 در حال بارگذاری صفحه...")
        
        page = await browser.newPage()
        
        # تنظیم User-Agent برای شبیه‌سازی مرورگر واقعی
        await page.setUserAgent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        # تنظیم تایم‌اوت برای بارگذاری
        await page.setDefaultNavigationTimeout(60000)  # 60 ثانیه
        
        # بارگذاری صفحه
        try:
            response = await page.goto(url, {
                'waitUntil': 'networkidle2',  # صبر برای اتمام بارگذاری شبکه
                'timeout': 60000
            })
            
            if response and response.status >= 400:
                return None, f"❌ خطا در بارگذاری صفحه: HTTP {response.status}", 0
                
        except Exception as e:
            return None, f"❌ خطا در بارگذاری صفحه: {str(e)[:100]}", 0
        
        if status_message:
            await status_message.edit_text("🖼️ در حال پردازش عکس‌ها و محتوا...")
        
        # اسکرول صفحه تا پایین برای بارگذاری تمام محتوای داینامیک
        await page.evaluate('''
            async function scrollToBottom() {
                const scrollHeight = document.body.scrollHeight;
                const windowHeight = window.innerHeight;
                let currentPosition = 0;
                
                while (currentPosition < scrollHeight) {
                    window.scrollTo(0, currentPosition);
                    await new Promise(resolve => setTimeout(resolve, 300));
                    currentPosition += windowHeight;
                    
                    // صبر برای بارگذاری محتوای جدید
                    if (currentPosition % (windowHeight * 3) === 0) {
                        await new Promise(resolve => setTimeout(resolve, 500));
                    }
                }
                
                // اسکرول به بالا
                window.scrollTo(0, 0);
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            await scrollToBottom();
        ''')
        
        if status_message:
            await status_message.edit_text("📸 در حال بارگذاری تصاویر...")
        
        # صبر برای بارگذاری کامل تصاویر لازی لود
        await page.evaluate('''
            async function waitForImages() {
                const images = document.querySelectorAll('img[data-src], img[data-original], img.lazy');
                for (const img of images) {
                    if (img.dataset.src) {
                        img.src = img.dataset.src;
                    }
                    if (img.dataset.original) {
                        img.src = img.dataset.original;
                    }
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
            await waitForImages();
        ''')
        
        # اسکرول مجدد برای اطمینان از بارگذاری همه چیز
        await page.evaluate('''
            window.scrollTo(0, document.body.scrollHeight);
            await new Promise(resolve => setTimeout(resolve, 1000));
            window.scrollTo(0, 0);
            await new Promise(resolve => setTimeout(resolve, 500));
        ''')
        
        if status_message:
            await status_message.edit_text("📝 در حال تولید PDF...")
        
        # تنظیمات PDF
        pdf_options = {
            'format': 'A4',
            'printBackground': True,  # حفظ پس‌زمینه‌ها
            'margin': {
                'top': '20px',
                'right': '20px',
                'bottom': '20px',
                'left': '20px'
            },
            'preferCSSPageSize': False,
            'scale': 1,  # مقیاس 100%
            'displayHeaderFooter': True,  # نمایش هدر و فوتر
            'headerTemplate': f'''
                <div style="font-size:8px; width:100%; text-align:center; padding:5px;">
                    {urlparse(url).netloc}
                </div>
            ''',
            'footerTemplate': '''
                <div style="font-size:8px; width:100%; text-align:center; padding:5px;">
                    صفحه <span class="pageNumber"></span> از <span class="totalPages"></span>
                </div>
            '''
        }
        
        # ایجاد نام فایل بر اساس URL
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.replace('.', '_')
        path = parsed_url.path.replace('/', '_')[:50] if parsed_url.path else 'home'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pdf_{domain}_{path}_{timestamp}.pdf"
        # حذف کاراکترهای غیرمجاز
        filename = "".join(c for c in filename if c.isalnum() or c in '._-')
        filepath = os.path.join(PDF_FOLDER, filename)
        
        # تولید PDF
        await page.pdf({'path': filepath, **pdf_options})
        
        # بررسی حجم فایل
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        # محدودیت 50MB برای تلگرام (با Bot API معمولی)
        if file_size_mb > 50:
            # برای فایل‌های بزرگتر از 50MB، از Pyrogram استفاده خواهد شد
            logger.info(f"PDF حجم {file_size_mb:.2f} MB دارد، با Pyrogram ارسال می‌شود")
        
        if status_message:
            await status_message.edit_text(f"✅ PDF ساخته شد!\n📦 حجم: {file_size_mb:.2f} MB")
        
        return filepath, None, file_size
        
    except Exception as e:
        logger.error(f"خطا در تولید PDF: {e}")
        error_msg = f"❌ خطا در تولید PDF: {str(e)[:150]}"
        if status_message:
            await status_message.edit_text(error_msg)
        return None, error_msg, 0
        
    finally:
        # بستن مرورگر
        if page:
            try:
                await page.close()
            except:
                pass
        if browser:
            try:
                await browser.close()
            except:
                pass


async def capture_full_page_screenshot(url: str, status_message=None) -> tuple:
    """
    گرفتن اسکرین‌شات کامل از صفحه (به عنوان جایگزین در صورت عدم موفقیت PDF)
    
    Returns:
        tuple: (filepath, error_message, file_size)
    """
    
    if not PYPPETEER_AVAILABLE:
        return None, "pyppeteer نصب نیست", 0
    
    browser = None
    page = None
    
    try:
        if status_message:
            await status_message.edit_text("📸 در حال گرفتن اسکرین‌شات از صفحه...")
        
        browser = await launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox'],
            handleSIGINT=False,
            handleSIGTERM=False
        )
        
        page = await browser.newPage()
        await page.setViewport({'width': 1920, 'height': 1080})
        await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
        
        # اسکرول به پایین
        await page.evaluate('''
            async function scroll() {
                let totalHeight = 0;
                const distance = 100;
                const scrollHeight = document.body.scrollHeight;
                
                while (totalHeight < scrollHeight) {
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    await new Promise(resolve => setTimeout(resolve, 100));
                }
                window.scrollTo(0, 0);
            }
            await scroll();
        ''')
        
        # گرفتن اسکرین‌شات کامل
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"screenshot_{timestamp}.png"
        filepath = os.path.join(PDF_FOLDER, filename)
        
        await page.screenshot({
            'path': filepath,
            'fullPage': True,
            'type': 'png'
        })
        
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        return filepath, None, file_size
        
    except Exception as e:
        logger.error(f"خطا در اسکرین‌شات: {e}")
        return None, f"خطا: {str(e)[:100]}", 0
        
    finally:
        if page:
            await page.close()
        if browser:
            await browser.close()


async def pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    هندلر دستور /pdf - تبدیل صفحه وب به PDF
    استفاده: /pdf https://example.com
    """
    
    user = update.effective_user
    
    # بررسی وجود آرگومان
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "❌ لطفاً آدرس صفحه را وارد کنید.\n"
            "مثال: `/pdf https://example.com`\n\n"
            "ℹ️ این قابلیت صفحه وب را به PDF تبدیل می‌کند.\n"
            "✅ تمام عکس‌ها و GIFها حفظ می‌شوند.\n"
            "✅ صفحه تا پایین اسکرول می‌شود.\n"
            "✅ کیفیت اصلی حفظ می‌شود.",
            parse_mode='Markdown'
        )
        return
    
    url = context.args[0]
    
    # اعتبارسنجی URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            await update.message.reply_text("❌ آدرس نامعتبر است. لطفاً یک URL معتبر وارد کنید.")
            return
    except Exception:
        await update.message.reply_text("❌ آدرس نامعتبر است.")
        return
    
    # پیام وضعیت
    status_message = await update.message.reply_text(
        "🔄 در حال پردازش صفحه وب...\n"
        "این عملیات ممکن است چند لحظه طول بکشد."
    )
    
    try:
        # تلاش برای تولید PDF
        filepath, error, file_size = await html_to_pdf(url, status_message)
        
        # اگر PDF موفقیت‌آمیز نبود، اسکرین‌شات بگیر
        if error or not filepath:
            if "pyppeteer" in str(error).lower():
                await status_message.edit_text(
                    "❌ ماژول pyppeteer نصب نیست.\n\n"
                    "برای نصب از دستور زیر استفاده کنید:\n"
                    "```\npip install pyppeteer\n```\n"
                    "و سپس Node.js را نصب کنید:\n"
                    "```\n# روی سرور:\napt-get install -y chromium\n```",
                    parse_mode='Markdown'
                )
                return
            
            await status_message.edit_text(
                f"⚠️ تولید PDF با مشکل مواجه شد.\n"
                f"در حال تلاش برای گرفتن اسکرین‌شات...\n\n{error}"
            )
            
            filepath, error, file_size = await capture_full_page_screenshot(url, status_message)
            
            if error or not filepath:
                await status_message.edit_text(
                    f"❌ خطا در تبدیل صفحه:\n{error}\n\n"
                    "نکات:\n"
                    "- مطمئن شوید سایت قابل دسترسی است\n"
                    "- برخی سایتها ممکن است دسترسی ربات را محدود کنند\n"
                    "- pyppeteer و Chromium باید نصب باشند"
                )
                return
        
        # آپدیت پیام وضعیت
        await status_message.edit_text(
            f"✅ صفحه با موفقیت تبدیل شد!\n"
            f"📦 حجم: {file_size / (1024*1024):.2f} MB\n"
            f"⏫ در حال ارسال..."
        )
        
        # ارسال فایل
        file_size_mb = file_size / (1024 * 1024)
        
        with open(filepath, 'rb') as pdf_file:
            # انتخاب کپشن مناسب
            if filepath.endswith('.png'):
                caption = f"📸 اسکرین‌شات صفحه\n🌐 {url}\n📦 حجم: {file_size_mb:.2f} MB"
            else:
                caption = f"📄 PDF صفحه وب\n🌐 {url}\n📦 حجم: {file_size_mb:.2f} MB\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            # ارسال به صورت سند
            await update.message.reply_document(
                document=pdf_file,
                caption=caption,
                filename=os.path.basename(filepath),
                read_timeout=300,
                write_timeout=300
            )
        
        # حذف فایل موقت
        os.remove(filepath)
        
        # حذف پیام وضعیت
        await status_message.delete()
        
    except Exception as e:
        logger.error(f"خطا در pdf_command: {e}")
        await status_message.edit_text(
            f"❌ خطا در پردازش:\n{str(e)[:150]}\n\n"
            "نکات:\n"
            "- مطمئن شوید pyppeteer نصب شده: pip install pyppeteer\n"
            "- Chromium باید نصب باشد\n"
            "- برخی سایتها ممکن است قابل تبدیل نباشند"
        )