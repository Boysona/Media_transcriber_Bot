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
import subprocess
from PIL import Image
from io import BytesIO

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot tokens and configurations
BOT_TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4"
ADMIN_ID = 5978150981
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com"
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW2Do2R8HA" # Replace with your actual Gemini API key.

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking file (for transcription bot's user activity)
users_file = 'users.json'
user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

# User-specific language settings (for transcription bot's translate/summarize)
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
user_memory = {} # For Gemini API chat context
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0 # for transcription
total_audio_files = 0
total_voice_clips = 0
total_videos_transcribed = 0 # Renamed to differentiate from downloaded videos
total_processing_time = 0  # in seconds
processing_start_time = None

# Video download statistics
total_videos_downloaded = 0
total_youtube_downloads = 0
total_social_downloads = 0

# Admin state for broadcast
admin_state = {}

# File size limit for transcription media
FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB

# CTA button link for downloaded videos
BOT_USERNAME = "@transcriberbo"
POWERED_BY_CAPTION = f"Powered by {BOT_USERNAME}"

def get_more_info_button():
    keyboard = InlineKeyboardMarkup()
    # You can update this URL to something relevant to your bot, e.g., a help page, a channel, etc.
    button = InlineKeyboardButton(text="More Info", url=f"https://t.me/{BOT_USERNAME.replace('@','')}")
    keyboard.add(button)
    return keyboard

def get_user_display_name(user):
    """Returns the user's full display name (first_name + last_name) or username."""
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    elif user.first_name:
        return user.first_name
    elif user.username:
        return f"@{user.username}"
    return "there" # Fallback if no name or username

def ask_gemini(user_id, user_message):
    """Sends a message to the Gemini API and returns the response."""
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:] # Keep only the last 10 messages for context
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

def set_bot_info():
    """Sets the bot's commands, short description, and full description.
    These are commented out to prevent automatic setting on Telegram at runtime,
    but serve as a reference for manual setting or future activation.
    """
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "View instructions"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    # bot.set_my_commands(commands) # Uncomment to set bot commands

    # bot.set_my_short_description( # Uncomment to set bot short description
    #     "I can transcribe audio/video to text and download videos from links."
    # )

    # bot.set_my_description( # Uncomment to set bot full description
    #     """Hello! I'm your versatile media bot.
        
# Here's what I can do:
# * **Transcribe Media:** Send me any voice message, audio file, or video, and I'll convert the speech to text for you.
# * **Translate & Summarize:** After transcription, you'll get options to translate or summarize the text into your preferred language.
# * **Download Videos:** Send me a link from social media platforms (like TikTok, YouTube, Instagram, X/Twitter, Pinterest, Facebook, Snapchat) and I'll download the video for you. For YouTube, you can even choose the video quality!

# I'm here to make your media management easier. Try sending me a file or a link!
# """
# )

def update_user_activity(user_id):
    """Updates the last activity timestamp for a user."""
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command."""
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()
    
    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        bot.send_message(message.chat.id, "Welcome to the Admin Panel! What would you like to do?", reply_markup=keyboard)
    else:
        display_name = get_user_display_name(message.from_user)
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome, {display_name}!
• Send me a **voice message**, **video message**, or **audio file** to transcribe.
• Send me a **video link** (TikTok, YouTube, etc.) to download.
It's all free!"""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    help_text = (
        """ℹ️ How to use this bot:

This bot offers two main features: **transcribing media** and **downloading videos from links**.

1.  **For Transcription:**
    * **Send a File:** Send a voice message, audio, or video.
    * **Receive Transcription:** The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
    * **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.

2.  **For Video Downloads:**
    * **Send a Link:** Send a link from supported platforms like TikTok, YouTube, Pinterest, Instagram, Snapchat, Facebook, X/Twitter.
    * **Receive Video:** The bot will download the video and send it to you.
    * **YouTube Specific:** For YouTube links, you will be prompted to select your desired video quality.

3.  **Commands:**
    -   `/start`: Restart the bot and see main options.
    -   `/status`: View bot usage statistics.
    -   `/help`: Display these instructions.
    -   `/language`: Change your preferred language for translations and summaries.
    -   `/privacy`: View the bot's privacy notice.

Enjoy using the bot! If you have any questions, feel free to contact the administrator through the "More Info" button on downloaded videos."""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    """Handles the /privacy command, displaying the bot's privacy policy."""
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and video links you send are temporarily processed for transcription or download. These files are **deleted immediately** after processing. We do not permanently store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and video downloading.
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
        "📊 Overall Bot Statistics\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Active Users Today: {active_today}\n"
        f"▫️ Total Registered Users: {len(user_data)}\n\n"
        "⚙️ Media Processing Statistics (Transcription)\n"
        f"▫️ Total Files Transcribed: {total_files_processed}\n"
        f"▫️ Audio Files Transcribed: {total_audio_files}\n"
        f"▫️ Voice Clips Transcribed: {total_voice_clips}\n"
        f"▫️ Videos Transcribed: {total_videos_transcribed}\n"
        f"⏱️ Total Transcription Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "🎬 Video Download Statistics\n"
        f"▫️ Total Videos Downloaded: {total_videos_downloaded}\n"
        f"▫️ YouTube Downloads: {total_youtube_downloads}\n"
        f"▫️ Social Media Downloads (Other): {total_social_downloads}\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌"
    )

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users_admin(message):
    """Admin command to show total registered users."""
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast_admin(message):
    """Admin command to initiate a broadcast message."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document']
)
def broadcast_message(message):
    """Admin command to send the broadcast message to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    success = fail = 0
    for uid in user_data:
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to user {uid}: {e}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_transcription_file(message):
    """Handles incoming voice messages, audio files, video files, and video notes for transcription."""
    global total_files_processed, total_audio_files, total_voice_clips, total_videos_transcribed, total_processing_time
    update_user_activity(message.from_user.id)

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

    processing_message = bot.send_message(message.chat.id, "Processing your file for transcription... Please wait.")

    try:
        data = bot.download_file(info.file_path)
        with open(local_path, 'wb') as f:
            f.write(data)

        bot.edit_message_text("Transcribing...", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
        
        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe_media(local_path) or ""
        uid = str(message.from_user.id)

        user_transcriptions.setdefault(uid, {})[message.message_id] = transcription

        total_files_processed += 1
        if message.voice:
            total_voice_clips += 1
        elif message.audio:
            total_audio_files += 1
        elif message.video or message.video_note:
            total_videos_transcribed += 1

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

# --- Language Selection and Saving (for Transcription features) ---

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
    markup.add(*buttons) # Add all buttons at once
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
        fn = 'summary.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(summary)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Summary in {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)

def transcribe_media(path: str) -> str | None:
    """Performs the transcription of an audio/video file."""
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

# --- Video Download Features ---

def is_supported_url(url):
    """Checks if the URL is from a supported social media platform."""
    platforms = ['tiktok.com', 'youtube.com', 'youtu.be', 'pinterest.com', 'pin.it',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    """Checks if the URL is a YouTube URL."""
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

@bot.message_handler(func=lambda msg: is_youtube_url(msg.text) and (msg.text.startswith('http://') or msg.text.startswith('https://')))
def handle_youtube_url(msg):
    global total_videos_downloaded, total_youtube_downloads
    url = msg.text
    update_user_activity(msg.from_user.id)
    try:
        bot.send_chat_action(msg.chat.id, 'typing')
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True,
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            thumbnail_url = info_dict.get('thumbnail')
            title = info_dict.get('title', 'YouTube Video')
            formats = info_dict.get('formats', [])

        resolutions = {}
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('height') is not None:
                height = f['height']
                if height <= 1080: # Filter for common qualities
                    resolutions[f'{height}p'] = f['format_id']

        if not resolutions:
            bot.send_message(msg.chat.id, "Sorry, no suitable video qualities could be found for this YouTube video.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        buttons = []
        sorted_resolutions = sorted(resolutions.keys(), key=lambda x: int(x[:-1]), reverse=True) # Higher quality first
        for res in sorted_resolutions:
            format_id = resolutions[res]
            video_id = str(uuid.uuid4())[:8] # Unique ID for callback
            # Store video info with a unique ID for later retrieval in callback
            bot.video_info = getattr(bot, 'video_info', {}) # Initialize if not exists
            bot.video_info[video_id] = {'url': url, 'format_id': format_id}
            buttons.append(InlineKeyboardButton(res, callback_data=f'dl_yt:{video_id}'))
        markup.add(*buttons)

        if thumbnail_url:
            try:
                response = requests.get(thumbnail_url)
                response.raise_for_status()
                image = Image.open(BytesIO(response.content))
                bio = BytesIO()
                image.save(bio, 'JPEG')
                bio.seek(0)
                bot.send_photo(msg.chat.id, photo=bio, caption=f"Please select the quality to download for: **{title}**", reply_markup=markup, parse_mode="Markdown")
            except requests.exceptions.RequestException as e:
                logging.error(f"Error downloading YouTube thumbnail: {e}")
                bot.send_message(msg.chat.id, f"Please select the quality to download for: **{title}**", reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Error processing YouTube thumbnail: {e}")
                bot.send_message(msg.chat.id, f"Please select the quality to download for: **{title}**", reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(msg.chat.id, f"Please select the quality to download for: **{title}**", reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error extracting YouTube info for {url}: {e}")
        bot.send_message(msg.chat.id, f"❌ An error occurred while fetching YouTube video information. Please check the link or try again later.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_yt:'))
def download_youtube_video(call):
    global total_videos_downloaded, total_youtube_downloads
    output_path = None
    try:
        video_id = call.data.split(':')[1]
        if not hasattr(bot, 'video_info') or video_id not in bot.video_info:
            bot.answer_callback_query(call.id, "Download link expired. Please send the YouTube link again.")
            bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None) # Remove old buttons
            return
        
        video_data = bot.video_info.pop(video_id) # Remove from memory after selection
        url = video_data['url']
        format_id = video_data['format_id']

        bot.answer_callback_query(call.id, f"Starting download for selected quality...")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Downloading your YouTube video ({format_id})... This might take a moment.",
            reply_markup=None # Remove quality selection buttons
        )
        
        bot.send_chat_action(call.message.chat.id, 'record_video')
        output_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")
        ydl_opts = {
            'format': format_id,
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': 'mp4', # Ensure output is mp4
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        bot.send_chat_action(call.message.chat.id, 'upload_video')
        with open(output_path, 'rb') as video_file:
            bot.send_video(
                call.message.chat.id,
                video_file,
                caption=POWERED_BY_CAPTION,
                reply_markup=get_more_info_button()
            )
        total_videos_downloaded += 1
        total_youtube_downloads += 1
    except Exception as e:
        logging.error(f"Error downloading YouTube video from callback: {e}")
        bot.send_message(call.message.chat.id, f"❌ An error occurred during YouTube video download. Please try again.")
    finally:
        if output_path and os.path.exists(output_path):
            os.remove(output_path)

@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text) and (msg.text.startswith('http://') or msg.text.startswith('https://')))
def handle_social_video_url(msg):
    """Handles incoming social media video links (non-YouTube)."""
    global total_videos_downloaded, total_social_downloads
    url = msg.text
    video_path = None
    update_user_activity(msg.from_user.id)
    try:
        processing_message = bot.send_message(msg.chat.id, "Downloading your video... This might take a moment.")
        bot.send_chat_action(msg.chat.id, 'record_video')
        
        # Call download_video_any, which now returns video_path and metadata
        video_path, metadata = download_video_any(url)
        
        caption_text = POWERED_BY_CAPTION
        if metadata:
            if metadata.get('description'):
                caption_text = f"{metadata['description']}\n\n{caption_text}"
            elif metadata.get('title'):
                caption_text = f"{metadata['title']}\n\n{caption_text}"
            
            # Add hashtags if present
            if metadata.get('tags'):
                hashtags = " ".join([f"#{tag.replace(' ', '_')}" for tag in metadata['tags']])
                caption_text = f"{caption_text}\n\n{hashtags}"

        # Truncate caption if too long for Telegram (max 1024 characters)
        if len(caption_text) > 1024:
            caption_text = caption_text[:1020] + "..."

        bot.edit_message_text("Uploading your video...", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
        bot.send_chat_action(msg.chat.id, 'upload_video')
        with open(video_path, 'rb') as video_file:
            bot.send_video(
                msg.chat.id,
                video_file,
                caption=caption_text,
                reply_markup=get_more_info_button(),
                parse_mode="Markdown" # Use Markdown for caption if you want bold/etc.
            )
        total_videos_downloaded += 1
        total_social_downloads += 1
        bot.delete_message(chat_id=processing_message.chat.id, message_id=processing_message.message_id) # Delete "uploading" message
    except Exception as e:
        logging.error(f"Error processing social media video link {url}: {e}")
        bot.send_message(msg.chat.id, f"❌ An error occurred while downloading the video. Please check the link or try again later.")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)

def download_video_any(url):
    """Downloads video from any supported social media URL using yt_dlp.
    Returns the path to the downloaded video and its metadata.
    """
    unique_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Prioritize mp4 with separate video/audio then merge
        'merge_output_format': 'mp4',
        'outtmpl': output_path,
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'writedescription': True, # To get description/hashtags
        'writethumbnail': True, # To ensure we get thumbnail info if available
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}, # Ensure audio is extracted and merged
            {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}, # Convert to mp4 if not already
        ],
        'extractor_args': {
            'pinterest': {
                'video_only': False,
            },
            'instagram': { # Specific Instagram options if needed
                'client_id': 'YOUR_INSTAGRAM_CLIENT_ID', # Replace with actual client ID if you have one
                'client_secret': 'YOUR_INSTAGRAM_CLIENT_SECRET' # Replace with actual client secret if you have one
            },
            'facebook': { # Specific Facebook options if needed
                'client_id': 'YOUR_FACEBOOK_CLIENT_ID', # Replace with actual client ID if you have one
                'client_secret': 'YOUR_FACEBOOK_CLIENT_SECRET' # Replace with actual client secret if you have one
            }
        },
        'keepvideo': False, # Delete intermediate files
    }

    info_dict = {}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            # yt-dlp might return a dictionary even if download fails for some reason
            # Check if the file actually exists
            if not os.path.exists(output_path):
                # If the file doesn't exist at the expected path, it might be due to format issues.
                # yt_dlp might have changed the extension or the merge didn't happen.
                # Let's try to find the actual file if it was downloaded with a different extension.
                # This is a fallback and might not catch all cases.
                downloaded_files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(unique_id)]
                if downloaded_files:
                    output_path = os.path.join(DOWNLOAD_DIR, downloaded_files[0])
                    logging.warning(f"Found downloaded file with different name: {output_path}")
                else:
                    raise Exception("Video download failed: No file found at expected path.")
    except Exception as e:
        logging.error(f"yt_dlp download error for {url}: {e}")
        # Clean up any partial downloads if an error occurs
        if os.path.exists(output_path):
            os.remove(output_path)
        # Attempt to remove any other files with the unique_id prefix
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(unique_id):
                os.remove(os.path.join(DOWNLOAD_DIR, f))
        raise # Re-raise the exception to be caught by the calling handler

    return output_path, info_dict


@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    """Handles messages that are not recognized as media files or supported links."""
    update_user_activity(message.from_user.id)
    # Check if it's a text message that looks like an unsupported URL
    if message.text and (message.text.startswith('http://') or message.text.startswith('https://')):
        bot.send_message(message.chat.id, "I can only download videos from supported platforms like TikTok, YouTube, Instagram, etc. Please send a valid link or a voice/audio/video file for transcription.")
    else:
        bot.send_message(message.chat.id, "I can only process voice messages, audio files, video files for transcription, or video links from supported platforms for download. Please send one of these.")

# --- Webhook setup ---

@app.route('/', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram updates."""
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook_route', methods=['GET','POST'])
def set_webhook_manual_route():
    """Route to manually set the webhook (for testing/debugging)."""
    try:
        bot.set_webhook(url=f"{WEBHOOK_URL}/")
        logging.info(f"Webhook manually set to {WEBHOOK_URL}/")
        return f"Webhook manually set to {WEBHOOK_URL}/", 200
    except Exception as e:
        logging.error(f"Failed to manually set webhook: {e}")
        return f"Failed to manually set webhook: {e}", 500

@app.route('/delete_webhook_route', methods=['GET','POST'])
def delete_webhook_manual_route():
    """Route to manually delete the webhook (for testing/debugging)."""
    try:
        bot.delete_webhook()
        logging.info('Webhook manually deleted.')
        return 'Webhook manually deleted.', 200
    except Exception as e:
        logging.error(f"Failed to manually delete webhook: {e}")
        return f"Failed to manually delete webhook: {e}", 500

if __name__ == "__main__":
    # Clean up old downloads directory on startup for a fresh start
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    set_bot_info() # Set bot commands and descriptions (these are now commented out inside the function)

    # Delete any existing webhook before setting a new one
    try:
        bot.delete_webhook()
        logging.info("Existing webhook deleted successfully.")
    except Exception as e:
        logging.warning(f"Failed to delete existing webhook (might not exist): {e}")

    # Set the webhook to the deployed URL
    try:
        bot.set_webhook(url=f"{WEBHOOK_URL}/")
        logging.info(f"Webhook set to {WEBHOO_URL}/")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        # If webhook setting fails, the bot might not receive updates correctly.
        # This is a critical error for webhook-based deployment.

    # Start the Flask app for the webhook
    # Render provides the PORT environment variable
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"Flask app starting on host 0.0.0.0, port {port}")
    app.run(host="0.0.0.0", port=port)
