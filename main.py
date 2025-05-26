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

# Bot token (bot 1 iyo bot 2 labadaba)
TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID (bot 1 iyo bot 2 labadaba)
ADMIN_ID = 5978150981

# Download directory (bot 1 iyo bot 2 labadaba)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking file (bot 1)
users_file = 'users.json'
user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

# User-specific language settings (bot 1)
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

# In-memory chat history and transcription store (bot 1)
user_memory = {} # For Gemini API chat context
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables) (bot 1)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0  # in seconds
processing_start_time = None

# Gemini API Key (bot 1)
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"

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

# Dictionary lagu keydinayo icon message_id marka link la soo diro (Bot 2)
bot.icon_messages = {}

# User tracking file (Bot 2)
USER_FILE = "users.txt"

def save_user_id(user_id):
    if not os.path.exists(USER_FILE):
        open(USER_FILE, "w").close()
    with open(USER_FILE, "r+") as f:
        ids = f.read().splitlines()
        if str(user_id) not in ids:
            f.write(f"{user_id}\n")

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

    # Short description (About) - Isku-dar labadii short description
    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds! Your go-to bot for downloading media from popular platforms!"
    )

    # Full description (What can this bot do?) - Isku-dar labadii full description
    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free and in multiple languages.

🔥Enjoy free usage and start now!👌🏻

I am also a media downloader bot. Send me links from TikTok, YouTube, Instagram, Pinterest, and Facebook, and I'll download the media for you, including video descriptions and subtitles where available.
"""
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
    save_user_id(message.from_user.id) # Save user for Bot 2's user tracking

    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()

    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("📢 Send Ads (broadcast)", "📊 Total Users", "/status")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        name = message.from_user.first_name # Isticmaal magaca caadiga ah
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear {name}!
• Send me:
• Voice message
• Video message
• Audio file
• to transcribe for free

Or send me a link from TikTok, YouTube, Instagram, Facebook, or other supported platforms, and I’ll download it for you.
"""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, and videos, and also downloads media from various platforms.

**Transcription & Summarization:**
1.  **Send a File:** Send a voice message, audio, or video.
2.  **Receive Transcription:** The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
3.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.

**Media Downloading:**
1.  **Send a Link:** Send a link from supported platforms (TikTok, YouTube, Instagram, Pinterest, Facebook, X/Twitter, Snapchat, etc.).
2.  **Receive Media:** The bot will download the media for you, including video descriptions and subtitles where available. For YouTube, you will get quality options.

**Commands:**
-   `/start`: Restart the bot.
-   `/status`: View bot statistics.
-   `/help`: Display these instructions.
-   `/language`: Change your preferred language for translations and summaries.
-   `/privacy`: View the bot's privacy notice.

Enjoy transcribing and downloading your media quickly and easily!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    """Handles the /privacy command, displaying the bot's privacy policy."""
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and links you send for downloading are temporarily processed. These files are **deleted immediately** after processing. We do not store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and media downloading.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed, total users).
    * To set your preferred language for future interactions.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Your text input to these models is handled according to their respective privacy policies, but we do not store this data after processing.
    * Media downloading uses `yt-dlp`, which accesses public media content directly from the source platforms. No user-identifiable data is shared with these platforms through this process.

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
        f"▫️ Total Users Today: {active_today}\n\n"
        "⚙️ Processing Statistics\n"
        f"▫️ Total Files Processed (Transcription): {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n"
        f"⏱️ Total Transcription Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌\n"
        "See you next time!💫"
    )

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📊 Total Users" and m.from_user.id == ADMIN_ID)
def total_users_admin(msg):
    """Admin command to show total registered users (from both bots)."""
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            count = len(f.readlines())
        bot.send_message(msg.chat.id, f"👥 Total registered users: {count}")
    else:
        bot.send_message(msg.chat.id, "No users yet.")

@bot.message_handler(func=lambda m: m.text == "📢 Send Ads (broadcast)" and m.from_user.id == ADMIN_ID)
def send_broadcast(message):
    """Admin command to initiate a broadcast message."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice', 'sticker']
)
def broadcast_message(message):
    """Admin command to send the broadcast message to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    success = fail = 0
    if not os.path.exists(USER_FILE):
        bot.send_message(message.chat.id, "No users to broadcast to.")
        return

    with open(USER_FILE, "r") as f:
        user_ids = f.read().splitlines()

    for uid_str in user_ids:
        try:
            uid = int(uid_str)
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to user {uid_str}: {e}")
            fail += 1
        except ValueError:
            logging.error(f"Invalid user ID in file: {uid_str}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
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
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message.message_id}")
        )

        if len(transcription) > 4000:
            fn = 'transcription.txt'
            with open(fn, 'w', encoding='utf-8') as f:
                f.write(transcription)

            bot.edit_message_text("Sending transcription as a file...", chat_id=processing_message.chat.id, message_id=processing_message.message_id)
            with open(fn, 'rb') as doc:
                # Gaabi caption-ka halkan
                caption_text = "Here’s your transcription. Tap a button below to translate or summarize."
                if len(caption_text) > 1000: # Xaddidaad 1000 xaraf ah
                    caption_text = caption_text[:997] + "..."
                bot.send_document(
                    message.chat.id,
                    doc,
                    reply_to_message_id=message.message_id,
                    reply_markup=buttons,
                    caption=caption_text
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
        fn = 'translation.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(translated)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            # Gaabi caption-ka halkan
            caption_text = f"Translation to {lang}"
            if len(caption_text) > 1000: # Xaddidaad 1000 xaraf ah
                caption_text = caption_text[:997] + "..."
            bot.send_document(message.chat.id, doc, caption=caption_text, reply_to_message_id=message_id)
        os.remove(fn)
    else:
        # Hubi in qoraalka aanu ka badan 4096 xaraf (max fariinta text-ka)
        if len(translated) > 4096:
            translated = translated[:4093] + "..."
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
            # Gaabi caption-ka halkan
            caption_text = f"Summary in {lang}"
            if len(caption_text) > 1000: # Xaddidaad 1000 xaraf ah
                caption_text = caption_text[:997] + "..."
            bot.send_document(message.chat.id, doc, caption=caption_text, reply_to_message_id=message_id)
        os.remove(fn)
    else:
        # Hubi in qoraalka aanu ka badan 4096 xaraf
        if len(summary) > 4096:
            summary = summary[:4093] + "..."
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)


@bot.message_handler(commands=['translate'])
def handle_translate(message):
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
def handle_summarize(message):
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

# --- Media Downloader Functions (from Bot 2) ---

def is_supported_url(url):
    platforms = ['tiktok.com', 'youtube.com', 'youtu.be', 'instagram.com', 'pinterest.com', 'pin.it',
                 'facebook.com', 'fb.watch', 'x.com', 'twitter.com', 'snapchat.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and msg.text.startswith(('http://', 'https://')))
def handle_media_link(msg):
    url = msg.text
    chat_id = msg.chat.id
    icon_msg = bot.send_message(chat_id, "👀")
    bot.icon_messages[chat_id] = icon_msg.message_id

    if is_youtube_url(url):
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
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            description = info.get('description', '')
            formats = info.get('formats', [])

        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') and f['height'] <= 1080}

        if not resolutions:
            bot.send_message(chat_id, "No suitable resolutions found for download.")
            if chat_id in bot.icon_messages:
                bot.delete_message(chat_id, bot.icon_messages[chat_id])
                bot.icon_messages.pop(chat_id, None)
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1])):
            vid_id = str(uuid.uuid4())[:8]
            if not hasattr(bot, 'video_info'):
                bot.video_info = {}
            bot.video_info[vid_id] = {
                'url': url,
                'format_id': fid,
                'description': description
            }
            markup.add(InlineKeyboardButton(res, callback_data=f'dl:{vid_id}'))

        bot.send_message(chat_id, f"Choose quality for: {title}", reply_markup=markup)

    except Exception as e:
        logging.error(f"Error handling YouTube URL: {e}")
        bot.send_message(chat_id, f"Error: {e}")
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video(call):
    vid = call.data.split(":")[1]
    chat_id = call.message.chat.id
    if not hasattr(bot, 'video_info') or vid not in bot.video_info:
        bot.answer_callback_query(call.id, "Download link expired. Please send the link again.")
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        return

    data = bot.video_info.pop(vid) # Remove from memory after selection
    url, fmt, description = data['url'], data['format_id'], data.get('description', '')
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")

    try:
        bot.answer_callback_query(call.id, "Downloading...")
        bot.edit_message_text("Downloading selected quality...", chat_id=call.message.chat.id, message_id=call.message.message_id)

        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        final_description = ""
        if description:
            final_description = f"{description}\n\n"
        final_description += "Power by @Bot_makerrBot"

        # Gaabi description-ka si uusan u dhaafin xaddidaadda Telegram (1024 xaraf)
        if len(final_description) > 1024:
            final_description = final_description[:1021] + "..."

        with open(video_path, 'rb') as f:
            bot.send_video(chat_id, f, caption=final_description, reply_to_message_id=call.message.message_id)

        subtitle_path = video_path.replace('.mp4', '.en.srt')
        if os.path.exists(subtitle_path):
            with open(subtitle_path, 'rb') as sub_file:
                bot.send_document(chat_id, sub_file, caption="Subtitles")

    except Exception as e:
        logging.error(f"Error downloading YouTube video: {e}")
        bot.send_message(chat_id, f"Error downloading: {e}")
    finally:
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        cleanup_downloads_dir()

def handle_social_video_download(msg):
    url = msg.text
    chat_id = msg.chat.id
    try:
        bot.send_chat_action(chat_id, 'upload_video')
        path, sub_path, description = download_video_any_platform(url)
        if path:
            final_description = ""
            if description:
                final_description = f"{description}\n\n"
            final_description += "Power by @Bot_makerrBot"

            # Gaabi description-ka si uusan u dhaafin xaddidaadda Telegram (1024 xaraf)
            if len(final_description) > 1024:
                final_description = final_description[:1021] + "..."

            with open(path, 'rb') as f:
                bot.send_video(chat_id, f, caption=final_description)

            if sub_path and os.path.exists(sub_path):
                with open(sub_path, 'rb') as sub:
                    bot.send_document(chat_id, sub, caption="Subtitles")
        else:
            bot.send_message(chat_id, "An error occurred during download.")
    except Exception as e:
        logging.error(f"Error handling social video: {e}")
        bot.send_message(chat_id, f"Error: {e}")
    finally:
        if chat_id in bot.icon_messages:
            bot.delete_message(chat_id, bot.icon_messages[chat_id])
            bot.icon_messages.pop(chat_id, None)
        cleanup_downloads_dir()

def download_video_any_platform(url):
    unique_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': output_path,
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'writesubtitles': True, # Add subtitle writing
        'writeautomaticsub': True, # Add automatic subtitle writing
        'subtitleslangs': ['en'], # Prefer English subtitles
        'convert_subtitles': 'srt', # Convert to srt
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            description = info.get('description', '')
        subtitle_path = output_path.replace('.mp4', '.en.srt') # Check for .en.srt
        return output_path, subtitle_path if os.path.exists(subtitle_path) else None, description
    except Exception as e:
        logging.error(f"Download error for any platform: {e}")
        return None, None, ""

def cleanup_downloads_dir():
    """Removes all files from the downloads directory."""
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
    """Handles messages that are not media files or supported links."""
    update_user_activity(message.from_user.id)
    if not is_supported_url(message.text) and not message.text.startswith('/'):
        bot.send_message(message.chat.id, "Please send a voice, audio, video file, or a supported media link.")

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
    RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', "https://your-render-app-name.onrender.com")
    url = f"{RENDER_URL}/"
    try:
        bot.set_webhook(url=url)
        return f"Webhook set to {url}", 200
    except Exception as e:
        return f"Failed to set webhook: {e}", 500

@app.route('/delete_webhook', methods=['GET','POST'])
def delete_webhook_route():
    """Route to manually delete the webhook."""
    try:
        bot.delete_webhook()
        return 'Webhook deleted.', 200
    except Exception as e:
        return f"Failed to delete webhook: {e}", 500

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

