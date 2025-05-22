import os
import re
import uuid
import shutil
import logging
import requests
import json
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
import yt_dlp
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from telegram import Update # U beddelay telebot.types.Update

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token and required channel
TOKEN = "7770743573:AAG1ewm-hyqVIYsFgJGzVz7oOzFnRTvP2TY"
REQUIRED_CHANNEL = "@transcriberbo"

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

async def ask_gemini(user_id, user_message): # Waa inuu noqdaa async
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

FILE_SIZE_LIMIT = 200 * 1024 * 1024  # 200MB
admin_state = {}

async def set_bot_info(application): # Waxaa loo baahan yahay application argument
    commands = [
        BotCommand("start", "Restart bot"),
        BotCommand("status", " show statistics"),
        BotCommand("info", " show instructions"),
        BotCommand("language", "Change preferred language for translate/summarize"),
    ]
    await application.bot.set_my_commands(commands)

    # Short description (About)
    await application.bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )

    # Full description (What can this bot do?)
    await application.bot.set_my_description(
        """This bot can Transcribe and Summarize and translate any media files (Voice messages, Audio files or Videos) for free"""
    )

async def check_subscription(user_id, context): # Waxaa loo baahan yahay context
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception: # telebot.apihelper.ApiTelegramException -> Exception
        return False

async def send_subscription_message(chat_id, context): # Waxaa loo baahan yahay context
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(" Click here to join the channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")]
    ])
    await context.bot.send_message( # context.bot.send_message
        chat_id,
        """🔓 Unlock everything — for FREE!
💸 No fees, no limits. Ever.
✨ Just join the channel below
🤖 Then come back and enjoy unlimited access to the bot!""",
        reply_markup=markup
    )

def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

# Dhammaan func-yada handler-ka waa inay ahaadaan async oo ay qaataan Update iyo ContextTypes.DEFAULT_TYPE
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    update_user_activity(update.effective_user.id)
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()
    if not await check_subscription(update.effective_user.id, context):
        return await send_subscription_message(update.effective_chat.id, context)
    if update.effective_user.id == ADMIN_ID:
        keyboard = [["Send Broadcast", "Total Users", "/status"]]
        reply_markup = telegram.ReplyKeyboardMarkup(keyboard, resize_keyboard=True) # telegram.ReplyKeyboardMarkup
        await update.message.reply_text("Admin Panel", reply_markup=reply_markup)
    else:
        name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
        await update.message.reply_text(
            f"""👋🏻 Welcome dear!
• Send me

• Voice message
• Video massage
• Audio file
• TikTok video link
• to transcribe for free"""
        )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user.id)

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

    await update.message.reply_text(text)

async def total_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(f"Total registered users: {len(user_data)}")

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        admin_state[update.effective_user.id] = 'awaiting_broadcast'
        await update.message.reply_text("Send the broadcast message now:")

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID and admin_state.get(update.effective_user.id) == 'awaiting_broadcast':
        admin_state[update.effective_user.id] = None
        success = fail = 0
        for uid in user_data:
            try:
                await context.bot.copy_message(uid, update.effective_chat.id, update.message.message_id)
                success += 1
            except Exception: # telebot.apihelper.ApiTelegramException -> Exception
                fail += 1
        await update.message.reply_text(
            f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
        )

# TikTok link detection
TIKTOK_REGEX = re.compile(r'(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+')

async def tiktok_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = TIKTOK_REGEX.search(update.message.text).group(0)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Download", callback_data=f"download|{url}"),
         InlineKeyboardButton("Transcribe", callback_data=f"transcribe_tiktok|{url}|{update.message.message_id}")]
    ])
    await update.message.reply_text("TikTok link detected—choose an action:", reply_markup=markup)

async def callback_download_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global total_tiktok_downloads
    query = update.callback_query
    await query.answer()
    _, url = query.data.split("|", 1)

    await context.bot.send_chat_action(query.message.chat.id, 'upload_video')

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
            await context.bot.send_video(query.message.chat.id, video)
        await context.bot.send_message(query.message.chat.id, f"\n{caption}")
        total_tiktok_downloads += 1
    except Exception as e:
        logging.error(f"TikTok download error: {e}")
        await context.bot.send_message(query.message.chat.id, "⚠️ Failed to download TikTok video.")
    finally:
        if 'path' in locals() and os.path.exists(path):
            os.remove(path)

async def callback_transcribe_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, url, message_id_str = query.data.split("|", 2)
    message_id = int(message_id_str)
    await context.bot.send_chat_action(query.message.chat.id, 'typing')
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
        uid = str(query.from_user.id)
        
        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        global total_processing_time
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message_id}"),
             InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message_id}")]
        ])

        if len(transcription) > 4000:
            fn = 'tiktok_transcription.txt'
            with open(fn, 'w', encoding='utf-8') as f:
                f.write(transcription)
            await context.bot.send_chat_action(query.message.chat.id, 'upload_document')
            with open(fn, 'rb') as doc:
                await context.bot.send_document(
                    query.message.chat.id,
                    doc,
                    reply_markup=buttons,
                    caption="Here’s your transcription. Tap a button below to translate or summarize."
                )
            os.remove(fn)
        else:
            await context.bot.send_chat_action(query.message.chat.id, 'typing')
            await context.bot.send_message(
                query.message.chat.id,
                transcription,
                reply_markup=buttons
            )
    except Exception as e:
        logging.error(f"TikTok transcribe error: {e}")
        await context.bot.send_message(query.message.chat.id, "⚠️ Failed to transcribe TikTok.")
    finally:
        if 'path' in locals() and os.path.exists(path):
            os.remove(path)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(update.effective_user.id)
    if not await check_subscription(update.effective_user.id, context):
        return await send_subscription_message(update.effective_chat.id, context)

    file_obj = update.message.voice or update.message.audio or update.message.video or update.message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return await update.message.reply_text("The file size you uploaded is too large (max allowed is 20MB).")

    file_info = await context.bot.get_file(file_obj.file_id)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
    await context.bot.send_chat_action(update.effective_chat.id, 'typing')

    try:
        await file_info.download_to_drive(local_path)

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(local_path) or ""
        uid = str(update.effective_user.id)
        
        user_transcriptions.setdefault(uid, {})[update.message.message_id] = transcription

        total_files_processed += 1
        if update.message.voice:
            total_voice_clips += 1
        elif update.message.audio:
            total_audio_files += 1
        elif update.message.video or update.message.video_note:
            total_videos += 1

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{update.message.message_id}"),
             InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{update.message.message_id}")]
        ])

        if len(transcription) > 4000:
            fn = 'transcription.txt'
            with open(fn, 'w', encoding='utf-8') as f:
                f.write(transcription)
            await context.bot.send_chat_action(update.effective_chat.id, 'upload_document')
            with open(fn, 'rb') as doc:
                await context.bot.send_document(
                    update.effective_chat.id,
                    doc,
                    reply_to_message_id=update.message.message_id,
                    reply_markup=buttons,
                    caption="Here’s your transcription. Tap a button below to translate or summarize."
                )
            os.remove(fn)
        else:
            await update.message.reply_text(
                transcription,
                reply_markup=buttons
            )
    except Exception as e:
        logging.error(f"Error processing file: {e}")
        await update.message.reply_text("⚠️ An error occurred during transcription.")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

# --- Language Selection and Saving ---

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

def generate_language_keyboard(callback_prefix, message_id=None):
    buttons = []
    for lang in LANGUAGES:
        cb_data = f"{callback_prefix}|{lang['name']}"
        if message_id is not None:
            cb_data += f"|{message_id}"
        buttons.append(InlineKeyboardButton(f"{lang['name']} {lang['flag']}", callback_data=cb_data))
    # Waxaan u baahanahay inaan ku xidhno buttons liiska liiska kale si loo helo qaab dhismeedka saxda ah
    return InlineKeyboardMarkup([buttons[i:i + 3] for i in range(0, len(buttons), 3)]) # Use row_width in list comprehension


async def select_language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not await check_subscription(update.effective_user.id, context):
        return await send_subscription_message(update.effective_chat.id, context)

    markup = generate_language_keyboard("set_lang")
    await update.message.reply_text(
        "Please select your preferred language for future translations and summaries:",
        reply_markup=markup
    )

async def callback_set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    _, lang = query.data.split("|", 1)
    user_language_settings[uid] = lang
    save_user_language_settings()
    await query.edit_message_text(
        text=f"✅ Your preferred language has been set to: **{lang}**",
        parse_mode="Markdown"
    )

async def button_translate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    _, message_id_str = query.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        await query.answer("❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        await query.answer("Translating with your preferred language...")
        await do_translate_with_saved_lang(query.message, uid, preferred_lang, message_id, context)
    else:
        markup = generate_language_keyboard("translate_to", message_id)
        await query.edit_message_text(
            text="Please select the language you want to translate into:",
            reply_markup=markup
        )

async def button_summarize_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    _, message_id_str = query.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        await query.answer("❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        await query.answer("Summarizing with your preferred language...")
        await do_summarize_with_saved_lang(query.message, uid, preferred_lang, message_id, context)
    else:
        markup = generate_language_keyboard("summarize_in", message_id)
        await query.edit_message_text(
            text="Please select the language you want the summary in:",
            reply_markup=markup
        )

async def callback_translate_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    parts = query.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    user_language_settings[uid] = lang
    save_user_language_settings()
    await query.edit_message_text(
        text=f"Translating to **{lang}**...",
        parse_mode="Markdown"
    )
    if message_id:
        await do_translate_with_saved_lang(query.message, uid, lang, message_id, context)
    else:
        if uid in user_transcriptions and query.message.reply_to_message and query.message.reply_to_message.message_id in user_transcriptions[uid]:
             await do_translate_with_saved_lang(query.message, uid, lang, query.message.reply_to_message.message_id, context)
        else:
            await query.message.reply_text("❌ No transcription found for this message to translate. Please use the inline buttons on the transcription.")

async def callback_summarize_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    parts = query.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    user_language_settings[uid] = lang
    save_user_language_settings()
    await query.edit_message_text(
        text=f"Summarizing in **{lang}**...",
        parse_mode="Markdown"
    )
    if message_id:
        await do_summarize_with_saved_lang(query.message, uid, lang, message_id, context)
    else:
        if uid in user_transcriptions and query.message.reply_to_message and query.message.reply_to_message.message_id in user_transcriptions[uid]:
            await do_summarize_with_saved_lang(query.message, uid, lang, query.message.reply_to_message.message_id, context)
        else:
            await query.message.reply_text("❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription.")

async def do_translate_with_saved_lang(message, uid, lang, message_id, context): # Waxaa loo baahan yahay context
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        await message.reply_text("❌ No transcription available for this specific message to translate.")
        return

    prompt = f"Translate the following text to {lang}:\n\n{original}"
    await context.bot.send_chat_action(message.chat.id, 'typing')
    translated = await ask_gemini(uid, prompt) # Waa inuu noqdaa await

    if translated.startswith("Error:"):
        await message.reply_text(f"Error during translation: {translated}")
        return

    if len(translated) > 4000:
        fn = 'translation.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(translated)
        await context.bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            await context.bot.send_document(message.chat.id, doc, caption=f"Translation to {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        await context.bot.send_message(message.chat.id, translated, reply_to_message_id=message_id)

async def do_summarize_with_saved_lang(message, uid, lang, message_id, context): # Waxaa loo baahan yahay context
    original = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original:
        await message.reply_text("❌ No transcription available for this specific message to summarize.")
        return

    prompt = f"Summarize the following text in {lang}:\n\n{original}"
    await context.bot.send_chat_action(message.chat.id, 'typing')
    summary = await ask_gemini(uid, prompt) # Waa inuu noqdaa await

    if summary.startswith("Error:"):
        await message.reply_text(f"Error during summarization: {summary}")
        return

    if len(summary) > 4000:
        fn = 'summary.txt'
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(summary)
        await context.bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            await context.bot.send_document(message.chat.id, doc, caption=f"Summary in {lang}", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        await context.bot.send_message(message.chat.id, summary, reply_to_message_id=message_id)


async def handle_translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not await check_subscription(update.effective_user.id, context):
        return await send_subscription_message(update.effective_chat.id, context)

    if not update.message.reply_to_message or uid not in user_transcriptions or update.message.reply_to_message.message_id not in user_transcriptions[uid]:
        return await update.message.reply_text("❌ Please reply to a transcription message to translate it.")

    transcription_message_id = update.message.reply_to_message.message_id
    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        await do_translate_with_saved_lang(update.message, uid, preferred_lang, transcription_message_id, context)
    else:
        markup = generate_language_keyboard("translate_to", transcription_message_id)
        await update.message.reply_text(
            "Please select the language you want to translate into:",
            reply_markup=markup
        )

async def handle_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not await check_subscription(update.effective_user.id, context):
        return await send_subscription_message(update.effective_chat.id, context)

    if not update.message.reply_to_message or uid not in user_transcriptions or update.message.reply_to_message.message_id not in user_transcriptions[uid]:
        return await update.message.reply_text("❌ Please reply to a transcription message to summarize it.")

    transcription_message_id = update.message.reply_to_message.message_id
    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        await do_summarize_with_saved_lang(update.message, uid, preferred_lang, transcription_message_id, context)
    else:
        markup = generate_language_keyboard("summarize_in", transcription_message_id)
        await update.message.reply_text(
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

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user.id)
    if not await check_subscription(update.effective_user.id, context):
        return await send_subscription_message(update.effective_chat.id, context)
    await update.message.reply_text(" Please send only voice, audio, video, or a TikTok video link.")

# Flask app for webhook
app = Flask(__name__)
application = Application.builder().token(TOKEN).build() # Global application object

@app.route('/', methods=['POST'])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return '', 200

@app.route('/set_webhook', methods=['GET','POST'])
async def set_webhook_route(): # Magac la beddelay si looga fogaado isku dhac magac
    url = "https://media-transcriber-bot.onrender.com"
    await application.bot.set_webhook(url=url)
    return f"Webhook set to {url}", 200

@app.route('/delete_webhook', methods=['GET','POST'])
async def delete_webhook_route(): # Magac la beddelay
    await application.bot.delete_webhook()
    return 'Webhook deleted.', 200

if __name__ == "__main__":
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Ku dar handlers-ka application object-ka
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("info", help_handler))
    application.add_handler(CommandHandler("status", status_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("Total Users") & filters.User(ADMIN_ID), total_users))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("Send Broadcast") & filters.User(ADMIN_ID), send_broadcast))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(TIKTOK_REGEX), tiktok_link_handler))
    application.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_ID), broadcast_message)) # Catch all for broadcast
    application.add_handler(CallbackQueryHandler(callback_download_tiktok, pattern=r"download\|.*"))
    application.add_handler(CallbackQueryHandler(callback_transcribe_tiktok, pattern=r"transcribe_tiktok\|.*"))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO | filters.Video, handle_file))
    application.add_handler(CommandHandler("language", select_language_command))
    application.add_handler(CallbackQueryHandler(callback_set_language, pattern=r"set_lang\|.*"))
    application.add_handler(CallbackQueryHandler(button_translate_handler, pattern=r"btn_translate\|.*"))
    application.add_handler(CallbackQueryHandler(button_summarize_handler, pattern=r"btn_summarize\|.*"))
    application.add_handler(CallbackQueryHandler(callback_translate_to, pattern=r"translate_to\|.*"))
    application.add_handler(CallbackQueryHandler(callback_summarize_in, pattern=r"summarize_in\|.*"))
    application.add_handler(CommandHandler("translate", handle_translate))
    application.add_handler(CommandHandler("summarize", handle_summarize))
    application.add_handler(MessageHandler(filters.ALL, fallback))

    # set_bot_info wuxuu u baahan yahay application
    import asyncio
    asyncio.run(set_bot_info(application))

    # Bilaw Flask app-ka
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
