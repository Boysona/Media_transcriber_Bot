import logging
from flask import Flask, request, abort
import telebot

# ===== STATIC CONFIG =====
BOT_TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4"  # ← Ku beddel token-kaaga
WEBHOOK_URL = "https://media-transcriber-bot-sal5.onrender.com/"  # ← Ku beddel URL sax ah (HTTPS)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== AUTO REPLY MESSAGE (URL link instead of @) =====
REPLY_MESSAGE = """\
This bot is currently down due to technical issues,  
Please use the other bot like https://t.me/MediaToTextBot if you need voice-to-text urgently,  
Or wait until this bot is fixed,  
Thank you.

هذا البوت لا يعمل حاليًا بسبب مشاكل فنية،  
يرجى استخدام البوت الآخر مثل https://t.me/MediaToTextBot إذا كنت بحاجة لتحويل الصوت إلى نص بشكل عاجل،  
أو انتظر حتى يتم إصلاح هذا البوت،  
شكرًا لك.
"""

# ===== HANDLE ALL INCOMING MESSAGES OF ANY TYPE =====
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    try:
        # Isticmaal send_message oo leh reply_to_message_id si loo jawaabo fariinta saxda ah
        bot.send_message(
            chat_id=message.chat.id,
            text=REPLY_MESSAGE,
            reply_to_message_id=message.message_id,
            parse_mode="Markdown"  # ama HTML haddii aad rabto
        )
    except Exception as e:
        logging.error(f"Failed to send reply: {e}")

# ===== MAIN WEBHOOK ROUTE =====
@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method in ("GET", "HEAD"):
        return "OK", 200

    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
            bot.process_new_updates([update])
            return "", 200

    return abort(403)

# ===== SET WEBHOOK =====
@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook_route():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        return f"Failed to set webhook: {e}", 500

# ===== DELETE WEBHOOK =====
@app.route("/delete_webhook", methods=["GET", "POST"])
def delete_webhook_route():
    try:
        bot.delete_webhook()
        return "Webhook deleted.", 200
    except Exception as e:
        logging.error(f"Failed to delete webhook: {e}")
        return f"Failed to delete webhook: {e}", 500

# ===== AUTO WEBHOOK SETUP ON START =====
def set_webhook_on_startup():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set successfully to {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Failed to set webhook on startup: {e}")

# ===== MAIN ENTRY =====
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    set_webhook_on_startup()
    app.run(host="0.0.0.0", port=8080)
