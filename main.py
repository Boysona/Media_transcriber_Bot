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

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token and required channel
TOKEN = "7648822901:AAFaEh_dZkDvPE_AGSk4NN6nnUMvEy2MkHc"
REQUIRED_CHANNEL = "@transcriberbo"

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID
ADMIN_ID = 5978150981

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
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

# In-memory chat history and transcription store
# Waxaan u badalnay 'last_transcription' una badalnay 'user_transcriptions'
# Tani waxay kaydin doontaa qoraal kasta oo ku xiran fariinta u gaarka ah (message_id)
user_memory = {}
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_tiktok_downloads = 0
total_processing_time = 0  # in seconds
processing_start_time = None

GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"

def ask_gemini(user_id, user_message):
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    # Kani waxa uu xaddidayaa taariikhda la xasuusan karo 10 fariimood oo keliya
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
        telebot.types.BotCommand("start", "Restart bot"),
        telebot.types.BotCommand("status", " show statistics"),
        telebot.types.BotCommand("info", " show instructions"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
    ]
    bot.set_my_commands(commands)

    # Short description (About)
    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )

    # Full description (What can this bot do?)
    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice massages , tiktok videos, audio files, and videos— free and in multiple languages.

     🔥Enjoy free usage and start now!👌🏻"""
    )

bot.set_my_description(
    description="transcribe voice messages, audio files, videos, for free"
)

def check_subscription(user_id):
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException:
        return False

def send_subscription_message(chat_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(" Click here to join the channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")
    )
    bot.send_message(
        chat_id,
        """This bot only works when you join the channel 👉🏻 @transcriberbo, so first join the channel, then come back for the bot to work for you🥰""",
        reply_markup=markup
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
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)
    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=keyboard)
    else:
        name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear!
• Send me

• Voice message
• Video massage
• Audio file
• TikTok video link
• to transcribe for free"""
        )

@bot.message_handler(commands=['info'])
def help_handler(message):
    help_text = (
        """ℹ️ How to use this bot:

1.  **Join the Channel:** Make sure you've joined our channel: https://t.me/transcriberbo. This is required to use the bot.

2.  **Send a File:** You can send voice messages, audio files, video files, video notes, or even TikTok video URLs directly to the bot.

3.  **Transcription:** The bot will automatically process your file or TikTok link and transcribe the content into text.

4.  **Receive Text:** Once the transcription is complete, the bot will send you the text back in the chat.

    -   If the transcription is short, it will be sent as a reply to your original message.
    -   If the transcription is longer than 4000 characters, it will be sent as a separate text file.

5.  **TikTok Actions:**
    -   If you send a TikTok video URL, you'll get options to **Download** the video or just **Transcribe** it.

6.  **Commands:**
    -   `/start`: Restarts the bot and shows the welcome message.
    -   `/status`: Displays bot statistics, including the number of users and processing information.
    -   `/info`: Shows these usage instructions.
    -   `/translate`: Translate your last transcription (uses saved language or asks for one).
    -   `/summarize`: Summarize your last transcription (uses saved language or asks for one).
    -   `/language`: Change your preferred language for translation and summarization.

Enjoy transcribing and downloading your media files and TikTok videos quickly and easily!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
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
    for uid in user_data:
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException:
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

# TikTok link detection
TIKTOK_REGEX = re.compile(r'(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+')

@bot.message_handler(func=lambda m: m.text and TIKTOK_REGEX.search(m.text))
def tiktok_link_handler(message):
    url = TIKTOK_REGEX.search(message.text).group(0)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("Download", callback_data=f"download|{url}"),
        telebot.types.InlineKeyboardButton("Transcribe", callback_data=f"transcribe_tiktok|{url}|{message.message_id}") # Ku dar message_id
    )
    bot.send_message(message.chat.id, "TikTok link detected—choose an action:", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("download|"))
def callback_download_tiktok(call):
    global total_tiktok_downloads
    _, url = call.data.split("|", 1)

    # Instead of 'typing', show 'upload_video' to indicate uploading
    bot.send_chat_action(call.message.chat.id, 'upload_video')

    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'mp4',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            caption = info.get('description', 'No caption found.')

        with open(path, 'rb') as video:
            bot.send_video(call.message.chat.id, video)
        bot.send_message(call.message.chat.id, f"\n{caption}")
        total_tiktok_downloads += 1
    except Exception as e:
        logging.error(f"TikTok download error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok video.")
    finally:
        if 'path' in locals() and os.path.exists(path):
            os.remove(path)

# Waxaan u bedelnay `callback_transcribe_tiktok` si uu u kaydiyo qoraalka gaar ahaan message_id
@bot.callback_query_handler(func=lambda c: c.data.startswith("transcribe_tiktok|"))
def callback_transcribe_tiktok(call):
    _, url, message_id_str = call.data.split("|", 2) # Kala goynta message_id
    message_id = int(message_id_str)
    bot.send_chat_action(call.message.chat.id, 'typing')
    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'mp4',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(path) or ""
        uid = str(call.from_user.id)
        
        # Ku kaydi qoraalka user_transcriptions
        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        global total_processing_time
        total_processing_time += processing_time

        # Create inline buttons
        buttons = InlineKeyboardMarkup()
        # Waxaan ku daray message_id callback_data
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
        bot.send_message(call.message.chat.id, "⚠️ Failed to transcribe TikTok.")
    finally:
        if 'path' in locals() and os.path.exists(path):
            os.remove(path)

# Waxaanu wax ka bedelnay handle_file si uu u kaydiyo qoraalka gaar ahaan message_id
@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

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
        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(local_path) or ""
        uid = str(message.from_user.id)
        
        # Ku kaydi qoraalka user_transcriptions
        user_transcriptions.setdefault(uid, {})[message.message_id] = transcription

        # Update statistics
        total_files_processed += 1
        if message.voice:
            total_voice_clips += 1
        elif message.audio:
            total_audio_files += 1
        elif message.video or message.video_note:
            total_videos += 1

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        # Create inline buttons
        buttons = InlineKeyboardMarkup()
        # Waxaan ku daray message_id callback_data
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
        if os.path.exists(local_path):
            os.remove(local_path)

# --- Language Selection and Saving ---

# List of common languages with emojis (can be expanded)
LANGUAGES = [
    {"name": "English", "flag": "🇬🇧"},
    {"name": "Somali", "flag": "🇸🇴"},
    {"name": "Arabic", "flag": "🇸🇦"},
    {"name": "Spanish", "flag": "🇪🇸"},
    {"name": "French", "flag": "🇫🇷"},
    {"name": "German", "flag": "🇩🇪"},
    {"name": "Italian", "flag": "🇮🇹"},
    {"name": "Portuguese", "flag": "🇵🇹"},
    {"name": "Russian", "flag": "🇷🇺"},
    {"name": "Chinese (Simplified)", "flag": "🇨🇳"},
    {"name": "Japanese", "flag": "🇯🇵"},
    {"name": "Korean", "flag": "🇰🇷"},
    {"name": "Hindi", "flag": "🇮🇳"},
    {"name": "Bengali", "flag": "🇧🇩"},
    {"name": "Urdu", "flag": "🇵🇰"},
    {"name": "Swahili", "flag": "🇰🇪"},
    {"name": "Amharic", "flag": "🇪🇹"},
    {"name": "Turkish", "flag": "🇹🇷"},
    {"name": "Vietnamese", "flag": "🇻🇳"},
    {"name": "Thai", "flag": "🇹🇭"},
    {"name": "Dutch", "flag": "🇳🇱"},
    {"name": "Swedish", "flag": "🇸🇪"},
    {"name": "Norwegian", "flag": "🇳🇴"},
    {"name": "Danish", "flag": "🇩🇰"},
    {"name": "Finnish", "flag": "🇫🇮"},
    {"name": "Greek", "flag": "🇬🇷"},
    {"name": "Hebrew", "flag": "🇮🇱"},
    {"name": "Polish", "flag": "🇵🇱"},
    {"name": "Czech", "flag": "🇨🇿"},
    {"name": "Hungarian", "flag": "🇭🇺"},
    {"name": "Romanian", "flag": "🇷🇴"},
    {"name": "Ukrainian", "flag": "🇺🇦"},
    {"name": "Indonesian", "flag": "🇮🇩"},
    {"name": "Malay", "flag": "🇲🇾"},
    {"name": "Filipino", "flag": "🇵🇭"},
    {"name": "Persian", "flag": "🇮🇷"},
    {"name": "Farsi", "flag": "🇮🇷"}, # Alias for Persian
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


def generate_language_keyboard(callback_prefix, message_id=None): # Waxaan ku daray message_id argument
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    for lang in LANGUAGES:
        # Waxaan ku daray message_id callback_data
        cb_data = f"{callback_prefix}|{lang['name']}"
        if message_id is not None:
            cb_data += f"|{message_id}"
        buttons.append(InlineKeyboardButton(f"{lang['name']} {lang['flag']}", callback_data=cb_data))
    markup.add(*buttons)
    return markup

@bot.message_handler(commands=['language'])
def select_language_command(message):
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

# Waxaan wax ka bedelnay `button_translate_handler` iyo `button_summarize_handler` si ay u qaataan `message_id`
@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1) # Kala goynta message_id
    message_id = int(message_id_str)

    # Hubi in qoraalka la heli karo
    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Translating with your preferred language...")
        do_translate_with_saved_lang(call.message, uid, preferred_lang, message_id)
    else:
        # Waxaan ku daray message_id markii aan soo saarayno keyboard-ka luqadda
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
    _, message_id_str = call.data.split("|", 1) # Kala goynta message_id
    message_id = int(message_id_str)

    # Hubi in qoraalka la heli karo
    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, "Summarizing with your preferred language...")
        do_summarize_with_saved_lang(call.message, uid, preferred_lang, message_id)
    else:
        # Waxaan ku daray message_id markii aan soo saarayno keyboard-ka luqadda
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want the summary in:",
            reply_markup=markup
        )
    bot.answer_callback_query(call.id)

# Waxaan wax ka bedelnay `callback_translate_to` iyo `callback_summarize_in` si ay u qaataan `message_id`
@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None # Soo qaado message_id

    user_language_settings[uid] = lang # Save for future use
    save_user_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Translating to **{lang}**...",
        parse_mode="Markdown"
    )
    if message_id:
        do_translate_with_saved_lang(call.message, uid, lang, message_id)
    else: # Hadii ay ka timid /translate command
        # Waxaan u baahanahay inaan qoraalka ka helno fariintii ugu dambeysay (haday jirto) ama si kale
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
    message_id = int(parts[2]) if len(parts) > 2 else None # Soo qaado message_id

    user_language_settings[uid] = lang # Save for future use
    save_user_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Summarizing in **{lang}**...",
        parse_mode="Markdown"
    )
    if message_id:
        do_summarize_with_saved_lang(call.message, uid, lang, message_id)
    else: # Hadii ay ka timid /summarize command
        # Waxaan u baahanahay inaan qoraalka ka helno fariintii ugu dambeysay (haday jirto) ama si kale
        if uid in user_transcriptions and call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions[uid]:
            do_summarize_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id)


# Modified do_translate to use a provided language and specific message_id
def do_translate_with_saved_lang(message, uid, lang, message_id):
    # Hadda waxaan qoraalka ka soo qaadaneynaa user_transcriptions, anagoo isticmaalayna message_id
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to translate.")
        return

    # PROMPT-KA HAGAAGSAN EE TARJUMAADA: Kaliya tarjumaadda, faahfaahin dheeraad ah ma jirto.
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
            # U dir tarjumaadda si ay uga jawaabto fariintii asalka ahayd ee qoraalka
            bot.send_document(message.chat.id, doc, caption=f"Translation to {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        # U dir tarjumaadda si ay uga jawaabto fariintii asalka ahayd ee qoraalka
        bot.send_message(message.chat.id, translated, reply_to_message_id=message_id)

# Modified do_summarize to use a provided language and specific message_id
def do_summarize_with_saved_lang(message, uid, lang, message_id):
    # Hadda waxaan qoraalka ka soo qaadaneynaa user_transcriptions, anagoo isticmaalayna message_id
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to summarize.")
        return

    # PROMPT-KA HAGAAGSAN EE SOO KOOBIDDA: Kaliya soo koobidda, faahfaahin dheeraad ah ma jirto.
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
            # U dir soo koobidda si ay uga jawaabto fariintii asalka ahayd ee qoraalka
            bot.send_document(message.chat.id, doc, caption=f"Summary in {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        # U dir soo koobidda si ay uga jawaabto fariintii asalka ahayd ee qoraalka
        bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)


@bot.message_handler(commands=['translate'])
def handle_translate(message):
    uid = str(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    # Si loo hubiyo in command-kan uu shaqeeyo, waxaan u baahanahay inaan ku jawaabno fariin transcription ah
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
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    # Si loo hubiyo in command-kan uu shaqeeyo, waxaan u baahanahay inaan ku jawaabno fariin transcription ah
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

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)
    bot.send_message(message.chat.id, " Please send only voice, audio, video, or a TikTok video link.")

@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook():
    url = "https://only-me-cwkd.onrender.com"
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
    bot.set_webhook(url="https://media-transcriber-bot.onrender.com")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
