import os
import re
import uuid
import shutil
import logging
import requests
import telebot
import json
import yt_dlp
import subprocess
import threading
import time
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from PIL import Image
from io import BytesIO

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token and Admin ID
BOT_TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4"
ADMIN_ID = 5978150981
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com"

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking file
users_file = 'users.json'
user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

# User-specific language settings
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
        json.dump(user_data, indent=4)

def save_user_language_settings():
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

# In-memory chat history and transcription store
user_memory = {} # For Gemini API chat context
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0  # in seconds
processing_start_time = None

# Gemini API Key
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"

def ask_gemini(user_id, user_message):
    """Sends a message to the Gemini API and returns the response."""
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
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

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {}

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
        "This bot transcribes voice, audio, and video messages, and downloads videos from various links."
    )

    bot.set_my_description(
        """Halkan waxaad ka heli kartaa adeegyo isku dhaf ah! 🎉
• **Duubista Codka iyo Fiidyowga:** Ii soo dir fariin cod, fiidyow, ama fayl maqal ah si aan ugu rogo qoraal.
• **Soo dejinta Fiidyowyada:** Kaliya ii soo dir linkiga fiidyowga aad rabto inaad soo dejiso (sida TikTok, YouTube, Instagram, iwm).

Waad ku mahadsantahay isticmaalka bot-kayga! Hadaan ku caawiyo wax kale iweydii."""
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
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()

    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        name = message.from_user.first_name
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome, dear **{name}**!
I'm your all-in-one media bot. Send me:
• **Voice messages, video messages, or audio files** to transcribe for free.
• A **video link** (from TikTok, YouTube, Instagram, etc.) to download the video.
""", parse_mode="Markdown"
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    help_text = (
        """ℹ️ How to use this bot:

This bot has two main functions: **transcription** and **video downloading**.

**1. Transcription:**
- Send a **voice message**, **audio file**, or **video** directly to the bot.
- The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
- After transcription, you'll see options to **Translate** or **Summarize** the text.

**2. Video Downloading:**
- Send a **video link** (e.g., from TikTok, YouTube, Instagram, Pinterest, Facebook, X/Twitter, Snapchat).
- For YouTube links, you'll be prompted to choose a resolution.
- The bot will download and send you the video.

**Commands:**
- `/start`: Restart the bot and see main options.
- `/status`: View bot statistics.
- `/help`: Display these instructions.
- `/language`: Change your preferred language for translations and summaries.
- `/privacy`: View the bot's privacy notice.

Enjoy using the bot!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    """Handles the /privacy command, displaying the bot's privacy policy."""
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and video links you send are temporarily processed for transcription or download. These files are **deleted immediately** after processing. We do not store your media content.
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
        f"▫️ Total Files Processed (Transcriptions & Downloads): {total_files_processed}\n"
        f"▫️ Audio Files Transcribed: {total_audio_files}\n"
        f"▫️ Voice Clips Transcribed: {total_voice_clips}\n"
        f"▫️ Videos Transcribed/Downloaded: {total_videos}\n"
        f"⏱️ Total Transcription Processing Time: {hours} hours {minutes} minutes {seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌"
    )

    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users(message):
    """Admin command to show total registered users."""
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast(message):
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
def handle_transcription_media(message):
    """Handles incoming voice messages, audio files, video files, and video notes for transcription."""
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)

    try:
        bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error setting reaction for message {message.message_id}: {e}")
    except Exception as e:
        logging.error(f"Unexpected error setting reaction for message {message.message_id}: {e}")

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB) for transcription.")

    info = bot.get_file(file_obj.file_id)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")

    processing_message = bot.send_message(message.chat.id, "Processing your file for transcription... Please wait.")

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
            os.remove(local_path)

# --- Language Selection and Saving ---

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

# --- Video Downloading Features ---

def get_footer_caption(description=None):
    """Generates the common footer caption for downloaded videos."""
    caption = "Power by @transcriberbo"
    if description:
        caption = f"{description}\n\n{caption}"
    return caption

def is_supported_url(url):
    platforms = ['tiktok.com', 'youtube.com', 'pinterest.com', 'pin.it',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    return 'youtube.com' in url.lower()

@bot.message_handler(func=lambda msg: is_youtube_url(msg.text) and msg.text.startswith(('http://', 'https://')))
def handle_youtube_url(msg):
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
            description = info_dict.get('description')
            formats = info_dict.get('formats', [])

        resolutions = {}
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('height') is not None:
                height = f['height']
                if height <= 1080:
                    resolutions[f'{height}p'] = f['format_id']

        if not resolutions:
            bot.send_message(msg.chat.id, "Ma jiro tayo muuqaal ah oo la heli karo.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        buttons = []
        sorted_resolutions = sorted(resolutions.keys(), key=lambda x: int(x[:-1]))
        for res in sorted_resolutions:
            format_id = resolutions[res]
            video_id = str(uuid.uuid4())[:8]
            bot.video_info = getattr(bot, 'video_info', {})
            bot.video_info[video_id] = {'url': url, 'format_id': format_id, 'description': description}
            buttons.append(InlineKeyboardButton(res, callback_data=f'dl:{video_id}'))
        markup.add(*buttons)

        if thumbnail_url:
            try:
                response = requests.get(thumbnail_url)
                response.raise_for_status()
                image = Image.open(BytesIO(response.content))
                bio = BytesIO()
                image.save(bio, 'JPEG')
                bio.seek(0)
                bot.send_photo(msg.chat.id, photo=bio, caption=f"Dooro tayada aad rabto inaad soo dejiso: **{title}**", reply_markup=markup, parse_mode="Markdown")
            except requests.exceptions.RequestException as e:
                logging.error(f"Error downloading thumbnail: {e}")
                bot.send_message(msg.chat.id, f"Dooro tayada aad rabto inaad soo dejiso: **{title}**", reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Error processing thumbnail: {e}")
                bot.send_message(msg.chat.id, f"Dooro tayada aad rabto inaad soo dejiso: **{title}**", reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(msg.chat.id, f"Dooro tayada aad rabto inaad soo dejiso: **{title}**", reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error extracting YouTube info: {e}")
        bot.send_message(msg.chat.id, f"Error extracting YouTube info: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video(call):
    global total_files_processed, total_videos
    try:
        video_id = call.data.split(':')[1]
        if not hasattr(bot, 'video_info') or video_id not in bot.video_info:
            bot.answer_callback_query(call.id, "Download expired. Please try again.")
            return

        video_data = bot.video_info[video_id]
        url = video_data['url']
        format_id = video_data['format_id']
        description = video_data.get('description')

        bot.answer_callback_query(call.id, f"Soo dejinta tayada {format_id}...")
        download_msg = bot.send_message(call.message.chat.id, f"Starting download for {format_id}...")
        bot.send_chat_action(call.message.chat.id, 'record_video')
        output_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")
        ydl_opts = {
            'format': format_id,
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        bot.edit_message_text("Uploading video...", chat_id=download_msg.chat.id, message_id=download_msg.message_id)
        bot.send_chat_action(call.message.chat.id, 'upload_video')
        with open(output_path, 'rb') as video_file:
            bot.send_video(call.message.chat.id, video_file, caption=get_footer_caption(description), reply_to_message_id=call.message.message_id)

        total_files_processed += 1
        total_videos += 1

    except Exception as e:
        logging.error(f"Error downloading YouTube video: {e}")
        bot.send_message(call.message.chat.id, f"Error downloading video: {e}")
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)
        # Clear the specific video info after use
        if hasattr(bot, 'video_info') and video_id in bot.video_info:
            del bot.video_info[video_id]

@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text) and msg.text.startswith(('http://', 'https://')))
def handle_social_video(msg):
    global total_files_processed, total_videos
    url = msg.text
    video_path = None
    update_user_activity(msg.from_user.id)
    try:
        bot.send_chat_action(msg.chat.id, 'record_video')
        download_msg = bot.send_message(msg.chat.id, "Starting download...")
        video_path, description = download_video_any(url)

        bot.edit_message_text("Uploading video...", chat_id=download_msg.chat.id, message_id=download_msg.message_id)
        bot.send_chat_action(msg.chat.id, 'upload_video')
        with open(video_path, 'rb') as video_file:
            bot.send_video(msg.chat.id, video_file, caption=get_footer_caption(description), reply_to_message_id=msg.message_id)

        total_files_processed += 1
        total_videos += 1

    except Exception as e:
        logging.error(f"Error downloading social media video: {e}")
        bot.send_message(msg.chat.id, f"Error: {e}")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)

def download_video_any(url):
    unique_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
    video_description = None
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': output_path,
        'quiet': True,
        'noplaylist': True,
        'extract_flat': False,
        'extractor_args': {
            'pinterest': {
                'video_only': False,
            }
        },
        'simulate': True,  # Simulate to get info before actual download
        'force_generic_extractor': False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        video_description = info.get('description', None)
        # Now, perform the actual download with full options
        ydl_opts['simulate'] = False
        ydl_opts['force_generic_extractor'] = True # Sometimes helps with non-YouTube links
        with yt_dlp.YoutubeDL(ydl_opts) as ydl_actual:
            ydl_actual.download([url])
    return output_path, video_description

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    """Handles messages that are not media files or supported links."""
    update_user_activity(message.from_user.id)
    if message.text and message.text.startswith(('http://', 'https://')):
        bot.send_message(message.chat.id, "I couldn't process that link. Please make sure it's a valid video link from a supported platform (like TikTok, YouTube, Instagram, etc.).")
    else:
        bot.send_message(message.chat.id, "Please send only voice, audio, or video files for transcription, or a video link for download.")

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
    # Ensure this matches your Render app's public URL
    bot.set_webhook(url=f"{WEBHOOK_URL}/")
    return f"Webhook set to {WEBHOOK_URL}/", 200

@app.route('/delete_webhook', methods=['GET','POST'])
def delete_webhook_route():
    """Route to manually delete the webhook."""
    bot.delete_webhook()
    return 'Webhook deleted.', 200

if __name__ == "__main__":
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    set_bot_info()

    try:
        bot.delete_webhook()
        logging.info("Old webhook deleted successfully.")
    except Exception as e:
        logging.warning(f"Failed to delete old webhook (might not exist): {e}")

    try:
        bot.set_webhook(url=f"{WEBHOOK_URL}/")
        logging.info(f"Webhook set to {WEBHOOK_URL}/")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")

    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
