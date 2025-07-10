import os
import uuid
import logging
import requests
import telebot
import json
from flask import Flask, request, abort
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ReplyKeyboardMarkup
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
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")
    ]
)

# --- BOT CONFIGURATION ---
TOKEN = "7790991731:AAFgEjc6fO-iTSSkpt3lEJBH86gQY5nIgAw"
ADMIN_ID = 5978150981
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com"

REQUIRED_CHANNEL = "@transcriber_bot_news_channel"

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- AssemblyAI Configuration ---
ASSEMBLYAI_API_KEY = "6dab0a0669624f44afa50d679242e473"
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
tts_users_collection = None
processing_stats_collection = None

# --- In-memory caches ---
local_user_data = {}
_user_translation_language_cache = {}
_user_summary_language_cache = {}
_media_language_cache = {}
_tts_voice_cache = {}
_tts_pitch_cache = {}
_tts_rate_cache = {}

# --- User states ---
user_tts_mode = {}
user_pitch_input_mode = {}
user_rate_input_mode = {}

# --- Statistics counters ---
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0.0
bot_start_time = datetime.now()

# Admin uptime message storage
admin_uptime_message = {}
admin_uptime_lock = threading.Lock()

# --- User transcription cache ---
user_transcriptions = {}
GEMINI_API_KEY = "AIzaSyCHrGhRKXAp3DuQGH8HLB60ggryZeUFA9E"
user_memory = {}

# ========================================
# LANGUAGES CONFIGURATION (AUTONYMS)
# ========================================
LANGUAGES = {
    "Auto ⚙️": "auto",
    "English": "en",
    "العربية": "ar",
    "Español": "es",
    "Français": "fr",
    "Deutsch": "de",
    "中文": "zh",
    "日本語": "ja",
    "Português": "pt",
    "Русский": "ru",
    "Türkçe": "tr",
    "हिन्दी": "hi",
    "Soomaali": "so",
    "Italiano": "it",
    "Bahasa Indonesia": "id",
    "Tiếng Việt": "vi",
    "ไทย": "th",
    "한국어": "ko",
    "Nederlands": "nl",
    "Polski": "pl",
    "Svenska": "sv",
    "Filipino": "tl",
    "Ελληνικά": "el",
    "עברית": "he",
    "Magyar": "hu",
    "Čeština": "cs",
    "Dansk": "da",
    "Suomi": "fi",
    "Norsk": "no",
    "Română": "ro",
    "Slovenčina": "sk",
    "Українська": "uk",
    "Bahasa Melayu": "ms",
    "বাংলা": "bn",
    "اردو": "ur",
    "नेपाली": "ne",
    "සිංහල": "si",
    "မြန်မာ": "my",
    "ქართული": "ka",
    "Հայերեն": "hy",
    "Azərbaycanca": "az",
    "Oʻzbekcha": "uz",
    "Српски": "sr",
    "Hrvatski": "hr",
    "Slovenščina": "sl",
    "Latviešu": "lv",
    "Lietuvių": "lt",
    "አማርኛ": "am",
    "Kiswahili": "sw",
    "isiZulu": "zu",
    "Afrikaans": "af",
    "ລາວ": "lo",
    "فارسی": "fa"
}

def get_lang_code(lang_name: str) -> str | None:
    for key, code in LANGUAGES.items():
        if lang_name.lower() in key.lower():
            return code
    return None

# ========================================
# TTS VOICES CONFIGURATION
# ========================================
TTS_VOICES_BY_LANGUAGE = {
    "العربية": ["ar-DZ-AminaNeural", "ar-EG-SalmaNeural"],
    "English": ["en-US-AriaNeural", "en-GB-LibbyNeural"],
    "Español": ["es-ES-ElviraNeural", "es-MX-DaliaNeural"],
    "Français": ["fr-FR-DeniseNeural", "fr-CA-SylvieNeural"],
    "Deutsch": ["de-DE-KatjaNeural", "de-CH-LeniNeural"],
    "中文": ["zh-CN-XiaoxiaoNeural", "zh-TW-HsiaoChenNeural"],
    "日本語": ["ja-JP-NanamiNeural", "ja-JP-KeitaNeural"],
    "Português": ["pt-BR-FranciscaNeural", "pt-PT-RaquelNeural"],
    "Русский": ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"],
    "Türkçe": ["tr-TR-EmelNeural", "tr-TR-AhmetNeural"],
    "हिन्दी": ["hi-IN-SwaraNeural", "hi-IN-MadhurNeural"],
    "Soomaali": ["so-SO-UbaxNeural", "so-SO-MuuseNeural"],
    "Italiano": ["it-IT-ElsaNeural", "it-IT-DiegoNeural"],
    "Bahasa Indonesia": ["id-ID-GadisNeural", "id-ID-ArdiNeural"],
    "Tiếng Việt": ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
    "ไทย": ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"],
    "한국어": ["ko-KR-SunHiNeural", "ko-KR-InJoonNeural"],
    "Nederlands": ["nl-NL-ColetteNeural", "nl-NL-MaartenNeural"],
    "Polski": ["pl-PL-ZofiaNeural", "pl-PL-MarekNeural"],
    "Svenska": ["sv-SE-SofieNeural", "sv-SE-MattiasNeural"],
    "Filipino": ["fil-PH-BlessicaNeural", "fil-PH-AngeloNeural"],
    "Ελληνικά": ["el-GR-AthinaNeural", "el-GR-NestorasNeural"],
    "עברית": ["he-IL-HilaNeural", "he-IL-AvriNeural"],
    "Magyar": ["hu-HU-NoemiNeural", "hu-HU-AndrasNeural"],
    "Čeština": ["cs-CZ-VlastaNeural", "cs-CZ-AntoninNeural"],
    "Dansk": ["da-DK-ChristelNeural", "da-DK-JeppeNeural"],
    "Suomi": ["fi-FI-SelmaNeural", "fi-FI-HarriNeural"],
    "Norsk": ["nb-NO-PernilleNeural", "nb-NO-FinnNeural"],
    "Română": ["ro-RO-AlinaNeural", "ro-RO-EmilNeural"],
    "Slovenčina": ["sk-SK-ViktoriaNeural", "sk-SK-LukasNeural"],
    "Українська": ["uk-UA-PolinaNeural", "uk-UA-OstapNeural"],
    "Bahasa Melayu": ["ms-MY-YasminNeural", "ms-MY-OsmanNeural"],
    "বাংলা": ["bn-BD-NabanitaNeural", "bn-BD-BasharNeural"],
    "اردو": ["ur-PK-AsmaNeural", "ur-PK-FaizanNeural"],
    "नेपाली": ["ne-NP-SaritaNeural", "ne-NP-AbhisekhNeural"],
    "සිංහල": ["si-LK-ThiliniNeural", "si-LK-SameeraNeural"],
    "မြန်မာ": ["my-MM-NilarNeural", "my-MM-ThihaNeural"],
    "ქართული": ["ka-GE-EkaNeural", "ka-GE-GiorgiNeural"],
    "Հայերեն": ["hy-AM-AnahitNeural", "hy-AM-AraratNeural"],
    "Azərbaycanca": ["az-AZ-BanuNeural", "az-AZ-BabekNeural"],
    "Oʻzbekcha": ["uz-UZ-MadinaNeural", "uz-UZ-SuhrobNeural"],
    "Српски": ["sr-RS-SophieNeural", "sr-RS-NikolaNeural"],
    "Hrvatski": ["hr-HR-GabrijelaNeural", "hr-HR-SreckoNeural"],
    "Slovenščina": ["sl-SI-PetraNeural", "sl-SI-RokNeural"],
    "Latviešu": ["lv-LV-EveritaNeural", "lv-LV-AnsisNeural"],
    "Lietuvių": ["lt-LT-OnaNeural", "lt-LT-LeonasNeural"],
    "አማርኛ": ["am-ET-MekdesNeural", "am-ET-AbebeNeural"],
    "Kiswahili": ["sw-KE-ZuriNeural", "sw-KE-RafikiNeural"],
    "isiZulu": ["zu-ZA-ThandoNeural", "zu-ZA-ThembaNeural"],
    "Afrikaans": ["af-ZA-AdriNeural", "af-ZA-WillemNeural"],
    "ລາວ": ["lo-LA-KeomanyNeural", "lo-LA-ChanthavongNeural"],
    "فارسی": ["fa-IR-DilaraNeural", "fa-IR-ImanNeural"]
}

ORDERED_TTS_LANGUAGES = [
    "English", "العربية", "Español", "Français", "Deutsch",
    "中文", "日本語", "Português", "Русский", "Türkçe",
    "हिन्दी", "Soomaali", "Italiano", "Bahasa Indonesia", "Tiếng Việt",
    "ไทย", "한국어", "Nederlands", "Polski", "Svenska",
    "Filipino", "Ελληνικά", "עברית", "Magyar", "Čeština",
    "Dansk", "Suomi", "Norsk", "Română", "Slovenčina",
    "Українська", "Bahasa Melayu", "বাংলা", "اردو", "नेपाली",
    "සිංහල", "ລາວ", "မြန်မာ", "ქართული", "Հայերեն",
    "Azərbaycanca", "Oʻzbekcha", "Српски", "Hrvatski", "Slovenščina",
    "Latviešu", "Lietuvių", "አማርኛ", "Kiswahili", "isiZulu",
    "Afrikaans", "فارسی"
]

# ========================================
# MODERN UI COMPONENTS
# ========================================
def generate_main_menu():
    """Create modern main menu with ReplyKeyboardMarkup"""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "🎤 Media Language", "🔊 Text-to-Speech",
        "🌐 Translate Language", "📝 Summary Language",
        "👤 User Info", "ℹ️ Help"
    ]
    markup.add(*buttons)
    return markup

def generate_language_keyboard(callback_prefix: str, message_id: int | None = None):
    """Create modern inline keyboard for language selection"""
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    
    for lang_display_name in LANGUAGES.keys():
        cb_data = f"{callback_prefix}|{lang_display_name}"
        if message_id is not None:
            cb_data += f"|{message_id}"
        buttons.append(InlineKeyboardButton(lang_display_name, callback_data=cb_data))
    
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])
        
    return markup

def make_tts_language_keyboard():
    """Modern TTS language keyboard with autonyms"""
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    
    for lang_name in ORDERED_TTS_LANGUAGES:
        buttons.append(
            InlineKeyboardButton(lang_name, callback_data=f"tts_lang|{lang_name}")
        )
    
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])
        
    return markup

def make_tts_voice_keyboard_for_language(lang_name: str):
    """Voice selection keyboard for a specific language"""
    markup = InlineKeyboardMarkup(row_width=2)
    voices = TTS_VOICES_BY_LANGUAGE.get(lang_name, [])
    
    for voice in voices:
        markup.add(InlineKeyboardButton(voice, callback_data=f"tts_voice|{voice}"))
    
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="tts_back_to_languages"))
    return markup

def make_pitch_keyboard():
    """Pitch adjustment keyboard"""
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("High", callback_data="pitch_set|+50"),
        InlineKeyboardButton("Normal", callback_data="pitch_set|0"),
        InlineKeyboardButton("Low", callback_data="pitch_set|-50")
    )
    return markup

def make_rate_keyboard():
    """Speech rate adjustment keyboard"""
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("Fast", callback_data="rate_set|+50"),
        InlineKeyboardButton("Normal", callback_data="rate_set|0"),
        InlineKeyboardButton("Slow", callback_data="rate_set|-50")
    )
    return markup

# ========================================
# DATABASE FUNCTIONS
# ========================================
def connect_to_mongodb():
    global mongo_client, db
    global users_collection, translation_language_settings_collection
    global summary_language_settings_collection, media_language_settings_collection
    global tts_users_collection, processing_stats_collection
    global local_user_data, _user_translation_language_cache
    global _user_summary_language_cache, _media_language_cache
    global _tts_voice_cache, _tts_pitch_cache, _tts_rate_cache

    try:
        mongo_client = MongoClient(MONGO_URI)
        mongo_client.admin.command('ismaster')
        db = mongo_client[DB_NAME]
        users_collection = db["users"]
        translation_language_settings_collection = db["user_translation_language_settings"]
        summary_language_settings_collection = db["user_summary_language_settings"]
        media_language_settings_collection = db["user_media_language_settings"]
        tts_users_collection = db["tts_users"]
        processing_stats_collection = db["file_processing_stats"]

        # Create indexes
        users_collection.create_index([("last_active", ASCENDING)])
        translation_language_settings_collection.create_index([("_id", ASCENDING)])
        summary_language_settings_collection.create_index([("_id", ASCENDING)])
        media_language_settings_collection.create_index([("_id", ASCENDING)])
        tts_users_collection.create_index([("_id", ASCENDING)])
        processing_stats_collection.create_index([("user_id", ASCENDING)])

        # Load data into memory
        for user_doc in users_collection.find({}):
            local_user_data[user_doc["_id"]] = user_doc
            
        for lang_setting in translation_language_settings_collection.find({}):
            _user_translation_language_cache[lang_setting["_id"]] = lang_setting.get("language")
            
        for lang_setting in summary_language_settings_collection.find({}):
            _user_summary_language_cache[lang_setting["_id"]] = lang_setting.get("language")
            
        for media_lang_setting in media_language_settings_collection.find({}):
            _media_language_cache[media_lang_setting["_id"]] = media_lang_setting.get("media_language")
            
        for tts_user in tts_users_collection.find({}):
            _tts_voice_cache[tts_user["_id"]] = tts_user.get("voice", "en-US-AriaNeural")
            _tts_pitch_cache[tts_user["_id"]] = tts_user.get("pitch", 0)
            _tts_rate_cache[tts_user["_id"]] = tts_user.get("rate", 0)

        logging.info("MongoDB connected and data loaded to memory")

    except ConnectionFailure as e:
        logging.error(f"MongoDB connection failed: {e}")
        exit(1)
    except Exception as e:
        logging.error(f"Error during MongoDB connection: {e}")
        exit(1)

def update_user_activity_db(user_id: int):
    user_id_str = str(user_id)
    now_iso = datetime.now().isoformat()

    if user_id_str not in local_user_data:
        local_user_data[user_id_str] = {
            "_id": user_id_str,
            "last_active": now_iso,
            "transcription_count": 0
        }
    else:
        local_user_data[user_id_str]["last_active"] = now_iso

    try:
        users_collection.update_one(
            {"_id": user_id_str},
            {"$set": {"last_active": now_iso}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error updating user activity: {e}")

def get_user_data_db(user_id: str) -> dict | None:
    if user_id in local_user_data:
        return local_user_data[user_id]
    try:
        doc = users_collection.find_one({"_id": user_id})
        if doc:
            local_user_data[user_id] = doc
        return doc
    except Exception as e:
        logging.error(f"Error fetching user data: {e}")
        return None

def increment_transcription_count_db(user_id: str):
    now_iso = datetime.now().isoformat()

    if user_id not in local_user_data:
        local_user_data[user_id] = {
            "_id": user_id,
            "last_active": now_iso,
            "transcription_count": 1
        }
    else:
        local_user_data[user_id]["transcription_count"] = local_user_data[user_id].get("transcription_count", 0) + 1
        local_user_data[user_id]["last_active"] = now_iso

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
        logging.error(f"Error incrementing transcription count: {e}")

def get_user_translation_language_db(user_id: str) -> str | None:
    return _user_translation_language_cache.get(user_id)

def set_user_translation_language_db(user_id: str, lang: str):
    _user_translation_language_cache[user_id] = lang
    try:
        translation_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting translation language: {e}")

def get_user_summary_language_db(user_id: str) -> str | None:
    return _user_summary_language_cache.get(user_id)

def set_user_summary_language_db(user_id: str, lang: str):
    _user_summary_language_cache[user_id] = lang
    try:
        summary_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting summary language: {e}")

def get_user_media_language_setting_db(user_id: str) -> str | None:
    return _media_language_cache.get(user_id)

def set_user_media_language_setting_db(user_id: str, lang: str):
    _media_language_cache[user_id] = lang
    try:
        media_language_settings_collection.update_one(
            {"_id": user_id},
            {"$set": {"media_language": lang}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting media language: {e}")

def get_tts_user_voice_db(user_id: str) -> str:
    return _tts_voice_cache.get(user_id, "en-US-AriaNeural")

def set_tts_user_voice_db(user_id: str, voice: str):
    _tts_voice_cache[user_id] = voice
    try:
        tts_users_collection.update_one(
            {"_id": user_id},
            {"$set": {"voice": voice}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS voice: {e}")

def get_tts_user_pitch_db(user_id: str) -> int:
    return _tts_pitch_cache.get(user_id, 0)

def set_tts_user_pitch_db(user_id: str, pitch: int):
    _tts_pitch_cache[user_id] = pitch
    try:
        tts_users_collection.update_one(
            {"_id": user_id},
            {"$set": {"pitch": pitch}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS pitch: {e}")

def get_tts_user_rate_db(user_id: str) -> int:
    return _tts_rate_cache.get(user_id, 0)

def set_tts_user_rate_db(user_id: str, rate: int):
    _tts_rate_cache[user_id] = rate
    try:
        tts_users_collection.update_one(
            {"_id": user_id},
            {"$set": {"rate": rate}},
            upsert=True
        )
    except Exception as e:
        logging.error(f"Error setting TTS rate: {e}")

# ========================================
# UTILITY FUNCTIONS
# ========================================
def keep_typing(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing')
            time.sleep(4)
        except Exception as e:
            logging.error(f"Typing error: {e}")
            break

def keep_recording(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'record_audio')
            time.sleep(4)
        except Exception as e:
            logging.error(f"Recording error: {e}")
            break

def update_uptime_message(chat_id, message_id):
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
                logging.error(f"Uptime error: {e}")
            break
        except Exception as e:
            logging.error(f"Uptime thread error: {e}")
            break

def check_subscription(user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Subscription check error: {e}")
        return False

def send_subscription_message(chat_id: int):
    if bot.get_chat(chat_id).type == 'private':
        if not REQUIRED_CHANNEL:
            return
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton(
                "Join Channel",
                url=f"https://t.me/{REQUIRED_CHANNEL[1:]}"
            )
        )
        bot.send_message(
            chat_id,
            """🔒 Access Restricted\n\nPlease join our channel to use this bot:\n@transcriber_bot_news_channel\n\nJoin and send /start again.""",
            reply_markup=markup,
            disable_web_page_preview=True
        )

def ask_gemini(user_id, user_message):
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    
    system_instruction_part = {"text": "For translations, use only a comma (,) as a pause symbol."}
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

FILE_SIZE_LIMIT = 20 * 1024 * 1024
admin_state = {}
processing_message_ids = {}

# ========================================
# BOT COMMAND HANDLERS (MODERN UI)
# ========================================
@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id_str = str(message.from_user.id)

    if user_id_str not in local_user_data:
        local_user_data[user_id_str] = {
            "_id": user_id_str,
            "last_active": datetime.now().isoformat(),
            "transcription_count": 0
        }
        try:
            users_collection.insert_one(local_user_data[user_id_str])
        except Exception as e:
            logging.error(f"New user error: {e}")
    else:
        update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and user_id_str != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[user_id_str] = None
    user_pitch_input_mode[user_id_str] = None
    user_rate_input_mode[user_id_str] = None

    if message.from_user.id == ADMIN_ID:
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("📊 Statistics", "📣 Broadcast", "👥 User Count")
        sent_message = bot.send_message(
            message.chat.id,
            "🛠️ **Admin Dashboard**\n\nBot status and controls:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        with admin_uptime_lock:
            if admin_uptime_message.get(ADMIN_ID) and admin_uptime_message[ADMIN_ID].get('thread') and admin_uptime_message[ADMIN_ID]['thread'].is_alive():
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
        welcome_msg = (
            "🌟 **Welcome to Media Transcriber Bot!**\n\n"
            "I can transcribe audio/video, translate text, summarize content, "
            "and convert text to speech.\n\n"
            "Use the menu below to get started:"
        )
        bot.send_message(
            message.chat.id,
            welcome_msg,
            parse_mode="Markdown",
            reply_markup=generate_main_menu()
        )

@bot.message_handler(commands=['help'])
@bot.message_handler(func=lambda m: m.text == "ℹ️ Help")
def help_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and user_id != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    help_text = (
        "📘 **Quick Guide**\n\n"
        "1. **Transcribe Media**\n"
        "   - Send voice/audio/video files\n"
        "   - Set language first with \"🎤 Media Language\"\n\n"
        "2. **Translate Text**\n"
        "   - Use \"🌐 Translate Language\" to set target\n"
        "   - Reply to transcription with /translate\n\n"
        "3. **Summarize Content**\n"
        "   - Use \"📝 Summary Language\" to set target\n"
        "   - Reply to transcription with /summarize\n\n"
        "4. **Text-to-Speech**\n"
        "   - Use \"🔊 Text-to-Speech\" to configure\n"
        "   - Then send any text to convert\n\n"
        "5. **User Settings**\n"
        "   - View your settings with \"👤 User Info\"\n\n"
        "🛠️ Need help? Contact @user33230"
    )
    
    bot.send_message(
        message.chat.id, 
        help_text,
        parse_mode="Markdown",
        reply_markup=generate_main_menu()
    )

@bot.message_handler(commands=['Langstt'])
@bot.message_handler(func=lambda m: m.text == "🎤 Media Language")
def select_media_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "🗣️ **Set Media Language**\n\nSelect the language of your audio/video files:",
        parse_mode="Markdown",
        reply_markup=generate_language_keyboard("set_media_lang")
    )

@bot.message_handler(commands=['Langtts'])
@bot.message_handler(func=lambda m: m.text == "🔊 Text-to-Speech")
def cmd_text_to_speech(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and user_id != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    bot.send_message(
        message.chat.id,
        "🔊 **Text-to-Speech Settings**\n\nSelect language for voice conversion:",
        parse_mode="Markdown",
        reply_markup=make_tts_language_keyboard()
    )

@bot.message_handler(commands=['bottomtrnlang'])
@bot.message_handler(func=lambda m: m.text == "🌐 Translate Language")
def select_translation_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "🌍 **Set Translation Language**\n\nSelect your preferred translation language:",
        parse_mode="Markdown",
        reply_markup=generate_language_keyboard("set_translation_lang")
    )

@bot.message_handler(commands=['bottomsumlang'])
@bot.message_handler(func=lambda m: m.text == "📝 Summary Language")
def select_summary_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "📄 **Set Summary Language**\n\nSelect your preferred summary language:",
        parse_mode="Markdown",
        reply_markup=generate_language_keyboard("set_summary_lang")
    )

@bot.message_handler(commands=['userinfo'])
@bot.message_handler(func=lambda m: m.text == "👤 User Info")
def cmd_userinfo(message):
    uid_str = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid_str != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_data = get_user_data_db(uid_str) or {}
    media_lang = get_user_media_language_setting_db(uid_str) or "Not set"
    trans_lang = get_user_translation_language_db(uid_str) or "Not set"
    summary_lang = get_user_summary_language_db(uid_str) or "Not set"
    tts_voice = get_tts_user_voice_db(uid_str) or "Not set"
    tts_pitch = get_tts_user_pitch_db(uid_str)
    tts_rate = get_tts_user_rate_db(uid_str)
    
    user_info = (
        "👤 **Your Profile**\n\n"
        f"🆔 User ID: `{uid_str}`\n"
        f"🕒 Last Active: `{user_data.get('last_active', 'Unknown')}`\n"
        f"📊 Transcription Count: `{user_data.get('transcription_count', 0)}`\n\n"
        "⚙️ **Language Settings**\n"
        f"🎤 Media Language: `{media_lang}`\n"
        f"🌐 Translation Language: `{trans_lang}`\n"
        f"📝 Summary Language: `{summary_lang}`\n\n"
        "🔊 **TTS Settings**\n"
        f"🗣️ Voice: `{tts_voice}`\n"
        f"🎚️ Pitch: `{tts_pitch}`\n"
        f"⏩ Rate: `{tts_rate}`"
    )
    
    bot.send_message(
        message.chat.id,
        user_info,
        parse_mode="Markdown",
        reply_markup=generate_main_menu()
    )

@bot.message_handler(commands=['status'])
@bot.message_handler(func=lambda m: m.text == "📊 Statistics" and m.from_user.id == ADMIN_ID)
def status_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and user_id != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[user_id] = None
    user_pitch_input_mode[user_id] = None
    user_rate_input_mode[user_id] = None

    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    today_iso = datetime.now().date().isoformat()
    active_today_count = sum(1 for user_doc in local_user_data.values() if user_doc.get("last_active", "").startswith(today_iso))
    total_registered_users = len(local_user_data)

    try:
        total_processed_media = processing_stats_collection.count_documents({"type": {"$in": ["voice", "audio", "video"]}})
        voice_count = processing_stats_collection.count_documents({"type": "voice"})
        audio_count = processing_stats_collection.count_documents({"type": "audio"})
        video_count = processing_stats_collection.count_documents({"type": "video"})
        total_tts_conversions = processing_stats_collection.count_documents({"type": "tts"})

        pipeline = [{"$group": {"_id": None, "total_time": {"$sum": "$processing_time"}}}]
        agg_result = list(processing_stats_collection.aggregate(pipeline))
        total_proc_seconds = agg_result[0]["total_time"] if agg_result else 0
    except Exception as e:
        logging.error(f"Stats error: {e}")
        total_processed_media = voice_count = audio_count = video_count = 0
        total_tts_conversions = 0
        total_proc_seconds = 0

    proc_hours = int(total_proc_seconds) // 3600
    proc_minutes = (int(total_proc_seconds) % 3600) // 60
    proc_seconds = int(total_proc_seconds) % 60

    text = (
        "📊 **Bot Statistics**\n\n"
        "🟢 Status: Online\n"
        f"⏱️ Uptime: {days}d {hours:02d}h {minutes:02d}m {seconds:02d}s\n\n"
        "👥 Users\n"
        f"▫️ Today: {active_today_count}\n"
        f"▫️ Total: {total_registered_users}\n\n"
        "⚙️ Processing\n"
        f"▫️ Media Files: {total_processed_media}\n"
        f"▫️ Voice: {voice_count}\n"
        f"▫️ Audio: {audio_count}\n"
        f"▫️ Video: {video_count}\n"
        f"▫️ TTS Conversions: {total_tts_conversions}\n"
        f"⏱️ Processing Time: {proc_hours}h {proc_minutes}m {proc_seconds}s\n\n"
        "🚀 Thank you for using our service!"
    )

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "👥 User Count" and m.from_user.id == ADMIN_ID)
def total_users(message):
    total_registered = len(local_user_data)
    bot.send_message(message.chat.id, f"👥 Total users: {total_registered}")

@bot.message_handler(func=lambda m: m.text == "📣 Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast_prompt(message):
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "📢 Send broadcast message:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
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
            logging.error(f"Broadcast error: {e}")
            fail += 1
        time.sleep(0.05)

    bot.send_message(
        message.chat.id,
        f"📣 Broadcast Complete\n\n✅ Success: {success}\n❌ Failed: {fail}"
    )

# ========================================
# MEDIA PROCESSING HANDLERS
# ========================================
@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note', 'document'])
def handle_file(message):
    uid_str = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid_str != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid_str] = None
    user_pitch_input_mode[uid_str] = None
    user_rate_input_mode[uid_str] = None

    file_obj = None
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
        if mime.startswith("video/") or mime.startswith("audio/"):
            file_obj = message.document
            type_str = "video" if mime.startswith("video/") else "audio"
        else:
            bot.send_message(message.chat.id, "❌ Unsupported file type. Send audio/video files.")
            return
    else:
        bot.send_message(message.chat.id, "❌ Send voice/audio/video files for transcription.")
        return

    size = file_obj.file_size
    if size and size > FILE_SIZE_LIMIT:
        bot.send_message(message.chat.id, "📦 File too large (max 20MB)")
        return

    processing_reply = bot.reply_to(message, "🔄 Processing...")

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
        logging.error(f"Processing init error: {e}")
        stop_typing.set()
        try:
            bot.delete_message(message.chat.id, processing_reply.message_id)
        except Exception as delete_e:
            logging.error(f"Delete message error: {delete_e}")
        bot.send_message(message.chat.id, "❌ Processing failed. Try again.")

def process_media_file(message, stop_typing, file_obj, type_str, processing_message_id):
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
        upload_response = requests.post(ASSEMBLYAI_UPLOAD_URL, headers=headers, data=file_content)
        upload_response.raise_for_status()
        audio_url = upload_response.json().get('upload_url')

        if not audio_url:
            raise Exception("Audio URL missing")

        media_lang_name = get_user_media_language_setting_db(uid_str)
        if not media_lang_name:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=processing_message_id,
                text="⚠️ Set media language first using /Langstt"
            )
            return

        media_lang_code = get_lang_code(media_lang_name)
        if not media_lang_code:
            raise ValueError(f"Invalid language: {media_lang_name}")

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
            raise Exception(f"Transcript error: {transcript_result.get('error', 'Unknown')}")

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
            raise Exception(f"Transcription failed: {polling_result.get('error', 'Unknown')}")

        user_transcriptions.setdefault(uid_str, {})[message.message_id] = transcription

        def delete_transcription_later(u_id, msg_id):
            time.sleep(600)
            if u_id in user_transcriptions and msg_id in user_transcriptions[u_id]:
                del user_transcriptions[u_id][msg_id]
                logging.info(f"Deleted transcription: {u_id}/{msg_id}")

        threading.Thread(
            target=delete_transcription_later,
            args=(uid_str, message.message_id),
            daemon=True
        ).start()

        increment_transcription_count_db(uid_str)

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
        )

        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Delete processing msg error: {delete_e}")

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
                caption="📝 Transcription complete"
            )
            transcript_file_buffer.close()
        else:
            bot.reply_to(
                message,
                transcription,
                reply_markup=buttons
            )

    except requests.exceptions.RequestException as req_e:
        logging.error(f"API error: {req_e}")
        error_message = "❌ Network error. Try again later."
        if "400" in str(req_e) and "language_code" in str(req_e):
            error_message = f"❌ Unsupported language: {media_lang_name}"
        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Delete msg error: {delete_e}")
        bot.send_message(message.chat.id, error_message)
    except Exception as e:
        logging.error(f"Processing error: {e}")
        try:
            bot.delete_message(message.chat.id, processing_message_id)
        except Exception as delete_e:
            logging.error(f"Delete msg error: {delete_e}")
        bot.send_message(
            message.chat.id,
            "❌ Transcription failed. The audio might be noisy or language mismatch."
        )
    finally:
        stop_typing.set()
        if message.chat.id in processing_message_ids:
            del processing_message_ids[message.chat.id]

# ========================================
# CALLBACK HANDLERS
# ========================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("set_media_lang|"))
def callback_set_media_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, lang_display_name = call.data.split("|", 1)
    set_user_media_language_setting_db(uid, lang_display_name)

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Media language set to: **{lang_display_name}**\n\nSend audio/video for transcription.",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Set to {lang_display_name}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_translation_lang|"))
def callback_set_translation_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, lang_display_name = call.data.split("|", 1)
    set_user_translation_language_db(uid, lang_display_name)

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Translation language set to: **{lang_display_name}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Set to {lang_display_name}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_summary_lang|"))
def callback_set_summary_language(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, lang_display_name = call.data.split("|", 1)
    set_user_summary_language_db(uid, lang_display_name)

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Summary language set to: **{lang_display_name}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Set to {lang_display_name}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription missing")
        return

    preferred_lang = get_user_translation_language_db(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Translating...")
        threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, preferred_lang, message_id)).start()
    else:
        markup = generate_language_keyboard("translate_to", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Select translation language:",
            reply_markup=markup
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription missing")
        return

    preferred_lang = get_user_summary_language_db(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Summarizing...")
        threading.Thread(target=do_summarize_with_saved_lang, args=(call.message, uid, preferred_lang, message_id)).start()
    else:
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Select summary language:",
            reply_markup=markup
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    parts = call.data.split("|")
    lang_display_name = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    set_user_translation_language_db(uid, lang_display_name)

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🌐 Translating to **{lang_display_name}**...",
        parse_mode="Markdown"
    )

    if message_id:
        threading.Thread(target=do_translate_with_saved_lang, args=(call.message, uid, lang_display_name, message_id)).start()
    else:
        bot.send_message(call.message.chat.id, "❌ Transcription missing")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

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
                text=f"📝 Summarizing in **{transcription_lang}**...",
                parse_mode="Markdown"
            )
        else:
            bot.answer_callback_query(call.id, "Set media language first")
            return
    else:
        set_user_summary_language_db(uid, lang_display_name)
        target_lang_for_gemini = lang_display_name
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"📝 Summarizing in **{lang_display_name}**...",
            parse_mode="Markdown"
        )

    if message_id:
        threading.Thread(target=do_summarize_with_saved_lang, args=(call.message, uid, target_lang_for_gemini, message_id)).start()
    else:
        bot.send_message(call.message.chat.id, "❌ Transcription missing")
    bot.answer_callback_query(call.id)

def do_translate_with_saved_lang(message, uid, lang_display_name, message_id):
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ Transcription missing")
        return

    lang_name_only = lang_display_name.split(' ')[0]
    prompt = f"Translate to {lang_name_only}:\n\n{original}"
    
    bot.send_chat_action(message.chat.id, 'typing')
    translated = ask_gemini(uid, prompt)

    if translated.startswith("Error:"):
        bot.send_message(message.chat.id, f"❌ Translation error: {translated}")
        return

    if len(translated) > 4000:
        import io
        translation_file_buffer = io.BytesIO(translated.encode('utf-8'))
        translation_file_buffer.name = f"{uuid.uuid4()}_translation.txt"
        bot.send_chat_action(message.chat.id, 'upload_document')
        bot.send_document(message.chat.id, translation_file_buffer, caption=f"🌐 {lang_display_name} Translation", reply_to_message_id=message_id)
        translation_file_buffer.close()
    else:
        bot.send_message(message.chat.id, translated, reply_to_message_id=message_id)

def do_summarize_with_saved_lang(message, uid, lang_display_name, message_id):
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ Transcription missing")
        return

    if lang_display_name == "Auto ⚙️":
        transcription_lang = get_user_media_language_setting_db(uid)
        if not transcription_lang:
            target_lang_for_gemini = "English"
            bot.send_message(message.chat.id, "⚠️ Using English for summary")
        else:
            lang_name_for_prompt = transcription_lang.split(' ')[0]
    else:
        lang_name_for_prompt = lang_display_name.split(' ')[0]

    prompt = f"Summarize in {lang_name_for_prompt}:\n\n{original}"
    bot.send_chat_action(message.chat.id, 'typing')
    summary = ask_gemini(uid, prompt)

    if summary.startswith("Error:"):
        bot.send_message(message.chat.id, f"❌ Summarization error: {summary}")
        return

    if len(summary) > 4000:
        import io
        summary_file_buffer = io.BytesIO(summary.encode('utf-8'))
        summary_file_buffer.name = f"{uuid.uuid4()}_summary.txt"
        bot.send_chat_action(message.chat.id, 'upload_document')
        bot.send_document(message.chat.id, summary_file_buffer, caption=f"📝 {lang_display_name} Summary", reply_to_message_id=message_id)
        summary_file_buffer.close()
    else:
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)

# ========================================
# TTS HANDLERS
# ========================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("tts_lang|"))
def on_tts_language_select(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    _, lang_name = call.data.split("|", 1)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔊 Select voice for {lang_name}:",
        reply_markup=make_tts_voice_keyboard_for_language(lang_name)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("tts_voice|"))
def on_tts_voice_change(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
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

    bot.answer_callback_query(call.id, f"✔️ Voice set")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔊 Voice: *{voice}*\n🎚️ Pitch: *{current_pitch}*\n⏩ Rate: *{current_rate}*\n\nSend text to convert to speech.",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda c: c.data == "tts_back_to_languages")
def on_tts_back_to_languages(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔊 Select TTS language:",
        reply_markup=make_tts_language_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['voice_pitch'])
def cmd_voice_pitch(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = "awaiting_pitch_input"
    user_rate_input_mode[uid] = None

    bot.send_message(
        message.chat.id,
        "🎚️ Set voice pitch:\n\nSelect preset or enter value (-100 to +100):",
        reply_markup=make_pitch_keyboard()
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("pitch_set|"))
def on_pitch_set_callback(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    try:
        _, pitch_value_str = call.data.split("|", 1)
        pitch_value = int(pitch_value_str)
        set_tts_user_pitch_db(uid, pitch_value)
        user_pitch_input_mode[uid] = None
        bot.answer_callback_query(call.id, f"✔️ Pitch set")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🎚️ Pitch set to *{pitch_value}*\n\nSend text for speech.",
            parse_mode="Markdown"
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid value")
    except Exception as e:
        logging.error(f"Pitch error: {e}")
        bot.answer_callback_query(call.id, "Error occurred")

@bot.message_handler(commands=['voice_rate'])
def cmd_voice_rate(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = "awaiting_rate_input"

    bot.send_message(
        message.chat.id,
        "⏩ Set speech rate:\n\nSelect preset or enter value (-100 to +100):",
        reply_markup=make_rate_keyboard()
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("rate_set|"))
def on_rate_set_callback(call):
    uid = str(call.from_user.id)
    update_user_activity_db(call.from_user.id)

    if call.message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(call.message.chat.id):
        send_subscription_message(call.message.chat.id)
        bot.answer_callback_query(call.id)
        return

    try:
        _, rate_value_str = call.data.split("|", 1)
        rate_value = int(rate_value_str)
        set_tts_user_rate_db(uid, rate_value)
        user_rate_input_mode[uid] = None
        bot.answer_callback_query(call.id, f"✔️ Rate set")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"⏩ Rate set to *{rate_value}*\n\nSend text for speech.",
            parse_mode="Markdown"
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid value")
    except Exception as e:
        logging.error(f"Rate error: {e}")
        bot.answer_callback_query(call.id, "Error occurred")

async def synth_and_send_tts(chat_id: int, user_id: str, text: str):
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
            bot.send_message(chat_id, "❌ Audio generation failed")
            return

        with open(filename, "rb") as f:
            bot.send_audio(chat_id, f, caption=f"🔊 {voice}")

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
            logging.error(f"TTS stat error: {e}")

    except MSSpeechError as e:
        logging.error(f"TTS error: {e}")
        bot.send_message(chat_id, f"❌ TTS error: {e}")
    except Exception as e:
        logging.error(f"TTS exception: {e}")
        bot.send_message(chat_id, "❌ TTS failed")
    finally:
        stop_recording.set()
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception as e:
                logging.error(f"File delete error: {e}")

# ========================================
# TEXT MESSAGE HANDLERS
# ========================================
@bot.message_handler(func=lambda message: message.content_type == 'text' and not message.text.startswith('/'))
def handle_text_for_tts_or_fallback(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    if user_rate_input_mode.get(uid) == "awaiting_rate_input":
        try:
            rate_val = int(message.text)
            if -100 <= rate_val <= 100:
                set_tts_user_rate_db(uid, rate_val)
                bot.send_message(message.chat.id, f"⏩ Rate set to *{rate_val}*", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid rate (-100 to 100)")
            user_rate_input_mode[uid] = None
            return
        except ValueError:
            pass

    if user_pitch_input_mode.get(uid) == "awaiting_pitch_input":
        try:
            pitch_val = int(message.text)
            if -100 <= pitch_val <= 100:
                set_tts_user_pitch_db(uid, pitch_val)
                bot.send_message(message.chat.id, f"🎚️ Pitch set to *{pitch_val}*", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "❌ Invalid pitch (-100 to 100)")
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
        if saved_voice != "en-US-AriaNeural":
            user_tts_mode[uid] = saved_voice
            threading.Thread(
                target=lambda: asyncio.run(synth_and_send_tts(message.chat.id, uid, message.text))
            ).start()
        else:
            bot.send_message(
                message.chat.id,
                "📝 Send audio/video for transcription or use /Langtts for text-to-speech",
                reply_markup=generate_main_menu()
            )

@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'document'])
def fallback_non_text_or_media(message):
    uid = str(message.from_user.id)
    update_user_activity_db(message.from_user.id)

    if message.chat.type == 'private' and uid != str(ADMIN_ID) and not check_subscription(message.chat.id):
        send_subscription_message(message.chat.id)
        return

    user_tts_mode[uid] = None
    user_pitch_input_mode[uid] = None
    user_rate_input_mode[uid] = None

    if message.document and not (message.document.mime_type.startswith("audio/") or message.document.mime_type.startswith("video/")):
        bot.send_message(
            message.chat.id,
            "❌ Unsupported file type. Send audio/video files.",
            reply_markup=generate_main_menu()
        )
        return

    bot.send_message(
        message.chat.id,
        "❌ Unsupported content. Send text, audio, or video files.",
        reply_markup=generate_main_menu()
    )

# ========================================
# FLASK ROUTES & STARTUP
# ========================================
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
        logging.error(f"Webhook error: {e}")
        return f"Webhook error: {e}", 500

@app.route("/delete_webhook", methods=["GET", "POST"])
def delete_webhook_route():
    try:
        bot.delete_webhook()
        return "Webhook deleted", 200
    except Exception as e:
        logging.error(f"Webhook delete error: {e}")
        return f"Webhook delete error: {e}", 500

def set_bot_commands():
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("Langstt", "Set media transcription language"),
        BotCommand("Langtts", "Set text-to-speech settings"),
        BotCommand("bottomtrnlang", "Set translation language"),
        BotCommand("bottomsumlang", "Set summary language"),
        BotCommand("userinfo", "View your profile info"),
        BotCommand("help", "Show help guide"),
        BotCommand("status", "View bot statistics"),
        BotCommand("privacy", "View privacy policy")
    ]
    try:
        bot.set_my_commands(commands)
        logging.info("Commands set successfully")
    except Exception as e:
        logging.error(f"Command error: {e}")

def set_webhook_on_startup():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set: {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Webhook setup error: {e}")

if __name__ == "__main__":
    connect_to_mongodb()
    set_webhook_on_startup()
    set_bot_commands()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
