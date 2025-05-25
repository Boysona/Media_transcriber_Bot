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
import yt_dlp
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import imageio_ffmpeg # For FFmpeg setup

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === CONFIGURATION ===
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981
REQUIRED_CHANNEL = "@transcriberbo" # From the second bot
USER_FILE = "users.json" # Changed from .txt to .json for consistency with user_data
DOWNLOAD_DIR = "downloads"
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com" # Ensure this matches your deployment URL
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA" # Replace with your actual Gemini API key

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB

# === SETUP FFmpeg from imageio_ffmpeg ===
try:
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")
    logging.info(f"FFmpeg path set to: {ffmpeg_path}")
except Exception as e:
    logging.error(f"Error setting FFmpeg path: {e}")

# === CLEAN DOWNLOAD DIRECTORY ON START ===
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# === INIT BOT & FLASK ===
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True) # Set threaded to True for async handling

# === Whisper model for transcription ===
# Load the Whisper model once at startup
try:
    model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")
    logging.info("Whisper model loaded successfully.")
except Exception as e:
    logging.error(f"Error loading Whisper model: {e}")
    # Handle this error, perhaps exit or disable transcription features

# === USER MANAGEMENT & DATA STORAGE ===
users_file = 'users.json'
user_data = {} # Stores user IDs and last activity
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}
            logging.warning(f"Could not decode {users_file}, initializing empty user_data.")

user_language_settings_file = 'user_language_settings.json'
user_language_settings = {} # Stores user preferred languages for translate/summarize
if os.path.exists(user_language_settings_file):
    with open(user_language_settings_file, 'r') as f:
        try:
            user_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_language_settings = {}
            logging.warning(f"Could not decode {user_language_settings_file}, initializing empty user_language_settings.")

# In-memory chat history for Gemini and transcription store
user_memory = {} # Format: {user_id: [{"role": "user/model", "text": "message"}]}
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_tiktok_downloads = 0
total_processing_time = 0  # in seconds
processing_start_time = None

admin_state = {} # To manage admin broadcast flow

def save_user_data():
    """Saves the user_data dictionary to a JSON file."""
    with open(users_file, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    """Saves the user_language_settings dictionary to a JSON file."""
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

def update_user_activity(user_id):
    """Updates the last activity timestamp for a user and saves the data."""
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

def check_subscription(user_id):
    """Checks if the user is subscribed to the required channel."""
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error checking subscription for user {user_id}: {e}")
        return False

def send_subscription_message(chat_id):
    """Sends a message prompting the user to subscribe to the channel."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("Click here to join the channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")
    )
    bot.send_message(
        chat_id,
        "This bot only works when you join the channel 👉🏻 @transcriberbo. Please join the channel first, then come back to use the bot.🥰",
        reply_markup=markup
    )

def set_bot_info():
    """Sets the bot's commands, short description, and full description."""
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "View instructions"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    bot.set_my_commands(commands)

    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )

    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, TikTok videos, audio files, and videos—free and in multiple languages.

     🔥Enjoy free usage and start now!👌🏻"""
    )

# === GEMINI AI INTEGRATION ===
def ask_gemini(user_id, user_message):
    """Sends a message to the Gemini API and returns the response."""
    # Append user message to memory
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    # Use last 10 messages for context
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        resp = requests.post(url, headers={'Content-Type': 'application/json'}, json={"contents": [{"parts": parts}]})
        resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        result = resp.json()
        if "candidates" in result and result['candidates']:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            user_memory[user_id].append({"role": "model", "text": reply})
            return reply
        else:
            logging.error(f"Gemini API did not return candidates: {result}")
            return "Error: Gemini API did not return a valid response."
    except requests.exceptions.RequestException as e:
        logging.error(f"Request to Gemini API failed: {e}")
        return f"Error: Failed to connect to Gemini API. {e}"
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding Gemini API response: {e}, Response: {resp.text}")
        return "Error: Failed to decode Gemini API response."

# === TRANSCRIPTION ===
def transcribe(path: str) -> str | None:
    """Transcribes an audio file using the Whisper model."""
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

# --- Language Selection and Saving ---

# List of common languages with emojis (ordered by approximate global prevalence/popularity)
LANGUAGES = [
    {"name": "English", "flag": "🇬🇧"},
    {"name": "Chinese", "flag": "🇨🇳"},
    {"name": "Spanish", "flag": "🇪🇸"},
    {"name": "Hindi", "flag": "🇮🇳"},
    {"name": "Arabic", "flag": "🇸🇦"},
    {"name": "French", "flag": "🇫🇷"},
    {"name": "Bengali", "flag": "🇧🇩"},
    {"name": "Russian", "flag": "🇷🇺"},
    {"name": "Portuguese", "flag": "🇵🇹"},
    {"name": "Urdu", "flag": "🇵🇰"},
    {"name": "German", "flag": "🇩🇪"},
    {"name": "Japanese", "flag": "🇯🇵"},
    {"name": "Korean", "flag": "🇰🇷"},
    {"name": "Vietnamese", "flag": "🇻🇳"},
    {"name": "Turkish", "flag": "🇹🇷"},
    {"name": "Italian", "flag": "🇮🇹"},
    {"name": "Thai", "flag": "🇹🇭"},
    {"name": "Swahili", "flag": "🇰🇪"},
    {"name": "Dutch", "flag": "🇳🇱"},
    {"name": "Polish", "flag": "🇵🇱"},
    {"name": "Ukrainian", "flag": "🇺🇦"},
    {"name": "Indonesian", "flag": "🇮🇩"},
    {"name": "Malay", "flag": "🇲🇾"},
    {"name": "Filipino", "flag": "🇵🇭"},
    {"name": "Persian", "flag": "🇮🇷"},
    {"name": "Amharic", "flag": "🇪🇹"},
    {"name": "Somali", "flag": "🇸🇴"},
    {"name": "Swedish", "flag": "🇸🇪"},
    {"name": "Norwegian", "flag": "🇳🇴"},
    {"name": "Danish", "flag": "🇩🇰"},
    {"name": "Finnish", "flag": "🇫🇮"},
    {"name": "Greek", "flag": "🇬🇷"},
    {"name": "Hebrew", "flag": "🇮🇱"},
    {"name": "Czech", "flag": "🇨🇿"},
    {"name": "Hungarian", "flag": "🇭🇺"},
    {"name": "Romanian", "flag": "🇷🇴"},
    {"name": "Nepali", "flag": "🇳🇵"},
    {"name": "Sinhala", "flag": "🇱🇰"},
    {"name": "Tamil", "flag": "🇮🇳"},
    {"name": "Telugu", "flag": "🇮🇳"},
    {"name": "Kannada", "flag": "🇮🇳"},
    {"name": "Malayalam", "flag": "🇮🇳"},
    {"name": "Gujarati", "flag": "🇮🇳"},
    {"name": "Punjabi", "flag": "🇮🇳"},
    {"name": "Marathi", "flag": "🇮🇳"},
    {"name": "Oriya", "flag": "🇮🇳"},
    {"name": "Assamese", "flag": "🇮🇳"},
    {"name": "Khmer", "flag": "🇰🇭"},
    {"name": "Lao", "flag": "🇱🇦"},
    {"name": "Burmese", "flag": "🇲🇲"},
    {"name": "Georgian", "flag": "🇬🇪"},
    {"name": "Armenian", "flag": "🇦🇲"},
    {"name": "Azerbaijani", "flag": "🇦🇿"},
    {"name": "Kazakh", "flag": "🇰🇿"},
    {"name": "Uzbek", "flag": "🇺🇿"},
    {"name": "Kyrgyz", "flag": "🇰🇬"},
    {"name": "Tajik", "flag": "🇹🇯"},
    {"name": "Turkmen", "flag": "🇹🇲"},
    {"name": "Mongolian", "flag": "🇲🇳"},
    {"name": "Estonian", "flag": "🇪🇪"},
    {"name": "Latvian", "flag": "🇱🇻"},
    {"name": "Lithuanian", "flag": "🇱🇹"},
]

def generate_language_keyboard(callback_prefix, message_id=None):
    """Generates an inline keyboard for language selection."""
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    for lang in LANGUAGES:
        cb_data = f"{callback_prefix}|{lang['name']}"
        if message_id is not None:
            cb_data += f"|{message_id}"
        buttons.append(InlineKeyboardButton(f"{lang['name']} {lang['flag']}", callback_data=cb_data))
    markup.add(*buttons)
    return markup

def do_translate_with_saved_lang(message, uid, lang, message_id):
    """Translates the transcription to the specified language."""
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to translate.")
        return

    prompt = f"Translate the following text into {lang}. Provide only the translated text, with no additional notes, explanations, or introductory/concluding remarks:\n\n{original}"

    bot.send_chat_action(message.chat.id, 'typing')
    translated = ask_gemini(uid, prompt)

    if translated.startswith("Error:"):
        bot.send_message(message.chat.id, f"Error during translation: {translated}")
        return

    if len(translated) > 4000:
        fn = 'translation.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(translated)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Translation to {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, translated, reply_to_message_id=message_id)

def do_summarize_with_saved_lang(message, uid, lang, message_id):
    """Summarizes the transcription in the specified language."""
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to summarize.")
        return

    prompt = f"Summarize the following text in {lang}. Provide only the summarized text, with no additional notes, explanations, or different versions:\n\n{original}"

    bot.send_chat_action(message.chat.id, 'typing')
    summary = ask_gemini(uid, prompt)

    if summary.startswith("Error:"):
        bot.send_message(message.chat.id, f"Error during summarization: {summary}")
        return

    if len(summary) > 4000:
        fn = 'summary.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(summary)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Summary in {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)

# === MESSAGE HANDLERS ===

@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command."""
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()

    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        bot.send_message(
            message.chat.id,
            f"👋🏻 Welcome {username}, send me a TikTok, YouTube, Instagram, Facebook or other link, or a voice message, audio file, or video to process for free!"
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, videos, and TikTok video links. It also downloads media from various platforms and offers translation and summarization.

1.  **Join the Channel:** Make sure you've joined our channel: https://t.me/transcriberbo.
2.  **Send a File/Link:** Send a voice message, audio, video, or a supported URL (TikTok, YouTube, Instagram, Facebook, X, Pinterest).
3.  **Receive Processing Options:**
    * For supported URLs, you might get options to **Download** or **Transcribe**.
    * For direct media uploads, the bot will automatically transcribe.
4.  **Download Features:** When downloading TikTok videos, the bot will also provide the video's description.
5.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.
6.  **Commands:**
    -   `/start`: Restart the bot and see welcome message.
    -   `/status`: View bot statistics.
    -   `/help`: Display these instructions.
    -   `/language`: Change your preferred language for translations and summaries.
    -   `/privacy`: View the bot's privacy notice.

Enjoy processing your media quickly and easily!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    """Handles the /privacy command."""
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and TikTok links you send are temporarily processed for transcription/download. These files are **deleted immediately** after processing. We do not store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and media downloads.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed).
    * To set your preferred language for future interactions.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Your text input to these models is handled according to their respective privacy policies, but we do not store this data after processing.

4.  **Data Retention:**
    * Media files are deleted immediately after processing.
    * Transcriptions are held temporarily in memory.
    * User IDs and language preferences are retained to maintain your settings and usage statistics. You can always delete your data by stopping using the bot or contacting the bot administrator.

By using this bot, you agree to the terms outlined in this Privacy Notice.

If you have any questions, please contact the bot administrator."""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    """Handles the /status command and displays bot statistics."""
    update_user_activity(message.from_user.id)

    today = datetime.now().date()
    active_today = sum(
        1 for timestamp in user_data.values()
        if datetime.fromisoformat(timestamp).date() == today
    )

    total_seconds = int(total_processing_time)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    text = (
        "📊 Overall Statistics\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Users Today: {active_today}\n\n"
        "⚙️ Processing Statistics\n"
        f"▫️ Total Files Processed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n"
        f"▫️ TikTok Downloads: {total_tiktok_downloads}\n\n"
        f"⏱️ Total Processing Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌\n"
        "See you next time! 💫"
    )

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def admin_total_users(message):
    """Admin command to show total registered users."""
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def ask_broadcast(message):
    """Admin command to initiate a broadcast message."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice', 'video_note', 'sticker']
)
def broadcast_message(message):
    """Admin function to broadcast a message to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    success = fail = 0
    if not user_data:
        bot.send_message(message.chat.id, "No users to broadcast to.")
        return

    for uid_str in user_data:
        try:
            uid = int(uid_str)
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Error sending broadcast to user {uid}: {e}")
            fail += 1
        except ValueError: # Handle cases where uid_str might not be a valid int
            logging.error(f"Invalid user ID in user_data: {uid_str}")
            fail += 1

    bot.send_message(
        message.chat.id,
        f"✅ Broadcast finished.\nSuccessful: {success}\nFailed: {fail}"
    )

@bot.message_handler(commands=['language'])
def select_language_command(message):
    """Handles the /language command for selecting preferred language."""
    uid = str(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    markup = generate_language_keyboard("set_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future translations and summaries:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_lang|"))
def callback_set_language(call):
    """Callback for setting the user's preferred language."""
    uid = str(call.from_user.id)
    _, lang = call.data.split("|", 1)
    user_language_settings[uid] = lang
    save_user_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Your preferred language has been set to: **{lang}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Language set to {lang}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    """Callback for initiating translation from inline button."""
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Translating with your preferred language...")
        do_translate_with_saved_lang(call.message, uid, preferred_lang, message_id)
    else:
        markup = generate_language_keyboard("translate_to", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want to translate into:",
            reply_markup=markup
        )
    # No need to answer callback query again here, it's already answered inside if/else

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    """Callback for initiating summarization from inline button."""
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Summarizing with your preferred language...")
        do_summarize_with_saved_lang(call.message, uid, preferred_lang, message_id)
    else:
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want the summary in:",
            reply_markup=markup
        )
    # No need to answer callback query again here, it's already answered inside if/else

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    """Callback for translating to a specific language."""
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    user_language_settings[uid] = lang
    save_user_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Translating to **{lang}**...",
        parse_mode="Markdown"
    )
    if message_id:
        do_translate_with_saved_lang(call.message, uid, lang, message_id)
    else:
        # Fallback to replying to the original message if message_id is not present in callback data
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
            do_translate_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to translate. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    """Callback for summarizing in a specific language."""
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    user_language_settings[uid] = lang
    save_user_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Summarizing in **{lang}**...",
        parse_mode="Markdown"
    )
    if message_id:
        do_summarize_with_saved_lang(call.message, uid, lang, message_id)
    else:
        # Fallback to replying to the original message if message_id is not present in callback data
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
            do_summarize_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)

# === URL HANDLING ===
def is_supported_url(url):
    """Checks if the URL is from a supported social media platform."""
    platforms = [
        'tiktok.com', 'youtube.com', 'youtu.be', 'pinterest.com', 'pin.it',
        'instagram.com', 'snapchat.com', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com'
    ]
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    """Checks if the URL is a YouTube URL."""
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

# TikTok link detection
TIKTOK_REGEX = re.compile(r'(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+')

@bot.message_handler(func=lambda m: m.text and is_supported_url(m.text))
def handle_any_link(message):
    """Handles various social media links."""
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    url = message.text
    if TIKTOK_REGEX.search(url):
        tiktok_link_handler(message)
    elif is_youtube_url(url):
        handle_youtube_url(message)
    else:
        handle_other_social_video(message)

# Specific handler for TikTok links (called from handle_any_link)
def tiktok_link_handler(message):
    """Handles TikTok links specifically, providing download/transcribe options."""
    url = TIKTOK_REGEX.search(message.text).group(0)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("Download", callback_data=f"download_tiktok|{url}"),
        telebot.types.InlineKeyboardButton("Transcribe", callback_data=f"transcribe_tiktok|{url}|{message.message_id}")
    )
    bot.send_message(message.chat.id, "TikTok link detected—choose an action:", reply_markup=markup)
    bot.set_message_reaction(message.chat.id, message.message_id, reaction="👀") # Add 👀 reaction

@bot.callback_query_handler(func=lambda c: c.data.startswith("download_tiktok|"))
def callback_download_tiktok(call):
    """Callback for downloading TikTok videos, including description."""
    global total_tiktok_downloads
    _, url = call.data.split("|", 1)

    bot.answer_callback_query(call.id, "Downloading TikTok video...")
    bot.send_chat_action(call.message.chat.id, 'upload_video')

    path = None
    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4', # Ensure MP4 for Telegram
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'writesubtitles': False, # Not needed for video download
            'writeautomaticsub': False, # Not needed for video download
            'writedescription': True, # To get description
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            # Fetch description directly from info dictionary
            caption = info.get('description', 'No description available.')
            if 'hashtags' in info and info['hashtags']:
                caption += "\n" + " ".join(f"#{tag}" for tag in info['hashtags'])
            if 'title' in info:
                caption = info['title'] + "\n" + caption # Add title to caption


        if os.path.exists(path):
            with open(path, 'rb') as video:
                bot.send_video(call.message.chat.id, video, caption=caption[:1024]) # Telegram caption limit is 1024 chars
            total_tiktok_downloads += 1
        else:
            bot.send_message(call.message.chat.id, "⚠️ Video file not found after download.")

    except Exception as e:
        logging.error(f"TikTok download error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok video. It might be private or geo-restricted.")
    finally:
        if path and os.path.exists(path):
            os.remove(path)
        # Clean up any other files left by yt-dlp (e.g., info.json)
        for file in os.listdir(DOWNLOAD_DIR):
            if file.startswith(info.get('id', '')): # Check if file belongs to this download
                os.remove(os.path.join(DOWNLOAD_DIR, file))

@bot.callback_query_handler(func=lambda c: c.data.startswith("transcribe_tiktok|"))
def callback_transcribe_tiktok(call):
    """Callback for transcribing TikTok videos."""
    _, url, message_id_str = call.data.split("|", 2)
    message_id = int(message_id_str)

    bot.answer_callback_query(call.id, "Transcribing TikTok video...")
    bot.send_chat_action(call.message.chat.id, 'typing')

    path = None
    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestaudio/best', # Download only audio for transcription
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'ogg',
                'preferredquality': '192',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = os.path.join(DOWNLOAD_DIR, f"{info['id']}.ogg") # yt-dlp renames to .ogg if specified

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(path) or ""
        uid = str(call.from_user.id)

        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        global total_processing_time
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message_id}")
        )

        if len(transcription) > 4000:
            fn = 'tiktok_transcription.txt'
            with open(fn, 'w', encoding='utf-8') as f:
                f.write(transcription)
            bot.send_chat_action(call.message.chat.id, 'upload_document')
            with open(fn, 'rb') as doc:
                bot.send_document(
                    call.message.chat.id,
                    doc,
                    reply_markup=buttons,
                    caption="Here’s your transcription. Tap a button below to translate or summarize."
                )
            os.remove(fn)
        else:
            bot.send_chat_action(call.message.chat.id, 'typing')
            bot.send_message(
                call.message.chat.id,
                transcription,
                reply_markup=buttons
            )
    except Exception as e:
        logging.error(f"TikTok transcribe error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to transcribe TikTok. It might be private or geo-restricted.")
    finally:
        if path and os.path.exists(path):
            os.remove(path)
        # Clean up any other files left by yt-dlp (e.g., info.json)
        for file in os.listdir(DOWNLOAD_DIR):
            if file.startswith(info.get('id', '')): # Check if file belongs to this download
                os.remove(os.path.join(DOWNLOAD_DIR, file))

# YouTube Handling (Integrated from the first bot)
@bot.message_handler(func=lambda msg: is_youtube_url(msg.text))
def handle_youtube_url(msg):
    """Handles YouTube URLs, providing quality selection for download."""
    update_user_activity(msg.from_user.id)
    if not check_subscription(msg.from_user.id):
        return send_subscription_message(msg.chat.id)

    url = msg.text
    try:
        bot.send_chat_action(msg.chat.id, 'typing')
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            formats = info.get('formats', [])

        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') and f.get('height') <= 1080}

        if not resolutions:
            bot.send_message(msg.chat.id, "No suitable resolutions found for download.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1]) if x[0][:-1].isdigit() else 0):
            vid_id = str(uuid.uuid4())[:8]
            # Use bot.message_id_to_video_info to store info per message
            # This helps avoid conflicts if multiple users request videos concurrently
            bot.message_id_to_video_info = getattr(bot, 'message_id_to_video_info', {})
            bot.message_id_to_video_info[vid_id] = {'url': url, 'format_id': fid}
            markup.add(InlineKeyboardButton(res, callback_data=f'dl_youtube:{vid_id}'))

        bot.send_message(msg.chat.id, f"Choose quality for: {title}", reply_markup=markup)
        bot.set_message_reaction(msg.chat.id, msg.message_id, reaction="👀") # Add 👀 reaction

    except Exception as e:
        logging.error(f"Error handling YouTube URL: {e}")
        bot.send_message(msg.chat.id, f"Error processing YouTube link: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_youtube:'))
def download_youtube_video(call):
    """Callback for downloading YouTube videos based on selected quality."""
    vid = call.data.split(":")[1]
    if not hasattr(bot, 'message_id_to_video_info') or vid not in bot.message_id_to_video_info:
        bot.answer_callback_query(call.id, "Download link expired or invalid. Please send the link again.")
        return

    data = bot.message_id_to_video_info.pop(vid) # Remove from dict after use
    url, fmt = data['url'], data['format_id']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
    subtitle_path = video_path.replace('.mp4', '.en.srt') # Default English subtitle path

    bot.answer_callback_query(call.id, "Downloading YouTube video...")
    bot.send_chat_action(call.message.chat.id, 'upload_video')

    try:
        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'merge_output_format': 'mp4', # Ensure final format is mp4
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(video_path):
            with open(video_path, 'rb') as f:
                bot.send_video(call.message.chat.id, f, reply_to_message_id=call.message.message_id)

            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub_file:
                    bot.send_document(call.message.chat.id, sub_file, caption="Subtitles", reply_to_message_id=call.message.message_id)
        else:
            bot.send_message(call.message.chat.id, "Download failed or video file not found.")

    except Exception as e:
        logging.error(f"Error downloading YouTube video: {e}")
        bot.send_message(call.message.chat.id, f"Error downloading YouTube video: {e}")
    finally:
        # Clean up downloaded files
        for file in os.listdir(DOWNLOAD_DIR):
            full_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.isfile(full_path) and full_path.startswith(os.path.join(DOWNLOAD_DIR, basename)):
                os.remove(full_path)


# Other Social Platforms (Integrated from the first bot, now with transcription)
def download_and_process_any_social_video(url, chat_id, message_id):
    """Downloads video from other social platforms and offers transcription."""
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
    audio_path = os.path.join(DOWNLOAD_DIR, f"{basename}.ogg") # For transcription

    ydl_opts_video = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4',
        'outtmpl': video_path,
        'quiet': True,
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'ignoreerrors': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'convert_subtitles': 'srt'
    }
    ydl_opts_audio = {
        'format': 'bestaudio/best',
        'outtmpl': audio_path,
        'quiet': True,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'ogg',
            'preferredquality': '192',
        }],
        'ignoreerrors': True
    }

    try:
        # First, download the video
        bot.send_chat_action(chat_id, 'upload_video')
        with yt_dlp.YoutubeDL(ydl_opts_video) as ydl_video:
            video_info = ydl_video.extract_info(url, download=True)
            actual_video_path = ydl_video.prepare_filename(video_info)

        if actual_video_path and os.path.exists(actual_video_path):
            with open(actual_video_path, 'rb') as f:
                bot.send_video(chat_id, f, reply_to_message_id=message_id)

            subtitle_path = actual_video_path.replace('.mp4', '.en.srt')
            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub:
                    bot.send_document(chat_id, sub, caption="Subtitles", reply_to_message_id=message_id)
        else:
            bot.send_message(chat_id, "⚠️ Video file not found after download.")
            return

        # Then, download audio for transcription
        bot.send_chat_action(chat_id, 'typing')
        with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl_audio:
            audio_info = ydl_audio.extract_info(url, download=True)
            actual_audio_path = ydl_audio.prepare_filename(audio_info) # This might still be .webm or .mp4, but postprocessor will convert to .ogg
            # Ensure the path reflects the post-processed ogg
            actual_audio_path = os.path.join(DOWNLOAD_DIR, f"{audio_info['id']}.ogg")


        if actual_audio_path and os.path.exists(actual_audio_path):
            global processing_start_time
            processing_start_time = datetime.now()

            transcription = transcribe(actual_audio_path) or ""
            uid = str(chat_id)
            user_transcriptions.setdefault(uid, {})[message_id] = transcription

            processing_time = (datetime.now() - processing_start_time).total_seconds()
            global total_processing_time
            total_processing_time += processing_time

            buttons = InlineKeyboardMarkup()
            buttons.add(
                InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message_id}"),
                InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message_id}")
            )

            if len(transcription) > 4000:
                fn = 'transcription_social.txt'
                with open(fn, 'w', encoding='utf-8') as f:
                    f.write(transcription)
                bot.send_chat_action(chat_id, 'upload_document')
                with open(fn, 'rb') as doc:
                    bot.send_document(
                        chat_id,
                        doc,
                        reply_to_message_id=message_id,
                        reply_markup=buttons,
                        caption="Here’s your transcription. Tap a button below to translate or summarize."
                    )
                os.remove(fn)
            else:
                bot.send_chat_action(chat_id, 'typing')
                bot.send_message(
                    chat_id,
                    f"Transcription:\n{transcription}",
                    reply_markup=buttons,
                    reply_to_message_id=message_id
                )
        else:
            bot.send_message(chat_id, "⚠️ Failed to extract audio for transcription.")

    except Exception as e:
        logging.error(f"Error processing other social video: {e}")
        bot.send_message(chat_id, f"Error processing link: {e}")
    finally:
        # Clean up all generated files for this URL
        for file in os.listdir(DOWNLOAD_DIR):
            full_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.isfile(full_path) and file.startswith(basename):
                os.remove(full_path)
            elif 'video_info' in locals() and file.startswith(video_info.get('id', '')): # Clean up based on info id
                os.remove(full_path)


@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text) and not TIKTOK_REGEX.search(msg.text))
def handle_other_social_video(msg):
    """Handler for other supported social media platforms (non-TikTok, non-YouTube)."""
    update_user_activity(msg.from_user.id)
    if not check_subscription(msg.from_user.id):
        return send_subscription_message(msg.chat.id)

    url = msg.text
    bot.set_message_reaction(msg.chat.id, msg.message_id, reaction="👀") # Add 👀 reaction
    download_and_process_any_social_video(url, msg.chat.id, msg.message_id)


@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    """Handles direct media uploads (voice, audio, video)."""
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    # Add 👀 reaction to the message
    bot.set_message_reaction(message.chat.id, message.message_id, reaction="👀")

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    info = bot.get_file(file_obj.file_id)
    # Ensure correct file extension for download based on mime_type
    file_extension = '.ogg' if message.voice else \
                     '.mp3' if message.audio else \
                     '.mp4' if message.video or message.video_note else \
                     '.bin' # Fallback
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}{file_extension}")
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        data = bot.download_file(info.file_path)
        with open(local_path, 'wb') as f:
            f.write(data)

        # Convert to ogg if it's not already (Whisper prefers ogg)
        if file_extension != '.ogg':
            converted_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
            try:
                # Use ffmpeg directly via subprocess if imageio_ffmpeg is not handling conversion smoothly
                # For simplicity, relying on imageio_ffmpeg which should set PATH for ffmpeg
                os.system(f'ffmpeg -i "{local_path}" -vn -acodec libopus -b:a 128k "{converted_path}" -y')
                transcribe_path = converted_path
            except Exception as ffmpeg_err:
                logging.error(f"FFmpeg conversion failed: {ffmpeg_err}. Attempting transcription with original file.")
                transcribe_path = local_path
        else:
            transcribe_path = local_path


        bot.send_chat_action(message.chat.id, 'typing')
        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(transcribe_path) or ""
        uid = str(message.from_user.id)

        user_transcriptions.setdefault(uid, {})[message.message_id] = transcription

        total_files_processed += 1
        if message.voice:
            total_voice_clips += 1
        elif message.audio:
            total_audio_files += 1
        elif message.video or message.video_note:
            total_videos += 1

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message.message_id}")
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
                    caption="Here’s your transcription. Tap a button below to translate or summarize."
                )
            os.remove(fn)
        else:
            bot.reply_to(
                message,
                transcription,
                reply_markup=buttons
            )
    except Exception as e:
        logging.error(f"Error processing file: {e}")
        bot.send_message(message.chat.id, "⚠️ An error occurred during transcription.")
    finally:
        # Clean up both original and converted paths
        if os.path.exists(local_path):
            os.remove(local_path)
        if 'converted_path' in locals() and os.path.exists(converted_path):
            os.remove(converted_path)


@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    """Handles unsupported message types."""
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)
    # Check if it's a link that wasn't caught by general link handler (e.g., malformed URL)
    if message.text and re.match(r'https?://[^\s]+', message.text):
        bot.send_message(message.chat.id, "Please provide a valid link from TikTok, YouTube, Instagram, Facebook, X, or Pinterest.")
    else:
        bot.send_message(message.chat.id, "Please send only voice, audio, video, or a supported social media link (TikTok, YouTube, Instagram, Facebook, X, Pinterest).")

# === WEBHOOK ENDPOINTS ===
@app.route('/', methods=['POST'])
def webhook():
    """Receives Telegram updates via webhook."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

@app.route('/set_webhook', methods=['GET', 'POST'])
def set_webhook_route():
    """Sets the Telegram webhook."""
    bot.set_webhook(url=WEBHOOK_URL)
    return f"Webhook set to {WEBHOOK_URL}", 200

@app.route('/delete_webhook', methods=['GET', 'POST'])
def delete_webhook_route():
    """Deletes the Telegram webhook."""
    bot.delete_webhook()
    return "Webhook deleted", 200

# === APP RUN ===
if __name__ == "__main__":
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    set_bot_info()
    bot.delete_webhook()
    bot.set_webhook(url=WEBHOOK_URL) # Ensure this URL is correct for your deployment
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

