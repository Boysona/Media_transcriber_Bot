import os
import uuid
import logging
import requests
import telebot
import asyncio
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, abort
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand

# --- KEEP: MSSpeech for Text-to-Speech ---
from msspeech import MSSpeech, MSSpeechError

# --- NEW: MongoDB client and collections ---
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- BOT CONFIGURATION ---
TOKEN = "8127715382:AAEM7g2Bf2bgElaVEOC7jPZ9NSiYOTUl6IU"  # <-- your bot token
ADMIN_ID = 5978150981  # <-- admin Telegram ID
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com"  # <-- your Render URL

REQUIRED_CHANNEL = "@transcriber_bot_news_channel"  # <-- required subscription channel

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Download directory (temporary files for TTS)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- MONGODB CONFIGURATION ---
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

# Collections
mongo_client: MongoClient = None
db = None
users_collection = None
tts_users_collection = None
processing_stats_collection = None

# --- In-memory caches (to reduce DB hits) ---
# Global dictionaries to hold user data in RAM
local_user_data = {}            # { user_id: { "last_active": "...", "tts_count": N, ... } }
_tts_voice_cache = {}           # { user_id: voice_name }
_tts_pitch_cache = {}           # { user_id: pitch_value }
_tts_rate_cache = {}            # { user_id: rate_value }

# --- User state for Text-to-Speech input mode ---
# { user_id: voice_name (e.g. "en-US-AriaNeural") or None }
user_tts_mode = {}
user_pitch_input_mode = {}      # { user_id: "awaiting_pitch_input" or None }
user_rate_input_mode = {}       # { user_id: "awaiting_rate_input" or None }

# --- Statistics counters (in-memory for quick access) ---
total_tts_conversions = 0
bot_start_time = datetime.now()

# Admin uptime message storage
admin_uptime_message = {}
admin_uptime_lock = threading.Lock()

# Placeholder for keeping track of recording threads
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
    global users_collection, tts_users_collection, processing_stats_collection
    global local_user_data, _tts_voice_cache, _tts_pitch_cache, _tts_rate_cache

    try:
        mongo_client = MongoClient(MONGO_URI)
        mongo_client.admin.command('ismaster')
        db = mongo_client[DB_NAME]
        users_collection = db["users"]
        tts_users_collection = db["tts_users"]
        processing_stats_collection = db["file_processing_stats"] # Re-purposing for TTS stats

        # Create indexes (if not already created)
        users_collection.create_index([("last_active", ASCENDING)])
        tts_users_collection.create_index([("_id", ASCENDING)])
        processing_stats_collection.create_index([("user_id", ASCENDING)])
        processing_stats_collection.create_index([("type", ASCENDING)])
        processing_stats_collection.create_index([("timestamp", ASCENDING)])

        logging.info("Connected to MongoDB and indexes created. Loading data to memory...")

        # --- Load all user data into in-memory caches on startup ---
        for user_doc in users_collection.find({}):
            local_user_data[user_doc["_id"]] = user_doc
        logging.info(f"Loaded {len(local_user_data)} user documents into local_user_data.")

        for tts_user in tts_users_collection.find({}):
            _tts_voice_cache[tts_user["_id"]] = tts_user.get("voice", "en-US-AriaNeural")
            _tts_pitch_cache[tts_user["_id"]] = tts_user.get("pitch", 0)
            _tts_rate_cache[tts_user["_id"]] = tts_user.get("rate", 0)
        logging.info(f"Loaded {len(_tts_voice_cache)} TTS voice, pitch, and rate settings.")

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
            "tts_count": 0 # Initialize for new users
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
            local_user_data[user_id] = doc # Load into cache
        return doc
    except Exception as e:
        logging.error(f"Error fetching user data for {user_id} from DB: {e}")
        return None

def increment_tts_count_db(user_id: str):
    """
    Increment tts_count in local_user_data cache and then in MongoDB,
    also update last_active.
    """
    now_iso = datetime.now().isoformat()

    # Update in-memory cache
    if user_id not in local_user_data:
        local_user_data[user_id] = {
            "_id": user_id,
            "last_active": now_iso,
            "tts_count": 1
        }
    else:
        local_user_data[user_id]["tts_count"] = local_user_data[user_id].get("tts_count", 0) + 1
        local_user_data[user_id]["last_active"] = now_iso

    # Persist to MongoDB
    try:
        users_collection.update_one(
            {"_id": user_id},
            {
                "$inc": {"tts_count": 1},
                "$set": {"last_active": now_iso}
            },
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error incrementing TTS count for {user_id} in DB: {e}")


def get_tts_user_voice_db(user_id: str) -> str:
    """
    Return TTS voice from cache (default "en-US-AriaNeural").
    """
    return _tts_voice_cache.get(user_id, "en-US-AriaNeural")

def set_tts_user_voice_db(user_id: str, voice: str):
    """
    Save TTS voice in DB and update cache.
    """
    _tts_voice_cache[user_id] = voice # Update in-memory cache
    try:
        tts_users_collection.update_one(
            {"_id": user_id},
            {"$set": {"voice": voice}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS voice for {user_id} in DB: {e}")

def get_tts_user_pitch_db(user_id: str) -> int:
    """
    Return TTS pitch from cache (default 0).
    """
    return _tts_pitch_cache.get(user_id, 0)

def set_tts_user_pitch_db(user_id: str, pitch: int):
    """
    Save TTS pitch in DB and update cache.
    """
    _tts_pitch_cache[user_id] = pitch # Update in-memory cache
    try:
        tts_users_collection.update_one(
            {"_id": user_id},
            {"$set": {"pitch": pitch}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS pitch for {user_id} in DB: {e}")

def get_tts_user_rate_db(user_id: str) -> int:
    """
    Return TTS rate from cache (default 0).
    """
    return _tts_rate_cache.get(user_id, 0)

def set_tts_user_rate_db(user_id: str, rate: int):
    """
    Save TTS rate in DB and update cache.
    """
    _tts_rate_cache[user_id] = rate # Update in-memory cache
    try:
        tts_users_collection.update_one(
            {"_id": user_id},
            {"$set": {"rate": rate}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS rate for {user_id} in DB: {e}")

# ────────────────────────────────────────────────────────────────────────────────
#   U T I L I T I E S   (keep recording, update uptime)
# ────────────────────────────────────────────────────────────────────────────────

def keep_recording(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'record_audio')
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error sending record_audio action: {e}")
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
            "tts_count": 0
        }
        # Immediately save new user to DB
        try:
            users_collection.insert_one(local_user_data[user_id_str])
            logging.info(f"New user {user_id_str} inserted into MongoDB.")
        except Exception as e:
            logging.error(f"Error inserting new user {user_id_str} into DB: {e}")
    else:
        # Just update activity if already exists
        update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF on /start
    user_tts_mode[user_id_str] = None
    user_pitch_input_mode[user_id_str] = None
    user_rate_input_mode[user_id_str] = None

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
            """Welcome! I'm your dedicated Text-to-Speech bot.
You can convert any text into spoken audio in various languages and voices.

To get started, use the /text_to_speech command to select your preferred language and voice.
Then, simply send me the text you want me to speak!

You can also adjust the voice characteristics:
• /voice_pitch to change pitch (higher or lower)
• /voice_rate to change speed (faster or slower)

For more help, use /help.
"""
        )
        bot.send_message(message.chat.id, welcome_message)

@bot.message_handler(commands=['help'])
def help_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    help_text = (
        """ℹ️ How to Use This Bot
	1.	Text-to-Speech
 • Use /text_to_speech to pick a language and voice first.
 • Then send any text, and the bot will reply with an audio clip.
 • Adjust voice characteristics with:
  • /voice_pitch to change pitch (higher or lower)
  • /voice_rate to change speed (faster or slower)
	2.	Settings
 • Your last selected voice, pitch, and rate settings are saved for your next session.

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

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    privacy_text = (
        """**Privacy Notice**

Your privacy is paramount. Here's a transparent look at how this bot handles your data in real-time:

1.  **Data We Process & Its Lifecycle:**
    * **Text for Speech Synthesis:** When you send text for conversion to speech, it is processed to generate the audio and then **not stored**. The generated audio file is also temporary and deleted after sending.
    * **User IDs, TTS Voices, and Activity Data:** Your Telegram User ID and your chosen preferences (TTS voice, **pitch, and rate**) are stored in MongoDB. Basic activity (like last active timestamp and TTS conversion count) are also stored. This helps us remember your preferences and track basic, aggregated activity (like when you last used the bot) to improve service and understand overall usage patterns. This data is also kept in-memory for quick access during bot operation and is persisted to MongoDB.

2.  **How Your Data is Used:**
    * To deliver the bot's core service: converting text to speech.
    * To enhance bot performance and gain insights into general usage trends through anonymous, collective statistics (e.g., total TTS conversions).
    * To maintain your personalized voice settings and preferences across sessions.

3.  **Data Sharing Policy:**
    * We **do not share** your personal data or text input with any third parties.
    * Text-to-speech is facilitated by integrating with the Microsoft Cognitive Services Speech API. Your input sent to this model is governed by their privacy policies, but we ensure that your data is **not stored by us** after processing by this service.

4.  **Data Retention:**
    * **Generated audio files:** Deleted immediately post-processing.
    * **User IDs and voice preferences:** Stored in MongoDB to support your settings and for anonymous usage statistics. This data is also cached in memory for performance. If you wish to have your stored preferences removed, you can cease using the bot or contact the bot administrator for explicit data deletion.

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

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Count active today
    today_iso = datetime.now().date().isoformat()
    active_today_count = sum(1 for user_doc in local_user_data.values() if user_doc.get("last_active", "").startswith(today_iso))

    # Total registered users from local_user_data
    total_registered_users = len(local_user_data)

    # Processing stats (TTS conversions)
    try:
        total_tts_conversions_db = processing_stats_collection.count_documents({"type": "tts"})
    except Exception as e:
        logging.error(f"Error fetching TTS stats from DB: {e}")
        total_tts_conversions_db = 0

    text = (
        "📊 Bot Statistics\n\n"
        "🟢 **Bot Status: Online**\n"
        f"⏱️ The bot has been running for: {days} days, {hours:02d} hours, {minutes:02d} minutes, {seconds:02d} seconds\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Users Today : {active_today_count}\n"
        f"▫️ Total Registered Users : {total_registered_users}\n\n"
        "⚙️ Processing Statistics \n"
        f"▫️ **Total Text-to-Speech Conversions: {total_tts_conversions_db}**\n\n"
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

# --- TTS VOICES BY LANGUAGE (Updated for requested voices) ---
TTS_VOICES_BY_LANGUAGE = {
    "English 🇬🇧": [
        "en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural",
        "en-GB-LibbyNeural", "en-GB-RyanNeural", "en-GB-ThomasNeural",
        "en-AU-NatashaNeural", "en-AU-WilliamNeural", "en-CA-ClaraNeural",
        "en-IE-EmilyNeural", "en-IE-ConnorNeural", "en-IN-NeerjaNeural", "en-IN-PrabhatNeural"
    ],
    "Arabic 🇸🇦": [
        "ar-SA-HamedNeural", "ar-SA-ZariyahNeural", "ar-EG-SalmaNeural", "ar-EG-ShakirNeural",
        "ar-DZ-IsmaelNeural", "ar-BH-LailaNeural", "ar-BH-AliNeural",
        "ar-IQ-RanaNeural", "ar-IQ-BasselNeural", "ar-KW-FahedNeural", "ar-KW-NouraNeural",
        "ar-QA-MoazNeural",
        "ar-SY-LaithNeural",
        "ar-AE-FatimaNeural", "ar-AE-HamdanNeural"
    ],
    "Spanish 🇪🇸": [
        "es-ES-AlvaroNeural", "es-ES-ElviraNeural", "es-MX-DaliaNeural", "es-MX-JorgeNeural",
        "es-AR-ElenaNeural", "es-AR-TomasNeural", "es-CO-SalomeNeural", "es-CO-GonzaloNeural",
        "es-US-PalomaNeural",  "es-CL-LorenzoNeural", "es-CL-CatalinaNeural",
        "es-PE-CamilaNeural",  "es-VE-PaolaNeural", "es-VE-SebastianNeural",
        "es-CR-MariaNeural", "es-CR-JuanNeural",
        "es-DO-RamonaNeural"
    ],
    "Hindi 🇮🇳": [
        "hi-IN-SwaraNeural", "hi-IN-MadhurNeural"
    ],
    "French 🇫🇷": [
        "fr-FR-DeniseNeural", "fr-FR-HenriNeural", "fr-CA-SylvieNeural", "fr-CA-JeanNeural",
        "fr-CH-ArianeNeural", "fr-CH-FabriceNeural", "fr-BE-GerardNeural"
    ],
    "German 🇩🇪": [
        "de-DE-KatjaNeural", "de-DE-ConradNeural", "de-CH-LeniNeural", "de-CH-JanNeural",
        "de-AT-IngridNeural", "de-AT-JonasNeural"
    ],
    "Chinese 🇨🇳": [
        "zh-CN-XiaoxiaoNeural", "zh-CN-YunyangNeural", "zh-CN-YunjianNeural",
        "zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural"
    ],
    "Japanese 🇯🇵": [
        "ja-JP-NanamiNeural", "ja-JP-KeitaNeural"
    ],
    "Portuguese 🇧🇷": [
        "pt-BR-FranciscaNeural", "pt-BR-AntonioNeural", "pt-PT-RaquelNeural", "pt-PT-DuarteNeural"
    ],
    "Russian 🇷🇺": [
        "ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural", "ru-RU-LarisaNeural", "ru-RU-MaximNeural"
    ],
    "Turkish 🇹🇷": [
        "tr-TR-EmelNeural", "tr-TR-AhmetNeural"
    ],
    "Korean 🇰🇷": [
        "ko-KR-SunHiNeural", "ko-KR-InJoonNeural"
    ],
    "Italian 🇮🇹": [
        "it-IT-ElsaNeural", "it-IT-DiegoNeural"
    ],
    "Indonesian 🇮🇩": [
        "id-ID-GadisNeural", "id-ID-ArdiNeural"
    ],
    "Vietnamese 🇻🇳": [
        "vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"
    ],
    "Thai 🇹🇭": [
        "th-TH-PremwadeeNeural", "th-TH-NiwatNeural"
    ],
    "Dutch 🇳🇱": [
        "nl-NL-ColetteNeural", "nl-NL-MaartenNeural"
    ],
    "Polish 🇵🇱": [
        "pl-PL-ZofiaNeural", "pl-PL-MarekNeural"
    ],
    "Swedish 🇸🇪": [
        "sv-SE-SofieNeural", "sv-SE-MattiasNeural"
    ],
    "Filipino 🇵🇭": [
        "fil-PH-BlessicaNeural", "fil-PH-AngeloNeural"
    ],
    "Greek 🇬🇷": [
        "el-GR-AthinaNeural", "el-GR-NestorasNeural"
    ],
    "Hebrew 🇮🇱": [
        "he-IL-AvriNeural", "he-IL-HilaNeural"
    ],
    "Hungarian 🇭🇺": [
        "hu-HU-NoemiNeural", "hu-HU-AndrasNeural"
    ],
    "Czech 🇨🇿": [
        "cs-CZ-VlastaNeural", "cs-CZ-AntoninNeural"
    ],
    "Danish 🇩🇰": [
        "da-DK-ChristelNeural", "da-DK-JeppeNeural"
    ],
    "Finnish 🇫🇮": [
        "fi-FI-SelmaNeural", "fi-FI-HarriNeural"
    ],
    "Norwegian 🇳🇴": [
        "nb-NO-PernilleNeural", "nb-NO-FinnNeural"
    ],
    "Romanian 🇷🇴": [
        "ro-RO-AlinaNeural", "ro-RO-EmilNeural"
    ],
    "Slovak 🇸🇰": [
        "sk-SK-LukasNeural", "sk-SK-ViktoriaNeural"
    ],
    "Ukrainian 🇺🇦": [
        "uk-UA-PolinaNeural", "uk-UA-OstapNeural"
    ],
    "Malay 🇲🇾": [
        "ms-MY-YasminNeural", "ms-MY-OsmanNeural"
    ],
    "Bengali 🇧🇩": [
        "bn-BD-NabanitaNeural", "bn-BD-BasharNeural"
    ],
    "Urdu 🇵🇰": [
        "ur-PK-AsmaNeural", "ur-PK-FaizanNeural"
    ],
    "Nepali 🇳🇵": [
        "ne-NP-SaritaNeural", "ne-NP-AbhisekhNeural"
    ],
    "Sinhala 🇱🇰": [
        "si-LK-SameeraNeural", "si-LK-ThiliniNeural"
    ],
    "Lao 🇱🇦": [
        "lo-LA-ChanthavongNeural", "lo-LA-KeomanyNeural"
    ],
    "Myanmar 🇲🇲": [
        "my-MM-NilarNeural", "my-MM-ThihaNeural"
    ],
    "Georgian 🇬🇪": [
        "ka-GE-EkaNeural", "ka-GE-GiorgiNeural"
    ],
    "Armenian 🇦🇲": [
        "hy-AM-AnahitNeural", "hy-AM-AraratNeural"
    ],
    "Azerbaijani 🇦🇿": [
        "az-AZ-BabekNeural", "az-AZ-BanuNeural"
    ],
    "Uzbek 🇺🇿": [
        "uz-UZ-MadinaNeural", "uz-UZ-SuhrobNeural"
    ],
    "Serbian 🇷🇸": [
        "sr-RS-NikolaNeural", "sr-RS-SophieNeural"
    ],
    "Croatian 🇭🇷": [
        "hr-HR-GabrijelaNeural", "hr-HR-SreckoNeural"
    ],
    "Slovenian 🇸🇮": [
        "sl-SI-PetraNeural", "sl-SI-RokNeural"
    ],
    "Latvian 🇱🇻": [
        "lv-LV-EveritaNeural", "lv-LV-AnsisNeural"
    ],
    "Lithuanian 🇱🇹": [
        "lt-LT-OnaNeural", "lt-LT-LeonasNeural"
    ],
    "Amharic 🇪🇹": [
        "am-ET-MekdesNeural", "am-ET-AbebeNeural"
    ],
    "Swahili 🇰🇪": [
        "sw-KE-ZuriNeural", "sw-KE-RafikiNeural"
    ],
    "Zulu 🇿🇦": [
        "zu-ZA-ThandoNeural", "zu-ZA-ThembaNeural"
    ],
    "Afrikaans 🇿🇦": [
        "af-ZA-AdriNeural", "af-ZA-WillemNeural"
    ],
    "Somali 🇸🇴": [
        "so-SO-UbaxNeural", "so-SO-MuuseNeural"
    ],
    "Persian 🇮🇷": [
        "fa-IR-DilaraNeural", "fa-IR-ImanNeural"
    ],
}

ORDERED_TTS_LANGUAGES = [
    "English 🇬🇧", "Arabic 🇸🇦", "Spanish 🇪🇸", "French 🇫🇷", "German 🇩🇪",
    "Chinese 🇨🇳", "Japanese 🇯🇵", "Portuguese 🇧🇷", "Russian 🇷🇺", "Turkish 🇹🇷",
    "Hindi 🇮🇳", "Somali 🇸🇴", "Italian 🇮🇹", "Indonesian 🇮🇩", "Vietnamese 🇻🇳",
    "Thai 🇹🇭", "Korean 🇰🇷", "Dutch 🇳🇱", "Polish 🇵🇱", "Swedish 🇸🇪",
    "Filipino 🇵🇭", "Greek 🇬🇷", "Hebrew 🇮🇱", "Hungarian 🇭🇺", "Czech 🇨🇿",
    "Danish 🇩🇰", "Finnish 🇫🇮", "Norwegian 🇳🇴", "Romanian 🇷🇴", "Slovak 🇸🇰",
    "Ukrainian 🇺🇦", "Malay 🇲🇾", "Bengali 🇧🇩", "Urdu 🇵🇰", "Nepali 🇳🇵",
    "Sinhala 🇱🇰", "Lao 🇱🇦", "Myanmar 🇲🇲", "Georgian 🇬🇪", "Armenian 🇦🇲",
    "Azerbaijani 🇦🇿", "Uzbek 🇺🇿", "Serbian 🇷🇸", "Croatian 🇭🇷", "Slovenian 🇸🇮",
    "Latvian 🇱🇻", "Lithuanian 🇱🇹", "Amharic 🇪🇹", "Swahili 🇰🇪", "Zulu 🇿🇦",
    "Afrikaans 🇿🇦", "Persian 🇮🇷"
]

def make_tts_language_keyboard():
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    for lang_name in ORDERED_TTS_LANGUAGES:
        if lang_name in TTS_VOICES_BY_LANGUAGE:
            buttons.append(
                InlineKeyboardButton(lang_name, callback_data=f"tts_lang|{lang_name}")
            )
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])
    return markup

def make_tts_voice_keyboard_for_language(lang_name: str):
    markup = InlineKeyboardMarkup(row_width=2)
    voices = TTS_VOICES_BY_LANGUAGE.get(lang_name, [])
    for voice in voices:
        markup.add(InlineKeyboardButton(voice, callback_data=f"tts_voice|{voice}"))
    markup.add(InlineKeyboardButton("⬅️ Back to Languages", callback_data="tts_back_to_languages"))
    return markup

def make_pitch_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Fastest", callback_data="pitch_set|+100"),
        InlineKeyboardButton("Faster", callback_data="pitch_set|+50"),
        InlineKeyboardButton("Normal", callback_data="pitch_set|0"),
        InlineKeyboardButton("Slower", callback_data="pitch_set|-50"),
        InlineKeyboardButton("Slowest", callback_data="pitch_set|-100")
    )
    return markup

def make_rate_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("2x", callback_data="rate_set|+100"),
        InlineKeyboardButton("1.5x", callback_data="rate_set|+50"),
        InlineKeyboardButton("Normal", callback_data="rate_set|0"),
        InlineKeyboardButton("0.5x", callback_data="rate_set|-50")
    )
    return markup

@bot.message_handler(commands=['voice_rate'])
def cmd_voice_rate(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = "awaiting_rate_input"

    bot.send_message(
        message.chat.id,
        "Changing voice rate (speed). Select a preset below, or enter a number from -100 to +100 (or 0 for reset):",
        reply_markup=make_rate_keyboard()
    )

@bot.callback_query_handler(lambda c: c.data.startswith("rate_set|"))
def on_rate_set_callback(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    try:
        _, rate_value_str = call.data.split("|", 1)
        rate_value = int(rate_value_str)

        set_tts_user_rate_db(uid, rate_value)
        user_rate_input_mode[uid] = None

        bot.answer_callback_query(call.id, f"✔️ Rate set to {rate_value} (Preset)")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔊 Rate set to *{rate_value}*. Now you can send text for speech or use /text_to_speech to pick a voice.",
            parse_mode="Markdown"
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid rate value.")
    except Exception as e:
        logging.error(f"Error setting rate from callback: {e}")
        bot.answer_callback_query(call.id, "An error occurred.")

@bot.message_handler(commands=['voice_pitch'])
def cmd_voice_pitch(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = "awaiting_pitch_input"
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "Changing voice pitch. Select a preset below, or enter a number from -100 to +100 (or 0 for reset):",
        reply_markup=make_pitch_keyboard()
    )

@bot.callback_query_handler(lambda c: c.data.startswith("pitch_set|"))
def on_pitch_set_callback(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    try:
        _, pitch_value_str = call.data.split("|", 1)
        pitch_value = int(pitch_value_str)

        set_tts_user_pitch_db(uid, pitch_value)
        user_pitch_input_mode[uid] = None

        bot.answer_callback_query(call.id, f"✔️ Pitch set to {pitch_value} (Preset)")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔊 Pitch set to *{pitch_value}*. Now you can send text for speech or use /text_to_speech to pick a voice.",
            parse_mode="Markdown"
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid pitch value.")
    except Exception as e:
        logging.error(f"Error setting pitch from callback: {e}")
        bot.answer_callback_query(call.id, "An error occurred.")

@bot.message_handler(commands=['text_to_speech'])
def cmd_text_to_speech(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    bot.send_message(message.chat.id, "Select the language of the text you want to convert to audio using the menu below:", reply_markup=make_tts_language_keyboard())

@bot.callback_query_handler(lambda c: c.data.startswith("tts_lang|"))
def on_tts_language_select(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, lang_name = call.data.split("|", 1)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Choose the voice that will read the language {lang_name} you selected using the buttons below :",
        reply_markup=make_tts_voice_keyboard_for_language(lang_name)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(lambda c: c.data.startswith("tts_voice|"))
def on_tts_voice_change(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, voice = call.data.split("|", 1)
    set_tts_user_voice_db(uid, voice)

    user_tts_mode[uid] = voice

    current_pitch = get_tts_user_pitch_db(uid)
    current_rate = get_tts_user_rate_db(uid)

    bot.answer_callback_query(call.id, f"✔️ Voice changed to {voice}")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔊 Now using: *{voice}*. Current pitch: *{current_pitch}*. Current rate: *{current_rate}*. You can start sending text messages to convert them to speech.",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(lambda c: c.data == "tts_back_to_languages")
def on_tts_back_to_languages(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Select the language of the text you want to convert to audio using the menu below:",
        reply_markup=make_tts_language_keyboard()
    )
    bot.answer_callback_query(call.id)

async def synth_and_send_tts(chat_id: int, user_id: str, text: str):
    """
    Use MSSpeech to synthesize text -> mp3, send and delete file.
    """
    voice = get_tts_user_voice_db(user_id)
    pitch = get_tts_user_pitch_db(user_id)
    rate = get_tts_user_rate_db(user_id)
    filename = os.path.join(DOWNLOAD_DIR, f"tts_{user_id}_{uuid.uuid4()}.mp3")

    stop_recording = threading.Event()
    recording_thread = threading.Thread(target=keep_recording, args=(chat_id, stop_recording))
    recording_thread.daemon = True
    recording_thread.start()

    try:
        mss = MSSpeech()
        await mss.set_voice(voice)
        await mss.set_rate(rate)
        await mss.set_pitch(pitch)
        await mss.set_volume(1.0)

        await mss.synthesize(text, filename)

        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            bot.send_message(chat_id, "❌ MP3 file not generated or empty. Please try again.")
            return

        with open(filename, "rb") as f:
            bot.send_audio(chat_id, f, caption=f"🎤 Voice: {voice}, Pitch: {pitch}, Rate: {rate}")

        # Log TTS conversion success
        try:
            processing_stats_collection.insert_one({
                "user_id": user_id,
                "type": "tts",
                "timestamp": datetime.now().isoformat(),
                "status": "success",
                "voice": voice,
                "pitch": pitch,
                "rate": rate,
                "text_length": len(text)
            })
            increment_tts_count_db(user_id)
        except Exception as e:
            logging.error(f"Error inserting TTS processing stat (success): {e}")

    except MSSpeechError as e:
        logging.error(f"TTS error: {e}")
        bot.send_message(chat_id, f"❌ An error occurred with the voice synthesis: {e}")
        try:
            processing_stats_collection.insert_one({
                "user_id": user_id,
                "type": "tts",
                "timestamp": datetime.now().isoformat(),
                "status": "fail_msspeech_error",
                "voice": voice,
                "pitch": pitch,
                "rate": rate,
                "error_message": str(e)
            })
        except Exception as e2:
            logging.error(f"Error inserting TTS processing stat (msspeech_error): {e2}")

    except Exception as e:
        logging.exception("TTS error")
        bot.send_message(chat_id, "❌ An unexpected error occurred during text-to-speech conversion. Please try again.")
        try:
            processing_stats_collection.insert_one({
                "user_id": user_id,
                "type": "tts",
                "timestamp": datetime.now().isoformat(),
                "status": "fail_unknown",
                "voice": voice,
                "pitch": pitch,
                "rate": rate,
                "error_message": str(e)
            })
        except Exception as e2:
            logging.error(f"Error inserting TTS processing stat (unknown error): {e2}")
    finally:
        stop_recording.set()
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception as e:
                logging.error(f"Error deleting TTS file {filename}: {e}")

@bot.message_handler(func=lambda message: message.content_type == 'text' and not message.text.startswith('/'))
def handle_text_for_tts(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    if user_rate_input_mode.get(uid) == "awaiting_rate_input":
        try:
            rate_val = int(message.text)
            if -100 <= rate_val <= 100:
                set_tts_user_rate_db(uid, rate_val)
                bot.send_message(message.chat.id, f"🔊 Voice rate set to *{rate_val}*.", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid rate. Please enter a number from -100 to +100 or 0 for reset.")
            user_rate_input_mode[uid] = None
            return
        except ValueError:
            pass

    if user_pitch_input_mode.get(uid) == "awaiting_pitch_input":
        try:
            pitch_val = int(message.text)
            if -100 <= pitch_val <= 100:
                set_tts_user_pitch_db(uid, pitch_val)
                bot.send_message(message.chat.id, f"🔊 Voice pitch set to *{pitch_val}*.", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid pitch. Please enter a number from -100 to +100 or 0 for reset.")
            user_pitch_input_mode[uid] = None
            return
        except ValueError:
            pass

    if user_tts_mode.get(uid):
        threading.Thread(
            target=lambda: asyncio.run(synth_and_send_tts(message.chat.id, uid, message.text))
        ).start()
    else:
        saved_voice = get_tts_user_voice_db(uid)
        if saved_voice != "en-US-AriaNeural": # If a custom voice was previously set
            user_tts_mode[uid] = saved_voice
            threading.Thread(
                target=lambda: asyncio.run(synth_and_send_tts(message.chat.id, uid, message.text))
            ).start()
        else:
            bot.send_message(
                message.chat.id,
                "Please use the /text_to_speech command first to select a voice before sending text for conversion."
            )

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note', 'document', 'photo', 'sticker'])
def handle_unsupported_media_types(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "I only support **Text-to-Speech** functionality. Please use the /text_to_speech command and then send me text to convert to audio.",
        parse_mode="Markdown"
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
        BotCommand("text_to_speech", "Set Text to voice language & voice"),
        BotCommand("voice_pitch", "Adjust voice pitch"),
        BotCommand("voice_rate", "Adjust voice speed"),
        BotCommand("help", "How to use the bot"),
        BotCommand("privacy", "Read privacy notice"),
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

