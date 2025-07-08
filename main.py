import os
import uuid
import logging
import requests
import telebot
import json
from flask import Flask, request, abort
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import asyncio
import threading
import time

# --- KEEP: MongoDB client and collections ---
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- BOT CONFIGURATION (Using Media Transcriber Bot's Token and Webhook) ---
TOKEN = "7790991731:AAFgEjc6fO-iTSSkpt3lEJBH86gQY5nIgAw"  # <-- your bot token
ADMIN_ID = 5978150981  # <-- admin Telegram ID
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com"  # <-- your Render URL (Make sure this is correct and unique for your bot 1)

REQUIRED_CHANNEL = "@transcriber_bot_news_channel"  # <-- required subscription channel

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Download directory (temporary files)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- AssemblyAI Configuration ---
ASSEMBLYAI_API_KEY = "6dab0a0669624f44afa50d679242e473" # Your AssemblyAI API Key
ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"

# --- MONGODB CONFIGURATION ---
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

# Collections
mongo_client: MongoClient = None
db = None
users_collection = None
translation_language_settings_collection = None
summary_language_settings_collection = None
media_language_settings_collection = None
processing_stats_collection = None

# --- In-memory caches (to reduce DB hits) ---
local_user_data = {}            # { user_id: { "last_active": "...", "transcription_count": N, ... } }
_user_translation_language_cache = {}       # { user_id: language_name }
_user_summary_language_cache = {} # { user_id: language_name }
_media_language_cache = {}      # { user_id: media_language }

# --- Statistics counters (in-memory for quick access) ---
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0.0
bot_start_time = datetime.now()

# Admin uptime message storage
admin_uptime_message = {}
admin_uptime_lock = threading.Lock()

# --- User transcription cache (short-lived in-memory for translation/summarization) ---
user_transcriptions = {} # { user_id: { message_id: "transcription text" } }

GEMINI_API_KEY = "AIzaSyCHrGhRKXAp3DuQGH8HLB60ggryZeUFA9E"  # <-- your Gemini API Key
# User memory for Gemini (in-memory, resets on bot restart)
user_memory = {} # { user_id: [{"role": "user", "text": "..."}] }

def ask_gemini(user_id, user_message):
    """
    Send conversation history to Gemini and return the response text.
    """
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    
    # Add system instruction for pause symbols
    system_instruction_part = {"text": "For translations, use only a comma (,) as a pause symbol. Do not use a period (.)."}
    
    full_parts = [system_instruction_part] + parts

    resp = requests.post(
        url,
        headers={'Content-Type': 'application/json'},
        json={"contents": [{"parts": full_parts}]}
    )
    result = resp.json()
    if "candidates" in result:
        reply = result['candidates'][0]['content']['parts'][0]['text']
        user_memory[user_id].append({"role": "model", "text": reply})
        return reply
    return "Error: " + json.dumps(result)

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {}

# Placeholder for keeping track of typing threads
processing_message_ids = {}

# ────────────────────────────────────────────────────────────────────────────────
#   M O N G O   H E L P E R   F U N C T I O N S
# ────────────────────────────────────────────────────────────────────────────────

def connect_to_mongodb():
    """
    Connect to MongoDB at startup, set up collections and indexes.
    Also, load all user data into in-memory caches.
    """
    global mongo_client, db
    global users_collection, translation_language_settings_collection, summary_language_settings_collection, media_language_settings_collection, processing_stats_collection
    global local_user_data, _user_translation_language_cache, _user_summary_language_cache, _media_language_cache

    try:
        mongo_client = MongoClient(MONGO_URI)
        mongo_client.admin.command('ismaster')
        db = mongo_client[DB_NAME]
        users_collection = db["users"]
        translation_language_settings_collection = db["user_translation_language_settings"]
        summary_language_settings_collection = db["user_summary_language_settings"]
        media_language_settings_collection = db["user_media_language_settings"]
        processing_stats_collection = db["file_processing_stats"]

        # Create indexes (if not already created)
        users_collection.create_index([("last_active", ASCENDING)])
        translation_language_settings_collection.create_index([("_id", ASCENDING)])
        summary_language_settings_collection.create_index([("_id", ASCENDING)])
        media_language_settings_collection.create_index([("_id", ASCENDING)])
        processing_stats_collection.create_index([("user_id", ASCENDING)])
        processing_stats_collection.create_index([("type", ASCENDING)])
        processing_stats_collection.create_index([("timestamp", ASCENDING)])

        logging.info("Connected to MongoDB and indexes created. Loading data to memory...")

        # --- Load all user data into in-memory caches on startup ---
        for user_doc in users_collection.find({}):
            local_user_data[user_doc["_id"]] = user_doc
        logging.info(f"Loaded {len(local_user_data)} user documents into local_user_data.")

        for lang_setting in translation_language_settings_collection.find({}):
            _user_translation_language_cache[lang_setting["_id"]] = lang_setting.get("language")
        logging.info(f"Loaded {len(_user_translation_language_cache)} user translation language settings.")

        for lang_setting in summary_language_settings_collection.find({}):
            _user_summary_language_cache[lang_setting["_id"]] = lang_setting.get("language")
        logging.info(f"Loaded {len(_user_summary_language_cache)} user summary language settings.")

        for media_lang_setting in media_language_settings_collection.find({}):
            _media_language_cache[media_lang_setting["_id"]] = media_lang_setting.get("media_language")
        logging.info(f"Loaded {len(_media_language_cache)} media language settings.")

        logging.info("All essential user data loaded into in-memory caches.")

    except ConnectionFailure as e:
        logging.error(f"MongoDB connection failed: {e}")
        exit(1)
    except Exception as e:
        logging.error(f"Error during MongoDB connection or initial data load: {e}")
        exit(1)


def update_user_activity_db(user_id: int):
    """
    Update user.last_active = now() in local_user_data cache and then in MongoDB.
    """
    user_id_str = str(user_id)
    now_iso = datetime.now().isoformat()

    # Update in-memory cache
    if user_id_str not in local_user_data:
        local_user_data[user_id_str] = {
            "_id": user_id_str,
            "last_active": now_iso,
            "transcription_count": 0
        }
    else:
        local_user_data[user_id_str]["last_active"] = now_iso

    # Persist to MongoDB
    try:
        users_collection.update_one(
            {"_id": user_id_str},
            {"$set": {"last_active": now_iso}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error updating user activity for {user_id_str} in DB: {e}")

def get_user_data_db(user_id: str) -> dict | None:
    """
    Return user document from local_user_data cache. If not found, try MongoDB
    and load into cache.
    """
    if user_id in local_user_data:
        return local_user_data[user_id]
    try:
        doc = users_collection.find_one({"_id": user_id})
        if doc:
            local_user_data[user_id] = doc
        return doc
    except Exception as e:
        logging.error(f"Error fetching user data for {user_id} from DB: {e}")
        return None

def increment_transcription_count_db(user_id: str):
    """
    Increment transcription_count in local_user_data cache and then in MongoDB,
    also update last_active.
    """
    now_iso = datetime.now().isoformat()

    # Update in-memory cache
    if user_id not in local_user_data:
        local_user_data[user_id] = {
            "_id": user_id,
            "last_active": now_iso,
            "transcription_count": 1
        }
    else:
        local_user_data[user_id]["transcription_count"] = local_user_data[user_id].get("transcription_count", 0) + 1
        local_user_data[user_id]["last_active"] = now_iso

    # Persist to MongoDB
    try:
        users_collection.update_one(
            {"_id": user_id},
            {
                "$inc": {"transcription_count": 1},
                "$set": {"last_active": now_iso}
            },
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error incrementing transcription count for {user_id} in DB: {e}")

def get_user_translation_language_db(user_id: str) -> str | None:
    """
    Return user's preferred language for translations from cache.
    """
    return _user_translation_language_cache.get(user_id)

def set_user_translation_language_db(user_id: str, lang: str):
    """
    Save preferred language for translations in DB and update cache.
    """
    _user_translation_language_cache[user_id] = lang
    try:
        translation_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting preferred translation language for {user_id} in DB: {e}")

def get_user_summary_language_db(user_id: str) -> str | None:
    """
    Return user's preferred language for summaries from cache.
    """
    return _user_summary_language_cache.get(user_id)

def set_user_summary_language_db(user_id: str, lang: str):
    """
    Save preferred language for summaries in DB and update cache.
    """
    _user_summary_language_cache[user_id] = lang
    try:
        summary_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting preferred summary language for {user_id} in DB: {e}")

def get_user_media_language_setting_db(user_id: str) -> str | None:
    """
    Return language chosen for media transcription from cache.
    """
    return _media_language_cache.get(user_id)

def set_user_media_language_setting_db(user_id: str, lang: str):
    """
    Save media transcription language in DB and update cache.
    """
    _media_language_cache[user_id] = lang
    try:
        media_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"media_language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting media language for {user_id} in DB: {e}")

# ────────────────────────────────────────────────────────────────────────────────
#   U T I L I T I E S   (keep typing, update uptime)
# ────────────────────────────────────────────────────────────────────────────────

def keep_typing(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing')
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error sending typing action: {e}")
            break

def update_uptime_message(chat_id, message_id):
    """
    Live-update the admin uptime message every second.
    """
    while True:
        try:
            elapsed = datetime.now() - bot_start_time
            total_seconds = int(elapsed.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)

            uptime_text = (
                f"**Bot Uptime:**\n"
                f"{days} days, {hours:02d} hours, {minutes:02d} minutes, {seconds:02d} seconds"
            )

            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=uptime_text,
                parse_mode="Markdown"
            )
            time.sleep(1)
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                logging.error(f"Error updating uptime message: {e}")
            break
        except Exception as e:
            logging.error(f"Unexpected error in uptime thread: {e}")
            break

# ────────────────────────────────────────────────────────────────────────────────
#   S U B S C R I P T I O N   C H E C K
# ────────────────────────────────────────────────────────────────────────────────

def check_subscription(user_id: int) -> bool:
    """
    If REQUIRED_CHANNEL is set, verify user is a member.
    """
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error checking subscription for user {user_id}: {e}")
        return False

def send_subscription_message(chat_id: int):
    """
    Prompt user to join REQUIRED_CHANNEL.
    """
    if bot.get_chat(chat_id).type == 'private':
        if not REQUIRED_CHANNEL:
            return
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton(
                "Click here to join the channel",
                url=f"https://t.me/{REQUIRED_CHANNEL[1:]}"
            )
        )
        bot.send_message(
            chat_id,
            """😪Sorry,dear
🔰You need to subscribe to the bot's channel in order to use it.
- @transcriber_bot_news_channel
‼️! | Subscribe and then send /start.""",
            reply_markup=markup,
            disable_web_page_preview=True
        )

# ────────────────────────────────────────────────────────────────────────────────
#   B O T   H A N D L E R S
# ────────────────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id_str = str(message.from_user.id)

    # Ensure user is in local_user_data and DB
    if user_id_str not in local_user_data:
        local_user_data[user_id_str] = {
            "_id": user_id_str,
            "last_active": datetime.now().isoformat(),
            "transcription_count": 0
        }
        try:
            users_collection.insert_one(local_user_data[user_id_str])
            logging.info(f"New user {user_id_str} inserted into MongoDB.")
        except Exception as e:
            logging.error(f"Error inserting new user {user_id_str} into DB: {e}")
    else:
        update_user_activity_db(message.from_user.id)

    # Check subscription
    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        sent_message = bot.send_message(
            message.chat.id,
            "Admin Panel and Uptime (updating live)...",
            reply_markup=keyboard
        )
        with admin_uptime_lock:
            if (
                admin_uptime_message.get(ADMIN_ID)
                and admin_uptime_message[ADMIN_ID].get('thread')
                and admin_uptime_message[ADMIN_ID]['thread'].is_alive()
            ):
                pass

            admin_uptime_message[ADMIN_ID] = {
                'message_id': sent_message.message_id,
                'chat_id': message.chat.id
            }
            uptime_thread = threading.Thread(
                target=update_uptime_message,
                args=(message.chat.id, sent_message.message_id)
            )
            uptime_thread.daemon = True
            uptime_thread.start()
            admin_uptime_message[ADMIN_ID]['thread'] = uptime_thread
    else:
        welcome_message = (
            """Choose your Media (Voice, Audio, Video) file language using the below buttons."""
        )
        markup = generate_language_keyboard("set_media_lang")
        bot.send_message(
            message.chat.id,
            welcome_message,
            reply_markup=markup
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    help_text = (
        """ℹ️ How to Use This Bot
	1.	Transcription (Speech-to-Text)
 • Send a voice message, audio file, video note, or video(e.g. MP4, MP3).
 • The bot will process it and return the transcribed text.
	2.	Translation
 • After you receive the transcription, tap “Translate” or use /translate.
 • You will be prompted to choose a translation language if you haven't set one yet.
	3.	Summarization
 • After you receive the transcription, tap “Summarize” or use /summarize.
 • You will be prompted to choose a summarization language if you haven't set one yet.
	4.	Language Settings
 • /translate_language — set your preferred language for translations.
 • /summary_language — set your preferred language for summaries.
 • /media_language — set the language of any audio/video you send for transcription.
	5.	Privacy & Limits
 • Media files up to 20 MB.
 • Transcriptions are stored temporarily (10 minutes) for follow-up translate/summarize actions, then deleted.
 • All other settings (language) are saved for your next session.

⸻

If you need help or encounter any issues, contact @user33230. Enjoy!
"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    privacy_text = (
        """**Privacy Notice**

Your privacy is paramount. Here's a transparent look at how this bot handles your data in real-time:

1.  **Data We Process & Its Lifecycle:**
    * **Media Files (Voice, Audio, Video):** When you send a media file (voice, audio, video note, or a video file as a document), it's temporarily downloaded for **immediate transcription**. Crucially, these files are **deleted instantly** from our servers once the transcription is complete. We do not store your media content.
    * **Transcriptions:** The text generated from your media is held **temporarily in-memory** (for 10 minutes only). After 10 minutes, the transcription is automatically deleted and cannot be retrieved. This data is used only for immediate translation or summarization requests.
    * **User IDs, Language Preferences, and Activity Data:** Your Telegram User ID and your chosen preferences (language for translations/summaries, media transcription language) are stored in MongoDB. Basic activity (like last active timestamp and transcription count) are also stored. This helps us remember your preferences and track basic, aggregated activity (like when you last used the bot) to improve service and understand overall usage patterns. This data is also kept in-memory for quick access during bot operation and is persisted to MongoDB.

2.  **How Your Data is Used:**
    * To deliver the bot's core services: transcribing, translating, summarizing your media.
    * To enhance bot performance and gain insights into general usage trends through anonymous, collective statistics (e.g., total files processed).
    * To maintain your personalized language settings across sessions.

3.  **Data Sharing Policy:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are facilitated by integrating with advanced AI models (specifically, AssemblyAI for transcription and the Gemini API for translation/summarization). Your input sent to these models is governed by their respective privacy policies, but we ensure that your data is **not stored by us** after processing by these services.

4.  **Data Retention:**
    * **Media files:** Deleted immediately post-processing.
    * **Transcriptions:** Held in-memory for 10 minutes, then permanently deleted.
    * **User IDs and language preferences:** Stored in MongoDB to support your settings and for anonymous usage statistics. This data is also cached in memory for performance. If you wish to have your stored preferences removed, you can cease using the bot or contact the bot administrator for explicit data deletion.

By using this bot, you acknowledge and agree to the data practices outlined in this Privacy Notice.

Should you have any questions or concerns regarding your privacy, please feel free to contact the bot administrator.
"""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Count active today
    today_iso = datetime.now().date().isoformat()
    active_today_count = sum(1 for user_doc in local_user_data.values() if user_doc.get("last_active", "").startswith(today_iso))

    # Total registered users from local_user_data
    total_registered_users = len(local_user_data)

    # Processing stats (voice/audio/video counts + total processing time)
    try:
        total_processed_media = processing_stats_collection.count_documents({"type": {"$in": ["voice", "audio", "video"]}})
        voice_count = processing_stats_collection.count_documents({"type": "voice"})
        audio_count = processing_stats_collection.count_documents({"type": "audio"})
        video_count = processing_stats_collection.count_documents({"type": "video"})

        pipeline = [
            {"$group": {"_id": None, "total_time": {"$sum": "$processing_time"}}}
        ]
        agg_result = list(processing_stats_collection.aggregate(pipeline))
        total_proc_seconds = agg_result[0]["total_time"] if agg_result else 0
    except Exception as e:
        logging.error(f"Error fetching processing stats from DB: {e}")
        total_processed_media = voice_count = audio_count = video_count = 0
        total_proc_seconds = 0

    proc_hours = int(total_proc_seconds) // 3600
    proc_minutes = (int(total_proc_seconds) % 3600) // 60
    proc_seconds = int(total_proc_seconds) % 60

    text = (
        "📊 Bot Statistics\n\n"
        "🟢 **Bot Status: Online**\n"
        f"⏱️ The bot has been running for: {days} days, {hours:02d} hours, {minutes:02d} minutes, {seconds:02d} seconds\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Users Today : {active_today_count}\n"
        f"▫️ Total Registered Users : {total_registered_users}\n\n"
        "⚙️ Processing Statistics \n"
        f"▫️ Total Media Files Processed: {total_processed_media}\n"
        f"▫️ Voice Clips: {voice_count}\n"
        f"▫️ Audio Files: {audio_count}\n"
        f"▫️ Videos: {video_count}\n"
        f"⏱️ Total Processing Time: {proc_hours} hours {proc_minutes} minutes {proc_seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌"
    )

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users(message):
    total_registered = len(local_user_data)
    bot.send_message(message.chat.id, f"Total registered users (from memory): {total_registered}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast_prompt(message):
    admin_state[message.from_user.id] = 'awaiting_broadcast_message'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast_message',
    content_types=['text', 'photo', 'video', 'audio', 'document']
)
def broadcast_message(message):
    admin_state[message.from_user.id] = None
    success = fail = 0
    for uid in local_user_data.keys():
        if uid == str(ADMIN_ID):
            continue
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to {uid}: {e}")
            fail += 1
        time.sleep(0.05)

    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

# ────────────────────────────────────────────────────────────────────────────────
#   M E D I A   H A N D L I N G  (voice, audio, video, video_note, document)
# ────────────────────────────────────────────────────────────────────────────────

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note', 'document'])
def handle_file(message):
    uid_str = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    file_obj = None
    is_document_video = False
    type_str = ""
    if message.voice:
        file_obj = message.voice
        type_str = "voice"
    elif message.audio:
        file_obj = message.audio
        type_str = "audio"
    elif message.video:
        file_obj = message.video
        type_str = "video"
    elif message.video_note:
        file_obj = message.video_note
        type_str = "video"
    elif message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("video/"):
            file_obj = message.document
            is_document_video = True
            type_str = "video"
        elif mime.startswith("audio/"):
            file_obj = message.document
            is_document_video = True
            type_str = "audio"
        else:
            bot.send_message(
                message.chat.id,
                "❌ The file you sent is not a supported audio/video format. "
                "Please send a voice message, audio file, video note, or video file (e.g. .mp4)."
            )
            return
    else:
        bot.send_message(
            message.chat.id,
            "❌ Please send only voice messages, audio files, video notes, or video files."
        )
        return

    size = file_obj.file_size
    if size and size > FILE_SIZE_LIMIT:
        bot.send_message(message.chat.id, "😓 Sorry, the file size you uploaded is too large (max allowed is 20MB).")
        return

    processing_reply = bot.reply_to(message, "Processing...")

    stop_typing = threading.Event()
    typing_thread = threading.Thread(target=keep_typing, args=(message.chat.id, stop_typing))
    typing_thread.daemon = True
    typing_thread.start()
    processing_message_ids[message.chat.id] = stop_typing

    try:
        threading.Thread(
            target=process_media_file,
            args=(message, stop_typing, file_obj, type_str, processing_reply.message_id)
        ).start()
    except Exception as e:
        logging.error(f"Error initiating file processing: {e}")
        stop_typing.set()
        try:
            bot.delete_message(message.chat.id, processing_reply.message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message: {delete_e}")
        bot.send_message(message.chat.id, "😓 Sorry, an unexpected error occurred. Please try again.")


def process_media_file(message, stop_typing, file_obj, type_str, processing_message_id):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time

    uid_str = str(message.from_user.id)
    processing_start_time = datetime.now()
    transcription = None

    try:
        file_info = bot.get_file(file_obj.file_id)
        telegram_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

        response = requests.get(telegram_file_url)
        response.raise_for_status()
        file_content = response.content

        headers = {"authorization": ASSEMBLYAI_API_KEY}
        upload_response = requests.post(
            ASSEMBLYAI_UPLOAD_URL,
            headers=headers,
            data=file_content
        )
        upload_response.raise_for_status()
        audio_url = upload_response.json().get('upload_url')

        if not audio_url:
            raise Exception("Failed to get audio_url from AssemblyAI upload.")

        media_lang_name = get_user_media_language_setting_db(uid_str)
        if not media_lang_name:
             bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=processing_message_id,
                text="⚠️ Transcription failed. Please set your media language using /media_language before sending files."
            )
             return

        media_lang_code = get_lang_code(media_lang_name)

        if not media_lang_code:
            raise ValueError(f"The language '{media_lang_name}' does not have a valid code for transcription.")

        transcript_request_json = {
            "audio_url": audio_url,
            "language_code": media_lang_code,
            "speech_model": "best"
        }
        transcript_response = requests.post(
            ASSEMBLYAI_TRANSCRIPT_URL,
            headers={"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"},
            json=transcript_request_json
        )
        transcript_response.raise_for_status()
        transcript_result = transcript_response.json()
        transcript_id = transcript_result.get("id")

        if not transcript_id:
            raise Exception(f"Failed to get transcript ID from AssemblyAI: {transcript_result.get('error', 'Unknown error')}")

        polling_url = f"{ASSEMBLYAI_TRANSCRIPT_URL}/{transcript_id}"
        while True:
            polling_response = requests.get(polling_url, headers=headers)
            polling_response.raise_for_status()
            polling_result = polling_response.json()

            if polling_result['status'] in ['completed', 'error']:
                break
            time.sleep(2)

        if polling_result['status'] == 'completed':
            transcription = polling_result.get("text", "")
        else:
            raise Exception(f"AssemblyAI transcription failed: {polling_result.get('error', 'Unknown error')}")

        user_transcriptions.setdefault(uid_str, {})[message.message_id] = transcription

        def delete_transcription_later(u_id, msg_id):
            time.sleep(600)
            if u_id in user_transcriptions and msg_id in user_transcriptions[u_id]:
                del user_transcriptions[u_id][msg_id]
                logging.info(f"Deleted transcription for user {u_id}, message {msg_id} after 10 minutes")

        threading.Thread(
            target=delete_transcription_later,
            args=(uid_str, message.message_id),
            daemon=True
        ).start()

        total_files_processed += 1
        if type_str == "voice":
            total_voice_clips += 1
        elif type_str == "audio":
            total_audio_files += 1
        else:
            total_videos += 1

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        increment_transcription_count_db(uid_str)

        try:
            processing_stats_collection.insert_one({
                "user_id": uid_str,
                "message_id": message.message_id,
                "type": type_str,
                "processing_time": processing_time,
                "timestamp": datetime.now().isoformat(),
                "status": "success"
            })
        except Exception as e:
            logging.error(f"Error inserting processing stat (success): {e}")

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
        )

        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message: {delete_e}")

        if len(transcription) > 4000:
            import io
            transcript_file_buffer = io.BytesIO(transcription.encode('utf-8'))
            transcript_file_buffer.name = f"{uuid.uuid4()}_transcription.txt"

            bot.send_chat_action(message.chat.id, 'upload_document')
            bot.send_document(
                message.chat.id,
                transcript_file_buffer,
                reply_to_message_id=message.message_id,
                reply_markup=buttons,
                caption="Here’s your transcription. Tap a button below for more options."
            )
            transcript_file_buffer.close()
        else:
            bot.reply_to(
                message,
                transcription,
                reply_markup=buttons
            )

    except requests.exceptions.RequestException as req_e:
        logging.error(f"Network or API request error for user {uid_str}: {req_e}", exc_info=True)
        error_message = f"😓 Network or API error during transcription: {req_e}. Please try again later."
        if "400 Client Error: Bad Request for url" in str(req_e) and "language_code" in str(req_e):
             error_message = (
                f"❌ The language you selected for transcription *({media_lang_name})* is not supported by AssemblyAI for this specific request. "
                "Please choose a different language using /media_language or try again with a different file."
             )
        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message on error: {delete_e}")
        bot.send_message(message.chat.id, error_message, parse_mode="Markdown")
        proc_time = (datetime.now() - processing_start_time).total_seconds()
        try:
            processing_stats_collection.insert_one({
                "user_id": uid_str,
                "message_id": message.message_id,
                "type": type_str,
                "processing_time": proc_time,
                "timestamp": datetime.now().isoformat(),
                "status": "fail_api_error"
            })
        except Exception as e2:
            logging.error(f"Error inserting processing stat (request exception): {e2}")

    except Exception as e:
        logging.error(f"Error processing file for user {uid_str}: {e}", exc_info=True)
        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message on error: {delete_e}")
        bot.send_message(
            message.chat.id,
            "😓𝗪𝗲’𝗿𝗲 𝘀𝗼𝗿𝗿𝘆, 𝗮𝗻 𝗲𝗿𝗿𝗼𝗿 𝗼𝗰𝗰𝘂𝗿𝗿𝗲𝗱 𝗱𝘂𝗿𝗶𝗻𝗴 𝘁𝗿𝗮𝗻𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻.\n"
            "The audio might be noisy or spoken too quickly.\n"
            "Please try again or upload a different file.\n"
            "Make sure the file you’re sending and the selected language match — otherwise, an error may occur."
        )
        proc_time = (datetime.now() - processing_start_time).total_seconds()
        try:
            processing_stats_collection.insert_one({
                "user_id": uid_str,
                "type": type_str,
                "processing_time": proc_time,
                "timestamp": datetime.now().isoformat(),
                "status": "fail"
            })
        except Exception as e2:
            logging.error(f"Error inserting processing stat (exception): {e2}")

    finally:
        stop_typing.set()
        if message.chat.id in processing_message_ids:
            del processing_message_ids[message.chat.id]

# --- Language list and helper functions ---

LANGUAGES = {
   "Auto ⚙️": "auto",
  "English 🇬🇧": "en",
  "中文 🇨🇳": "zh",
  "Español 🇪🇸": "es",
‎  "العربية 🇸🇦": "ar",
  "हिंदी 🇮🇳": "hi",
  "Português 🇵🇹": "pt",
  "Bengali 🇧🇩": "bn",       
  "Русский 🇷🇺": "ru",
  "Français 🇫🇷": "fr",
  "Deutsch 🇩🇪": "de",
‎  "اردو 🇵🇰": "ur",
  "日本語 🇯🇵": "ja",
  "한국어 🇰🇷": "ko",
  "Türkçe 🇹🇷": "tr",
  "Italiano 🇮🇹": "it",
‎  "فارسی 🇮🇷": "fa",
  "ภาษาไทย 🇹🇭": "th",
  "Tiếng Việt 🇻🇳": "vi",
  "Soomaali 🇸🇴": "so",
  "Indonesia 🇮🇩": "id",
  "Melayu 🇲🇾": "ms",
  "Polski 🇵🇱": "pl",
  "Українська 🇺🇦": "uk",     
  "Čeština 🇨🇿": "cs",
  "Română 🇷🇴": "ro",
  "Magyar 🇭🇺": "hu",
  "Nederlands 🇳🇱": "nl",
  "Svenska 🇸🇪": "sv",
  "Norsk 🇳🇴": "no",
  "Dansk 🇩🇰": "da",
  "Suomi 🇫🇮": "fi",
  "Ελληνικά 🇬🇷": "el",
  "Tagalog 🇵🇭": "tl",
  "O’zbekcha 🇺🇿": "uz",
  "Қазақша 🇰🇿": "kk",
  "Azərbaycan 🇦🇿": "az",
  "Български 🇧🇬": "bg",
  "Srpski 🇷🇸": "sr"
}

def get_lang_code(lang_name: str) -> str | None:
    for key, code in LANGUAGES.items():
        if lang_name.lower() in key.lower():
            return code
    return None


def generate_language_keyboard(callback_prefix: str, message_id: int | None = None):
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []

    if callback_prefix == "summarize_in":
        cb_data = f"{callback_prefix}|Auto ⚙️"
        if message_id is not None:
            cb_data += f"|{message_id}"
        buttons.append(InlineKeyboardButton("Auto ⚙️", callback_data=cb_data))
    else:
        for lang_display_name in LANGUAGES.keys():
            if lang_display_name == "Auto ⚙️" and callback_prefix != "summarize_in":
                continue
            
            cb_data = f"{callback_prefix}|{lang_display_name}"
            if message_id is not None:
                cb_data += f"|{message_id}"
            buttons.append(InlineKeyboardButton(lang_display_name, callback_data=cb_data))
    
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])
        
    return markup

@bot.message_handler(commands=['translate_language'])
def select_translation_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    markup = generate_language_keyboard("set_translation_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future **translations**:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_translation_lang|"))
def callback_set_translation_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    _, lang_display_name = call.data.split("|", 1)
    set_user_translation_language_db(uid, lang_display_name)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Your preferred language for **translations** has been set to: **{lang_display_name}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Translation language set to {lang_display_name}")

@bot.message_handler(commands=['summary_language'])
def select_summary_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    markup = generate_language_keyboard("set_summary_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future **summaries**:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_summary_lang|"))
def callback_set_summary_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    _, lang_display_name = call.data.split("|", 1)
    set_user_summary_language_db(uid, lang_display_name)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Your preferred language for **summaries** has been set to: **{lang_display_name}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Summary language set to {lang_display_name}")


@bot.message_handler(commands=['media_language'])
def select_media_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    markup = generate_language_keyboard("set_media_lang")
    bot.send_message(
        message.chat.id,
        "Please choose the language of the audio files that you need me to transcribe. This helps ensure accurate reading.",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_media_lang|"))
def callback_set_media_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    _, lang_display_name = call.data.split("|", 1)
    set_user_media_language_setting_db(uid, lang_display_name)

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ The transcription language for your media is set to: **{lang_display_name}**\n\n"
             "Now, please send your voice message, audio file, video note, or video file for me to transcribe. I support media files up to 20MB in size 📞 Need help? Contact @user33230",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Media language set to {lang_display_name}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = get_user_translation_language_db(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Translating with your preferred language...")
        threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, preferred_lang, message_id)).start()
    else:
        markup = generate_language_keyboard("translate_to", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want to translate into:",
            reply_markup=markup
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = get_user_summary_language_db(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Summarizing with your preferred language...")
        threading.Thread(target=do_summarize_with_saved_lang, args=(call.message, uid, preferred_lang, message_id)).start()
    else:
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want the summary in (type /summary_language for more options, or press auto):",
            reply_markup=markup
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    parts = call.data.split("|")
    lang_display_name = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    set_user_translation_language_db(uid, lang_display_name)

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Translating to **{lang_display_name}**...",
        parse_mode="Markdown"
    )

    if message_id:
        threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, lang_display_name, message_id)).start()
    else:
        if uid in user_transcriptions and call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions[uid]:
            threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, lang_display_name, call.message.reply_to_message.message_id)).start()
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to translate. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    parts = call.data.split("|")
    lang_display_name = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    if lang_display_name == "Auto ⚙️":
        transcription_lang = get_user_media_language_setting_db(uid)
        if transcription_lang:
            target_lang_for_gemini = transcription_lang
            set_user_summary_language_db(uid, lang_display_name)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"Summarizing in the original transcription language (**{transcription_lang}**)...",
                parse_mode="Markdown"
            )
        else:
            bot.answer_callback_query(call.id, "Cannot determine original transcription language. Please select a specific language using /summary_language.")
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="Cannot determine original transcription language. Please use /summary_language to select a language, then try again."
            )
            return
    else:
        set_user_summary_language_db(uid, lang_display_name)
        target_lang_for_gemini = lang_display_name

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Summarizing in **{lang_display_name}**...",
            parse_mode="Markdown"
        )

    if message_id:
        threading.Thread(target=do_summarize_with_saved_lang, args=(call.message, uid, target_lang_for_gemini, message_id)).start()
    else:
        if uid in user_transcriptions and call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions[uid]:
            threading.Thread(target=do_summarize_with_saved_lang, args=(call.message, uid, target_lang_for_gemini, call.message.reply_to_message.message_id)).start()
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)

def do_translate_with_saved_lang(message, uid, lang_display_name, message_id):
    """
    Use Gemini to translate saved transcription into lang.
    """
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to translate.")
        return

    lang_name_only = lang_display_name.split(' ')[0]

    prompt = (
        f"Translate the following text into {lang_name_only}. Provide only the translated text, "
        f"with no additional notes, explanations, or introductory/concluding remarks:\n\n{original}"
    )
    bot.send_chat_action(message.chat.id, 'typing')
    translated = ask_gemini(uid, prompt)

    if translated.startswith("Error:"):
        bot.send_message(message.chat.id, f"😓 Sorry, an error occurred during translation: {translated}. Please try again later.")
        return

    if len(translated) > 4000:
        import io
        translation_file_buffer = io.BytesIO(translated.encode('utf-8'))
        translation_file_buffer.name = f"{uuid.uuid4()}_translation.txt"

        bot.send_chat_action(message.chat.id, 'upload_document')
        bot.send_document(message.chat.id, translation_file_buffer, caption=f"Translation to {lang_display_name}", reply_to_message_id=message_id)
        translation_file_buffer.close()
    else:
        bot.send_message(message.chat.id, translated, reply_to_message_id=message_id)

def do_summarize_with_saved_lang(message, uid, lang_display_name, message_id):
    """
    Use Gemini to summarize saved transcription into lang.
    """
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to summarize.")
        return

    if lang_display_name == "Auto ⚙️":
        transcription_lang = get_user_media_language_setting_db(uid)
        if not transcription_lang:
            target_lang_for_gemini = "English"
            bot.send_message(message.chat.id, "⚠️ Could not determine original transcription language for 'Auto' summary. Summarizing in English.")
        lang_name_for_prompt = transcription_lang.split(' ')[0]
    else:
        lang_name_for_prompt = lang_display_name.split(' ')[0]

    prompt = (
        f"Summarize the following text in {lang_name_for_prompt}. Provide only the summarized text, "
        f"with no additional notes, explanations, or different versions:\n\n{original}"
    )
    bot.send_chat_action(message.chat.id, 'typing')
    summary = ask_gemini(uid, prompt)

    if summary.startswith("Error:"):
        bot.send_message(chat_id=message.chat.id, text=f"😓 Sorry, an error occurred during summarization: {summary}. Please try again later.")
        return

    if len(summary) > 4000:
        import io
        summary_file_buffer = io.BytesIO(summary.encode('utf-8'))
        summary_file_buffer.name = f"{uuid.uuid4()}_summary.txt"

        bot.send_chat_action(message.chat.id, 'upload_document')
        bot.send_document(message.chat.id, summary_file_buffer, caption=f"Summary in {lang_display_name}", reply_to_message_id=message_id)
        summary_file_buffer.close()
    else:
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)

@bot.message_handler(commands=['translate'])
def handle_translate(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    transcription_message_id = None
    if message.reply_to_message and uid in user_transcriptions and message.reply_to_message.message_id in user_transcriptions[uid]:
        transcription_message_id = message.reply_to_message.message_id

    if transcription_message_id:
        preferred_lang = get_user_translation_language_db(uid)
        if preferred_lang:
            bot.send_message(message.chat.id, f"Translating to {preferred_lang}...")
            threading.Thread(target=do_translate_with_saved_lang, args=(message, uid, preferred_lang, transcription_message_id)).start()
        else:
            markup = generate_language_keyboard("translate_to", transcription_message_id)
            bot.send_message(
                message.chat.id,
                "Please select the language you want to translate into:",
                reply_markup=markup
            )
    else:
        markup = generate_language_keyboard("translate_to")
        bot.send_message(
            message.chat.id,
            "Please select the language you want to translate into, then reply to a transcription message with /translate to use it:",
            reply_markup=markup
        )

@bot.message_handler(commands=['summarize'])
def handle_summarize(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    transcription_message_id = None
    if message.reply_to_message and uid in user_transcriptions and message.reply_to_message.message_id in user_transcriptions[uid]:
        transcription_message_id = message.reply_to_message.message_id

    if transcription_message_id:
        preferred_lang = get_user_summary_language_db(uid)
        if preferred_lang:
            bot.send_message(message.chat.id, f"Summarizing in {preferred_lang}...")
            threading.Thread(target=do_summarize_with_saved_lang, args=(message, uid, preferred_lang, transcription_message_id)).start()
        else:
            markup = generate_language_keyboard("summarize_in", transcription_message_id)
            bot.send_message(
                message.chat.id,
                "Please select the language you want the summary in:",
                reply_markup=markup
            )
    else:
        markup = generate_language_keyboard("summarize_in", message.message_id)
        bot.send_message(
            message.chat.id,
            "Please select 'Auto' for summary, or use /summary_language to set your preferred summary language for future use:",
            reply_markup=markup
        )

@bot.message_handler(func=lambda message: message.content_type == 'text' and not message.text.startswith('/'))
def handle_text_fallback(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    bot.send_message(
        message.chat.id,
        "I only transcribe voice messages, audio files, video notes, or video files. "
        "Please send a supported media type for transcription."
    )

@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'document'])
def fallback_non_text_or_media(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    if message.document and not (message.document.mime_type.startswith("audio/") or message.document.mime_type.startswith("video/")):
        bot.send_message(
            message.chat.id,
            "Please send only voice messages, audio files, video notes, or video files for transcription. "
            "The document type you sent is not supported for transcription."
        )
        return

    bot.send_message(
        message.chat.id,
        "I only transcribe voice messages, audio files, video notes, or video files for transcription. "
        "Please send a supported type."
    )


# ────────────────────────────────────────────────────────────────────────────────
#   F L A S K   R O U T E S   (Webhook setup)
# ────────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method in ("GET", "HEAD"):
        return "OK", 200
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if content_type and content_type.startswith("application/json"):
            update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
            bot.process_new_updates([update])
            return "", 200
    return abort(403)

@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook_route():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        return f"Failed to set webhook: {e}", 500

@app.route("/delete_webhook", methods=["GET", "POST"])
def delete_webhook_route():
    try:
        bot.delete_webhook()
        return "Webhook deleted.", 200
    except Exception as e:
        logging.error(f"Failed to delete webhook: {e}")
        return f"Failed to delete webhook: {e}", 500

def set_bot_commands():
    """
    Sets the list of commands for the bot using set_my_commands.
    """
    commands = [
        BotCommand("start", "Start the Bot"),
        BotCommand("status", "View statistics"),
        BotCommand("translate_language", "change translation language for translate button"),
        BotCommand("summary_language", "change summary language for summarie bottom "),
        BotCommand("media_language", "change the language of the audio in your media files for transcription."),
        BotCommand("help", "How to use the bot"),
        BotCommand("privacy", "Read privacy notice"),
        #BotCommand("translate", "Translate a replied-to transcription"),
        #BotCommand("summarize", "Summarize a replied-to transcription")
    ]
    try:
        bot.set_my_commands(commands)
        logging.info("Bot commands set successfully.")
    except Exception as e:
        logging.error(f"Failed to set bot commands: {e}")


def set_webhook_on_startup():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set successfully to {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Failed to set webhook on startup: {e}")

def set_bot_info_and_startup():
    connect_to_mongodb()
    set_webhook_on_startup()
    set_bot_commands()

if __name__ == "__main__":
    set_bot_info_and_startup()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
