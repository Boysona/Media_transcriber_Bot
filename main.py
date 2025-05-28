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
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio # New import for TTS async functions
from msspeech import MSSpeech, MSSpeechError # New import for TTS

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- BOT CONFIGURATION (Using original bot's TOKEN and Webhook URL) ---
TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4" # Main Bot's Token

bot = telebot.TeleBot(TOKEN, threaded=True) # Set threaded to True for async operations
app = Flask(__name__)

# Admin ID
ADMIN_ID = 5978150981

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# --- User tracking files (Combined) ---
users_file = 'users.json' # Used for last activity and TTS voice
user_data = {} # Stores last activity timestamp
user_voice_settings = {} # Stores TTS voice settings
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            loaded_data = json.load(f)
            # Differentiate between general user data and TTS specific voice settings
            user_data = {k: v for k, v in loaded_data.items() if not k.startswith("voice_")}
            user_voice_settings = {k.replace("voice_", ""): v for k, v in loaded_data.items() if k.startswith("voice_")}
        except json.JSONDecodeError:
            user_data = {}
            user_voice_settings = {}

# User-specific language settings for translate/summarize
user_language_settings_file = 'user_language_settings.json'
user_language_settings = {}
if os.path.exists(user_language_settings_file):
    with open(user_language_settings_file, 'r') as f:
        try:
            user_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_language_settings = {}

def save_user_data():
    # Combine both user_data and user_voice_settings for single file saving
    combined_data = {**user_data}
    for uid, voice in user_voice_settings.items():
        combined_data[f"voice_{uid}"] = voice
    with open(users_file, 'w') as f:
        json.dump(combined_data, f, indent=4)

def save_user_language_settings():
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

# In-memory chat history and transcription store
user_memory = {}
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0  # in seconds
bot_start_time = datetime.now() # Kani waa waqtiga bot-ku bilaabmay

GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"

def ask_gemini(user_id, user_message):
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    resp = requests.post(url, headers={'Content-Type': 'application/json'}, json={"contents": [{"parts": parts}]})
    result = resp.json()
    if "candidates" in result:
        reply = result['candidates'][0]['content']['parts'][0]['text']
        user_memory[user_id].append({"role": "model", "text": reply})
        return reply
    return "Error: " + json.dumps(result)

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {}

def set_bot_info():
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "View instructions"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("change_voice", "Change text-to-speech voice"), # New command
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    bot.set_my_commands(commands)

    # Short description (About)
    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds! Also convert text to speech!"
    )

    # Full description (What can this bot do?)
    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free!
It also converts your text messages into speech using various voices.

     🔥Enjoy free usage and start now!👌🏻"""
    )

def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

@bot.message_handler(commands=['start'])
def start_handler(message):
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
        # Check for first_name, then username, then default to "user"
        display_name = message.from_user.first_name or (f"@{message.from_user.username}" if message.from_user.username else "user")
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear {display_name}!
I'm your all-in-one bot for media transcription and text-to-speech.
• Send me:
• Voice message
• Video message
• Audio file
• to transcribe for free
• Or simply send me a **text message** to convert it to speech!
Use /change_voice to pick your preferred speaking voice.
"""
        )
    # Halkan waxaan ka saarnay status_handler(message) si aanay ugu soo dirin si otomaatig ah


@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, and videos, and converts text to speech.

1.  **Send a File for Transcription:** Send a voice message, audio, or video.
    * The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
    * After transcription, you'll see options to **Translate** or **Summarize** the text.
2.  **Send Text for Speech:** Simply send any text message.
    * The bot will convert your text into an audio message.
    * Use `/change_voice` to select your preferred speaking voice.
3.  **Commands:**
    -   `/start`: Restart the bot.
    -   `/status`: View bot statistics.
    -   `/help`: Display these instructions.
    -   `/language`: Change your preferred language for translations and summaries.
    -   `/change_voice`: Select the voice for text-to-speech.
    -   `/privacy`: View the bot's privacy notice.

Enjoy transcribing your media and generating speech quickly and easily!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and TikTok links you send are temporarily processed for transcription. These files are **deleted immediately** after processing. We do not store your media content.
    * **Text for TTS:** Text you send for conversion to speech is processed to generate audio and is not permanently stored.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language and voice preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language & Voice Preferences:** Your chosen language for translations/summaries and your preferred text-to-speech voice are stored so you don't have to select them each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, summarization, and text-to-speech.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed).
    * To set your preferred language and voice for future interactions.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, summarization, and text-to-speech are performed using integrated AI models (Whisper and Gemini API, MSSpeech API). Your input to these models is handled according to their respective privacy policies, but we do not store this data after processing.

4.  **Data Retention:**
    * Media files are deleted immediately after transcription.
    * Text for TTS is not stored after processing.
    * Transcriptions are held temporarily in memory.
    * User IDs, language, and voice preferences are retained to maintain your settings and usage statistics. You can always delete your data by stopping using the bot or contacting the bot administrator.

By using this bot, you agree to the terms outlined in this Privacy Notice.

If you have any questions, please contact the bot administrator."""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")


@bot.message_handler(commands=['status'])
def status_handler(message):
    update_user_activity(message.from_user.id)

    # Calculate bot uptime
    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Count active users today
    today = datetime.now().date()
    active_today = sum(
        1 for timestamp in user_data.values()
        if datetime.fromisoformat(timestamp).date() == today
    )

    # Calculate total processing time components
    total_proc_seconds = int(total_processing_time)
    proc_hours = total_proc_seconds // 3600
    proc_minutes = (total_proc_seconds % 3600) // 60
    proc_seconds = total_proc_seconds % 60

    text = (
        "📊 Bot Statistics\n\n"
        "🟢 **Bot Status: Online**\n"
        f"⏳ Uptime: {days} days, {hours} hours, {minutes} minutes, {seconds} seconds\n\n"
        "👥 User Statistics\n"
        f"▫️ Total Users Today: {active_today}\n"
        f"▫️ Total Registered Users: {len(user_data)}\n\n" # Added total registered users
        "⚙️ Processing Statistics\n"
        f"▫️ Total Files Processed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n"
        f"⏱️ Total Processing Time: {proc_hours} hours {proc_minutes} minutes {proc_seconds} seconds\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌"
    )

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users(message):
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast(message):
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document']
)
def broadcast_message(message):
    admin_state[message.from_user.id] = None
    success = fail = 0
    for uid_key in user_data: # Iterate through keys in user_data
        uid = uid_key # Use the key directly as user ID
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to {uid}: {e}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)

    # Add reaction emoji based on file type
    try:
        if message.voice:
            bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
        elif message.audio:
            bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
        elif message.video or message.video_note:
            bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=["👀"])
    except Exception as e:
        logging.error(f"Error setting reaction: {e}")


    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    info = bot.get_file(file_obj.file_id)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        data = bot.download_file(info.file_path)
        with open(local_path, 'wb') as f:
            f.write(data)

        bot.send_chat_action(message.chat.id, 'typing')
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
        # Add Speak button for transcription
        buttons.add(InlineKeyboardButton("Speak this text 🔊", callback_data=f"btn_speak_transcription|{message.message_id}"))


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
                    caption="Here’s your transcription. Tap a button below for more options."
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
        if os.path.exists(local_path):
            os.remove(local_path)

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
    {"name": "Somali", "flag": "🇸🇴"}, # Moved Somali to be higher
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
    uid = str(message.from_user.id)

    markup = generate_language_keyboard("set_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future translations and summaries:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_lang|"))
def callback_set_language(call):
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
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
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
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
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
        if uid in user_transcriptions and call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions[uid]:
             do_translate_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to translate. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
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
        if uid in user_transcriptions and call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions[uid]:
            do_summarize_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)

def do_translate_with_saved_lang(message, uid, lang, message_id):
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
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

# --- NEW: TEXT-TO-SPEECH FUNCTIONALITY (Integrated from the second bot) ---

# Group voices by language for better organization
VOICES_BY_LANGUAGE = {
    "English 🇬🇧": [
        "en-US-AriaNeural", "en-US-GuyNeural",
        "en-GB-LibbyNeural", "en-GB-RyanNeural",
    ],
    "Somali 🇸🇴": [
        "so-SO-UbaxNeural", "so-SO-MuuseNeural",
    ],
    # Add other languages if needed
}

def get_user_tts_voice(uid):
    # Default to a specific voice if not found in user settings
    return user_voice_settings.get(str(uid), "en-US-AriaNeural") # Default English voice

def make_language_keyboard_tts():
    kb = InlineKeyboardMarkup(row_width=1)
    for lang_name in VOICES_BY_LANGUAGE.keys():
        kb.add(InlineKeyboardButton(lang_name, callback_data=f"tts_lang|{lang_name}"))
    return kb

def make_voice_keyboard_for_language_tts(lang_name):
    kb = InlineKeyboardMarkup(row_width=2)
    voices = VOICES_BY_LANGUAGE.get(lang_name, [])
    for voice in voices:
        kb.add(InlineKeyboardButton(voice, callback_data=f"tts_voice|{voice}"))
    kb.add(InlineKeyboardButton("⬅️ Back to Languages", callback_data="tts_back_to_languages"))
    return kb

# ====== TEXT-TO-SPEECH SYNTHESIS ======
async def a_main_tts(voice, text, filename, rate=0, pitch=0, volume=1.0):
    mss = MSSpeech()
    await mss.set_voice(voice)
    await mss.set_rate(rate)
    await mss.set_pitch(pitch)
    await mss.set_volume(volume)
    return await mss.synthesize(text, filename)

async def synth_and_send_tts(chat_id, user_id, text_to_speak, reply_to_message_id=None):
    voice = get_user_tts_voice(user_id)
    filename = os.path.join(DOWNLOAD_DIR, f"tts_{user_id}_{uuid.uuid4()}.mp3") # Use DOWNLOAD_DIR for TTS
    logging.info(f"Synthesizing text for user {user_id} with voice {voice}")
    try:
        bot.send_chat_action(chat_id, "record_audio")
        await a_main_tts(voice, text_to_speak, filename)
        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            bot.send_message(chat_id, "❌ MP3 file not generated or empty. Please try again.")
            return

        with open(filename, "rb") as f:
            bot.send_audio(chat_id, f, caption=f"🎤 Voice: {voice}", reply_to_message_id=reply_to_message_id)
    except MSSpeechError as e:
        logging.error(f"MSSpeech TTS error for user {user_id}: {e}")
        bot.send_message(chat_id, f"❌ Wuu jiraa khalad dhinaca codka ah: {e}")
    except Exception as e:
        logging.exception(f"Unexpected TTS error for user {user_id}")
        bot.send_message(chat_id, "❌ Wuxuu dhacay khalad aan la filayn. Fadlan isku day mar kale.")
    finally:
        if os.path.exists(filename):
            os.remove(filename) # Clean up the audio file

# --- TTS Command Handlers ---
@bot.message_handler(commands=["change_voice"])
def cmd_change_voice(message):
    update_user_activity(message.from_user.id)
    bot.send_message(message.chat.id, "🎙️ Choose a language for Text-to-Speech:", reply_markup=make_language_keyboard_tts())

@bot.callback_query_handler(func=lambda c: c.data.startswith("tts_lang|"))
def on_language_select_tts(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    _, lang_name = call.data.split("|", 1)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🎙️ Choose a voice for {lang_name}:",
        reply_markup=make_voice_keyboard_for_language_tts(lang_name)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("tts_voice|"))
def on_voice_change_tts(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    _, voice = call.data.split("|", 1)
    user_voice_settings[uid] = voice
    save_user_data() # Save the combined user data
    bot.answer_callback_query(call.id, f"✔️ Voice changed to {voice}")
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"🔊 Hadda waxaad isticmaalaysaa: *{voice}*. Waxaad bilaabi kartaa inaad qorto qoraalka. (Fadlan fariin cusub soo dir, ha ahaan jawaab).",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda c: c.data == "tts_back_to_languages")
def on_back_to_languages_tts(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🎙️ Choose a language for Text-to-Speech:",
        reply_markup=make_language_keyboard_tts()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_speak_transcription|"))
def button_speak_transcription_handler(call):
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    transcription_text = user_transcriptions.get(uid, {}).get(message_id, "")
    if not transcription_text:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message to speak.")
        return

    bot.answer_callback_query(call.id, "Converting transcription to speech...")
    asyncio.run(synth_and_send_tts(call.message.chat.id, uid, transcription_text, reply_to_message_id=message_id))


# --- General Text Message Handler (for TTS) ---
@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_general_text_message(message):
    update_user_activity(message.from_user.id)
    # If it's a reply to a bot message, ensure it's not a transcription response itself
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.get_me().id:
        # Avoid processing replies to transcription results again as direct TTS inputs
        # unless it's a specific command related to it.
        # This simple check is for direct text messages, not replies meant for transcription actions.
        # A more robust check might involve checking `user_transcriptions` if the replied message was a transcription.
        pass # Do nothing for replies to bot's own messages unless it's a specific command
    else:
        # If it's a new text message and not a command, treat it as TTS input
        asyncio.run(synth_and_send_tts(message.chat.id, message.from_user.id, message.text, reply_to_message_id=message.message_id))


@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'document'])
def fallback(message):
    update_user_activity(message.from_user.id)
    bot.send_message(message.chat.id, "Fadlan soo dir oo kaliya fariimo cod, maqal, ama muuqaal ah si aan u qoro (transcribe) ama fariimo qoraal ah si aan ugu beddelo cod (text-to-speech).")

@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook():
    # Use the main bot's Webhook URL
    url = "https://media-transcriber-bot-hzlk.onrender.com" # Replace with your actual Render URL
    bot.set_webhook(url=url)
    return f"Webhook set to {url}", 200

@app.route('/delete_webhook', methods=['GET','POST'])
def delete_webhook():
    bot.delete_webhook()
    return 'Webhook deleted.', 200

if __name__ == "__main__":
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    set_bot_info()
    bot.delete_webhook()
    # The webhook URL should be the one for the Render service where this combined bot will be hosted.
    # Make sure to replace "https://media-transcriber-bot-hzlk.onrender.com" with your actual Render URL.
    bot.set_webhook(url="https://media-transcriber-bot-hzlk.onrender.com")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))

