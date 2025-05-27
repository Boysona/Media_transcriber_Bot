import os
import re
import uuid
import shutil
import logging
import requests
import telebot
import json
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token
TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4"
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID
ADMIN_ID = 5978150981

# Bot start time for uptime calculation
BOT_START_TIME = datetime.now()

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking file
users_file = 'users.json'
user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

# User-specific language settings
user_language_settings_file = 'user_language_settings.json'
user_language_settings = {}
if os.path.exists(user_language_settings_file):
    with open(user_language_settings_file, 'r') as f:
        try:
            user_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_language_settings = {}

def save_user_data():
    with open(users_file, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

# In-memory chat history and transcription store
user_memory = {}
user_transcriptions = {}  # {user_id: {message_id: "transcription_text"}}

# Statistics counters
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0.0
processing_start_time = None

GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"

def ask_gemini(user_id, prompt_text):
    user_memory.setdefault(user_id, []).append({"role": "user", "text": prompt_text})
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    resp = requests.post(url, headers={'Content-Type': 'application/json'}, json={"contents":[{"parts":parts}]})
    result = resp.json()
    if "candidates" in result:
        reply = result['candidates'][0]['content']['parts'][0]['text']
        user_memory[user_id].append({"role": "model", "text": reply})
        return reply
    return "Error: " + json.dumps(result)

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {}

def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

def set_bot_info():
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "View instructions"),
        telebot.types.BotCommand("language", "Change preferred language"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    bot.set_my_commands(commands)
    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )
    bot.set_my_description(
        "This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free and in multiple languages.\n\n🔥Enjoy free usage and start now!👌🏻"
    )

# --- Handlers ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()

    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        bot.send_message(message.chat.id, "👑 Admin Panel", reply_markup=keyboard)
    else:
        welcome_text = (
            "👋🏻 *Welcome!*\n\n"
            "🔹 *Bot-ka waxaa loo isticmaalayaa in lagu* transcribe-gareeyo\n"
            "   voice messages, audio files, iyo videos.\n"
            "🔹 Si aad u bilowdo, ku dir *voice message*, *audio file*,\n"
            "   ama *video*.\n\n"
            "Isticmaal /status si aad u aragto xaaladda bot-ka.\n"
            "Enjoy! 🚀"
        )
        bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        "ℹ️ How to use this bot:\n\n"
        "1. Send a voice message, audio file, or video.\n"
        "2. The bot will transcribe and send you the text.\n"
        "3. Use the inline buttons to *Translate* or *Summarize*.\n\n"
        "Commands:\n"
        "• /start — Restart the bot\n"
        "• /status — View bot statistics\n"
        "• /help — Show this help message\n"
        "• /language — Change preferred language\n"
        "• /privacy — View privacy notice"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    privacy_text = (
        "**Privacy Notice**\n\n"
        "• Media files are deleted immediately after transcription.\n"
        "• Transcriptions are held only temporarily in memory.\n"
        "• We store your User ID and language prefs for service improvement.\n"
        "• We do not share your data with third parties."
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    update_user_activity(message.from_user.id)

    # Uptime
    now = datetime.now()
    uptime = now - BOT_START_TIME
    days = uptime.days
    hours, rem = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(rem, 60)

    # Active users today
    today = now.date()
    active_today = sum(
        1 for ts in user_data.values()
        if datetime.fromisoformat(ts).date() == today
    )

    text = (
        f"📊 *Bot Status* 📊\n\n"
        f"▫️ *Uptime:* {days}d {hours}h {minutes}m {seconds}s\n"
        f"▫️ *Active Users Today:* {active_today}\n\n"
        f"⚙️ *Processing Stats*\n"
        f"▫️ Total Files Processed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n\n"
        "Thanks for using our service! 🙌"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users(message):
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast(message):
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document']
)
def broadcast_message(message):
    admin_state[message.from_user.id] = None
    success = fail = 0
    for uid in user_data:
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException:
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file is too large (max 20MB).")

    info = bot.get_file(file_obj.file_id)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
    bot.send_chat_action(message.chat.id, 'typing')
    data = bot.download_file(info.file_path)
    with open(local_path, 'wb') as f:
        f.write(data)

    # Transcribe
    global processing_start_time
    processing_start_time = datetime.now()
    transcription = transcribe(local_path) or ""
    uid = str(message.from_user.id)
    user_transcriptions.setdefault(uid, {})[message.message_id] = transcription

    total_files_processed += 1
    if message.voice:
        total_voice_clips += 1
    elif message.audio:
        total_audio_files += 1
    else:
        total_videos += 1

    processing_time = (datetime.now() - processing_start_time).total_seconds()
    total_processing_time += processing_time

    buttons = InlineKeyboardMarkup()
    buttons.add(
        InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
        InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
    )

    if len(transcription) > 4000:
        fn = 'transcription.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(transcription)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(
                message.chat.id,
                doc,
                reply_to_message_id=message.message_id,
                reply_markup=buttons,
                caption="Here’s your transcription."
            )
        os.remove(fn)
    else:
        bot.reply_to(message, transcription, reply_markup=buttons)

    os.remove(local_path)

# Language selection, translation, summarization handlers
# (generate_language_keyboard, select_language_command,
#  callback_set_language, button_translate_handler, button_summarize_handler,
#  callback_translate_to, callback_summarize_in, do_translate_with_saved_lang,
#  do_summarize_with_saved_lang, /translate, /summarize)

# ... [Include the rest of your language/translate/summarize handlers here] ...

def transcribe(path: str) -> str | None:
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(s.text for s in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET', 'POST'])
def set_webhook():
    url = "https://your-deployment-url.com"
    bot.set_webhook(url=url)
    return f"Webhook set to {url}", 200

@app.route('/delete_webhook', methods=['GET', 'POST'])
def delete_webhook():
    bot.delete_webhook()
    return 'Webhook deleted.', 200

if __name__ == "__main__":
    # Clean download dir on startup
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    set_bot_info()
    bot.delete_webhook()
    bot.set_webhook(url="https://your-deployment-url.com")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
