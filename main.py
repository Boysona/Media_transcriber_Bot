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
import imageio_ffmpeg # Ensure this is installed: pip install imageio-ffmpeg

# --- Configuration ---
# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token (replace with your actual bot token or use environment variables in production)
TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q" 
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID (replace with your actual admin ID)
ADMIN_ID = 5978150981 

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription (Choose model size based on your needs: tiny, base, small, medium, large)
# 'cpu' is for CPU, 'cuda' is for GPU (if available and configured).
# For better performance on CPU, 'int8' or 'float16' compute_type can be used if supported.
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking files
USERS_JSON_FILE = 'users.json'
USER_LANGUAGE_SETTINGS_FILE = 'user_language_settings.json'
# For the broadcast feature from Bot 2, we can also keep a simple list of user IDs
USER_IDS_BROADCAST_FILE = 'users_for_broadcast.txt' 

user_data = {}
if os.path.exists(USERS_JSON_FILE):
    with open(USERS_JSON_FILE, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

user_language_settings = {}
if os.path.exists(USER_LANGUAGE_SETTINGS_FILE):
    with open(USER_LANGUAGE_SETTINGS_FILE, 'r') as f:
        try:
            user_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_language_settings = {}

# In-memory chat history and transcription store
user_memory = {} # For Gemini API chat context
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0 # in seconds
processing_start_time = None

# Gemini API Key
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA" # Replace with your actual Gemini API key.
# It's highly recommended to use environment variables for API keys in a production environment.

# File size limit for media transcription/translation (20MB)
FILE_SIZE_LIMIT = 20 * 1024 * 1024 

# Admin state to manage admin commands (like broadcast)
admin_state = {} 

# Dictionary to store icon message_id for media downloads (from Bot 2)
bot.icon_messages = {}
# Dictionary to store video info for YouTube downloads (from Bot 2)
bot.video_info = {}

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

# --- Helper Functions ---

def save_user_data():
    """Saves the user activity data to a JSON file."""
    with open(USERS_JSON_FILE, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    """Saves the user language settings to a JSON file."""
    with open(USER_LANGUAGE_SETTINGS_FILE, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

def save_user_id_for_broadcast(user_id):
    """Saves a user ID to a text file for broadcast purposes."""
    if not os.path.exists(USER_IDS_BROADCAST_FILE):
        open(USER_IDS_BROADCAST_FILE, "w").close()
    with open(USER_IDS_BROADCAST_FILE, "r+") as f:
        ids = f.read().splitlines()
        if str(user_id) not in ids:
            f.write(f"{user_id}\n")

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

def set_bot_info():
    """Sets the bot's commands, short description, and full description."""
    commands = [
        telebot.types.BotCommand("start", "Restart the bot and get info"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "View instructions on using the bot"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    bot.set_my_commands(commands)

    bot.set_my_short_description(
        "Your go-to bot for transcribing, summarizing, translating, and downloading media from popular platforms!",
        language_code='en'
    )
    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free and in multiple languages.
        
Additionally, it downloads media from various platforms like TikTok, YouTube, Instagram, Pinterest, and Facebook, including video descriptions and subtitles where available.

🔥Enjoy free usage and start now!👌🏻""",
        language_code='en'
    )

def update_user_activity(user_id):
    """Updates the last activity timestamp for a user and saves their ID for broadcast."""
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()
    save_user_id_for_broadcast(user_id) # Save user ID for broadcast list

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

def transcribe(path: str) -> str | None:
    """Performs the transcription of an audio/video file."""
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

def is_supported_download_url(url):
    """Checks if a URL is from a supported platform for downloading."""
    platforms = ['tiktok.com', 'youtube.com', 'youtu.be', 'pinterest.com', 'pin.it',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    """Checks if a URL is a YouTube URL."""
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

def download_video_any(url):
    """Downloads video from various social media platforms using yt-dlp."""
    unique_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s") # Use %(ext)s for dynamic extension
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Prioritize mp4
        'merge_output_format': 'mp4',
        'outtmpl': output_path,
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'convert_subtitles': 'srt',
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(), # Use imageio-ffmpeg's ffmpeg
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'extractor_args': {
            'pinterest': {
                'video_only': False,
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # Find the actual downloaded file path
            downloaded_file = ydl.prepare_filename(info)
            if not downloaded_file.endswith('.mp4'): # Ensure it's the mp4 file after conversion
                downloaded_file = os.path.splitext(downloaded_file)[0] + '.mp4'

            description = info.get('description', '')
            
            # Subtitle path might vary based on the actual filename after download/conversion
            # Find the subtitle file based on the unique_id or downloaded_file base name
            subtitle_file_found = None
            base_filename_no_ext = os.path.splitext(downloaded_file)[0]
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(os.path.basename(base_filename_no_ext)) and f.endswith('.en.srt'):
                    subtitle_file_found = os.path.join(DOWNLOAD_DIR, f)
                    break
            
            return downloaded_file, subtitle_file_found, description
    except Exception as e:
        logging.error(f"Download error: {e}")
        return None, None, ""

# --- Message Handlers ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command."""
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    
    name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("📊 Total Users", "📢 Send Ads (broadcast)", "/status", "/help")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        welcome_text = (
            f"👋 Welcome dear {name}!\n"
            "I'm an all-in-one media bot. Here's what I can do:\n\n"
            "• **Transcribe, Summarize, Translate:** Send me a voice message, video message, or audio file.\n"
            "• **Download Media:** Send me a link from TikTok, YouTube, Instagram, Facebook, or Pinterest.\n\n"
            "For more info, use /help."
        )
        bot.send_message(message.chat.id, welcome_text)

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    update_user_activity(message.from_user.id)
    help_text = (
        """ℹ️ How to use this bot:

**For Transcriptions, Summaries, and Translations:**
1.  **Send a File:** Send a voice message, audio, or video.
2.  **Receive Transcription:** The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
3.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.

**For Media Downloads:**
1.  **Send a Link:** Send a link from supported platforms (TikTok, YouTube, Instagram, Pinterest, Facebook, X/Twitter).
2.  **Receive Media:** The bot will download the media (video or audio), along with descriptions and subtitles if available. For YouTube, you can choose video quality.

**Commands:**
-   `/start`: Restart the bot and get welcome info.
-   `/status`: View bot statistics.
-   `/help`: Display these instructions.
-   `/language`: Change your preferred language for translations and summaries.
-   `/privacy`: View the bot's privacy notice.

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
    * **Media Files:** Voice messages, audio files, video files, and links you send are temporarily processed for transcription or download. These files are **deleted immediately** after processing. We do not permanently store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences, track basic activity (like last seen) to improve bot service, and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and media downloading.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed).
    * To set your preferred language for future interactions.
    * For administrators to send broadcast messages to all users.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties, except as required by the functionality (e.g., sending text to Gemini API for processing).
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Media downloads use `yt-dlp`. Your input to these services is handled according to their respective privacy policies, but we do not store this data after processing.

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
    """Handles the /status command, displaying bot usage statistics."""
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
        "📊 Overall Statistics\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Active Users Today: {active_today}\n"
        f"▫️ Total Registered Users: {len(user_data)}\n\n"
        "⚙️ Media Processing Statistics (Transcription/Translation/Summarization)\n"
        f"▫️ Total Files Processed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos for Transcription: {total_videos}\n"
        f"⏱️ Total Processing Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌\n"
        "See you next time! 💫"
    )

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📊 Total Users" and m.from_user.id == ADMIN_ID)
def total_users_admin(message):
    """Admin command to show total registered users."""
    if os.path.exists(USER_IDS_BROADCAST_FILE):
        with open(USER_IDS_BROADCAST_FILE, "r") as f:
            count = len(f.readlines())
        bot.send_message(message.chat.id, f"👥 Total registered users: {count}")
    else:
        bot.send_message(message.chat.id, "No users yet.")

@bot.message_handler(func=lambda m: m.text == "📢 Send Ads (broadcast)" and m.from_user.id == ADMIN_ID)
def send_broadcast_admin(message):
    """Admin command to initiate a broadcast message."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now (text, photo, video, etc.):")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice', 'sticker']
)
def broadcast_message_admin(message):
    """Admin command to send the broadcast message to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    
    if not os.path.exists(USER_IDS_BROADCAST_FILE):
        bot.send_message(message.chat.id, "No users to broadcast to.")
        return
    
    with open(USER_IDS_BROADCAST_FILE, "r") as f:
        user_ids = f.read().splitlines()
    
    success = fail = 0
    bot.send_message(message.chat.id, "Starting broadcast... This might take a while.")
    
    for uid in user_ids:
        try:
            uid = int(uid)
            # Prevent sending broadcast to admin him/herself
            if uid == ADMIN_ID:
                continue

            if message.text:
                bot.send_message(uid, message.text)
            elif message.photo:
                # message.photo is a list of photo sizes, pick the largest
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
            logging.error(f"Failed to send broadcast to user {uid}: {e}")
            fail += 1
        except Exception as e:
            logging.error(f"Unexpected error sending broadcast to user {uid}: {e}")
            fail += 1
    
    bot.send_message(
        message.chat.id,
        f"✅ Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

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

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_transcription_file(message):
    """Handles incoming voice messages, audio files, video files, and video notes for transcription."""
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)

    try:
        # Add reaction emoji
        bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error setting reaction for message {message.message_id}: {e}")

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    info = bot.get_file(file_obj.file_id)
    # Using .ogg as a general intermediate format for whisper, it handles various inputs.
    # The actual extension from info.file_path is better for more accurate download.
    file_extension = os.path.splitext(info.file_path)[1] if info.file_path else ".ogg"
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}{file_extension}") 

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
            fn = os.path.join(DOWNLOAD_DIR, 'transcription.txt') # Save in DOWNLOAD_DIR
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
        # Clean up downloaded file
        if os.path.exists(local_path):
            os.remove(local_path) 
        # Clean up transcription file if it was created
        if 'fn' in locals() and os.path.exists(fn):
            os.remove(fn)

@bot.message_handler(func=lambda msg: msg.text and is_supported_download_url(msg.text))
def handle_download_url(message):
    """Handles messages containing URLs for media downloading."""
    update_user_activity(message.from_user.id)
    url = message.text
    chat_id = message.chat.id

    try:
        # React with an eye emoji
        icon_msg = bot.send_message(chat_id, "👀")
        bot.icon_messages[chat_id] = icon_msg.message_id

        if is_youtube_url(url):
            handle_youtube_download(message)
        else:
            handle_social_media_download(message)
    except Exception as e:
        logging.error(f"Error handling download URL: {e}")
        bot.send_message(chat_id, f"Error processing your request: {e}")
        # Clean up icon message if an error occurs early
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)

def handle_youtube_download(message):
    """Handles YouTube URL downloads, offering quality selection."""
    url = message.text
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, 'typing')
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            description = info.get('description', '')  # Video description
            formats = info.get('formats', [])

        # Prepare quality options up to 1080p, preferring mp4
        resolutions = {}
        for f in formats:
            # Check for video and audio streams, prefer mp4 container
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('ext') == 'mp4' and f.get('height') and f.get('height') <= 1080:
                resolutions[f'{f["height"]}p'] = f['format_id']
            # If no combined mp4, allow best available video + audio (will be merged to mp4)
            elif f.get('vcodec') != 'none' and f.get('height') and f.get('height') <= 1080 and f.get('format_id') not in resolutions.values():
                 resolutions[f'{f["height"]}p'] = f['format_id']
        
        if not resolutions:
            bot.send_message(chat_id, "No suitable video qualities found for download (only up to 1080p supported).")
            # Delete the eye emoji
            if chat_id in bot.icon_messages:
                bot.delete_message(chat_id, bot.icon_messages[chat_id])
                bot.icon_messages.pop(chat_id, None)
            return

        markup = InlineKeyboardMarkup(row_width=3)
        # Sort resolutions numerically
        sorted_resolutions = sorted(resolutions.items(), key=lambda x: int(x[0][:-1]))
        for res, fid in sorted_resolutions:
            vid_id = str(uuid.uuid4())[:8] # Unique ID for callback data
            bot.video_info[vid_id] = { # Store info for later callback
                'url': url,
                'format_id': fid,
                'description': description
            }
            markup.add(InlineKeyboardButton(res, callback_data=f'dl_youtube:{vid_id}'))

        bot.send_message(chat_id, f"Choose quality for: **{title}**", reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error handling YouTube URL: {e}")
        bot.send_message(chat_id, f"Error: Could not retrieve YouTube video info. {e}")
        # Delete the eye emoji
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)

def handle_social_media_download(message):
    """Handles social media URL downloads (non-YouTube)."""
    url = message.text
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, 'upload_video')
        path, sub_path, description = download_video_any(url)
        
        if path and os.path.exists(path):
            final_caption = f"{description}\n\nPower by @Bot_makerrBot" if description else "Power by @Bot_makerrBot"
            
            with open(path, 'rb') as f:
                bot.send_video(chat_id, f, caption=final_caption)

            if sub_path and os.path.exists(sub_path):
                with open(sub_path, 'rb') as sub:
                    bot.send_document(chat_id, sub, caption="Subtitles")
        else:
            bot.send_message(chat_id, "An error occurred during download or no media was found.")
    except Exception as e:
        logging.error(f"Error handling social media download: {e}")
        bot.send_message(chat_id, f"Error: {e}")
    finally:
        # Delete the eye emoji
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        # Clean up the downloads directory
        for file in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, file)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                logging.error(f'Failed to delete {file_path}. Reason: {e}')


@bot.callback_query_handler(func=lambda call: call.data.startswith('set_lang|'))
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

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_youtube:'))
def download_youtube_video_callback(call):
    """Callback handler for downloading YouTube videos after quality selection."""
    vid_id = call.data.split(":")[1]
    chat_id = call.message.chat.id
    
    if vid_id not in bot.video_info:
        bot.answer_callback_query(call.id, "Download link expired. Please try sending the URL again.")
        # Delete the eye emoji
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        return

    data = bot.video_info.pop(vid_id) # Remove info after use to prevent re-downloads
    url, fmt, description = data['url'], data['format_id'], data.get('description', '')
    
    # Edit the inline keyboard message to show "Downloading..."
    bot.edit_message_text(
        "Downloading your YouTube video... Please wait.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )
    bot.answer_callback_query(call.id, "Downloading started...")

    basename = str(uuid.uuid4())
    # Use generic mp4 for output template, yt-dlp will handle extension
    output_path = os.path.join(DOWNLOAD_DIR, f"{basename}.%(ext)s") 

    try:
        ydl_opts = {
            'format': fmt,
            'outtmpl': output_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file_path = ydl.prepare_filename(info)
            # Ensure it's the .mp4 file after conversion
            if not downloaded_file_path.endswith('.mp4'):
                downloaded_file_path = os.path.splitext(downloaded_file_path)[0] + '.mp4'


        final_caption = f"{description}\n\nPower by @Bot_makerrBot" if description else "Power by @Bot_makerrBot"

        if os.path.exists(downloaded_file_path):
            with open(downloaded_file_path, 'rb') as f:
                bot.send_video(chat_id, f, caption=final_caption)

            # Check for subtitle file
            subtitle_file_found = None
            base_filename_no_ext = os.path.splitext(downloaded_file_path)[0]
            for f_name in os.listdir(DOWNLOAD_DIR):
                if f_name.startswith(os.path.basename(base_filename_no_ext)) and f_name.endswith('.en.srt'):
                    subtitle_file_found = os.path.join(DOWNLOAD_DIR, f_name)
                    break
            
            if subtitle_file_found and os.path.exists(subtitle_file_found):
                with open(subtitle_file_found, 'rb') as sub_file:
                    bot.send_document(chat_id, sub_file, caption="Subtitles")
        else:
            bot.send_message(chat_id, "Downloaded video file not found.")

    except Exception as e:
        logging.error(f"Error downloading YouTube video in callback: {e}")
        bot.send_message(chat_id, f"Error downloading video: {e}")
    finally:
        # Delete the eye emoji
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        # Clean up the downloads directory
        for file in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, file)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                logging.error(f'Failed to delete {file_path}. Reason: {e}')


@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    """Handles messages that are not media files or supported URLs for download."""
    update_user_activity(message.from_user.id)
    if message.chat.id == ADMIN_ID and admin_state.get(message.chat.id) == 'awaiting_broadcast':
        # This case is handled by broadcast_message_admin, but if it falls here, it means
        # the content type wasn't explicitly listed in the broadcast handler, or an error occurred.
        # It's safer to have the broadcast handler handle all relevant types.
        # For now, we can just log it or ignore, as the broadcast handler already has `content_types`.
        pass
    else:
        bot.send_message(message.chat.id, "Please send a voice message, audio, video file for transcription, or a valid media link for download.")

# --- Webhook and Application Startup ---

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
    # Example: "https://media-transcriber-bot.onrender.com"
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://your-render-app-name.onrender.com") # Default for local testing
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
    # Make sure 'your-render-app-name.onrender.com' is replaced with your actual Render app URL
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://your-render-app-name.onrender.com")
    
    try:
        bot.set_webhook(url=f"{RENDER_URL}/")
        logging.info(f"Webhook set to {RENDER_URL}/")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        # If webhook setting fails, the bot might not receive updates correctly.
        # Consider logging this error prominently or exiting if critical.

    # Start the Flask app for the webhook
    # Render provides the PORT environment variable
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
