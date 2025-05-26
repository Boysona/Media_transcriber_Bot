import os
import re
import uuid
import shutil
import logging
import requests
import telebot
import json
import yt_dlp
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token (HUBI INAAD TAN BEDDESHO! Waa in ay ahaataa TOKEN-kaaga dhabta ah)
TOKEN = "YOUR_BOT_TOKEN_HERE" # Replace with your actual bot token

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID (HUBI INAAD TAN BEDDESHO! Waa in ay ahaataa ID-gaaga admin-ka ah)
ADMIN_ID = 5978150981 # Replace with your actual admin ID

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
# Model size can be 'tiny', 'base', 'small', 'medium', 'large'
# 'tiny' is faster but less accurate, 'large' is slower but more accurate.
# 'cpu' is for CPU, 'cuda' is for GPU (if available and configured).
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking files
users_file = 'users.json' # For general user data and last activity
user_language_settings_file = 'user_language_settings.json' # For transcription language preferences
media_downloader_users_file = 'media_downloader_users.txt' # For media downloader specific user tracking

user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

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

def save_media_downloader_user(user_id):
    if not os.path.exists(media_downloader_users_file):
        open(media_downloader_users_file, "w").close()
    with open(media_downloader_users_file, "r+") as f:
        ids = f.read().splitlines()
        if str(user_id) not in ids:
            f.write(f"{user_id}\n")

# In-memory chat history and transcription store
user_memory = {} # For Gemini API chat context
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables) for Transcriber Bot
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0  # in seconds
processing_start_time = None

# Gemini API Key (HUBI INAAD TAN BEDDESHO! Waa in ay ahaataa API Key-gaaga dhabta ah)
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY_HERE" # Replace with your actual Gemini API key.

def ask_gemini(user_id, user_message):
    """Sends a message to the Gemini API and returns the response."""
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    # Keep only the last 10 messages for context to avoid hitting token limits
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        resp = requests.post(url, headers={'Content-Type': 'application/json'}, json={"contents": [{"parts": parts}]})
        resp.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
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

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {} # To manage admin commands (like broadcast)

# Dictionary lagu keydinayo icon message_id marka link la soo diro (For Media Downloader)
bot.icon_messages = {}
# Dictionary for YouTube video info (For Media Downloader)
bot.video_info = {}

def set_bot_info():
    """Sets the bot's commands, short description, and full description."""
    commands = [
        telebot.types.BotCommand("start", "Restart the bot and see options"),
        telebot.types.BotCommand("status", "View Bot statistics (Transcriber)"),
        telebot.types.BotCommand("help", "View instructions"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    bot.set_my_commands(commands)

    # Short description (About) for combined bot
    bot.set_my_short_description(
        "A multi-purpose bot for transcribing audio/video, translating, summarizing, and downloading media from various platforms!"
    )

    # Full description (What can this bot do?) for combined bot
    bot.set_my_description(
        """This bot offers a wide range of features:

**Media Transcriber:**
* Quickly transcribes voice messages, audio files, and videos—free and in multiple languages.
* Provides options to translate or summarize transcriptions using advanced AI.

**Media Downloader:**
* Downloads media from popular platforms like TikTok, YouTube, Instagram, Pinterest, and Facebook.
* Supports video descriptions and subtitles where available.

Enjoy free usage and start now!👌🏻"""
    )

def update_user_activity(user_id):
    """Updates the last activity timestamp for a user."""
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command."""
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    save_media_downloader_user(message.from_user.id) # Also save for media downloader

    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()
    
    # Isticmaal magaca hore ee user-ka (first_name), meeshii @username laga xusi lahaa
    name = message.from_user.first_name if message.from_user.first_name else "dear"

    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("📊 Total Users", "📢 Send Broadcast", "/status", "/help")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome {name}!
I'm a multi-purpose bot. Here's what I can do:

**Transcriber:**
• Send me a voice message, video message, or audio file to transcribe for free.
• After transcription, you can translate or summarize the text.

**Downloader:**
• Send me a link from TikTok, YouTube, Instagram, Facebook, or other supported platforms, and I’ll download the media for you.

For more info, use /help.
"""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    update_user_activity(message.from_user.id)
    help_text = (
        """ℹ️ How to use this bot:

This bot offers two main functionalities:

**1. Media Transcriber:**
-   **Send a File:** Send a voice message, audio, or video.
-   **Receive Transcription:** The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
-   **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.

**2. Media Downloader:**
-   **Send a Link:** Send a link from TikTok, YouTube, Instagram, Pinterest, Facebook, X (Twitter), or other supported platforms.
-   **Receive Media:** The bot will download the media (video, audio, or image) for you, including video descriptions and subtitles where available.

**Commands:**
-   `/start`: Restart the bot and see main options.
-   `/status`: View bot statistics (for transcriber).
-   `/help`: Display these instructions.
-   `/language`: Change your preferred language for translations and summaries.
-   `/privacy`: View the bot's privacy notice.

Enjoy using this multi-purpose bot!"""
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
    * **Media Files:** Voice messages, audio files, video files, and links you send are temporarily processed for transcription or download. These files are **deleted immediately** after processing. We do not store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and media downloading.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed).
    * To set your preferred language for future interactions.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Your text input to these models is handled according to their respective privacy policies, but we do not store this data after processing.
    * Media downloading uses `yt-dlp`, which accesses public media content directly from the source platforms. Your link is processed to fetch public information about the media.

4.  **Data Retention:**
    * Media files are deleted immediately after transcription or download.
    * Transcriptions are held temporarily in memory.
    * User IDs and language preferences are retained to maintain your settings and usage statistics. You can always delete your data by stopping using the bot or contacting the bot administrator.

By using this bot, you agree to the terms outlined in this Privacy Notice.

If you have any questions, please contact the bot administrator."""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    """Handles the /status command, displaying bot usage statistics for Transcriber functions."""
    update_user_activity(message.from_user.id)

    # Count active users today
    today = datetime.now().date()
    active_today = sum(
        1 for timestamp in user_data.values()
        if datetime.fromisoformat(timestamp).date() == today
    )

    # Calculate time components
    total_seconds = int(total_processing_time)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    text = (
        "📊 Overall Statistics (Transcriber Functions)\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Active Users Today: {active_today}\n\n"
        "⚙️ Processing Statistics\n"
        f"▫️ Total Files Processed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n"
        f"⏱️ Total Processing Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌\n"
        "See you next time! 💫"
    )

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📊 Total Users" and m.from_user.id == ADMIN_ID)
def total_users_admin(message):
    """Admin command to show total registered users (from both user tracking files)."""
    # Count users from the general user_data.json
    total_general_users = len(user_data)

    # Count users from media_downloader_users.txt (avoiding duplicates if possible)
    media_downloader_unique_users = set()
    if os.path.exists(media_downloader_users_file):
        with open(media_downloader_users_file, "r") as f:
            for line in f:
                media_downloader_unique_users.add(line.strip())
    
    # Combine and count unique users from both sources
    all_unique_users = set(user_data.keys()) | media_downloader_unique_users
    total_unique_users = len(all_unique_users)

    bot.send_message(message.chat.id, f"Total unique registered users: {total_unique_users}")

@bot.message_handler(func=lambda m: m.text == "📢 Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast_admin(message):
    """Admin command to initiate a broadcast message."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice', 'sticker']
)
def broadcast_message_admin(message):
    """Admin command to send the broadcast message to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    
    # Get all unique user IDs from both sources
    all_unique_users = set(user_data.keys())
    if os.path.exists(media_downloader_users_file):
        with open(media_downloader_users_file, "r") as f:
            for line in f:
                all_unique_users.add(line.strip())

    success = fail = 0
    for uid_str in all_unique_users:
        try:
            uid = int(uid_str)
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
            elif message.sticker:
                bot.send_sticker(uid, message.sticker.file_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to user {uid_str}: {e}")
            fail += 1
        except Exception as e:
            logging.error(f"Unexpected error sending broadcast to user {uid_str}: {e}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

# --- Transcription Functionality ---
@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_transcription_file(message):
    """Handles incoming voice messages, audio files, video files, and video notes for transcription."""
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)

    # Add reaction emoji based on file type (👀 as requested)
    try:
        bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error setting reaction for message {message.message_id}: {e}")
    except Exception as e:
        logging.error(f"Unexpected error setting reaction for message {message.message_id}: {e}")

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    info = bot.get_file(file_obj.file_id)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg") # Using .ogg as a general intermediate format for whisper

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
            InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
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
        logging.error(f"Error processing file for transcription: {e}")
        bot.edit_message_text("⚠️ An error occurred during transcription.", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
    finally:
        if os.path.exists(local_path):
            os.remove(local_path) # Clean up downloaded file

# --- Language Selection and Saving for Transcription Actions ---

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
        # Update the message text to indicate action, then perform translation
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
        # Update the message text to indicate action, then perform summarization
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
        # Fallback if message_id is not passed (though it should be with current flow)
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
             do_translate_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to translate. Please use the inline buttons on the transcription.")
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
        # Fallback if message_id is not passed
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
            do_summarize_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription.")
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

def transcribe(path: str) -> str | None:
    """Performs the transcription of an audio/video file."""
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

# --- Media Downloader Functionality ---

def is_supported_url(url):
    platforms = ['tiktok.com', 'youtube.com', 'youtu.be', 'pinterest.com', 'pin.it',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

# React with icon and then handle the URL
@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and msg.text.startswith(('http://', 'https://')))
def handle_media_url(msg):
    update_user_activity(msg.from_user.id)
    icon_msg = bot.send_message(msg.chat.id, "👀")
    bot.icon_messages[msg.chat.id] = icon_msg.message_id
    
    if is_youtube_url(msg.text):
        handle_youtube_url_download(msg)
    else:
        handle_social_video_download(msg)

def handle_youtube_url_download(msg):
    url = msg.text
    chat_id = msg.chat.id
    try:
        bot.send_chat_action(chat_id, 'typing')
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'format_sort': ['res:1080', 'ext:mp4'], # Prioritize 1080p mp4
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            description = info.get('description', '')
            formats = info.get('formats', [])

        resolutions = {}
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('height') and f.get('ext') == 'mp4':
                res_key = f'{f["height"]}p'
                if f['height'] <= 1080: # Limit resolutions to 1080p
                    # Only store if it's the best quality for that resolution, or if we don't have it yet
                    if res_key not in resolutions or (f.get('tbr') and f['tbr'] > resolutions[res_key].get('tbr', 0)):
                        resolutions[res_key] = f

        if not resolutions:
            bot.send_message(chat_id, "No suitable MP4 resolutions found for download up to 1080p.")
            if chat_id in bot.icon_messages:
                bot.delete_message(chat_id, bot.icon_messages[chat_id])
                bot.icon_messages.pop(chat_id, None)
            return

        markup = InlineKeyboardMarkup(row_width=3)
        sorted_resolutions = sorted(resolutions.items(), key=lambda x: int(x[0][:-1]))
        for res_str, f_info in sorted_resolutions:
            vid_id = str(uuid.uuid4())[:8]
            bot.video_info[vid_id] = {
                'url': url,
                'format_id': f_info['format_id'],
                'description': description,
                'ext': f_info['ext'] # Store extension
            }
            markup.add(InlineKeyboardButton(res_str, callback_data=f'dl_yt:{vid_id}'))

        bot.send_message(chat_id, f"Choose quality for: {title}", reply_markup=markup)

    except Exception as e:
        logging.error(f"Error handling YouTube URL: {e}")
        bot.send_message(chat_id, f"Error processing YouTube link. Please try again or send a different link.")
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_yt:'))
def download_youtube_video_callback(call):
    vid = call.data.split(":")[1]
    chat_id = call.message.chat.id
    if vid not in bot.video_info:
        bot.answer_callback_query(call.id, "Download expired or invalid request. Try again.")
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        return

    data = bot.video_info.pop(vid) # Remove info after use
    url, fmt, description, ext = data['url'], data['format_id'], data.get('description', ''), data['ext']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.{ext}")

    try:
        bot.answer_callback_query(call.id, "Downloading...")
        bot.edit_message_text("Starting download...", chat_id=call.message.chat.id, message_id=call.message.message_id)

        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'noplaylist': True,
            'external_downloader': 'aria2c', # Use aria2c for faster downloads if available
            'external_downloader_args': ['-x', '16', '-s', '16', '-k', '1M']
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if description:
            final_description = f"{description}\n\nPower by @Bot_makerrBot"
        else:
            final_description = "Power by @Bot_makerrBot"

        bot.send_chat_action(chat_id, 'upload_video')
        with open(video_path, 'rb') as f:
            bot.send_video(chat_id, f, caption=final_description, reply_to_message_id=call.message.message_id)

        subtitle_path = video_path.replace(f'.{ext}', '.en.srt')
        if os.path.exists(subtitle_path):
            with open(subtitle_path, 'rb') as sub_file:
                bot.send_document(chat_id, sub_file, caption="Subtitles")

    except Exception as e:
        logging.error(f"Error downloading YouTube video in callback: {e}")
        bot.send_message(chat_id, f"Error downloading video: {e}")
    finally:
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        # Clean up downloaded files
        for file in os.listdir(DOWNLOAD_DIR):
            os.remove(os.path.join(DOWNLOAD_DIR, file))

def handle_social_video_download(msg):
    url = msg.text
    chat_id = msg.chat.id
    try:
        bot.send_chat_action(chat_id, 'upload_video')
        path, sub_path, description = download_video_any(url)
        if path:
            if description:
                final_description = f"{description}\n\nPower by @Bot_makerrBot"
            else:
                final_description = "Power by @Bot_makerrBot"

            with open(path, 'rb') as f:
                bot.send_video(chat_id, f, caption=final_description)

            if sub_path and os.path.exists(sub_path):
                with open(sub_path, 'rb') as sub:
                    bot.send_document(chat_id, sub, caption="Subtitles")
        else:
            bot.send_message(chat_id, "An error occurred during download.")
    except Exception as e:
        logging.error(f"Error handling social video: {e}")
        bot.send_message(chat_id, f"Error downloading from this link: {e}")
    finally:
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        for file in os.listdir(DOWNLOAD_DIR):
            os.remove(os.path.join(DOWNLOAD_DIR, file))

def download_video_any(url):
    unique_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': output_path,
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'convert_subtitles': 'srt',
        'external_downloader': 'aria2c', # Use aria2c for faster downloads if available
        'external_downloader_args': ['-x', '16', '-s', '16', '-k', '1M'],
        'extractor_args': {
            'pinterest': {
                'video_only': False,
            }
        }
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            description = info.get('description', '')
        
        # Check if the video was merged and the output file exists
        final_video_path = output_path
        if not os.path.exists(final_video_path):
            # yt-dlp might append info to the filename if format is 'bestvideo+bestaudio'
            # Look for a file that starts with the uuid in the downloads directory
            found_files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(unique_id)]
            if found_files:
                final_video_path = os.path.join(DOWNLOAD_DIR, found_files[0])
            else:
                return None, None, ""

        subtitle_path = final_video_path.replace('.mp4', '.en.srt')
        return final_video_path, subtitle_path if os.path.exists(subtitle_path) else None, description
    except Exception as e:
        logging.error(f"Download video any error: {e}")
        return None, None, ""

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    """Handles messages that are not media files or URLs."""
    update_user_activity(message.from_user.id)
    # Check if it's a command being typed
    if message.text.startswith('/'):
        return # Let command handlers manage this

    bot.send_message(message.chat.id, "Please send a valid media file (voice, audio, video) for transcription, or a supported link for downloading.")

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
    # IMPORTANT: Replace with your actual deployed URL
    # If deploying on Render, this will be your Render app URL
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://your-render-app-name.onrender.com") 
    # Example: "https://my-combined-bot.onrender.com"
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
    
    set_bot_info() # Set bot commands and descriptions
    
    # Delete existing webhook to prevent conflicts (useful during development/restarts)
    try:
        bot.delete_webhook()
        logging.info("Old webhook deleted successfully.")
    except Exception as e:
        logging.warning(f"Failed to delete old webhook (might not exist): {e}")

    # Set the webhook to the deployed URL for Render
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://media-transcriber-bot.onrender.com")
    try:
        bot.set_webhook(url=f"{RENDER_URL}/")
        logging.info(f"Webhook set to {RENDER_URL}/")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")

    # Start the Flask app for the webhook
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))

