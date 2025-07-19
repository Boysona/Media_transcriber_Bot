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

# --- NEW: AssemblyAI Configuration ---
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
translation_language_settings_collection = None # RENAMED
summary_language_settings_collection = None # NEW
media_language_settings_collection = None
tts_users_collection = None
processing_stats_collection = None

# --- In-memory caches (to reduce DB hits) ---
# Global dictionaries to hold user data in RAM
local_user_data = {}            # { user_id: { "last_active": "...", "transcription_count": N, ... } }
_user_translation_language_cache = {}       # RENAMED { user_id: language_name }
_user_summary_language_cache = {} # NEW: { user_id: language_name }
_media_language_cache = {}      # { user_id: media_language }
_tts_voice_cache = {}           # { user_id: voice_name }
_tts_pitch_cache = {}           # { user_id: pitch_value }
_tts_rate_cache = {}           # NEW: { user_id: rate_value } # For voice rate

# --- User state for Text-to-Speech input mode ---
# { user_id: voice_name (e.g. "en-US-AriaNeural") or None }
user_tts_mode = {}
user_pitch_input_mode = {}      # { user_id: "awaiting_pitch_input" or None }
user_rate_input_mode = {}       # NEW: { user_id: "awaiting_rate_input" or None }

# --- Statistics counters (in-memory for quick access) ---
# These are still in-memory and will reset on bot restart as per your original design.
# If you want these to persist, they would also need to be stored in MongoDB.
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
# This is explicitly designed to be temporary, so it will reset on bot restart.
# Your current code already handles its 10-minute deletion.
user_transcriptions = {} # { user_id: { message_id: "transcription text" } }

GEMINI_API_KEY = "AIzaSyCHrGhRKXAp3DuQGH8HLB60ggryZeUFA9E"  # <-- your Gemini API Key
# User memory for Gemini (in-memory, resets on bot restart)
user_memory = {} # { user_id: [{"role": "user", "text": "..."}] }

def ask_gemini(user_id, user_message):
    """
    Send conversation history to Gemini and return the response text.
    """
    # Note: we only keep last 10 messages in-memory per user for context
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    
    # NEW: Add system instruction for pause symbols
    system_instruction_part = {"text": "For translations, use only a comma (,) as a pause symbol. Do not use a period (.)."}
    
    # Insert system instruction at the beginning of parts
    full_parts = [system_instruction_part] + parts

    resp = requests.post(
        url,
        headers={'Content-Type': 'application/json'},
        json={"contents": [{"parts": full_parts}]} # Use full_parts here
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
    global users_collection, translation_language_settings_collection, summary_language_settings_collection, media_language_settings_collection, tts_users_collection, processing_stats_collection
    global local_user_data, _user_translation_language_cache, _user_summary_language_cache, _media_language_cache, _tts_voice_cache, _tts_pitch_cache, _tts_rate_cache

    try:
        mongo_client = MongoClient(MONGO_URI)
        mongo_client.admin.command('ismaster')
        db = mongo_client[DB_NAME]
        users_collection = db["users"]
        translation_language_settings_collection = db["user_translation_language_settings"] # RENAMED
        summary_language_settings_collection = db["user_summary_language_settings"] # NEW
        media_language_settings_collection = db["user_media_language_settings"]
        tts_users_collection = db["tts_users"]
        processing_stats_collection = db["file_processing_stats"]

        # Create indexes (if not already created)
        users_collection.create_index([("last_active", ASCENDING)])
        translation_language_settings_collection.create_index([("_id", ASCENDING)]) # RENAMED
        summary_language_settings_collection.create_index([("_id", ASCENDING)]) # NEW
        media_language_settings_collection.create_index([("_id", ASCENDING)])
        tts_users_collection.create_index([("_id", ASCENDING)])
        processing_stats_collection.create_index([("user_id", ASCENDING)])
        processing_stats_collection.create_index([("type", ASCENDING)])
        processing_stats_collection.create_index([("timestamp", ASCENDING)])

        logging.info("Connected to MongoDB and indexes created. Loading data to memory...")

        # --- Load all user data into in-memory caches on startup ---
        for user_doc in users_collection.find({}):
            local_user_data[user_doc["_id"]] = user_doc
        logging.info(f"Loaded {len(local_user_data)} user documents into local_user_data.")

        for lang_setting in translation_language_settings_collection.find({}): # RENAMED
            _user_translation_language_cache[lang_setting["_id"]] = lang_setting.get("language") # RENAMED
        logging.info(f"Loaded {len(_user_translation_language_cache)} user translation language settings.") # RENAMED

        for lang_setting in summary_language_settings_collection.find({}): # NEW
            _user_summary_language_cache[lang_setting["_id"]] = lang_setting.get("language") # NEW
        logging.info(f"Loaded {len(_user_summary_language_cache)} user summary language settings.") # NEW

        for media_lang_setting in media_language_settings_collection.find({}):
            _media_language_cache[media_lang_setting["_id"]] = media_lang_setting.get("media_language")
        logging.info(f"Loaded {len(_media_language_cache)} media language settings.")

        for tts_user in tts_users_collection.find({}):
            _tts_voice_cache[tts_user["_id"]] = tts_user.get("voice", "en-US-AriaNeural")
            _tts_pitch_cache[tts_user["_id"]] = tts_user.get("pitch", 0)
            _tts_rate_cache[tts_user["_id"]] = tts_user.get("rate", 0) # NEW: Load rate
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
            "transcription_count": 0 # Initialize for new users
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

# RENAMED AND MODIFIED
def get_user_translation_language_db(user_id: str) -> str | None:
    """
    Return user's preferred language for translations from cache.
    """
    return _user_translation_language_cache.get(user_id)

# RENAMED AND MODIFIED
def set_user_translation_language_db(user_id: str, lang: str):
    """
    Save preferred language for translations in DB and update cache.
    """
    _user_translation_language_cache[user_id] = lang # Update in-memory cache
    try:
        translation_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting preferred translation language for {user_id} in DB: {e}")

# NEW: Functions for summary language
def get_user_summary_language_db(user_id: str) -> str | None:
    """
    Return user's preferred language for summaries from cache.
    """
    return _user_summary_language_cache.get(user_id)

# NEW: Functions for summary language
def set_user_summary_language_db(user_id: str, lang: str):
    """
    Save preferred language for summaries in DB and update cache.
    """
    _user_summary_language_cache[user_id] = lang # Update in-memory cache
    try:
        summary_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting preferred summary language for {user_id} in DB: {e}")
# END NEW: Functions for summary language


def get_user_media_language_setting_db(user_id: str) -> str | None:
    """
    Return language chosen for media transcription from cache.
    """
    return _media_language_cache.get(user_id)

def set_user_media_language_setting_db(user_id: str, lang: str):
    """
    Save media transcription language in DB and update cache.
    """
    _media_language_cache[user_id] = lang # Update in-memory cache
    try:
        media_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"media_language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting media language for {user_id} in DB: {e}")

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

# NEW: Functions for TTS rate
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
# END NEW: Functions for TTS rate


# ────────────────────────────────────────────────────────────────────────────────
#   U T I L I T I E S   (keep typing, keep recording, update uptime)
# ────────────────────────────────────────────────────────────────────────────────

def keep_typing(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing')
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error sending typing action: {e}")
            break

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
    # Only send subscription message if it's a private chat
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
    user_first_name = message.from_user.first_name if message.from_user.first_name else "There"

    # Ensure user is in local_user_data and DB
    if user_id_str not in local_user_data:
        local_user_data[user_id_str] = {
            "_id": user_id_str,
            "last_active": datetime.now().isoformat(),
            "transcription_count": 0
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

    # NEW: Check subscription immediately on /start for all users except admin in private chat
    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return


    # Ensure TTS modes are OFF on /start
    user_tts_mode[user_id_str] = None
    user_pitch_input_mode[user_id_str] = None
    user_rate_input_mode[user_id_str] = None # NEW: Reset rate input mode

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
        # NEW WELCOME MESSAGE AND UI
        welcome_message = (
            f"🌟 *Hello {user_first_name}! Welcome to Media To Text Bot!* 🌟\n\n"
            "Your ultimate tool for converting spoken words into text, translating, summarizing, "
            "and even turning text back into natural-sounding speech. I can process voice messages, "
            "audio, and video files up to 20MB with high accuracy.\n\n"
            "Ready to get started? Send me an audio, voice message, or video to transcribe. Or send me text to generate AI voice Before you send your audio, voice message, video, or text, please select your transcription and text-to-audio language & voice using /lange and /voice:"
        )
        
        # Create a more visually appealing keyboard
        markup = InlineKeyboardMarkup(row_width=1)
        
        # Add "Add me to your groups" button
        markup.add(
            InlineKeyboardButton("Add me to your groups", url="https://t.me/mediatotextbot?startgroup=")
        )
        
        # Add "Menu" button
        markup.add(
            InlineKeyboardButton("how to use Menu commands❓", callback_data="show_main_menu")
        )
        
        bot.send_message(
            message.chat.id,
            welcome_message,
            reply_markup=markup,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda c: c.data == "show_main_menu")
def show_main_menu_handler(call):
    user_id = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    # Ensure TTS modes are OFF when showing menu
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    help_text = (
        """ℹ️ Here's how I can help you:

➡️ *Send a voice message, audio file, or video (up to 20MB)* and I'll transcribe it instantly.

📚 *After transcription, you can:*
 •  Tap *Translate* to convert it into your preferred language.
 •  Tap *Summarize* to get a concise overview.

⚙️ *Commands:*
 •  `/lange` - Set the language for transcribing your media.
 •  `/trane` - Set your preferred language for translations.
 •  `/sumy` - Set your preferred language for summaries.
 •  `/voice` - Convert text to speech. Pick a language and voice.
 •  `/pitch` - Adjust the pitch of the generated voice.
 •  `/rate` - Adjust the speed of the generated voice.
 •  `/help` - Get detailed instructions on how to use me.
 •  `/privacy` - Read my privacy policy.
 •  `/status` - Check bot's current statistics.

❓ If you need assistance, please contact @user33230.
"""
    )
    
    # Edit the message to show the menu, instead of sending a new one
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=help_text,
        parse_mode="Markdown",
        reply_markup=None # Remove inline buttons after showing menu
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "more_languages")
def more_languages_handler(call):
    # Show full language selection
    markup = generate_language_keyboard("set_media_lang")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="✨ Please select your media file language for transcription:",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['help'])
def help_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF on /help
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None # NEW: Reset rate input mode


    help_text = (
        """ℹ️ How to Use This Bot
	1.	Transcription (Speech-to-Text)
 • Send a voice message, audio file, video note, or video(e.g. MP4, MP3).
 • The bot will process it and return the transcribed text.
	2.	Translation
 • After you receive the transcription, tap “Translate” or use /trane.
 • You will be prompted to choose a translation language if you haven't set one yet.
	3.	Summarization
 • After you receive the transcription, tap “Summarize” or use /sumy.
 • You will be prompted to choose a summarization language if you haven't set one yet.
	4.	Text-to-Speech
 • Use /voice to pick a language and voice first.
 • Then send any text, and the bot will reply with an audio clip.
 • Adjust voice characteristics with:
  • /pitch to change pitch (higher or lower)
  • /rate to change speed (faster or slower)
	5.	Language Settings
 • /trane — set your preferred language for translations.
 • /sumy — set your preferred language for summaries.
 • /lange — set the language of any audio/video you send for transcription.
	6.	Privacy & Limits
 • Media files up to 20 MB.
 • Transcriptions are stored temporarily (10 minutes) for follow-up translate/summarize actions, then deleted.
 • All other settings (language, voice, pitch, rate) are saved for your next session.

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

    # Ensure TTS modes are OFF on /privacy
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None # NEW: Reset rate input mode

    privacy_text = (
        """**Privacy Notice**

Your privacy is paramount. Here's a transparent look at how this bot handles your data in real-time:

1.  **Data We Process & Its Lifecycle:**
    * **Media Files (Voice, Audio, Video):** When you send a media file (voice, audio, video note, or a video file as a document), it's temporarily downloaded for **immediate transcription**. Crucially, these files are **deleted instantly** from our servers once the transcription is complete. We do not store your media content.
    * **Text for Speech Synthesis:** When you send text for conversion to speech, it is processed to generate the audio and then **not stored**. The generated audio file is also temporary and deleted after sending.
    * **Transcriptions:** The text generated from your media is held **temporarily in-memory** (for 10 minutes only). After 10 minutes, the transcription is automatically deleted and cannot be retrieved. This data is used only for immediate translation or summarization requests.
    * **User IDs, Language Preferences, TTS Voices, and Activity Data:** Your Telegram User ID and your chosen preferences (language for translations/summaries, media transcription language, TTS voice, **pitch, and rate**) are stored in MongoDB. Basic activity (like last active timestamp and transcription count) are also stored. This helps us remember your preferences and track basic, aggregated activity (like when you last used the bot) to improve service and understand overall usage patterns. This data is also kept in-memory for quick access during bot operation and is persisted to MongoDB.

2.  **How Your Data is Used:**
    * To deliver the bot's core services: transcribing, translating, summarizing your media, and converting text to speech.
    * To enhance bot performance and gain insights into general usage trends through anonymous, collective statistics (e.g., total files processed).
    * To maintain your personalized language settings and voice preferences across sessions.

3.  **Data Sharing Policy:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are facilitated by integrating with advanced AI models (specifically, AssemblyAI for transcription and the Gemini API for translation/summarization). Text-to-speech uses the Microsoft Cognitive Services Speech API. Your input sent to these models is governed by their respective privacy policies, but we ensure that your data is **not stored by us** after processing by these services.

4.  **Data Retention:**
    * **Media files and generated audio files:** Deleted immediately post-processing.
    * **Transcriptions:** Held in-memory for 10 minutes, then permanently deleted.
    * **User IDs and language/voice preferences:** Stored in MongoDB to support your settings and for anonymous usage statistics. This data is also cached in memory for performance. If you wish to have your stored preferences removed, you can cease using the bot or contact the bot administrator for explicit data deletion.

By using this bot, you acknowledge and agree to the data practices outlined in this Privacy Notice.

Should you have any questions or concerns regarding your privacy, please feel free to contact the bot administrator.
"""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF on /status
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None # NEW: Reset rate input mode

    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Count active today
    today_iso = datetime.now().date().isoformat()
    # Check local_user_data for active users today
    active_today_count = sum(1 for user_doc in local_user_data.values() if user_doc.get("last_active", "").startswith(today_iso))


    # Total registered users from local_user_data
    total_registered_users = len(local_user_data)

    # Processing stats (voice/audio/video counts + total processing time + TTS conversions)
    try:
        total_processed_media = processing_stats_collection.count_documents({"type": {"$in": ["voice", "audio", "video"]}})
        voice_count = processing_stats_collection.count_documents({"type": "voice"})
        audio_count = processing_stats_collection.count_documents({"type": "audio"})
        video_count = processing_stats_collection.count_documents({"type": "video"})
        # NEW: Get total TTS conversions
        total_tts_conversions = processing_stats_collection.count_documents({"type": "tts"})

        pipeline = [
            {"$group": {"_id": None, "total_time": {"$sum": "$processing_time"}}}
        ]
        agg_result = list(processing_stats_collection.aggregate(pipeline))
        total_proc_seconds = agg_result[0]["total_time"] if agg_result else 0
    except Exception as e:
        logging.error(f"Error fetching processing stats from DB: {e}")
        total_processed_media = voice_count = audio_count = video_count = 0
        total_tts_conversions = 0 # Initialize to 0 on error
        total_proc_seconds = 0

    proc_hours = int(total_proc_seconds) // 3600
    proc_minutes = (int(total_proc_seconds) % 3600) // 60
    proc_seconds = int(total_proc_seconds) % 60

    text = (
        "📊 Bot Statistics\n\n"
        "🟢 **Bot Status: Online**\n"
        f"⏱️ The bot has been running for: {days} days, {hours:02d} hours, {minutes:02d} minutes, {seconds:02d} seconds\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Users Today : {active_today_count}\n" # Updated to reflect in-memory data
        f"▫️ Total Registered Users : {total_registered_users}\n\n" # Updated to reflect in-memory data
        "⚙️ Processing Statistics \n" # This still comes from DB
        f"▫️ Total Media Files Processed: {total_processed_media}\n"
        f"▫️ Voice Clips: {voice_count}\n"
        f"▫️ Audio Files: {audio_count}\n"
        f"▫️ Videos: {video_count}\n"
        f"▫️ **Total Text-to-Speech Conversions: {total_tts_conversions}**\n" # NEW: Added TTS conversions
        f"⏱️ Total Processing Time: {proc_hours} hours {proc_minutes} minutes {proc_seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌"
    )

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users(message):
    total_registered = len(local_user_data) # Get total users from in-memory cache
    bot.send_message(message.chat.id, f"Total registered users (from memory): {total_registered}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast_prompt(message): # Renamed function to avoid conflict if any
    admin_state[message.from_user.id] = 'awaiting_broadcast_message' # Changed state name for clarity
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast_message', # Updated state name
    content_types=['text', 'photo', 'video', 'audio', 'document']
)
def broadcast_message(message):
    admin_state[message.from_user.id] = None # Reset state
    success = fail = 0
    # Broadcast to every user currently in local_user_data (which reflects MongoDB)
    for uid in local_user_data.keys():
        # Do not send broadcast to the admin themselves
        if uid == str(ADMIN_ID):
            continue
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to {uid}: {e}")
            fail += 1
        # Add a small delay to avoid hitting Telegram API limits too quickly
        time.sleep(0.05) # 50 ms delay per message

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

    # NEW: Check subscription immediately when a file is sent IF IN PRIVATE CHAT
    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF when handling media
    user_tts_mode[uid_str] = None
    user_pitch_input_mode[uid_str] = None
    user_rate_input_mode[uid_str] = None # NEW: Reset rate input mode


    # Determine which file object to use
    file_obj = None
    is_document_video = False # This is no longer as relevant for AAI but keeping for type tracking
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

    # Check file size limit
    size = file_obj.file_size
    if size and size > FILE_SIZE_LIMIT:
        bot.send_message(message.chat.id, "😓 Sorry, the file size you uploaded is too large (max allowed is 20MB).")
        return

    # Send "Processing..." reply message
    processing_reply = bot.reply_to(message, "Processing...")

    # Start typing indicator
    stop_typing = threading.Event()
    typing_thread = threading.Thread(target=keep_typing, args=(message.chat.id, stop_typing))
    typing_thread.daemon = True
    typing_thread.start()
    processing_message_ids[message.chat.id] = stop_typing

    try:
        threading.Thread(
            target=process_media_file,
            args=(message, stop_typing, file_obj, type_str, processing_reply.message_id) # Pass file_obj directly now
        ).start()
    except Exception as e:
        logging.error(f"Error initiating file processing: {e}")
        stop_typing.set()
        # Delete the "Processing..." message
        try:
            bot.delete_message(message.chat.id, processing_reply.message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message: {delete_e}")
        bot.send_message(message.chat.id, "😓 Sorry, an unexpected error occurred. Please try again.")


def process_media_file(message, stop_typing, file_obj, type_str, processing_message_id):
    """
    Download media, upload to AssemblyAI, transcribe, store stats,
    send transcription, schedule deletion of the transcription after 10 minutes.
    """
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time

    uid_str = str(message.from_user.id)
    processing_start_time = datetime.now()
    transcription = None # Initialize transcription

    try:
        # 1. Download file from Telegram
        file_info = bot.get_file(file_obj.file_id)
        telegram_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

        # Download the file content directly
        response = requests.get(telegram_file_url)
        response.raise_for_status() # Raise an exception for bad status codes
        file_content = response.content

        # 2. Upload to AssemblyAI
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

        # 3. Request transcription from AssemblyAI
        media_lang_name = get_user_media_language_setting_db(uid_str)
        if not media_lang_name: # Handle case where media_language might not be set
             bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=processing_message_id,
                text="⚠️ Transcription failed. Please set your media language using /lange before sending files."
            )
             return # Exit early

        media_lang_code = get_lang_code(media_lang_name)


        if not media_lang_code:
            raise ValueError(f"The language '{media_lang_name}' does not have a valid code for transcription.")

        transcript_request_json = {
            "audio_url": audio_url,
            "language_code": media_lang_code,
            "speech_model": "best" # You can change this to "default" or "best" if needed
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

        # 4. Poll for transcription result
        polling_url = f"{ASSEMBLYAI_TRANSCRIPT_URL}/{transcript_id}"
        while True:
            polling_response = requests.get(polling_url, headers=headers)
            polling_response.raise_for_status()
            polling_result = polling_response.json()

            if polling_result['status'] in ['completed', 'error']:
                break
            time.sleep(2) # Poll every 2 seconds

        if polling_result['status'] == 'completed':
            transcription = polling_result.get("text", "")
        else:
            raise Exception(f"AssemblyAI transcription failed: {polling_result.get('error', 'Unknown error')}")

        # Store transcription in-memory
        user_transcriptions.setdefault(uid_str, {})[message.message_id] = transcription

        # Schedule deletion of this transcription after 10 minutes
        def delete_transcription_later(u_id, msg_id):
            time.sleep(600)
            if u_id in user_transcriptions and msg_id in user_transcriptions[u_id]:
                del user_transcriptions[u_id][msg_id]
                logging.info(f"Deleted transcription for user {u_id}, message {msg_id} after 10 minutes")

        threading.Thread(
            target=delete_transcription_later,
            args=(uid_str, message.message_id),  # FIXED: uid_str instead of uid_s
            daemon=True
        ).start()

        # Update counters (these are in-memory and will reset on bot restart)
        total_files_processed += 1
        if type_str == "voice":
            total_voice_clips += 1
        elif type_str == "audio":
            total_audio_files += 1
        else: # video or video_note or document that's video
            total_videos += 1

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        increment_transcription_count_db(uid_str) # This now updates both in-memory and DB

        # Store processing stat for success
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

        # Build inline buttons for Translate / Summarize
        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
        )

        # Delete the "Processing..." message
        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message: {delete_e}")

        # Send transcription (as file if too long)
        if len(transcription) > 4000:
            import io # Import io for in-memory file
            transcript_file_buffer = io.BytesIO(transcription.encode('utf-8'))
            transcript_file_buffer.name = f"{uuid.uuid4()}_transcription.txt" # Set filename

            bot.send_chat_action(message.chat.id, 'upload_document')
            bot.send_document(
                message.chat.id,
                transcript_file_buffer,
                reply_to_message_id=message.message_id,
                reply_markup=buttons,
                caption="Here’s your transcription. Tap a button below for more options."
            )
            transcript_file_buffer.close() # Close the buffer
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
                "Please choose a different language using /lange or try again with a different file."
             )
        # Delete the "Processing..." message
        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Error deleting 'Processing...' message on error: {delete_e}")
        bot.send_message(message.chat.id, error_message, parse_mode="Markdown")
        # Log failure stat
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
        logging.error(f"Error processing file for user {uid_str}: {e}", exc_info=True) # Added exc_info for full traceback
        # Delete the "Processing..." message
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
        # Log failure stat
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
        # Stop typing indicator
        stop_typing.set()
        if message.chat.id in processing_message_ids:
            del processing_message_ids[message.chat.id]

# --- Language list and helper functions (UPDATED to match bot 2 for AAI) ---

# UPDATED: Reordered and cleaned up LANGUAGES for better display and removal of requested languages
LANGUAGES = {
   "Auto ⚙️": "auto",  # Added "Auto" option
    "English 🇬🇧": "en",
    "العربية 🇸🇦": "ar",
    "Spanish 🇪🇸": "es",
    "French 🇫🇷": "fr",
    "German 🇩🇪": "de",
    "Chinese 🇨🇳": "zh",
    "Japanese 🇯🇵": "ja",
    "Portuguese 🇧🇷": "pt",
    "Russian 🇷🇺": "ru",
    "Turkish 🇹🇷": "tr",
    "हिंदी 🇮🇳": "hi",
    "Somali 🇸🇴": "so", 
    "Italian 🇮🇹": "it",
    "Indonesian 🇮🇩": "id",
    "Vietnamese 🇻🇳": "vi",
    "Thai 🇹🇭": "th",
    "Korean 🇰🇷": "ko",
    "Dutch 🇳🇱": "nl",
    "Polish 🇵🇱": "pl",
    "Swedish 🇸🇪": "sv",
    "Filipino 🇵🇭": "tl",
    "Greek 🇬🇷": "el",
    "Hebrew 🇮🇱": "he",
    "Hungarian 🇭🇺": "hu",
    "Czech 🇨🇿": "cs",
    "Danish 🇩🇰": "da",
    "Finnish 🇫🇮": "fi",
    "Norwegian 🇳🇴": "no",
    "Romanian 🇷🇴": "ro",
    "Slovak 🇸🇰": "sk",
    "Ukrainian 🇺🇦": "uk",
    "Malay 🇲🇾": "ms",
    "Bengali 🇧🇩": "bn",
    "Urdu 🇵🇰": "ur",
    "Nepali 🇳🇵": "ne",
    "Sinhala 🇱🇰": "si",
    "Myanmar 🇲🇲": "my",
    "Georgian 🇬🇪": "ka",
    "Armenian 🇦🇲": "hy",
    "Azerbaijani 🇦🇿": "az",
    "Uzbek 🇺🇿": "uz",
    "Serbian 🇷🇸": "sr",
    "Croatian 🇭🇷": "hr",
    "Slovenian 🇸🇮": "sl",
    "Latvian 🇱🇻": "lv",
    "Lithuanian 🇱🇹": "lt",
    "Amharic 🇪🇹": "am",
    "Swahili 🇰🇪": "sw",
    "Zulu 🇿🇦": "zu",
    "Afrikaans 🇿🇦": "af",
    "Lao 🇱🇦": "lo",
    "فارسی 🇮🇷": "fa", 
}

def get_lang_code(lang_name: str) -> str | None:
    # This now directly uses the LANGUAGES dictionary for lookup
    for key, code in LANGUAGES.items():
        # Match by full key (e.g., "English 🇬🇧") or just the language name (e.g., "English")
        if lang_name.lower() in key.lower():
            return code
    return None


def generate_language_keyboard(callback_prefix: str, message_id: int | None = None):
    """
    Create inline keyboard for selecting languages.
    - If callback_prefix is "summarize_in", only "Auto ⚙️" is shown initially.
      Other languages are assumed to be presented via /sumy.
    - For other prefixes, all languages from LANGUAGES are shown.
    - The "Show Original Transcription" button is removed.
    """
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []

    if callback_prefix == "summarize_in":
        # Only show "Auto" button for direct summarization action if /sumy hasn't been used
        cb_data = f"{callback_prefix}|Auto ⚙️"
        if message_id is not None:
            cb_data += f"|{message_id}"
        buttons.append(InlineKeyboardButton("Auto ⚙️", callback_data=cb_data))
    else:
        # For other actions (media_lang, translate_lang, etc.), show all languages
        for lang_display_name in LANGUAGES.keys():
            if lang_display_name == "Auto ⚙️" and callback_prefix != "summarize_in":
                # Ensure "Auto" is not repeated if it's explicitly added for summarization
                continue
            
            cb_data = f"{callback_prefix}|{lang_display_name}"
            if message_id is not None:
                cb_data += f"|{message_id}"
            buttons.append(InlineKeyboardButton(lang_display_name, callback_data=cb_data))
    
    # Arrange buttons to try for a 4-line appearance (3 buttons per row)
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])

    # The "Show Original Transcription" button is explicitly removed from here.
        
    return markup


# --- UPDATED: TTS VOICES BY LANGUAGE (Removed unwanted languages/voices) ---
TTS_VOICES_BY_LANGUAGE = {

    "Arabic 🇸🇦": [
    "ar-DZ-AminaNeural", "ar-DZ-IsmaelNeural", 
    "ar-BH-AliNeural", "ar-BH-LailaNeural",
    "ar-EG-SalmaNeural", "ar-EG-ShakirNeural",
    "ar-IQ-BasselNeural", "ar-IQ-RanaNeural",
    "ar-JO-SanaNeural", "ar-JO-TaimNeural",
    "ar-KW-FahedNeural", "ar-KW-NouraNeural",
    "ar-LB-LaylaNeural", "ar-LB-RamiNeural",
    "ar-LY-ImanNeural", "ar-LY-OmarNeural",
    "ar-MA-JamalNeural", "ar-MA-MounaNeural",
    "ar-OM-AbdullahNeural", "ar-OM-AyshaNeural",
    "ar-QA-AmalNeural", "ar-QA-MoazNeural",
    "ar-SA-HamedNeural", "ar-SA-ZariyahNeural",
    "ar-SY-AmanyNeural", "ar-SY-LaithNeural",
    "ar-TN-HediNeural", "ar-TN-ReemNeural",
    "ar-AE-FatimaNeural", "ar-AE-HamdanNeural",
    "ar-YE-MaryamNeural", "ar-YE-SalehNeural"
],

# Updated English voices with all requested entries
"English 🇬🇧": [
    "en-AU-NatashaNeural", "en-AU-WilliamNeural",
    "en-CA-ClaraNeural", "en-CA-LiamNeural",
    "en-HK-SamNeural", "en-HK-YanNeural",
    "en-IN-NeerjaNeural", "en-IN-PrabhatNeural",
    "en-IE-ConnorNeural", "en-IE-EmilyNeural",
    "en-KE-AsiliaNeural", "en-KE-ChilembaNeural",
    "en-NZ-MitchellNeural", "en-NZ-MollyNeural",
    "en-NG-AbeoNeural", "en-NG-EzinneNeural",
    "en-PH-James", "en-PH-RosaNeural", # Updated James
    "en-SG-LunaNeural", "en-SG-WayneNeural",
    "en-ZA-LeahNeural", "en-ZA-LukeNeural",
    "en-TZ-ElimuNeural", "en-TZ-ImaniNeural",
    "en-GB-LibbyNeural", "en-GB-MaisieNeural", 
    "en-GB-RyanNeural", "en-GB-SoniaNeural",
    "en-GB-ThomasNeural",
    "en-US-AriaNeural", "en-US-AnaNeural",
    "en-US-ChristopherNeural", "en-US-EricNeural",
    "en-US-GuyNeural", "en-US-JennyNeural",
    "en-US-MichelleNeural", "en-US-RogerNeural",
    "en-US-SteffanNeural"
],

# Updated Spanish voices with all requested entries
"Spanish 🇪🇸": [
    "es-AR-ElenaNeural", "es-AR-TomasNeural",
    "es-BO-MarceloNeural", "es-BO-SofiaNeural",
    "es-CL-CatalinaNeural", "es-CL-LorenzoNeural",
    "es-CO-GonzaloNeural", "es-CO-SalomeNeural",
    "es-CR-JuanNeural", "es-CR-MariaNeural",
    "es-CU-BelkysNeural", "es-CU-ManuelNeural",
    "es-DO-EmilioNeural", "es-DO-RamonaNeural",
    "es-EC-AndreaNeural", "es-EC-LorenaNeural",
    "es-SV-RodrigoNeural", "es-SV-LorenaNeural",
    "es-GQ-JavierNeural", "es-GQ-TeresaNeural",
    "es-GT-AndresNeural", "es-GT-MartaNeural",
    "es-HN-CarlosNeural", "es-HN-KarlaNeural",
    "es-MX-DaliaNeural", "es-MX-JorgeNeural",
    "es-NI-FedericoNeural", "es-NI-YolandaNeural",
    "es-PA-MargaritaNeural", "es-PA-RobertoNeural",
    "es-PY-MarioNeural", "es-PY-TaniaNeural",
    "es-PE-AlexNeural", "es-PE-CamilaNeural",
    "es-PR-KarinaNeural", "es-PR-VictorNeural",
    "es-ES-AlvaroNeural", "es-ES-ElviraNeural",
    "es-US-AlonsoNeural", "es-US-PalomaNeural",
    "es-UY-MateoNeural", "es-UY-ValentinaNeural",
    "es-VE-PaolaNeural", "es-VE-SebastianNeural"
],
    "Hindi 🇮🇳": [ # ONLY Hindi voices (removed Tamil, Telugu, Kannada, Malayalam, Gujarati, Marathi)
        "hi-IN-SwaraNeural", "hi-IN-MadhurNeural"
    ],
    "French 🇫🇷": [
        "fr-FR-DeniseNeural", "fr-FR-HenriNeural", "fr-CA-SylvieNeural", "fr-CA-JeanNeural",
        "fr-CH-ArianeNeural", "fr-CH-FabriceNeural", "fr-BE-GerardNeural" # Fixed typo and added Gerard
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
    "Somali 🇸🇴": [ # Added Somali
        "so-SO-UbaxNeural", "so-SO-MuuseNeural"
    ],
    "Persian 🇮🇷": [ # ADDED PERSIAN
        "fa-IR-DilaraNeural", "fa-IR-ImanNeural"
    ],
}
# Order TTS_VOICES_BY_LANGUAGE keys by priority/common usage for display
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
    "Afrikaans 🇿🇦", "Persian 🇮🇷" # ADDED PERSIAN
]

def make_tts_language_keyboard():
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    # Use the ORDERED_TTS_LANGUAGES to ensure consistent display order
    for lang_name in ORDERED_TTS_LANGUAGES:
        if lang_name in TTS_VOICES_BY_LANGUAGE: # Ensure the language exists in the actual voices dict
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
        InlineKeyboardButton("Fastest", callback_data="pitch_set|+100"), # Changed to use +100 for fastest
        InlineKeyboardButton("Faster", callback_data="pitch_set|+50"),
        InlineKeyboardButton("Normal", callback_data="pitch_set|0"),
        InlineKeyboardButton("Slower", callback_data="pitch_set|-50"),
        InlineKeyboardButton("Slowest", callback_data="pitch_set|-100") # Changed to use -100 for slowest
    )
    return markup

# NEW: Functions for voice rate
def make_rate_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("2x", callback_data="rate_set|+100"), # Max rate is +100
        InlineKeyboardButton("1.5x", callback_data="rate_set|+50"),
        InlineKeyboardButton("Normal", callback_data="rate_set|0"),
        InlineKeyboardButton("0.5x", callback_data="rate_set|-50") # Min rate is -100, but 0.5x is -50
    )
    return markup

@bot.message_handler(commands=['rate']) # RENAMED COMMAND
def cmd_voice_rate(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Deactivate other input modes, activate rate input mode
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

        # Deactivate rate input mode after selection
        user_rate_input_mode[uid] = None

        bot.answer_callback_query(call.id, f"✔️ Rate set to {rate_value} (Preset)")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔊 Rate set to *{rate_value}*. Now you can send text for speech or use /voice to pick a voice.", # RENAMED /text_to_speech
            parse_mode="Markdown"
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid rate value.")
    except Exception as e:
        logging.error(f"Error setting rate from callback: {e}")
        bot.answer_callback_query(call.id, "An error occurred.")
# END NEW: Functions for voice rate

@bot.message_handler(commands=['pitch']) # RENAMED COMMAND
def cmd_voice_pitch(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Deactivate TTS input mode, activate pitch input mode
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = "awaiting_pitch_input"
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

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

    # Deactivate pitch input mode after selection
    user_pitch_input_mode[uid] = None

    try:
        _, pitch_value_str = call.data.split("|", 1)
        pitch_value = int(pitch_value_str)

        set_tts_user_pitch_db(uid, pitch_value)

        # Deactivate pitch input mode after selection
        user_pitch_input_mode[uid] = None

        bot.answer_callback_query(call.id, f"✔️ Pitch set to {pitch_value} (Preset)")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔊 Pitch set to *{pitch_value}*. Now you can send text for speech or use /voice to pick a voice.", # RENAMED /text_to_speech
            parse_mode="Markdown"
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid pitch value.")
    except Exception as e:
        logging.error(f"Error setting pitch from callback: {e}")
        bot.answer_callback_query(call.id, "An error occurred.")

@bot.message_handler(commands=['voice']) # RENAMED COMMAND
def cmd_text_to_speech(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # On /voice, set TTS mode but no voice yet, deactivate pitch/rate input mode
    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None # NEW: Reset rate input mode

    bot.send_message(message.chat.id, "Select the language of the text you want to convert to audio using the menu below:", reply_markup=make_tts_language_keyboard())

@bot.callback_query_handler(lambda c: c.data.startswith("tts_lang|"))
def on_tts_language_select(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    # Deactivate pitch/rate input mode on TTS language selection
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, lang_name = call.data.split("|", 1)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Choose the voice that will read the language{lang_name} you selected using the buttons below :",
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

    # Deactivate pitch/rate input mode on TTS voice selection
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, voice = call.data.split("|", 1)
    set_tts_user_voice_db(uid, voice) # This now updates in-memory and DB

    # Store chosen voice in user_tts_mode to indicate readiness
    user_tts_mode[uid] = voice

    # Get current pitch and rate setting for the message
    current_pitch = get_tts_user_pitch_db(uid)
    current_rate = get_tts_user_rate_db(uid) # NEW: Get current rate

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

    # Going back: reset user_tts_mode (voice no longer selected) and pitch/rate input mode
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

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
    voice = get_tts_user_voice_db(user_id) # This gets from cache
    pitch = get_tts_user_pitch_db(user_id)
    rate = get_tts_user_rate_db(user_id) # NEW: Get current rate
    filename = os.path.join(DOWNLOAD_DIR, f"tts_{user_id}_{uuid.uuid4()}.mp3")

    stop_recording = threading.Event()
    recording_thread = threading.Thread(target=keep_recording, args=(chat_id, stop_recording))
    recording_thread.daemon = True
    recording_thread.start()

    try:
        mss = MSSpeech()
        await mss.set_voice(voice)
        await mss.set_rate(rate) # NEW: Apply rate
        await mss.set_pitch(pitch)
        await mss.set_volume(1.0)

        await mss.synthesize(text, filename)

        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            bot.send_message(chat_id, "❌ MP3 file not generated or empty. Please try again.")
            return

        with open(filename, "rb") as f:
            bot.send_audio(chat_id, f, caption=f"🎤 Voice: {voice}, Pitch: {pitch}, Rate: {rate}") # NEW: Show rate in caption

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

# REMOVED: @bot.message_handler(commands=['language']) and its associated callback_query_handler
# as per the request to remove the /language command and separate translation/summary languages.

@bot.message_handler(commands=['trane']) # RENAMED COMMAND
def select_translation_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF when setting translation language
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    markup = generate_language_keyboard("set_translation_lang") # New callback prefix
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future **translations**:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_translation_lang|")) # New callback prefix
def callback_set_translation_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    # Ensure TTS modes are OFF when setting translation language
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, lang_display_name = call.data.split("|", 1)
    set_user_translation_language_db(uid, lang_display_name) # Uses new function
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Your preferred language for **translations** has been set to: **{lang_display_name}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Translation language set to {lang_display_name}")

@bot.message_handler(commands=['sumy']) # RENAMED COMMAND
def select_summary_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF when setting summary language
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    markup = generate_language_keyboard("set_summary_lang") # New callback prefix
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future **summaries**:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_summary_lang|")) # New callback prefix
def callback_set_summary_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and str(call.from_user.id) != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    # Ensure TTS modes are OFF when setting summary language
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, lang_display_name = call.data.split("|", 1)
    set_user_summary_language_db(uid, lang_display_name) # Uses new function
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Your preferred language for **summaries** has been set to: **{lang_display_name}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Summary language set to {lang_display_name}")


@bot.message_handler(commands=['lange']) # RENAMED COMMAND
def select_media_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF on /lange
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

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

    # Ensure TTS modes are OFF when setting media language
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, lang_display_name = call.data.split("|", 1)
    set_user_media_language_setting_db(uid, lang_display_name) # This now updates in-memory and DB

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

    # Ensure TTS modes are OFF when using translate button
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = get_user_translation_language_db(uid) # Gets from cache (display name)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Translating with your preferred language...")
        threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, preferred_lang, message_id)).start()
    else:
        # Direct display of language buttons
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

    # Ensure TTS modes are OFF when using summarize button
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = get_user_summary_language_db(uid) # Gets from cache (display name) # NEW: using summary lang
    if preferred_lang:
        bot.answer_callback_query(call.id, "Summarizing with your preferred language...")
        threading.Thread(target=do_summarize_with_saved_lang, args=(call.message, uid, preferred_lang, message_id)).start()
    else:
        # Direct display of "Auto" button (only "Auto" is generated for "summarize_in" callback_prefix)
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want the summary in (type /sumy for more options, or press auto):", # RENAMED /summary_language
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

    # Ensure TTS modes are OFF when using translate_to callback
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    parts = call.data.split("|")
    lang_display_name = parts[1] # This is the full display name (e.g., "English 🇬🇧")
    message_id = int(parts[2]) if len(parts) > 2 else None

    set_user_translation_language_db(uid, lang_display_name) # Using translation specific setter

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Translating to **{lang_display_name}**...",
        parse_mode="Markdown"
    )

    if message_id:
        threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, lang_display_name, message_id)).start()
    else:
        # This branch might be rarely hit if message_id is always passed, but good for robustness
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

    # Ensure TTS modes are OFF when using summarize_in callback
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    parts = call.data.split("|")
    lang_display_name = parts[1] # This is the full display name (e.g., "Auto ⚙️")
    message_id = int(parts[2]) if len(parts) > 2 else None

    # Handle "Auto" option for summarization
    if lang_display_name == "Auto ⚙️":
        # Get the original transcription language (media_language)
        transcription_lang = get_user_media_language_setting_db(uid)
        if transcription_lang:
            target_lang_for_gemini = transcription_lang # Use the original media language for summarization
            # Set the summary language preference to "Auto" for future use
            set_user_summary_language_db(uid, lang_display_name)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"Summarizing in the original transcription language (**{transcription_lang}**)...",
                parse_mode="Markdown"
            )
        else:
            bot.answer_callback_query(call.id, "Cannot determine original transcription language. Please select a specific language using /sumy.") # RENAMED /summary_language
            # If "Auto" fails and no media_language is set, inform the user to use /sumy
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="Cannot determine original transcription language. Please use /sumy to select a language, then try again." # RENAMED /summary_language
            )
            return
    else:
        # This branch will primarily be hit if the user selected a language via /sumy and then interacted with a message.
        set_user_summary_language_db(uid, lang_display_name) # Using summary specific setter
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

# REMOVED: @bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_original|"))
# as per the request to remove the "Show Original Transcription" button.

def do_translate_with_saved_lang(message, uid, lang_display_name, message_id):
    """
    Use Gemini to translate saved transcription into lang.
    """
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to translate.")
        return

    # Extract just the language name without the flag for Gemini prompt
    lang_name_only = lang_display_name.split(' ')[0]

    prompt = (
        f"Translate the following text into {lang_name_only}. Provide only the translated text, "
        f"with no additional notes, explanations, or introductory/concluding remarks:\n\n{original}"
    )
    bot.send_chat_action(message.chat.id, 'typing')
    # ask_gemini stores history in user_memory, which is in-memory
    translated = ask_gemini(uid, prompt)

    if translated.startswith("Error:"):
        bot.send_message(message.chat.id, f"😓 Sorry, an error occurred during translation: {translated}. Please try again later.")
        return

    if len(translated) > 4000:
        import io # Import io for in-memory file
        translation_file_buffer = io.BytesIO(translated.encode('utf-8'))
        translation_file_buffer.name = f"{uuid.uuid4()}_translation.txt" # Set filename

        bot.send_chat_action(message.chat.id, 'upload_document')
        bot.send_document(message.chat.id, translation_file_buffer, caption=f"Translation to {lang_display_name}", reply_to_message_id=message_id)
        translation_file_buffer.close() # Close the buffer
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

    # Determine the target language for Gemini's prompt
    if lang_display_name == "Auto ⚙️":
        # Get the original transcription language (media_language) to use for summarization
        transcription_lang = get_user_media_language_setting_db(uid)
        if not transcription_lang:
            # Fallback if original media language isn't set, though callback_summarize_in tries to prevent this
            target_lang_for_gemini = "English" # Default to English if auto fails
            bot.send_message(message.chat.id, "⚠️ Could not determine original transcription language for 'Auto' summary. Summarizing in English.")
        # Extract just the language name without the flag for Gemini prompt if it has one
        lang_name_for_prompt = transcription_lang.split(' ')[0]
    else:
        # Extract just the language name without the flag for Gemini prompt
        lang_name_for_prompt = lang_display_name.split(' ')[0]

    prompt = (
        f"Summarize the following text in {lang_name_for_prompt}. Provide only the summarized text, "
        f"with no additional notes, explanations, or different versions:\n\n{original}"
    )
    bot.send_chat_action(message.chat.id, 'typing')
    # ask_gemini stores history in user_memory, which is in-memory
    summary = ask_gemini(uid, prompt)

    if summary.startswith("Error:"):
        bot.send_message(chat_id=message.chat.id, text=f"😓 Sorry, an error occurred during summarization: {summary}. Please try again later.")
        return

    # The "Show Original" button is explicitly removed from here.

    if len(summary) > 4000:
        import io # Import io for in-memory file
        summary_file_buffer = io.BytesIO(summary.encode('utf-8'))
        summary_file_buffer.name = f"{uuid.uuid4()}_summary.txt" # Set filename

        bot.send_chat_action(message.chat.id, 'upload_document')
        bot.send_document(message.chat.id, summary_file_buffer, caption=f"Summary in {lang_display_name}", reply_to_message_id=message_id)
        summary_file_buffer.close() # Close the buffer
    else:
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)

@bot.message_handler(commands=['translate'])
def handle_translate(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF on /translate
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    # Check for replied message first. If not a reply or transcription not found, directly show buttons.
    transcription_message_id = None
    if message.reply_to_message and uid in user_transcriptions and message.reply_to_message.message_id in user_transcriptions[uid]:
        transcription_message_id = message.reply_to_message.message_id

    # If transcription exists, try to translate with saved language, otherwise show language selection.
    if transcription_message_id:
        preferred_lang = get_user_translation_language_db(uid) # Gets from cache (display name)
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
        # No reply or transcription not found, show language selection directly without error.
        markup = generate_language_keyboard("translate_to") # No message_id here as we don't have a specific transcription yet
        bot.send_message(
            message.chat.id,
            "Please select the language you want to translate into, then reply to a transcription message with /translate to use it:",
            reply_markup=markup
        )

@bot.message_handler(commands=['summarize'])
def handle_summarize(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF on /summarize
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode

    # Check for replied message first. If not a reply or transcription not found, directly show buttons.
    transcription_message_id = None
    if message.reply_to_message and uid in user_transcriptions and message.reply_to_message.message_id in user_transcriptions[uid]:
        transcription_message_id = message.reply_to_message.message_id

    # If transcription exists, try to summarize with saved language, otherwise show language selection.
    if transcription_message_id:
        preferred_lang = get_user_summary_language_db(uid) # Gets from cache (display name)
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
        # No reply or transcription not found, show "Auto" option only by default.
        # User will be prompted to use /sumy for full list.
        markup = generate_language_keyboard("summarize_in", message.message_id) # Pass current message_id for the "Auto" button
        bot.send_message(
            message.chat.id,
            "Please select 'Auto' for summary, or use /sumy to set your preferred summary language for future use:", # RENAMED /summary_language
            reply_markup=markup
        )

@bot.message_handler(func=lambda message: message.content_type == 'text' and not message.text.startswith('/'))
def handle_text_for_tts_or_fallback(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Check if the user is in the "awaiting rate input" state
    if user_rate_input_mode.get(uid) == "awaiting_rate_input":
        try:
            rate_val = int(message.text)
            if -100 <= rate_val <= 100:
                set_tts_user_rate_db(uid, rate_val)
                bot.send_message(message.chat.id, f"🔊 Voice rate set to *{rate_val}*.", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid rate. Please enter a number from -100 to +100 or 0 for reset.")
            user_rate_input_mode[uid] = None # Reset the state
            return
        except ValueError:
            # If it's not a valid number, it's not a rate command, so fall through to pitch, then TTS or fallback
            pass

    # Check if the user is in the "awaiting pitch input" state
    if user_pitch_input_mode.get(uid) == "awaiting_pitch_input":
        try:
            pitch_val = int(message.text)
            if -100 <= pitch_val <= 100:
                set_tts_user_pitch_db(uid, pitch_val)
                bot.send_message(message.chat.id, f"🔊 Voice pitch set to *{pitch_val}*.", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid pitch. Please enter a number from -100 to +100 or 0 for reset.")
            user_pitch_input_mode[uid] = None # Reset the state
            return
        except ValueError:
            # If it's not a valid number, it's not a pitch command, so fall through to TTS or fallback
            pass


    # If user_tts_mode.get(uid) is set (voice chosen), or user has a saved voice, do TTS
    if user_tts_mode.get(uid):
        threading.Thread(
            target=lambda: asyncio.run(synth_and_send_tts(message.chat.id, uid, message.text))
        ).start()
    else:
        # Check if user has a saved voice in DB (and it was loaded to cache)
        saved_voice = get_tts_user_voice_db(uid) # This gets from cache
        if saved_voice != "en-US-AriaNeural":
            # if it was saved to something else, reactivate TTS mode
            user_tts_mode[uid] = saved_voice
            threading.Thread(
                target=lambda: asyncio.run(synth_and_send_tts(message.chat.id, uid, message.text))
            ).start()
        else:
            bot.send_message(
                message.chat.id,
                "I only transcribe voice messages, audio files, video notes, or video files. "
                "To convert text to speech, use the /voice command first." # RENAMED /text_to_speech
            )

@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'document'])
def fallback_non_text_or_media(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    # Ensure TTS modes are OFF when a non-text/non-media message is sent
    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None # NEW: Reset rate input mode


    # Handle document types that are NOT audio/video
    if message.document and not (message.document.mime_type.startswith("audio/") or message.document.mime_type.startswith("video/")):
        bot.send_message(
            message.chat.id,
            "Please send only voice messages, audio files, video notes, or video files for transcription, "
            "or use `/voice` for text to speech. The document type you sent is not supported for transcription." # RENAMED /text_to_speech
        )
        return

    # Fallback for other unsupported content types like photos, stickers if not caught by specific handlers
    bot.send_message(
        message.chat.id,
        "I only transcribe voice messages, audio files, video notes, or video files for transcription, "
        "or use `/voice` for text to speech. Please send a supported type." # RENAMED /text_to_speech
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
        # BotCommand("language", "Change your preferred language for translations and summaries."), # REMOVED
        BotCommand("trane", "Set translation language for translate button"), # NEW
        BotCommand("sumy", "Set summary language for summarie bottom "), # NEW
        BotCommand("lange", "Set the language of the audio in your media files for transcription."),
        BotCommand("voice", "Set Text to voice language & voice "),
        BotCommand("pitch", "Adjust voice pitch"),
        BotCommand("rate", "Adjust voice speed"), # NEW command
        BotCommand("help", "How to use the bot"), # UNCOMMENTED
        BotCommand("privacy", "Read privacy notice"),
        #BotCommand("translate", "Translate a replied-to transcription"), # UNCOMMENTED
        #BotCommand("summarize", "Summarize a replied-to transcription") # UNCOMMENTED
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
    set_bot_commands() # NEW: Call to set bot commands

if __name__ == "__main__":
    set_bot_info_and_startup()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

