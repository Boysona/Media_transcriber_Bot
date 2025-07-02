import os
import uuid
import logging
import requests
import telebot
import json
from flask import Flask, request, abort
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ReplyKeyboardMarkup, KeyboardButton
import asyncio
import threading
import time
import io # For in-memory file handling

# --- KEEP: MSSpeech for Text-to-Speech ---
from msspeech import MSSpeech, MSSpeechError

# --- MongoDB client and collections ---
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- BOT CONFIGURATION ---
TOKEN = "7790991731:AAGiX824l_3PbGS5ZiQTuHIeRQX71JLTa3o" # Both bots used the same token, so it's unified
ADMIN_ID = 5978150981
WEBHOOK_URL = "https://spam-remover-bot-r3lv.onrender.com" # !!! IMPORTANT: Update this to your actual Render URL !!!
CHANNEL_ID = "@transcriber_bot_news_channel" # <--- ADD THIS: Your channel username or ID (e.g., -1001234567890)

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Download directory (temporary files)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- MONGODB CONFIGURATION ---
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

# Collections (unified names for shared collections)
mongo_client: MongoClient = None
db = None
users_collection = None # This will now store both TTS and STT user data

# --- In-memory caches ---
local_user_data = {}            # { user_id: { "last_active": "...", "tts_count": N, "stt_lang": "en", ... } }
_tts_voice_cache = {}           # { user_id: voice_name }
_tts_pitch_cache = {}           # { user_id: pitch_value }
_tts_rate_cache = {}            # { user_id: rate_value }
_stt_lang_cache = {}            # { user_id: language_code }

# --- User state for Text-to-Speech/Speech-to-Text input mode ---
user_tts_mode = {}              # { user_id: current_tts_voice or None }
user_pitch_input_mode = {}      # { user_id: "awaiting_pitch_input" or None }
user_rate_input_mode = {}       # { user_id: "awaiting_rate_input" or None }
admin_broadcast_state = {}      # { user_id: True/False }

# --- Statistics counters (TTS specific, STT will be added to user_data) ---
total_tts_processed = 0
bot_start_time = datetime.now()

# Admin uptime message storage
admin_uptime_message = {}
admin_uptime_lock = threading.Lock()

# --- ASSEMBLYAI CONFIGURATION (for Speech-to-Text) ---
ASSEMBLYAI_API_KEY = "6dab0a0669624f44afa50d679242e473"

# --- Supported Languages for Speech-to-Text with Flags ---
STT_LANGUAGES = {
    "English 🇬🇧": "en", "Deutsch 🇩🇪": "de", "Русский 🇷🇺": "ru", "فارسى 🇮🇷": "fa",
    "Indonesia 🇮🇩": "id", "Казакша 🇰🇿": "kk", "Azerbaycan 🇦🇿": "az", "Italiano 🇮🇹": "it",
    "Türkçe 🇹🇷": "tr", "Български 🇧🇬": "bg", "Sroski 🇷🇸": "sr", "Français 🇫🇷": "fr",
    "العربية 🇸🇦": "ar", "Español 🇪🇸": "es", "اردو 🇵🇰": "ur", "ไทย 🇹🇭": "th",
    "Tiếng Việt 🇻🇳": "vi", "日本語 🇯🇵": "ja", "한국어 🇰🇷": "ko", "中文 🇨🇳": "zh",
    "Nederlands 🇳🇱": "nl", "Svenska 🇸🇪": "sv", "Norsk 🇳🇴": "no", "Dansk 🇩🇰": "da",
    "Suomi 🇫🇮": "fi", "Polski 🇵🇱": "pl", "Cestina 🇨🇿": "cs", "Magyar 🇭🇺": "hu",
    "Română 🇷🇴": "ro", "Melayu 🇲🇾": "ms", "O'zbekcha 🇺🇿": "uz", "Tagalog 🇵🇵🇭": "tl",
    "Português 🇵🇹": "pt", "हिन्दी 🇮🇳": "hi", "Somali 🇸🇴": "so" # Added Somali as requested
}

def connect_to_mongodb():
    """
    Connect to MongoDB at startup, set up collections and indexes.
    """
    global mongo_client, db, users_collection
    global local_user_data, _tts_voice_cache, _tts_pitch_cache, _tts_rate_cache, _stt_lang_cache

    try:
        mongo_client = MongoClient(MONGO_URI)
        mongo_client.admin.command('ismaster')
        db = mongo_client[DB_NAME]
        users_collection = db["users"] # Unified collection for all user data

        # Create indexes
        users_collection.create_index([("last_active", ASCENDING)])
        # No separate tts_users_collection, all in users_collection

        logging.info("Connected to MongoDB. Loading user data to memory...")

        # Load all user data into in-memory caches
        for user_doc in users_collection.find({}):
            user_id_str = user_doc["_id"]
            local_user_data[user_id_str] = user_doc
            # Load TTS preferences
            _tts_voice_cache[user_id_str] = user_doc.get("tts_voice", "en-US-AriaNeural")
            _tts_pitch_cache[user_id_str] = user_doc.get("tts_pitch", 0)
            _tts_rate_cache[user_id_str] = user_doc.get("tts_rate", 0)
            # Load STT preferences
            _stt_lang_cache[user_id_str] = user_doc.get("stt_lang", "en") # Default to English for STT

        logging.info(f"Loaded {len(local_user_data)} user documents with TTS/STT settings.")

    except ConnectionFailure as e:
        logging.error(f"MongoDB connection failed: {e}")
        exit(1)
    except Exception as e:
        logging.error(f"Error during MongoDB connection: {e}")
        exit(1)

def update_user_activity_db(user_id: int):
    """
    Update user.last_active in local_user_data cache and MongoDB.
    Initializes user if not exists.
    """
    user_id_str = str(user_id)
    now_iso = datetime.now().isoformat()

    # Update in-memory cache
    if user_id_str not in local_user_data:
        local_user_data[user_id_str] = {
            "_id": user_id_str,
            "last_active": now_iso,
            "tts_count": 0,
            "stt_count": 0,
            "tts_voice": "en-US-AriaNeural",
            "tts_pitch": 0,
            "tts_rate": 0,
            "stt_lang": "en"
        }
    else:
        local_user_data[user_id_str]["last_active"] = now_iso

    # Persist to MongoDB
    try:
        users_collection.update_one(
            {"_id": user_id_str},
            {"$set": {"last_active": now_iso}},
            upsert=True # Upsert ensures document is created if it doesn't exist
        )
    except Exception as e:
        logging.error(f"Error updating user activity for {user_id_str} in DB: {e}")

def increment_tts_count_db(user_id: str):
    """
    Increment tts_count in local_user_data cache and MongoDB.
    """
    now_iso = datetime.now().isoformat()

    # Update in-memory cache
    if user_id not in local_user_data:
        local_user_data[user_id] = {
            "_id": user_id,
            "last_active": now_iso,
            "tts_count": 1,
            "stt_count": 0,
            "tts_voice": "en-US-AriaNeural",
            "tts_pitch": 0,
            "tts_rate": 0,
            "stt_lang": "en"
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

def increment_stt_count_db(user_id: str):
    """
    Increment stt_count in local_user_data cache and MongoDB.
    """
    now_iso = datetime.now().isoformat()

    # Update in-memory cache
    if user_id not in local_user_data:
        local_user_data[user_id] = {
            "_id": user_id,
            "last_active": now_iso,
            "tts_count": 0,
            "stt_count": 1,
            "tts_voice": "en-US-AriaNeural",
            "tts_pitch": 0,
            "tts_rate": 0,
            "stt_lang": "en"
        }
    else:
        local_user_data[user_id]["stt_count"] = local_user_data[user_id].get("stt_count", 0) + 1
        local_user_data[user_id]["last_active"] = now_iso

    # Persist to MongoDB
    try:
        users_collection.update_one(
            {"_id": user_id},
            {
                "$inc": {"stt_count": 1},
                "$set": {"last_active": now_iso}
            },
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error incrementing STT count for {user_id} in DB: {e}")

def get_tts_user_voice_db(user_id: str) -> str:
    return _tts_voice_cache.get(user_id, "en-US-AriaNeural")

def set_tts_user_voice_db(user_id: str, voice: str):
    _tts_voice_cache[user_id] = voice
    try:
        users_collection.update_one( # Update in unified collection
            {"_id": user_id},
            {"$set": {"tts_voice": voice}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS voice for {user_id} in DB: {e}")

def get_tts_user_pitch_db(user_id: str) -> int:
    return _tts_pitch_cache.get(user_id, 0)

def set_tts_user_pitch_db(user_id: str, pitch: int):
    _tts_pitch_cache[user_id] = pitch
    try:
        users_collection.update_one( # Update in unified collection
            {"_id": user_id},
            {"$set": {"tts_pitch": pitch}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS pitch for {user_id} in DB: {e}")

def get_tts_user_rate_db(user_id: str) -> int:
    return _tts_rate_cache.get(user_id, 0)

def set_tts_user_rate_db(user_id: str, rate: int):
    _tts_rate_cache[user_id] = rate
    try:
        users_collection.update_one( # Update in unified collection
            {"_id": user_id},
            {"$set": {"tts_rate": rate}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS rate for {user_id} in DB: {e}")

def get_stt_user_lang_db(user_id: str) -> str:
    return _stt_lang_cache.get(user_id, "en") # Default to English

def set_stt_user_lang_db(user_id: str, lang_code: str):
    _stt_lang_cache[user_id] = lang_code
    try:
        users_collection.update_one( # Update in unified collection
            {"_id": user_id},
            {"$set": {"stt_lang": lang_code}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting STT language for {user_id} in DB: {e}")


# --- MODIFIED CHAT ACTION FUNCTIONS ---
def keep_recording(chat_id, stop_event):
    """Keeps sending 'record_audio' action to show activity (for TTS)."""
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'record_audio') # TTS is "recording voice"
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error sending record_audio action: {e}")
            break

def keep_typing(chat_id, stop_event):
    """Keeps sending 'typing' action to show activity (for STT/transcription)."""
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing') # STT is "typing"
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error sending typing action: {e}")
            break

def keep_uploading_document(chat_id, stop_event):
    """Keeps sending 'upload_document' action to show activity (for file sending)."""
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'upload_document') # File sending is "sending a file"
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error sending upload_document action: {e}")
            break
# --- END MODIFIED CHAT ACTION FUNCTIONS ---

def update_uptime_message(chat_id, message_id):
    """
    Live-update the admin uptime message.
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
#   C H A N N E L   S U B S C R I P T I O N   V E R I F I C A T I O N
# ────────────────────────────────────────────────────────────────────────────────

def is_user_subscribed(user_id):
    """
    Checks if a user is subscribed to the CHANNEL_ID.
    Bot must be an admin in the channel to use get_chat_member.
    """
    if user_id == ADMIN_ID: # Admin bypasses subscription check
        return True
    try:
        chat_member = bot.get_chat_member(CHANNEL_ID, user_id)
        # Check if the user's status is one of the "active" statuses
        return chat_member.status in ['member', 'creator', 'administrator']
    except telebot.apihelper.ApiTelegramException as e:
        # If the user is not found, or other API errors occur (e.g., bot not admin)
        logging.warning(f"Error checking subscription for user {user_id}: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error in is_user_subscribed: {e}")
        return False

def subscription_required(func):
    """
    Decorator to check if a user is subscribed to the channel.
    If not, sends a message and prevents the handler from executing.
    """
    def wrapper(message):
        user_id = message.from_user.id
        if not is_user_subscribed(user_id):
            bot.send_message(
                message.chat.id,
                "😪Sorry, dear.\n"
                "🔰You need to subscribe to the bot's channel in order to use it.\n"
                f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
                "‼️! | Subscribe and then send /start"
            )
            return # Stop execution if not subscribed
        return func(message) # Execute the original handler if subscribed
    return wrapper

# ────────────────────────────────────────────────────────────────────────────────
#   B O T   H A N D L E R S   (Unified)
# ────────────────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    user_id_str = str(user_id)
    update_user_activity_db(user_id) # Ensure user is registered/updated

    # Reset all input modes
    user_tts_mode[user_id_str] = None
    user_pitch_input_mode[user_id_str] = None
    user_rate_input_mode[user_id_str] = None
    admin_broadcast_state.pop(user_id, None) # Clear broadcast state

    first_name = message.from_user.first_name if message.from_user.first_name else "There"

    if user_id == ADMIN_ID:
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add(KeyboardButton("Send Broadcast"), KeyboardButton("Total Users"), KeyboardButton("/status"))
        sent_message = bot.send_message(
            message.chat.id,
            "👋 Welcome, Admin! Admin Panel and Uptime (updating live)...",
            reply_markup=keyboard
        )
        with admin_uptime_lock:
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
        if not is_user_subscribed(user_id):
            # User is NOT subscribed, send the specific message and return
            bot.send_message(
                message.chat.id,
                "😪Sorry, dear.\n"
                "🔰You need to subscribe to the bot's channel in order to use it.\n"
                f"- {CHANNEL_ID}\n"
                "‼️! | Subscribe and then send /start"
            )
            return # Stop execution here if not subscribed
        else:
            # User is subscribed, send the regular welcome message
            welcome_message = (
                f"👋 Salaam {first_name}\n\n"
                "• Send a voice, video, or audio file.\n"
                "• I will transcribe it and send it back to you!\n"
                "• Or send me text to get realistic AI voices for Free\n\n"
                "Need help? Contact: @Zack_3d"
            )
            bot.send_message(message.chat.id, welcome_message)

@bot.message_handler(commands=['help'])
@subscription_required # <--- ADD THIS
def help_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Reset input modes
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    help_text = (
        "ℹ️ How to use this bot:\n\n"
        "**Text-to-Speech (TTS):**\n"
        "  • /text_to_speech - Choose a language and voice for text conversion.\n"
        "  • /voice_pitch - Adjust the pitch of the TTS voice.\n"
        "  • /voice_rate - Adjust the speaking speed of the TTS voice.\n"
        "  • After selecting a voice, just send me text to convert!\n\n"
        "**Speech-to-Text (STT):**\n"
        "  • /set_stt_language - Select the language for transcribing voice, audio, and video.\n"
        "  • Send me a voice message, audio file, or video note (up to 20MB) to get a transcription.\n\n"
        "**General Commands:**\n"
        "  • /start - Show welcome message.\n"
        "  • /help - Show this help message.\n"
        "  • /status - Show bot statistics.\n"
        "  • /privacy - View privacy policy.\n\n"
        "Feel free to explore and use the commands!"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
@subscription_required # <--- ADD THIS
def privacy_notice_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Reset TTS modes
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    privacy_text = (
        "**Privacy Notice**\n\n"
        "1. **Text for Speech Synthesis:** When you send text for conversion to speech, "
        "it is processed to generate the audio and then **not stored**.\n\n"
        "2. **Audio for Transcription:** When you send audio/video for transcription, "
        "it is processed by AssemblyAI to generate text and then **not stored** on our servers.\n\n"
        "3. **User IDs and Preferences:** Your Telegram User ID and your chosen preferences "
        "(TTS voice, pitch, rate, and STT language) are stored in MongoDB. "
        "We also track the number of TTS and STT conversions for statistical purposes.\n\n"
        "4. **Data Sharing Policy:** We **do not share** your personal data with any third parties.\n\n"
        "By using this bot, you agree to these practices."
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
@subscription_required # <--- ADD THIS
def status_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Reset TTS modes
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Get stats from MongoDB
    total_users = users_collection.count_documents({})
    # Aggregate TTS and STT counts
    agg_result = users_collection.aggregate([
        {"$group": {
            "_id": None,
            "total_tts": {"$sum": "$tts_count"},
            "total_stt": {"$sum": "$stt_count"}
        }}
    ]).next() # .next() gets the single result document
    
    total_tts = agg_result.get("total_tts", 0)
    total_stt = agg_result.get("total_stt", 0)

    text = (
        "📊 Bot Statistics\n\n"
        "🟢 **Bot Status: Online**\n"
        f"⏱️ Uptime: {days} days, {hours:02d}h:{minutes:02d}m:{seconds:02d}s\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🔊 Total Text-to-Speech Conversions: {total_tts}\n"
        f"🎙️ Total Speech-to-Text Transcriptions: {total_stt}\n\n"
        "Thanks for using our service! 🙌"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# ────────────────────────────────────────────────────────────────────────────────
#   A D M I N   H A N D L E R S
# ────────────────────────────────────────────────────────────────────────────────

# Admin handlers do not need @subscription_required because ADMIN_ID bypasses it.
@bot.message_handler(func=lambda message: message.chat.id == ADMIN_ID and message.text == "Total Users")
def handle_admin_total_users(message):
    total_users = users_collection.count_documents({})
    bot.send_message(message.chat.id, f"Total users registered: {total_users}")
    bot.send_message(
        message.chat.id,
        "What else, Admin?",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton("Send Broadcast"), KeyboardButton("Total Users"), KeyboardButton("/status")]], resize_keyboard=True)
    )

@bot.message_handler(func=lambda message: message.chat.id == ADMIN_ID and message.text == "Send Broadcast")
def handle_admin_send_broadcast(message):
    admin_broadcast_state[message.chat.id] = True
    bot.send_message(message.chat.id, "Okay, Admin. Send me the message (text, photo, video, document, etc.) you want to broadcast to all users. To cancel, type /cancel_broadcast")

@bot.message_handler(commands=['cancel_broadcast'], func=lambda message: message.chat.id == ADMIN_ID and message.chat.id in admin_broadcast_state)
def cancel_broadcast(message):
    del admin_broadcast_state[message.chat.id]
    bot.send_message(
        message.chat.id,
        "Broadcast cancelled. What else, Admin?",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton("Send Broadcast"), KeyboardButton("Total Users"), KeyboardButton("/status")]], resize_keyboard=True)
    )

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'voice'],
                     func=lambda message: message.chat.id == ADMIN_ID and admin_broadcast_state.get(message.chat.id, False))
def handle_broadcast_message(message):
    del admin_broadcast_state[message.chat.id] # Exit broadcast state after receiving the message
    
    bot.send_message(message.chat.id, "Broadcasting your message now...")
    
    # Corrected: Fetch all distinct _id (user_id) from the collection
    all_users_ids = [doc["_id"] for doc in users_collection.find({}, {"_id": 1})]

    sent_count = 0
    failed_count = 0

    for user_id_str in all_users_ids:
        user_chat_id = int(user_id_str) # Convert back to int for bot methods
        if user_chat_id == ADMIN_ID: # Don't send broadcast to admin themselves
            continue

        try:
            bot.copy_message(user_chat_id, message.chat.id, message.message_id)
            sent_count += 1
            time.sleep(0.1) # Small delay to avoid hitting Telegram's flood limits
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to user {user_chat_id}: {e}")
            failed_count += 1
        except Exception as e:
            logging.error(f"Unexpected error broadcasting to user {user_chat_id}: {e}")
            failed_count += 1

    bot.send_message(message.chat.id, f"Broadcast complete! Successfully sent to {sent_count} users. Failed for {failed_count} users.")
    bot.send_message(
        message.chat.id,
        "What else, Admin?",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton("Send Broadcast"), KeyboardButton("Total Users"), KeyboardButton("/status")]], resize_keyboard=True)
    )

# ────────────────────────────────────────────────────────────────────────────────
#   T E X T - T O - S P E E C H   F U N C T I O N A L I T Y
# ────────────────────────────────────────────────────────────────────────────────

# TTS VOICES BY LANGUAGE (Updated with all requested languages and voices)
TTS_VOICES_BY_LANGUAGE = {
    "English 🇬🇧": [
        "en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural", "en-US-DavisNeural",
        "en-GB-LibbyNeural", "en-GB-RyanNeural", "en-GB-MiaNeural", "en-GB-ThomasNeural",
        "en-AU-NatashaNeural", "en-AU-WilliamNeural", "en-CA-LindaNeural", "en-CA-ClaraNeural",
        "en-IE-EmilyNeural", "en-IE-ConnorNeural", "en-IN-NeerjaNeural", "en-IN-PrabhatNeural"
    ],
    "Arabic 🇸🇦": [
        "ar-SA-HamedNeural", "ar-SA-ZariyahNeural", "ar-EG-SalmaNeural", "ar-EG-ShakirNeural",
        "ar-DZ-AminaNeural", "ar-DZ-IsmaelNeural", "ar-BH-LailaNeural", "ar-BH-AliNeural",
        "ar-IQ-RanaNeural", "ar-IQ-BasselNeural", "ar-KW-FahedNeural", "ar-KW-NouraNeural",
        "ar-OM-AishaNeural", "ar-OM-SamirNeural", "ar-QA-MoazNeural", "ar-QA-ZainabNeural",
        "ar-SY-AmiraNeural", "ar-SY-LaithNeural", "ar-AE-FatimaNeural", "ar-AE-HamdanNeural",
        "ar-YE-HamdanNeural", "ar-YE-SarimNeural"
    ],
    "Spanish 🇪🇸": [
        "es-ES-AlvaroNeural", "es-ES-ElviraNeural", "es-MX-DaliaNeural", "es-MX-JorgeNeural",
        "es-AR-ElenaNeural", "es-AR-TomasNeural", "es-CO-SalomeNeural", "es-CO-GonzaloNeural",
        "es-US-PalomaNeural", "es-US-JuanNeural", "es-CL-LorenzoNeural", "es-CL-CatalinaNeural",
        "es-PE-CamilaNeural", "es-PE-DiegoNeural", "es-VE-PaolaNeural", "es-VE-SebastianNeural",
        "es-CR-MariaNeural", "es-CR-JuanNeural",
        "es-DO-RamonaNeural", "es-DO-AntonioNeural"
    ],
    "Hindi 🇮🇳": [
        "hi-IN-SwaraNeural", "hi-IN-MadhurNeural"
    ],
    "French 🇫🇷": [
        "fr-FR-DeniseNeural", "fr-FR-HenriNeural", "fr-CA-SylvieNeural", "fr-CA-JeanNeural",
        "fr-CH-ArianeNeural", "fr-CH-FabriceNeural", "fr-CH-CharlineNeural", "fr-BE-CamilleNeural"
    ],
    "German 🇩🇪": [
        "de-DE-KatjaNeural", "de-DE-ConradNeural", "de-CH-LeniNeural", "de-CH-JanNeural",
        "de-AT-IngridNeural", "de-AT-JonasNeural"
    ],
    "Chinese 🇨🇳": [
        "zh-CN-XiaoxiaoNeural", "zh-CN-YunyangNeural", "zh-CN-YunjianNeural", "zh-CN-XiaoyunNeural",
        "zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
        "zh-SG-XiaoMinNeural", "zh-SG-YunJianNeural"
    ],
    "Japanese 🇯🇵": [
        "ja-JP-NanamiNeural", "ja-JP-KeitaNeural", "ja-JP-MayuNeural", "ja-JP-DaichiNeural"
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
    "Somali 🇸🇴": [ # Added Somali
        "so-SO-UbaxNeural", "so-SO-MuuseNeural"
    ],
    "Persian 🇮🇷": [ # Added Persian as requested
        "fa-IR-DilaraNeural", "fa-IR-FaridNeural"
    ],
    "Kazakh 🇰🇿": [ # Added Kazakh as requested
        "kk-KZ-AigulNeural", "kk-KZ-NurbolatNeural"
    ]
}

# Order TTS_VOICES_BY_LANGUAGE keys by priority/common usage for display
# This ensures "Soomaali 🇸🇴" is included and the order is as requested
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
    "Afrikaans 🇿🇦", "Persian 🇮🇷", "Kazakh 🇰🇿"
]

def make_tts_language_keyboard():
    # Set row_width to 3 or 4 for languages
    # We'll dynamically choose based on total languages to make it somewhat even
    num_languages = len(ORDERED_TTS_LANGUAGES)
    if num_languages % 4 == 0 or num_languages % 4 >= 2: # Prefer 4 if possible, or if it avoids a single button row
        row_width = 4
    else:
        row_width = 3

    markup = InlineKeyboardMarkup(row_width=row_width)
    buttons = []
    for lang_name in ORDERED_TTS_LANGUAGES:
        buttons.append(
            InlineKeyboardButton(lang_name, callback_data=f"tts_lang|{lang_name}")
        )
    
    # Add buttons in rows of chosen width
    for i in range(0, len(buttons), row_width):
        markup.add(*buttons[i:i+row_width])
    return markup

def make_tts_voice_keyboard_for_language(lang_name: str):
    markup = InlineKeyboardMarkup(row_width=1)
    voices = TTS_VOICES_BY_LANGUAGE.get(lang_name, [])
    for voice in voices:
        markup.add(InlineKeyboardButton(voice, callback_data=f"tts_voice|{voice}"))
    markup.add(InlineKeyboardButton("⬅️ Back to Languages", callback_data="tts_back_to_languages"))
    return markup

def make_pitch_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Higher (+20)", callback_data="pitch_set|+20"),
        InlineKeyboardButton("Lower (-20)", callback_data="pitch_set|-20"),
        InlineKeyboardButton("Reset (0)", callback_data="pitch_set|0")
    )
    markup.add(InlineKeyboardButton("Enter manually", callback_data="pitch_manual_input"))
    return markup

def make_rate_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Faster (+20)", callback_data="rate_set|+20"),
        InlineKeyboardButton("Slower (-20)", callback_data="rate_set|-20"),
        InlineKeyboardButton("Normal (0)", callback_data="rate_set|0")
    )
    markup.add(InlineKeyboardButton("Enter manually", callback_data="rate_manual_input"))
    return markup

@bot.message_handler(commands=['voice_rate'])
@subscription_required # <--- ADD THIS
def cmd_voice_rate(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Deactivate other input modes
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = "awaiting_rate_input"

    bot.send_message(
        message.chat.id,
        "🔊 Select speaking speed or enter a number (-100 to +100):\n"
        f"Current speed: *{get_tts_user_rate_db(uid)}*",
        reply_markup=make_rate_keyboard(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(lambda c: c.data.startswith("rate_set|"))
def on_rate_set_callback(call):
    # Callback queries need to check subscription status themselves,
    # as decorators only work on message handlers.
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    try:
        _, rate_value_str = call.data.split("|", 1)
        rate_value = int(rate_value_str)
        set_tts_user_rate_db(uid, rate_value)
        user_rate_input_mode[uid] = None # Exit manual input mode if set by this

        bot.answer_callback_query(call.id, f"✔️ Speed set to {rate_value}")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔊 Speaking speed set to *{rate_value}*. Send text to convert!",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error setting rate: {e}")
        bot.answer_callback_query(call.id, "❌ Error setting speed")

@bot.callback_query_handler(lambda c: c.data == "rate_manual_input")
def on_rate_manual_input(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    user_rate_input_mode[uid] = "awaiting_rate_input"
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Please send a number between -100 and +100 for the speaking speed."
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['voice_pitch'])
@subscription_required # <--- ADD THIS
def cmd_voice_pitch(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Deactivate other input modes
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = "awaiting_pitch_input"
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "🔊 Select voice pitch or enter a number (-100 to +100):\n"
        f"Current pitch: *{get_tts_user_pitch_db(uid)}*",
        reply_markup=make_pitch_keyboard(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(lambda c: c.data.startswith("pitch_set|"))
def on_pitch_set_callback(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    try:
        _, pitch_value_str = call.data.split("|", 1)
        pitch_value = int(pitch_value_str)
        set_tts_user_pitch_db(uid, pitch_value)
        user_pitch_input_mode[uid] = None # Exit manual input mode if set by this

        bot.answer_callback_query(call.id, f"✔️ Pitch set to {pitch_value}")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔊 Voice pitch set to *{pitch_value}*. Send text to convert!",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error setting pitch: {e}")
        bot.answer_callback_query(call.id, "❌ Error setting pitch")

@bot.callback_query_handler(lambda c: c.data == "pitch_manual_input")
def on_pitch_manual_input(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    user_pitch_input_mode[uid] = "awaiting_pitch_input"
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Please send a number between -100 and +100 for the voice pitch."
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['text_to_speech'])
@subscription_required # <--- ADD THIS
def cmd_text_to_speech(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Reset input modes
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    bot.send_message(message.chat.id, "🎙️ Choose a language for Text-to-Speech:", reply_markup=make_tts_language_keyboard())

@bot.callback_query_handler(lambda c: c.data.startswith("tts_lang|"))
def on_tts_language_select(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    _, lang_name = call.data.split("|", 1)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🎙️ Choose a voice for {lang_name} note ⚠️Some voice names may not be available right now, so if one doesn't work for you, choose another voice name.:",
        reply_markup=make_tts_voice_keyboard_for_language(lang_name)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(lambda c: c.data.startswith("tts_voice|"))
def on_tts_voice_change(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    _, voice = call.data.split("|", 1)
    set_tts_user_voice_db(uid, voice)
    user_tts_mode[uid] = voice # Activate TTS input mode

    current_pitch = get_tts_user_pitch_db(uid)
    current_rate = get_tts_user_rate_db(uid)

    bot.answer_callback_query(call.id, f"✔️ Voice: {voice}")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔊 Ready! Voice: *{voice}*\nPitch: *{current_pitch}*\nSpeed: *{current_rate}*\n\nSend text to convert to speech.",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(lambda c: c.data == "tts_back_to_languages")
def on_tts_back_to_languages(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    user_tts_mode[uid] = None # Deactivate TTS input mode
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🎙️ Choose a language for Text-to-Speech:",
        reply_markup=make_tts_language_keyboard()
    )
    bot.answer_callback_query(call.id)

async def synth_and_send_tts(chat_id: int, user_id: str, text: str):
    """
    Convert text to speech and send audio
    """
    # This function is called from a message handler that is already decorated.
    # No need to re-check subscription here.
    voice = get_tts_user_voice_db(user_id)
    pitch = get_tts_user_pitch_db(user_id)
    rate = get_tts_user_rate_db(user_id)
    filename = os.path.join(DOWNLOAD_DIR, f"tts_{user_id}_{uuid.uuid4()}.mp3")

    stop_chat_action = threading.Event()
    # --- MODIFIED CHAT ACTION FOR TTS ---
    chat_action_thread = threading.Thread(target=keep_recording, args=(chat_id, stop_chat_action))
    # --- END MODIFIED CHAT ACTION ---
    chat_action_thread.daemon = True
    chat_action_thread.start()

    try:
        mss = MSSpeech()
        await mss.set_voice(voice)
        await mss.set_rate(rate)
        await mss.set_pitch(pitch)
        await mss.set_volume(1.0)

        await mss.synthesize(text, filename)

        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            bot.send_message(chat_id, "❌ Error generating audio. Please try again.")
            return

        with open(filename, "rb") as f:
            bot.send_audio(chat_id, f, caption=f"🔊 Voice: {voice}")
            increment_tts_count_db(user_id)
            global total_tts_processed
            total_tts_processed += 1
    except MSSpeechError as e:
        logging.error(f"TTS error: {e}")
        bot.send_message(chat_id, f"❌ TTS Error: {e}")
    except Exception as e:
        logging.exception("TTS error")
        bot.send_message(chat_id, "❌ Error during conversion. Please try again.")
    finally:
        stop_chat_action.set()
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception as e:
                logging.error(f"Error deleting TTS file: {e}")

# ────────────────────────────────────────────────────────────────────────────────
#   S P E E C H - T O - T E X T   F U N C T I O N A L I T Y
# ────────────────────────────────────────────────────────────────────────────────

def build_stt_language_keyboard():
    # Dynamic row width for STT languages, preferring 3 or 4
    num_stt_languages = len(STT_LANGUAGES)
    if num_stt_languages % 4 == 0 or num_stt_languages % 4 >= 2:
        row_width = 4
    else:
        row_width = 3

    markup = InlineKeyboardMarkup(row_width=row_width)
    buttons = []
    # Sort languages alphabetically by display name for easier navigation
    sorted_languages = sorted(STT_LANGUAGES.keys())
    for lang_name in sorted_languages:
        buttons.append(InlineKeyboardButton(lang_name, callback_data=f"stt_lang_set|{STT_LANGUAGES[lang_name]}"))
    
    # Add buttons in rows of chosen width
    for i in range(0, len(buttons), row_width):
        markup.add(*buttons[i:i+row_width])
    return markup

@bot.message_handler(commands=['set_stt_language'])
@subscription_required # <--- ADD THIS
def send_stt_language_prompt(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Reset input modes
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    current_lang_code = get_stt_user_lang_db(user_id)
    current_lang_name = next((name for name, code in STT_LANGUAGES.items() if code == current_lang_code), "English 🇬🇧")

    bot.send_message(
        message.chat.id,
        f"📝 Choose your **Speech-to-Text** transcription language:\n"
        f"Current language: *{current_lang_name}*",
        reply_markup=build_stt_language_keyboard(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(lambda c: c.data.startswith("stt_lang_set|"))
def save_user_stt_language(call):
    if not is_user_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first to use this feature.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            "😪Sorry, dear.\n"
            "🔰You need to subscribe to the bot's channel in order to use it.\n"
            f"- {CHANNEL_ID}\n" # Use the CHANNEL_ID variable here
            "‼️! | Subscribe and then send /start"
        )
        return

    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)
    _, code = call.data.split("|",1)
    set_stt_user_lang_db(uid, code)
    name = next((n for n,c in STT_LANGUAGES.items() if c==code), "Unknown")
    
    # Delete the message with the inline keyboard
    bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    
    bot.answer_callback_query(call.id, f"You set ✅ {name}")
    
    # Send a new, regular message instead of editing the old one
    bot.send_message(
        chat_id=call.message.chat.id,
        text=(
            f"✅ Transcription Language Set: *{name}*\n\n"
            "🎙️ Please send your voice message, audio file, or video note, and I’ll transcribe it for you with precision.\n\n"
            "📁 Supported file size: Up to 20MB\n\n"
            "📞 Need help? Contact: @Zack_3d"
        ),
        parse_mode="Markdown"
    )
    # --- END MODIFIED STT LANGUAGE CONFIRMATION MESSAGE ---

async def process_speech_to_text(chat_id: int, user_id: str, message_obj):
    """
    Handles transcription of voice, audio, video messages.
    """
    # This function is called from a message handler that is already decorated.
    # No need to re-check subscription here.
    lang_code = get_stt_user_lang_db(user_id)

    stop_chat_action = threading.Event()
    # --- MODIFIED CHAT ACTION FOR STT ---
    chat_action_thread = threading.Thread(target=keep_typing, args=(chat_id, stop_chat_action))
    # --- END MODIFIED CHAT ACTION ---
    chat_action_thread.daemon = True
    chat_action_thread.start()

    processing_msg = None
    try:
        processing_msg = bot.reply_to(message_obj, "⏳ Processing...")

        file_id = None
        file_size = 0

        if message_obj.voice:
            file_id = message_obj.voice.file_id
            file_size = message_obj.voice.file_size
        elif message_obj.audio:
            file_id = message_obj.audio.file_id
            file_size = message_obj.audio.file_size
        elif message_obj.video:
            file_id = message_obj.video.file_id
            file_size = message_obj.video.file_size
        elif message_obj.document: # Assuming document could be audio/video too
            file_id = message_obj.document.file_id
            file_size = message_obj.document.file_size
        
        if not file_id:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message_obj, "Unsupported file type for transcription. Please send a voice, audio, video, or document file.")
            return

        # --- ADDED: File size check ---
        if file_size > 20 * 1024 * 1024: # 20 MB in bytes
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message_obj, "The file is too large, the maximum file size allowed is 20 MB.")
            return
        # --- END ADDED ---

        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        file_data = requests.get(file_url).content

        upload_res = requests.post(
            "https://api.assemblyai.com/v2/upload",
            headers={"authorization": ASSEMBLYAI_API_KEY},
            data=file_data
        )
        audio_url = upload_res.json().get('upload_url')
        if not audio_url:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message_obj, "❌ Failed to upload file to transcription service.")
            return

        transcript_res = requests.post(
            "https://api.assemblyai.com/v2/transcript",
            headers={
                "authorization": ASSEMBLYAI_API_KEY,
                "content-type": "application/json"
            },
            json={
                "audio_url": audio_url,
                "language_code": lang_code,
                "speech_model": "best"
            }
        )

        res_json = transcript_res.json()
        transcript_id = res_json.get("id")
        if not transcript_id:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message_obj, f"❌ Transcription error: {res_json.get('error', 'Unknown')}")
            return

        polling_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
        while True:
            res = requests.get(polling_url, headers={"authorization": ASSEMBLYAI_API_KEY}).json()
            if res['status'] in ['completed', 'error']:
                break
            time.sleep(2)

        bot.delete_message(chat_id, processing_msg.message_id)

        if res['status'] == 'completed':
            text = res.get("text", "")
            if not text:
                bot.reply_to(message_obj, "ℹ️ No transcription text was returned.")
            elif len(text) <= 3000: # Use a safe margin before Telegram's 4096 char limit
                bot.reply_to(message_obj, text)
            else:
                # Send as a plain text file for long transcriptions
                transcript_file = io.BytesIO(text.encode("utf-8"))
                transcript_file.name = "transcript.txt" # Name for the file
                # --- MODIFIED CHAT ACTION FOR FILE SENDING ---
                file_send_stop_event = threading.Event()
                file_send_thread = threading.Thread(target=keep_uploading_document, args=(chat_id, file_send_stop_event))
                file_send_thread.daemon = True
                file_send_thread.start()
                try:
                    bot.send_document(
                        chat_id=chat_id, 
                        document=transcript_file, 
                        caption="Your transcription is too long to send as a message. Here it is as a file:"
                    )
                finally:
                    file_send_stop_event.set()
                # --- END MODIFIED CHAT ACTION ---

            increment_stt_count_db(user_id) # Increment STT count on success
        else:
            bot.reply_to(message_obj, f"❌ Sorry, transcription failed. Status: {res.get('status', 'N/A')}, Error: {res.get('error', 'N/A')}")

    except Exception as e:
        logging.error(f"Error handling media for STT: {e}")
        if processing_msg:
            try:
                bot.delete_message(chat_id, processing_msg.message_id)
            except Exception:
                pass # Ignore if message already deleted
        bot.reply_to(message_obj, f"⚠️ An unexpected error occurred during transcription: {str(e)}")
    finally:
        stop_chat_action.set()


# ────────────────────────────────────────────────────────────────────────────────
#   M E S S A G E   H A N D L I N G   (Unified)
# ────────────────────────────────────────────────────────────────────────────────

@bot.message_handler(content_types=['text'])
@subscription_required # <--- ADD THIS
def handle_text_messages(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Check for admin broadcast state first (admin bypasses subscription_required anyway)
    if message.chat.id == ADMIN_ID and admin_broadcast_state.get(message.chat.id, False):
        handle_broadcast_message(message)
        return

    # If it's the admin, but not in broadcast mode, direct to admin options
    if message.chat.id == ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "Admin, please use the admin options.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton("Send Broadcast"), KeyboardButton("Total Users"), KeyboardButton("/status")]], resize_keyboard=True)
        )
        return

    # Handle rate input from manual text entry
    if user_rate_input_mode.get(uid) == "awaiting_rate_input":
        try:
            rate_val = int(message.text)
            if -100 <= rate_val <= 100:
                set_tts_user_rate_db(uid, rate_val)
                bot.send_message(message.chat.id, f"🔊 Speed set to *{rate_val}*.", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid value. Please enter a number between -100 and +100.")
            user_rate_input_mode[uid] = None # Exit input mode
            return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Invalid input. Please enter a number for speed (e.g., 20, -10, 0).")
            return # Stay in input mode until valid input or command

    # Handle pitch input from manual text entry
    if user_pitch_input_mode.get(uid) == "awaiting_pitch_input":
        try:
            pitch_val = int(message.text)
            if -100 <= pitch_val <= 100:
                set_tts_user_pitch_db(uid, pitch_val)
                bot.send_message(message.chat.id, f"🔊 Pitch set to *{pitch_val}*.", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid value. Please enter a number between -100 and +100.")
            user_pitch_input_mode[uid] = None # Exit input mode
            return
        except ValueError:
            bot.send_message(message.chat.id, "❌ Invalid input. Please enter a number for pitch (e.g., 20, -10, 0).")
            return # Stay in input mode until valid input or command

    # If no specific input mode is active, assume text is for TTS
    if user_tts_mode.get(uid) or get_tts_user_voice_db(uid): # Check if a voice is set or TTS mode is active
        threading.Thread(
            target=lambda: asyncio.run(synth_and_send_tts(message.chat.id, uid, message.text))
        ).start()
    else:
        bot.send_message(
            message.chat.id,
            "I'm ready for either **Text-to-Speech** or **Speech-to-Text**!\n\n"
            "To convert text to speech, use /text_to_speech to choose a voice first, then send your text.\n"
            "To transcribe voice/audio/video, use /set_stt_language to pick a language, then send your file."
            , parse_mode="Markdown"
        )

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
@subscription_required # <--- ADD THIS
def handle_media_for_stt(message):
    chat_id = message.chat.id
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    # Reset any active TTS input modes if media is sent
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    # If it's the admin, and they are NOT in broadcast state, redirect them to admin menu
    if chat_id == ADMIN_ID and not admin_broadcast_state.get(chat_id, False):
        bot.send_message(
            chat_id,
            "Admin, please use the admin options.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton("Send Broadcast"), KeyboardButton("Total Users"), KeyboardButton("/status")]], resize_keyboard=True)
        )
        return

    # Check if STT language is set
    if not get_stt_user_lang_db(user_id):
        bot.send_message(
            chat_id,
            "❗ Please select a language for transcription first using /set_stt_language before sending a file."
        )
        return

    # Process media for STT asynchronously
    threading.Thread(
        target=lambda: asyncio.run(process_speech_to_text(chat_id, user_id, message))
    ).start()

# Fallback for unsupported content types
@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'location', 'contact', 'venue', 'game', 'invoice', 'successful_payment', 'connected_website', 'poll', 'dice', 'passport_data', 'proximity_alert_triggered', 'video_chat_started', 'video_chat_ended', 'video_chat_participants_invited', 'web_app_data', 'animation', 'forum_topic_created', 'forum_topic_closed', 'forum_topic_reopened', 'general_forum_topic_hidden', 'general_forum_topic_unhidden', 'write_access_allowed', 'user_shared', 'chat_shared', 'story'])
@subscription_required # <--- ADD THIS
def unsupported_content(message):
    bot.send_message(
        message.chat.id,
        "❌ I only convert **text to speech** (send text after choosing a voice) or **transcribe voice/audio/video** (send file after choosing an STT language).\n\n"
        "Please send text, a voice message, an audio file, or a video file. Use /help for more info.",
        parse_mode="Markdown"
    )

# ────────────────────────────────────────────────────────────────────────────────
#   W E B H O O K   S E T U P
# ────────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method in ("GET", "HEAD"):
        return "OK", 200
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if content_type and content_type.startswith("application/json"):
            json_string = request.get_data().decode("utf-8")
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return "", 200
    return abort(403)

@app.route("/set_webhook", methods=["GET"])
def set_webhook_route():
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        return f"Failed: {e}", 500

def set_bot_commands():
    commands = [
        BotCommand("start", "Get welcome message"),
        BotCommand("help", "Show help information"),
        BotCommand("text_to_speech", "Select voice for text-to-speech"),
        BotCommand("voice_pitch", "Adjust voice pitch"),
        BotCommand("voice_rate", "Adjust speaking speed"),
        BotCommand("set_stt_language", "Select language for speech-to-text"), # New command for STT language
        BotCommand("status", "Show bot statistics"),
        BotCommand("privacy", "View privacy policy")
    ]
    try:
        bot.set_my_commands(commands)
        logging.info("Bot commands set")
    except Exception as e:
        logging.error(f"Error setting commands: {e}")

def initialize_bot():
    connect_to_mongodb()
    set_bot_commands()
    try:
        # It's good practice to remove webhook first in case of previous deployment issues
        bot.remove_webhook()
        time.sleep(1) # Give Telegram a moment
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Webhook setup error: {e}")

if __name__ == "__main__":
    initialize_bot()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
