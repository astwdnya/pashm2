# Telegram Video Downloader Bot

## نمای کلی
این یک ربات تلگرام برای دانلود ویدیو از سایت‌های مختلف است که با استفاده از yt-dlp و python-telegram-bot ساخته شده.

## تغییرات اخیر (2 نوامبر 2025)

### رفع مشکلات Render.com
مشکلات مربوط به ری‌استارت ربات و OOM در سرور render.com حل شد:

1. **دانلود Non-blocking**: 
   - تمام عملیات دانلود (yt-dlp و HTTP) به ThreadPoolExecutor منتقل شدند
   - event loop همیشه پاسخگو می‌ماند

2. **مدیریت حافظه**:
   - محدودیت حجم فایل قابل تنظیم (`MAX_FILE_SIZE_MB`)
   - بررسی حجم قبل و حین دانلود
   - کیفیت ویدیو بر اساس محدودیت حجم

3. **پاکسازی خودکار**:
   - فایل‌های قدیمی‌تر از 1 ساعت
   - فایل‌های ناتمام (.part, .ytdl, .temp)
   - اجرا در استارت و قبل از هر دانلود

4. **مدیریت خطا بهبود یافته**:
   - پیام‌های کاربرپسند برای OOM، Timeout، Connection
   - cleanup در تمام مسیرهای خطا

## متغیرهای محیطی

```bash
BOT_TOKEN=              # توکن ربات تلگرام (الزامی)
API_ID=                 # API ID از my.telegram.org (الزامی)
API_HASH=               # API Hash از my.telegram.org (الزامی)
MAX_FILE_SIZE_MB=500    # محدودیت حجم (MB) - برای Render Free: 300
```

## سایت‌های پشتیبانی شده
- ✅ porn300.com
- ✅ xgroovy.com  
- ✅ YouTube, Vimeo, Dailymotion
- ✅ Twitter, Instagram, TikTok
- ✅ 1000+ سایت دیگر

## توصیه‌های Render.com
- **Free Tier**: `MAX_FILE_SIZE_MB=300`
- **Starter Tier**: `MAX_FILE_SIZE_MB=500`
- **Pro Tier**: `MAX_FILE_SIZE_MB=1000`

## معماری پروژه
- `main.py`: منطق اصلی ربات
- `keep_alive.py`: Flask server برای health check
- `downloads/`: پوشه موقت برای فایل‌ها (پاکسازی خودکار)

## ویژگی‌های کلیدی
- دانلود async و non-blocking
- پشتیبانی از فایل‌های تا 2GB (Pyrogram)
- محدودیت حجم قابل تنظیم
- پاکسازی خودکار فایل‌ها
- مدیریت خطای جامع
