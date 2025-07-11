import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
    MessageHandler,
    Filters,
    Dispatcher,
)
from pytube import YouTube
import requests
from io import BytesIO
from urllib.parse import urlparse
from flask import Flask, request, abort
import telegram

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Bot Token (Replace with yours)
TOKEN = "7790991731:AAFgEjc6f0-iTSSkpt3lEJBH86gQY5nIgAw"

# Webhook URL (Replace with your HTTPS endpoint)
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com"

# Supported domains
SUPPORTED_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
]

# Flask App for Webhook
app = Flask(__name__)

# Initialize Telegram Updater
updater = Updater(TOKEN, use_context=True)
dispatcher = updater.dispatcher
bot = updater.bot

# Start Command
def start(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    welcome_msg = (
        f"👋 *Welcome {user.first_name}!*\n\n"
        "📲 *SnapGrab Pro* lets you download videos from:\n"
        "- YouTube\n- Instagram\n- TikTok\n- Twitter/X\n\n"
        "🔗 *Just send a video link!*"
    )

    keyboard = [
        [InlineKeyboardButton("🆘 Help", callback_data="help")],
        [InlineKeyboardButton("🌐 Supported Sites", callback_data="sites")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    update.message.reply_text(
        welcome_msg, parse_mode="Markdown", reply_markup=reply_markup
    )

# Help Command
def help_command(update: Update, context: CallbackContext) -> None:
    help_text = (
        "❓ *How to use SnapGrab Pro:*\n\n"
        "1. Send a link from:\n"
        "   - YouTube\n   - Instagram\n   - TikTok\n   - Twitter/X\n\n"
        "2. The bot will process & send the video!\n\n"
        "⚠ *Note:* Some private/restricted videos may not be downloadable."
    )
    update.message.reply_text(help_text, parse_mode="Markdown")

# Supported Sites
def supported_sites(update: Update, context: CallbackContext) -> None:
    sites_text = (
        "🌐 *Supported Platforms:*\n\n"
        "- YouTube (Full HD)\n"
        "- Instagram (Reels/Posts)\n"
        "- TikTok (No Watermark)\n"
        "- Twitter/X (Videos)\n\n"
        "🔗 Just send a valid link!"
    )
    update.message.reply_text(sites_text, parse_mode="Markdown")

# Handle Inline Buttons
def button_handler(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    if query.data == "help":
        help_text = (
            "❓ *How to use SnapGrab Pro:*\n\n"
            "1. Send a link from YouTube/Instagram/TikTok/Twitter.\n"
            "2. The bot will process & send the video!\n\n"
            "⚠ *Note:* Some private videos may not be downloadable."
        )
        query.edit_message_text(help_text, parse_mode="Markdown")
    elif query.data == "sites":
        sites_text = (
            "🌐 *Supported Platforms:*\n\n"
            "- YouTube (Full HD)\n"
            "- Instagram (Reels/Posts)\n"
            "- TikTok (No Watermark)\n"
            "- Twitter/X (Videos)\n\n"
            "🔗 Just send a valid link!"
        )
        query.edit_message_text(sites_text, parse_mode="Markdown")

# Check if URL is valid & supported
def is_supported(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    return any(supported in domain for supported in SUPPORTED_DOMAINS)

# Download YouTube Video
def download_youtube(url, update: Update):
    try:
        yt = YouTube(url)
        stream = yt.streams.get_highest_resolution()

        # Send downloading status
        update.message.reply_text(f"⬇️ Downloading: *{yt.title}*...", parse_mode="Markdown")

        # Download to memory (no local save)
        buffer = BytesIO()
        stream.stream_to_buffer(buffer)
        buffer.seek(0)

        # Send video
        update.message.reply_video(
            video=buffer,
            caption=f"🎬 *{yt.title}*\n🔗 {url}",
            parse_mode="Markdown",
        )
    except Exception as e:
        update.message.reply_text(f"❌ Error downloading YouTube video: {e}")

# Handle Incoming Messages
def handle_message(update: Update, context: CallbackContext) -> None:
    text = update.message.text

    if not is_supported(text):
        update.message.reply_text(
            "❌ *Unsupported link!* Send a YouTube/Instagram/TikTok/Twitter URL.",
            parse_mode="Markdown",
        )
        return

    if "youtube.com" in text or "youtu.be" in text:
        download_youtube(text, update)
    else:
        update.message.reply_text(
            "🔄 *Processing...* (Other platforms coming soon!)", parse_mode="Markdown"
        )

# Error Handler
def error_handler(update: Update, context: CallbackContext) -> None:
    logger.error(f"Update {update} caused error: {context.error}")
    if update.message:
        update.message.reply_text("❌ An error occurred. Please try again later.")

# Set up handlers
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_command))
dispatcher.add_handler(CommandHandler("sites", supported_sites))
dispatcher.add_handler(CallbackQueryHandler(button_handler))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
dispatcher.add_error_handler(error_handler)

# ----------- Webhook Handler -----------
@app.route("/webhook", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method in ("GET", "HEAD"):
        return "✅ Webhook running", 200

    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher.process_update(update)
        return "", 200
    
    return abort(403)

# ----------- Set Webhook -----------
@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    try:
        success = bot.set_webhook(url=WEBHOOK_URL)
        if success:
            return f"✅ Webhook successfully set to {WEBHOOK_URL}", 200
        else:
            return "❌ Failed to set webhook", 500
    except Exception as e:
        logging.error(f"Set webhook failed: {e}")
        return f"❌ Error: {e}", 500

# ----------- Delete Webhook -----------
@app.route("/delete_webhook", methods=["GET"])
def delete_webhook():
    try:
        success = bot.delete_webhook()
        if success:
            return "🗑️ Webhook deleted successfully", 200
        else:
            return "❌ Failed to delete webhook", 500
    except Exception as e:
        logging.error(f"Delete webhook failed: {e}")
        return f"❌ Error: {e}", 500

# ----------- Run App -----------
if __name__ == "__main__":
    # Set webhook on startup
    bot.set_webhook(url=WEBHOOK_URL)
    
    # Run Flask app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
