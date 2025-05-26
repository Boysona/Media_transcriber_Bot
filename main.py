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
import yt_dlp
import imageio_ffmpeg

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q" 
ADMIN_ID = 5978150981 
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA" 
FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
DOWNLOAD_DIR = "downloads"
USERS_FILE = 'users.json' # Unified user tracking file
USER_LANGUAGE_SETTINGS_FILE = 'user_language_settings.json'

# --- Bot and Flask Initialization ---
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Ensure download directory exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- Global Data Structures ---
# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking and language settings (unified)
user_data = {} # Format: {user_id: {last_activity: "iso_datetime_str"}}
user_language_settings = {} # Format: {user_id: "language_name"}

# In-memory chat history and transcription store for Bot 1 features
user_memory = {} # For Gemini API chat context
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables for Bot 1 features)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0 # in seconds
processing_start_time = None

# Admin state for broadcast (Bot 1 feature)
admin_state = {} 

# For Bot 2 download features
bot.icon_messages = {} # To store message_id of reaction emoji
bot.video_info = {}    # To store YouTube video info for quality selection

# --- Helper Functions for Data Persistence ---
def load_user_data():
    global user_data
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            try:
                user_data = json.load(f)
            except json.JSONDecodeError:
                user_data = {}
    
def save_user_data():
    with open(USERS_FILE, 'w') as f:
        json.dump(user_data, f, indent=4)

def load_user_language_settings():
    global user_language_settings
    if os.path.exists(USER_LANGUAGE_SETTINGS_FILE):
        with open(USER_LANGUAGE_SETTINGS_FILE, 'r') as f:
            try:
                user_language_settings = json.load(f)
            except json.JSONDecodeError:
                user_language_settings = {}

def save_user_language_settings():
    with open(USER_LANGUAGE_SETTINGS_FILE, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

def update_user_activity(user_id):
    """Updates the last activity timestamp for a user and registers them."""
    uid_str = str(user_id)
    if uid_str not in user_data:
        user_data[uid_str] = {"last_activity": datetime.now().isoformat()}
    else:
        user_data[uid_str]["last_activity"] = datetime.now().isoformat()
    save_user_data()

# --- Gemini API Integration (Bot 1 Feature) ---
def ask_gemini(user_id, user_message):
    """Sends a message to the Gemini API and returns the response."""
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:] # Keep last 10 messages for context
    parts = [{"text": msg["text"]} for msg in history]
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        resp = requests.post(url, headers={'Content-Type': 'application/json'}, json={"contents": [{"parts": parts}]})
        resp.raise_for_status() 
        result = resp.json()
        
        if "candidates" in result and result['candidates']:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            user_memory[user_id].append({"role": "model", "text": reply})
            return reply
        else:
            logging.error(f"Gemini API returned no candidates: {json.dumps(result)}")
            return "Error: Gemini API did not return a valid response."
    except requests.exceptions.RequestException as e:
        logging.error(f"Request to Gemini API failed: {e}")
        return f"Error: Could not connect to Gemini API. {e}"
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode Gemini API response: {e}, Response: {resp.text}")
        return "Error: Failed to parse Gemini API response."
    except Exception as e:
        logging.error(f"An unexpected error occurred with Gemini API: {e}")
        return f"Error: An unexpected error occurred. {e}"

# --- Bot Setup (Combined) ---
def set_bot_info():
    """Sets the bot's commands, short description, and full description."""
    commands = [
        telebot.types.BotCommand("start", "Restart the bot and access features"),
        telebot.types.BotCommand("status", "View bot statistics (transcription)"),
        telebot.types.BotCommand("help", "Get information on how to use the bot"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
        telebot.types.BotCommand("translate", "Translate a transcription (reply to message)"),
        telebot.types.BotCommand("summarize", "Summarize a transcription (reply to message)"),
    ]
    bot.set_my_commands(commands)

    bot.set_my_short_description(
        "Your all-in-one media bot: transcribe, summarize, translate, and download videos!",
        language_code='en'
    )

    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free and in multiple languages.
        
I am also a media downloader bot. Send me links from various platforms like TikTok, YouTube, Instagram, Pinterest, and Facebook, and I'll download the media for you, including video descriptions and subtitles where available.

🔥Enjoy free usage and start now!👌🏻""",
        language_code='en'
    )

# --- Handlers (Combined and Prioritized) ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command."""
    user_id = message.from_user.id
    update_user_activity(user_id)
    
    # Prioritize first_name, then username
    name = message.from_user.first_name if message.from_user.first_name else f"@{message.from_user.username}" if message.from_user.username else "Dear User"
    
    if user_id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("📊 Total Users", "📢 Send Ads (broadcast)", "/status")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear {name}! I'm your all-in-one media bot.

• Send me a **link** from TikTok, YouTube, Instagram, Facebook, or other supported platforms to **download** the media.
• Send me a **voice message, video message, or audio file** to **transcribe** it for free.

For more info, use /help."""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    update_user_activity(message.from_user.id)
    help_text = (
        """ℹ️ How to use this bot:

**Media Transcription, Summarization & Translation:**
1.  **Send a File:** Send a voice message, audio, or video.
2.  **Receive Transcription:** The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
3.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text. You can also use /translate or /summarize commands by replying to a transcription.

**Media Downloading:**
1.  **Send a Link:** Send a supported media link (TikTok, YouTube, Instagram, Pinterest, Facebook, X/Twitter, etc.).
2.  **Receive Media:** The bot will download the media, including video descriptions and subtitles where available. For YouTube, you can choose the video quality.

**Commands:**
-   `/start`: Restart the bot and access features.
-   `/status`: View bot statistics (transcription).
-   `/help`: Display these instructions.
-   `/language`: Change your preferred language for translations and summaries.
-   `/privacy`: View the bot's privacy notice.
-   `/translate`: Translate a transcription (reply to the transcription message).
-   `/summarize`: Summarize a transcription (reply to the transcription message).

Enjoy using the bot!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    """Handles the /privacy command, displaying the bot's privacy policy."""
    update_user_activity(message.from_user.id)
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files (Transcription):** Voice messages, audio files, video files you send for transcription are **deleted immediately** after processing. We do not store your media content.
    * **Media Links (Download):** Links you send are used to download content temporarily. Downloaded media is **deleted immediately** after being sent to you. We do not store downloaded media.
    * **Transcriptions:** The text transcriptions generated are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences, track basic activity (like last seen), and send broadcast messages (for admin only). This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and media downloading.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed, total users).
    * To set your preferred language for future interactions.
    * To send broadcast messages from the administrator (if you are an admin).

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Your text input to these models is handled according to their respective privacy policies, but we do not store this data after processing.

4.  **Data Retention:**
    * Media files and downloaded media are deleted immediately after processing/sending.
    * Transcriptions are held temporarily in memory.
    * User IDs and language preferences are retained to maintain your settings and usage statistics. You can always delete your data by stopping using the bot or contacting the bot administrator.

By using this bot, you agree to the terms outlined in this Privacy Notice.

If you have any questions, please contact the bot administrator."""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    """Handles the /status command, displaying bot usage statistics (for transcription)."""
    update_user_activity(message.from_user.id)

    today = datetime.now().date()
    active_today = sum(
        1 for user_info in user_data.values()
        if datetime.fromisoformat(user_info.get("last_activity")).date() == today
    )

    total_seconds = int(total_processing_time)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    text = (
        "📊 Overall Statistics\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Users Today: {active_today}\n\n"
        "⚙️ Transcription Processing Statistics\n"
        f"▫️ Total Files Transcribed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n"
        f"⏱️ Total Transcription Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌\n"
        "See you next time! 💫"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📊 Total Users" and m.from_user.id == ADMIN_ID)
def total_users_admin(message):
    """Admin command to show total registered users."""
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "📢 Send Ads (broadcast)" and m.from_user.id == ADMIN_ID)
def send_broadcast_init(message):
    """Admin command to initiate a broadcast message."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now (text, photo, video, audio, document):")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice', 'video_note']
)
def broadcast_message(message):
    """Admin command to send the broadcast message to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    success = fail = 0
    for uid_str in user_data:
        try:
            uid = int(uid_str)
            if uid == message.from_user.id: # Don't send to self
                continue
            
            if message.text:
                bot.send_message(uid, message.text)
            elif message.photo:
                bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption or "")
            elif message.video:
                bot.send_video(uid, message.video.file_id, caption=message.caption or "")
            elif message.audio:
                bot.send_audio(uid, message.audio.file_id, caption=message.caption or "")
            elif message.voice:
                bot.send_voice(uid, message.voice.file_id)
            elif message.document:
                bot.send_document(uid, message.document.file_id, caption=message.caption or "")
            elif message.video_note:
                bot.send_video_note(uid, message.video_note.file_id)
            
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to user {uid_str}: {e}")
            fail += 1
        except Exception as e:
            logging.error(f"Unexpected error broadcasting to user {uid_str}: {e}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

# --- Language Selection and Saving (Bot 1 Feature) ---
LANGUAGES = [
    {"name": "English", "flag": "🇬🇧"}, {"name": "Chinese", "flag": "🇨🇳"}, {"name": "Spanish", "flag": "🇪🇸"},
    {"name": "Hindi", "flag": "🇮🇳"}, {"name": "Arabic", "flag": "🇸🇦"}, {"name": "French", "flag": "🇫🇷"},
    {"name": "Bengali", "flag": "🇧🇩"}, {"name": "Russian", "flag": "🇷🇺"}, {"name": "Portuguese", "flag": "🇵🇹"},
    {"name": "Urdu", "flag": "🇵🇰"}, {"name": "German", "flag": "🇩🇪"}, {"name": "Japanese", "flag": "🇯🇵"},
    {"name": "Korean", "flag": "🇰🇷"}, {"name": "Vietnamese", "flag": "🇻🇳"}, {"name": "Turkish", "flag": "🇹🇷"},
    {"name": "Italian", "flag": "🇮🇹"}, {"name": "Thai", "flag": "🇹🇭"}, {"name": "Swahili", "flag": "🇰🇪"},
    {"name": "Dutch", "flag": "🇳🇱"}, {"name": "Polish", "flag": "🇵🇱"}, {"name": "Ukrainian", "flag": "🇺🇦"},
    {"name": "Indonesian", "flag": "🇮🇩"}, {"name": "Malay", "flag": "🇲🇾"}, {"name": "Filipino", "flag": "🇵🇭"},
    {"name": "Persian", "flag": "🇮🇷"}, {"name": "Amharic", "flag": "🇪🇹"}, {"name": "Somali", "flag": "🇸🇴"}, 
    {"name": "Swedish", "flag": "🇸🇪"}, {"name": "Norwegian", "flag": "🇳🇴"}, {"name": "Danish", "flag": "🇩🇰"},
    {"name": "Finnish", "flag": "🇫🇮"}, {"name": "Greek", "flag": "🇬🇷"}, {"name": "Hebrew", "flag": "🇮🇱"},
    {"name": "Czech", "flag": "🇨🇿"}, {"name": "Hungarian", "flag": "🇭🇺"}, {"name": "Romanian", "flag": "🇷🇴"},
    {"name": "Nepali", "flag": "🇳🇵"}, {"name": "Sinhala", "flag": "🇱🇰"}, {"name": "Tamil", "flag": "🇮🇳"},
    {"name": "Telugu", "flag": "🇮🇳"}, {"name": "Kannada", "flag": "🇮🇳"}, {"name": "Malayalam", "flag": "🇮🇳"},
    {"name": "Gujarati", "flag": "🇮🇳"}, {"name": "Punjabi", "flag": "🇮🇳"}, {"name": "Marathi", "flag": "🇮🇳"},
    {"name": "Oriya", "flag": "🇮🇳"}, {"name": "Assamese", "flag": "🇮🇳"}, {"name": "Khmer", "flag": "🇰🇭"},
    {"name": "Lao", "flag": "🇱🇦"}, {"name": "Burmese", "flag": "🇲🇲"}, {"name": "Georgian", "flag": "🇬🇪"},
    {"name": "Armenian", "flag": "🇦🇲"}, {"name": "Azerbaijani", "flag": "🇦🇿"}, {"name": "Kazakh", "flag": "🇰🇿"},
    {"name": "Uzbek", "flag": "🇺🇿"}, {"name": "Kyrgyz", "flag": "🇰🇬"}, {"name": "Tajik", "flag": "🇹🇯"},
    {"name": "Turkmen", "flag": "🇹🇲"}, {"name": "Mongolian", "flag": "🇲🇳"}, {"name": "Estonian", "flag": "🇪🇪"},
    {"name": "Latvian", "flag": "🇱🇻"}, {"name": "Lithuanian", "flag": "🇱🇹"},
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

@bot.message_handler(commands=['language'])
def select_language_command(message):
    """Handles the /language command to let users set their preferred language."""
    update_user_activity(message.from_user.id)
    markup = generate_language_keyboard("set_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future translations and summaries:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_lang|"))
def callback_set_language(call):
    """Callback handler for setting the user's preferred language."""
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
    """Handles the 'Translate' inline button click."""
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Translating with your preferred language...")
        bot.edit_message_text(
            f"Translating to **{preferred_lang}**...",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown"
        )
        do_translate_with_saved_lang(call.message, uid, preferred_lang, message_id)
    else:
        markup = generate_language_keyboard("translate_to", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want to translate into:",
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    """Handles the 'Summarize' inline button click."""
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Summarizing with your preferred language...")
        bot.edit_message_text(
            f"Summarizing in **{preferred_lang}**...",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown"
        )
        do_summarize_with_saved_lang(call.message, uid, preferred_lang, message_id)
    else:
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want the summary in:",
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    """Callback handler for selecting a translation language from the inline keyboard."""
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
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
             do_translate_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to translate. Please use the inline buttons on the transcription or reply to it.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    """Callback handler for selecting a summarization language from the inline keyboard."""
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
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
            do_summarize_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription or reply to it.")
    bot.answer_callback_query(call.id)

def do_translate_with_saved_lang(message, uid, lang, message_id):
    """Performs the translation using Gemini API."""
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
        fn = os.path.join(DOWNLOAD_DIR, 'translation.txt')
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(translated)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Translation to {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, translated, reply_to_message_id=message_id)

def do_summarize_with_saved_lang(message, uid, lang, message_id):
    """Performs the summarization using Gemini API."""
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
        fn = os.path.join(DOWNLOAD_DIR, 'summary.txt')
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(summary)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Summary in {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)

@bot.message_handler(commands=['translate'])
def handle_translate_command(message):
    """Handles the /translate command."""
    uid = str(message.from_user.id)
    
    if not message.reply_to_message or uid not in user_transcriptions or message.reply_to_message.message_id not in user_transcriptions[uid]:
        return bot.send_message(message.chat.id, "❌ Please reply to a transcription message to translate it.")

    transcription_message_id = message.reply_to_message.message_id
    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        do_translate_with_saved_lang(message, uid, preferred_lang, transcription_message_id)
    else:
        markup = generate_language_keyboard("translate_to", transcription_message_id)
        bot.send_message(
            message.chat.id,
            "Please select the language you want to translate into:",
            reply_markup=markup
        )

@bot.message_handler(commands=['summarize'])
def handle_summarize_command(message):
    """Handles the /summarize command."""
    uid = str(message.from_user.id)
    
    if not message.reply_to_message or uid not in user_transcriptions or message.reply_to_message.message_id not in user_transcriptions[uid]:
        return bot.send_message(message.chat.id, "❌ Please reply to a transcription message to summarize it.")

    transcription_message_id = message.reply_to_message.message_id
    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        do_summarize_with_saved_lang(message, uid, preferred_lang, transcription_message_id)
    else:
        markup = generate_language_keyboard("summarize_in", transcription_message_id)
        bot.send_message(
            message.chat.id,
            "Please select the language you want the summary in:",
            reply_markup=markup
        )

def transcribe(path: str) -> str | None:
    """Performs the transcription of an audio/video file."""
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

# --- Media Downloader Functions (Bot 2 Features) ---
def is_supported_url(url):
    # Updated regex to handle various Pinterest URL formats more robustly
    pinterest_pattern = r'^(https?://)?(www\.)?(pinterest\.com|pin\.it)/.+'
    youtube_pattern = r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+'

    # Updated logic for YouTube URLs, as your provided examples were internal
    # 'youtube.com' and 'youtu.be'
    # These are NOT standard YouTube URLs. Assuming actual youtube.com/youtu.be are intended.
    # If the googleusercontent URLs are truly what you get, you'll need a different way to handle them.
    # For now, I'm sticking to standard URL patterns for broader compatibility.

    platforms = ['tiktok.com', 'instagram.com', 'snapchat.com', 'facebook.com', 'x.com', 'twitter.com']
    
    if re.match(youtube_pattern, url, re.IGNORECASE):
        return True
    if re.match(pinterest_pattern, url, re.IGNORECASE):
        return True
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    youtube_pattern = r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+'
    return re.match(youtube_pattern, url, re.IGNORECASE) is not None

def download_video_any(url):
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
    
    # Improved yt-dlp options for better compatibility and error handling
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Prioritize mp4, then fall back to best
        'outtmpl': video_path,
        'quiet': True,
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'convert_subtitles': 'srt',
        'ignoreerrors': True, # Continue if some error occurs during extraction
        'no_warnings': True, # Suppress warnings
        'retries': 3, # Retry downloads
        'fragment_retries': 3,
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        # Add a timeout for downloads
        'external_downloader_args': ['-timeout', '600'] # 600 seconds = 10 minutes timeout for external downloader
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            description = info.get('description', '')
        subtitle_path = video_path.replace('.mp4', '.en.srt') # Assuming English subtitles
        if os.path.exists(subtitle_path):
            return video_path, subtitle_path, description
        else:
            # Try to find any subtitle file if 'en' is not present
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(basename) and f.endswith('.srt'):
                    return video_path, os.path.join(DOWNLOAD_DIR, f), description
            return video_path, None, description
    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp Download Error for {url}: {e}")
        # Specific error handling for duration limits or unsupported links
        if "too long" in str(e).lower() or "exceeds" in str(e).lower() or "length limit" in str(e).lower():
            return None, None, "Video is too long to download or exceeds size limits."
        return None, None, f"Download error: {e}"
    except Exception as e:
        logging.error(f"General download error for {url}: {e}")
        return None, None, f"An unexpected error occurred during download: {e}"

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video(call):
    vid_id = call.data.split(":")[1]
    chat_id = call.message.chat.id
    
    if not hasattr(bot, 'video_info') or vid_id not in bot.video_info:
        bot.answer_callback_query(call.id, "Download expired or invalid. Please send the link again.")
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        return

    data = bot.video_info[vid_id]
    url, fmt, description = data['url'], data['format_id'], data.get('description', '')
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
    subtitle_path = None # Initialize subtitle_path outside try for finally block

    try:
        bot.answer_callback_query(call.id, "Downloading your video...")
        bot.edit_message_text("Downloading...", chat_id=call.message.chat.id, message_id=call.message.message_id)

        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'], # Prefer English subtitles
            'convert_subtitles': 'srt',
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'ignoreerrors': True, # Crucial for handling some problematic videos without crashing
            'no_warnings': True,
            'retries': 3,
            'fragment_retries': 3,
            'external_downloader_args': ['-timeout', '600'] # 10 minutes timeout
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Check if the downloaded file exists and is not empty
        if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
            raise Exception("Downloaded video file is missing or empty.")

        if description:
            final_description = f"{description}\n\nPower by @Bot_makerrBot"
        else:
            final_description = "Power by @Bot_makerrBot"

        bot.send_chat_action(chat_id, 'upload_video')
        with open(video_path, 'rb') as f:
            sent_msg = bot.send_video(chat_id, f, caption=final_description, reply_to_message_id=call.message.message_id)

        subtitle_path = video_path.replace('.mp4', '.en.srt')
        if not os.path.exists(subtitle_path):
            # Try to find any other subtitle file if English is not found
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(basename) and f.endswith('.srt'):
                    subtitle_path = os.path.join(DOWNLOAD_DIR, f)
                    break

        if subtitle_path and os.path.exists(subtitle_path):
            with open(subtitle_path, 'rb') as sub_file:
                bot.send_document(chat_id, sub_file, caption="Subtitles", reply_to_message_id=sent_msg.message_id)

        bot.delete_message(chat_id, call.message.message_id) # Delete the "Downloading..." message
        # Remove the icon message if it exists
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)

    except Exception as e:
        logging.error(f"Error downloading YouTube video: {e}")
        error_message = f"An error occurred during download: {e}"
        if "too long" in str(e).lower() or "exceeds" in str(e).lower() or "length limit" in str(e).lower():
            error_message = "This video is too long to download (exceeds typical limits for bots)."
        elif "Private video" in str(e):
             error_message = "This is a private video and cannot be downloaded."
        elif "unavailable" in str(e):
            error_message = "This video is unavailable or restricted."
        elif "No suitable MP4 video resolutions found" in str(e):
            error_message = "No suitable MP4 video resolutions found for download."
        
        bot.edit_message_text(error_message + "\nPlease try again or check the link.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
    finally:
        # Clean up files regardless of success or failure
        if os.path.exists(video_path):
            os.remove(video_path)
        if subtitle_path and os.path.exists(subtitle_path):
            os.remove(subtitle_path)

# --- General Message Handler (Prioritizes URLs, then media) ---
@bot.message_handler(content_types=['text', 'voice', 'audio', 'video', 'video_note', 'photo', 'document'])
def handle_all_messages(message):
    update_user_activity(message.from_user.id)
    user_id = message.from_user.id

    # 1. Check for URLs (Download Feature)
    if message.text and is_supported_url(message.text):
        url = message.text
        try:
            icon_msg = bot.send_message(user_id, "👀")
            bot.icon_messages[user_id] = icon_msg.message_id
            
            if is_youtube_url(url):
                bot.send_chat_action(user_id, 'typing')
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                    'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
                    'ignoreerrors': True, # Ignore errors during info extraction
                    'retries': 3,
                    'fragment_retries': 3,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    title = info.get('title', 'Video')
                    description = info.get('description', '')
                    formats = info.get('formats', [])

                resolutions = {f'{f["height"]}p': f['format_id']
                               for f in formats if f.get('vcodec') != 'none' and f.get('ext') == 'mp4' and f.get('height')} # Ensure height is present and mp4
                
                # Filter out resolutions that are too high or might cause issues
                resolutions = {res: fid for res, fid in resolutions.items() if int(res[:-1]) <= 1080}

                if not resolutions:
                    bot.send_message(user_id, "No suitable MP4 video resolutions found for download.")
                    if user_id in bot.icon_messages:
                        bot.delete_message(user_id, bot.icon_messages[user_id])
                        bot.icon_messages.pop(user_id, None)
                    return

                markup = InlineKeyboardMarkup(row_width=3)
                # Sort resolutions from highest to lowest
                sorted_resolutions = sorted(resolutions.items(), key=lambda x: int(x[0][:-1]), reverse=True)
                for res, fid in sorted_resolutions: 
                    vid_id = str(uuid.uuid4())[:8]
                    bot.video_info[vid_id] = {
                        'url': url,
                        'format_id': fid,
                        'description': description
                    }
                    markup.add(InlineKeyboardButton(res, callback_data=f'dl:{vid_id}'))

                # Delete the '👀' icon message before sending the quality selection
                if user_id in bot.icon_messages:
                    bot.delete_message(user_id, bot.icon_messages[user_id])
                    bot.icon_messages.pop(user_id, None)

                bot.send_message(user_id, f"Choose quality for: {title}", reply_markup=markup)

            else: # Other social media platforms (Pinterest, TikTok, Instagram, etc.)
                bot.send_chat_action(user_id, 'upload_video')
                processing_message = bot.send_message(user_id, "Downloading and preparing your media...")
                
                path, sub_path, download_error_msg = download_video_any(url) # download_video_any now returns error message
                
                # Delete the '👀' icon message and "Downloading..." message
                if user_id in bot.icon_messages:
                    bot.delete_message(user_id, bot.icon_messages[user_id])
                    bot.icon_messages.pop(user_id, None)
                bot.delete_message(user_id, processing_message.message_id)

                if path and os.path.exists(path) and os.path.getsize(path) > 0:
                    if description:
                        final_description = f"{description}\n\nPower by @Bot_makerrBot"
                    else:
                        final_description = "Power by @Bot_makerrBot"

                    with open(path, 'rb') as f:
                        sent_msg = bot.send_video(user_id, f, caption=final_description)

                    if sub_path and os.path.exists(sub_path):
                        with open(sub_path, 'rb') as sub:
                            bot.send_document(user_id, sub, caption="Subtitles", reply_to_message_id=sent_msg.message_id)
                    
                    bot.send_message(user_id, "Download complete! Enjoy your media.")
                else:
                    # Use the specific error message from download_video_any or a generic one
                    bot.send_message(user_id, download_error_msg or "An error occurred during download. Please try again or check the link. This might be due to video length or unsupported format.")
        except Exception as e:
            logging.error(f"General error handling URL {url}: {e}")
            bot.send_message(user_id, f"An unexpected error occurred while processing your link. Please try again.")
            if user_id in bot.icon_messages:
                bot.delete_message(user_id, bot.icon_messages[user_id])
                bot.icon_messages.pop(user_id, None)
        finally:
            # Ensure cleanup of temporary files in DOWNLOAD_DIR
            for file_name in os.listdir(DOWNLOAD_DIR):
                file_path = os.path.join(DOWNLOAD_DIR, file_name)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    logging.error(f"Failed to delete {file_path}. Reason: {e}")
        return # Exit if URL handled

    # 2. Check for Media Files (Transcription Feature)
    if message.voice or message.audio or message.video or message.video_note:
        global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
        
        file_obj = message.voice or message.audio or message.video or message.video_note
        if file_obj.file_size > FILE_SIZE_LIMIT:
            return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

        # Add reaction emoji
        try:
            bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Error setting reaction for message {message.message_id}: {e}")
        except Exception as e:
            logging.error(f"Unexpected error setting reaction for message {message.message_id}: {e}")

        info = bot.get_file(file_obj.file_id)
        local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg") # Using .ogg as a general intermediate format

        processing_message = bot.send_message(message.chat.id, "Processing your file... Please wait.")

        try:
            data = bot.download_file(info.file_path)
            with open(local_path, 'wb') as f:
                f.write(data)

            bot.edit_message_text("Transcribing...", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
            
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
                fn = os.path.join(DOWNLOAD_DIR, 'transcription.txt')
                with open(fn, 'w', encoding='utf-8') as f:
                    f.write(transcription)
                
                bot.edit_message_text("Sending transcription as a file...", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
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
                bot.edit_message_text(
                    transcription,
                    chat_id=processing_message.chat.id,
                    message_id=processing_message.message_id,
                    reply_markup=buttons
                )
        except Exception as e:
            logging.error(f"Error processing transcription file: {e}")
            bot.edit_message_text("⚠️ An error occurred during transcription.", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
        finally:
            if os.path.exists(local_path):
                os.remove(local_path) # Clean up downloaded file
        return # Exit if media file handled

    # 3. Fallback for other message types
    bot.send_message(message.chat.id, "Please send a supported media link (TikTok, YouTube, etc.) or a voice, audio, or video file for transcription.")

# --- Webhook and Server Setup ---
@app.route('/', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram updates."""
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook_route():
    """Route to manually set the webhook."""
    # This URL must be your actual deployed Render app URL
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://your-render-app-name.onrender.com") 
    bot.set_webhook(url=f"{RENDER_URL}/")
    return f"Webhook set to {RENDER_URL}/", 200

@app.route('/delete_webhook', methods=['GET','POST'])
def delete_webhook_route():
    """Route to manually delete the webhook."""
    bot.delete_webhook()
    return 'Webhook deleted.', 200

if __name__ == "__main__":
    # Clean up old downloads directory on startup for a fresh start
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    # Load existing user data and language settings
    load_user_data()
    load_user_language_settings()

    set_bot_info() # Set bot commands and descriptions

    # Delete existing webhook to prevent conflicts (useful during development/restarts)
    try:
        bot.delete_webhook()
        logging.info("Old webhook deleted successfully.")
    except Exception as e:
        logging.warning(f"Failed to delete old webhook (might not exist): {e}")

    # Set the webhook to the deployed URL for Render
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://your-render-app-name.onrender.com")
    try:
        bot.set_webhook(url=f"{RENDER_URL}/")
        logging.info(f"Webhook set to {RENDER_URL}/")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")

    # Start the Flask app for the webhook
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
