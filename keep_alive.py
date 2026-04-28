from flask import Flask, jsonify
from threading import Thread
import os

app = Flask(__name__)

@app.route('/')
def home():
    """صفحه اصلی برای health check"""
    return jsonify({
        "status": "alive",
        "message": "Bot is running!",
        "service": "Telegram File Downloader Bot"
    })

@app.route('/health')
def health():
    """Endpoint برای سرویس مانیتورینگ"""
    return jsonify({
        "status": "healthy",
        "uptime": "running"
    }), 200

@app.route('/ping')
def ping():
    """Endpoint ساده برای پینگ"""
    return "pong", 200

def run():
    """اجرای Flask در پورت مشخص شده"""
    port = int(os.getenv('PORT', 5000))
    try:
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"⚠️ پورت {port} در حال استفاده است - Flask server قبلاً اجرا شده")
        else:
            raise

def keep_alive():
    """اجرای Flask در یک Thread جداگانه"""
    t = Thread(target=run)
    t.daemon = True
    t.start()
    print(f"✅ Flask server started on port {os.getenv('PORT', 5000)}")
