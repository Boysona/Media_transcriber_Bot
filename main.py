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

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot token
TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4" # Consider moving this to an environment variable for security

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID
ADMIN_ID = 5978150981 # Consider moving this to an environment variable

# Download directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model for transcription
# 'tiny' model is very small and might not be accurate. Consider 'base' or 'small' for better quality
# Ensure the model is downloaded or accessible from your deployment environment
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
    """Saves user activity data to a JSON file."""
    with open(users_file, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    """Saves user language preferences to a JSON file."""
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

# In-memory chat history and transcription store
user_memory = {} # Stores chat history for Gemini
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0  # in seconds
bot_start_time = datetime.now() # This is when the bot started

# Gemini API key
GEMINI_API_KEY = "AIzaSyAto78yGVZobkOwPXnl8wCE9ZW8Do2R8HA" # Consider moving this to an environment variable for security

def ask_gemini(user_id, user_message):
    """
    Sends a message to the Gemini API and retrieves a response.
    Maintains a simple in-memory chat history for context.
    """
    # Append user message to history
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    # Keep only the last 10 messages for context
    history = user_memory[user_id][-10:]
    # Format history for Gemini API
    parts = [{"text": msg["text"]} for msg in history]
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {"contents": [{"parts": parts}]}
    
    try:
        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        result = resp.json()
        
        if "candidates" in result and result['candidates']:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            user_memory[user_id].append({"role": "model", "text": reply}) # Store Gemini's reply
            return reply
        elif "promptFeedback" in result and "blockReason" in result["promptFeedback"]:
            logging.warning(f"Gemini prompt blocked for user {user_id}: {result['promptFeedback']['blockReason']}")
            return "I'm sorry, your request could not be processed due to content policy. Please try rephrasing."
        else:
            logging.error(f"Unexpected Gemini API response for user {user_id}: {json.dumps(result)}")
            return "Error: Unexpected response from AI."
    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling Gemini API for user {user_id}: {e}")
        return "Error: Could not connect to AI service."
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding Gemini API response for user {user_id}: {e}. Response: {resp.text if 'resp' in locals() else 'N/A'}")
        return "Error: Invalid response from AI."


FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {} # To manage admin-specific interactions like broadcasting

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

    # Short description (About)
    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )

    # Full description (What can this bot do?)
    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free

     🔥Enjoy free usage and start now!👌🏻"""
    )

def update_user_activity(user_id):
    """Updates the last active timestamp for a user."""
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

---
## Bot Commands

@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command, welcoming users and setting up admin panel if applicable."""
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    
    # Initialize user data if new
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()
    
    # Admin panel for the admin user
    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("Send Broadcast", "Total Users", "/status")
        bot.send_message(message.chat.id, "Welcome to the Admin Panel!", reply_markup=keyboard)
    else:
        # Prioritize first_name, then username for display
        display_name = message.from_user.first_name if message.from_user.first_name else (
            f"@{message.from_user.username}" if message.from_user.username else "there"
        )
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear **{display_name}**!
• Send me:
• Voice message
• Video message
• Audio file
• to transcribe for free
""",
            parse_mode="Markdown"
        )


@bot.message_handler(commands=['help'])
def help_handler(message):
    """Provides instructions on how to use the bot."""
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, and videos.

1.  **Send a File:** Send a voice message, audio, or video.
2.  **Receive Transcription:** The bot will process your input and send back the transcribed text. Long transcriptions will be sent as a text file.
3.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.
4.  **Commands:**
    -   `/start`: Restart the bot.
    -   `/status`: View bot statistics.
    -   `/help`: Display these instructions.
    -   `/language`: Change your preferred language for translations and summaries.
    -   `/privacy`: View the bot's privacy notice.

Enjoy transcribing your media quickly and easily!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    """Displays the bot's privacy policy."""
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and TikTok links you send are temporarily processed for transcription. These files are **deleted immediately** after processing. We do not store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, and summarization.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed).
    * To set your preferred language for future interactions.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Your text input to these models is handled according to their respective privacy policies, but we do not store this data after processing.

4.  **Data Retention:**
    * Media files are deleted immediately after transcription.
    * Transcriptions are held temporarily in memory.
    * User IDs and language preferences are retained to maintain your settings and usage statistics. You can always delete your data by stopping using the bot or contacting the bot administrator.

By using this bot, you agree to the terms outlined in this Privacy Notice.

If you have any questions, please contact the bot administrator."""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    """Displays real-time statistics about the bot's operation."""
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
        f"▫️ Total Registered Users: {len(user_data)}\n\n"
        "⚙️ Processing Statistics\n"
        f"▫️ Total Files Processed: {total_files_processed}\n"
        f"▫️ Audio Files: {total_audio_files}\n"
        f"▫️ Voice Clips: {total_voice_clips}\n"
        f"▫️ Videos: {total_videos}\n"
        f"⏱️ Total Processing Time: {proc_hours}h {proc_minutes}m {proc_seconds}s\n\n"
        "⸻\n\n"
        "Thanks for using our service! 🙌"
    )

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

---
## Admin Functionality

@bot.message_handler(func=lambda m: m.text == "Total Users" and m.from_user.id == ADMIN_ID)
def total_users(message):
    """Admin command to show the total number of registered users."""
    bot.send_message(message.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "Send Broadcast" and m.from_user.id == ADMIN_ID)
def send_broadcast(message):
    """Admin command to initiate a broadcast message to all users."""
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send the broadcast message now:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document']
)
def broadcast_message(message):
    """Receives the broadcast message from the admin and sends it to all users."""
    admin_state[message.from_user.id] = None # Reset admin state
    success = fail = 0
    for uid in user_data:
        try:
            # Copy message to preserve its original format (text, photo, etc.)
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.warning(f"Failed to send broadcast to user {uid}: {e}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"Broadcast complete.\nSuccessful: {success}\nFailed: {fail}"
    )

---
## Media Processing

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    """Handles incoming voice messages, audio files, videos, and video notes for transcription."""
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)

    # Add reaction emoji based on file type
    try:
        reaction_emoji = "🎧"
        if message.voice:
            reaction_emoji = "🎤"
        elif message.audio:
            reaction_emoji = "🎶"
        elif message.video or message.video_note:
            reaction_emoji = "🎬"
        bot.set_message_reaction(chat_id=message.chat.id, message_id=message.message_id, reaction=[reaction_emoji])
    except Exception as e:
        logging.error(f"Error setting reaction: {e}")

    # Determine file object based on content type
    file_obj = message.voice or message.audio or message.video or message.video_note

    # Check file size limit
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    # Get file information and prepare local path
    info = bot.get_file(file_obj.file_id)
    # Use a unique temporary file name
    file_extension = os.path.splitext(info.file_path)[1] if info.file_path else '.ogg' # Default to .ogg
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}{file_extension}")
    
    # Indicate to the user that processing is happening
    sent_message = bot.send_message(message.chat.id, "Processing your file... please wait. ⏳")
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        # Download the file
        data = bot.download_file(info.file_path)
        with open(local_path, 'wb') as f:
            f.write(data)

        bot.send_chat_action(message.chat.id, 'typing')
        processing_start_time = datetime.now()

        # Transcribe the file
        transcription = transcribe(local_path)
        if transcription is None:
            raise Exception("Transcription failed.")

        uid = str(message.from_user.id)
        # Store transcription linked to the original message ID
        user_transcriptions.setdefault(uid, {})[message.message_id] = transcription

        # Update global statistics
        total_files_processed += 1
        if message.voice:
            total_voice_clips += 1
        elif message.audio:
            total_audio_files += 1
        elif message.video or message.video_note:
            total_videos += 1

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        # Create inline keyboard for post-transcription actions
        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate 🌐", callback_data=f"btn_translate|{message.message_id}"),
            InlineKeyboardButton("Summarize 📝", callback_data=f"btn_summarize|{message.message_id}")
        )
        
        # Send transcription back to user
        if len(transcription) > 4000:
            # If transcription is too long, send as a text file
            fn = os.path.join(DOWNLOAD_DIR, 'transcription.txt')
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
            os.remove(fn) # Clean up the temporary file
        else:
            # Send transcription directly as a reply
            bot.edit_message_text(
                chat_id=sent_message.chat.id,
                message_id=sent_message.message_id,
                text="✅ Done! Here's your transcription:"
            )
            bot.reply_to(
                message,
                transcription,
                reply_markup=buttons
            )
    except Exception as e:
        logging.error(f"Error processing file for user {message.from_user.id}: {e}")
        bot.edit_message_text(
            chat_id=sent_message.chat.id,
            message_id=sent_message.message_id,
            text="⚠️ An error occurred during transcription. Please try again or send a different file."
        )
    finally:
        # Clean up downloaded file
        if os.path.exists(local_path):
            os.remove(local_path)
            logging.info(f"Cleaned up {local_path}")

def transcribe(path: str) -> str | None:
    """
    Transcribes an audio file using the Whisper model.
    """
    try:
        # segments: an iterator over the transcribed segments
        # info: a named tuple with information about the transcription
        segments, info = model.transcribe(path, beam_size=5) # Increased beam_size for potentially better accuracy
        logging.info(f"Detected language: {info.language} with probability {info.language_probability:.2f}")
        
        full_transcription = " ".join(segment.text for segment in segments)
        return full_transcription.strip() # Remove leading/trailing whitespace
    except Exception as e:
        logging.error(f"Transcription error for file {path}: {e}")
        return None

---
## Language Selection and Actions

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
    """Generates an inline keyboard with language options."""
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
    """Allows users to select their preferred language for translation/summarization."""
    markup = generate_language_keyboard("set_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future translations and summaries:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_lang|"))
def callback_set_language(call):
    """Handles the callback for setting the preferred language."""
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
    """Handles the 'Translate' button callback, offering language selection or immediate translation."""
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, f"Translating to {preferred_lang}...")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Translating to **{preferred_lang}**...",
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
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    """Handles the 'Summarize' button callback, offering language selection or immediate summarization."""
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, f"Summarizing in {preferred_lang}...")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Summarizing in **{preferred_lang}**...",
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
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    """Handles the language selection for translation."""
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    # Save preferred language for future use
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
        # Fallback if message_id is not present in callback_data (e.g., if command was used directly)
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
             do_translate_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to translate. Please use the inline buttons on the transcription or reply to a transcription message.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    """Handles the language selection for summarization."""
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    # Save preferred language for future use
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
        # Fallback if message_id is not present in callback_data
        if call.message.reply_to_message and call.message.reply_to_message.message_id in user_transcriptions.get(uid, {}):
            do_summarize_with_saved_lang(call.message, uid, lang, call.message.reply_to_message.message_id)
        else:
            bot.send_message(call.message.chat.id, "❌ No transcription found for this message to summarize. Please use the inline buttons on the transcription or reply to a transcription message.")
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
        bot.send_message(message.chat.id, f"Error during translation: {translated}", reply_to_message_id=message_id)
        return

    # Send translated text
    if len(translated) > 4000:
        fn = os.path.join(DOWNLOAD_DIR, 'translation.txt')
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(translated)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Translation to {lang} 👇", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, f"**Translation ({lang}):**\n\n{translated}", parse_mode="Markdown", reply_to_message_id=message_id)

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
        bot.send_message(message.chat.id, f"Error during summarization: {summary}", reply_to_message_id=message_id)
        return

    # Send summarized text
    if len(summary) > 4000:
        fn = os.path.join(DOWNLOAD_DIR, 'summary.txt')
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(summary)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(fn, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=f"Summary in {lang} 👇", reply_to_message_id=message_id)
        os.remove(fn)
    else:
        bot.send_message(message.chat.id, f"**Summary ({lang}):**\n\n{summary}", parse_mode="Markdown", reply_to_message_id=message_id)


@bot.message_handler(commands=['translate'])
def handle_translate(message):
    """Command to initiate translation, requiring a reply to a transcription."""
    uid = str(message.from_user.id)

    if not message.reply_to_message or uid not in user_transcriptions or message.reply_to_message.message_id not in user_transcriptions[uid]:
        return bot.send_message(message.chat.id, "❌ Please reply to a transcription message to translate it.")

    transcription_message_id = message.reply_to_message.message_id
    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.send_message(message.chat.id, f"Translating to {preferred_lang}...")
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
    """Command to initiate summarization, requiring a reply to a transcription."""
    uid = str(message.from_user.id)

    if not message.reply_to_message or uid not in user_transcriptions or message.reply_to_message.message_id not in user_transcriptions[uid]:
        return bot.send_message(message.chat.id, "❌ Please reply to a transcription message to summarize it.")

    transcription_message_id = message.reply_to_message.message_id
    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.send_message(message.chat.id, f"Summarizing in {preferred_lang}...")
        do_summarize_with_saved_lang(message, uid, preferred_lang, transcription_message_id)
    else:
        markup = generate_language_keyboard("summarize_in", transcription_message_id)
        bot.send_message(
            message.chat.id,
            "Please select the language you want the summary in:",
            reply_markup=markup
        )

---
## Fallback and Webhook Setup

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    """Handles messages that are not voice, audio, or video files."""
    update_user_activity(message.from_user.id)
    bot.send_message(message.chat.id, "Please send only voice, audio, or video files for transcription.")

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
    # Replace with your actual deployed URL
    url = "https://media-transcriber-bot.onrender.com" 
    try:
        bot.set_webhook(url=url)
        return f"Webhook set to {url}", 200
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        return f"Failed to set webhook: {e}", 500

@app.route('/delete_webhook', methods=['GET','POST'])
def delete_webhook_route():
    """Route to manually delete the webhook."""
    try:
        bot.delete_webhook()
        return 'Webhook deleted.', 200
    except Exception as e:
        logging.error(f"Failed to delete webhook: {e}")
        return f"Failed to delete webhook: {e}", 500

if __name__ == "__main__":
    # Clean up downloads directory on startup
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    set_bot_info()
    # It's good practice to delete any old webhooks before setting a new one
    bot.delete_webhook()
    # Ensure this URL matches your deployment environment
    bot.set_webhook(url=os.environ.get('WEBHOOK_URL', "https://media-transcriber-bot.onrender.com")) 
    
    # Run the Flask app
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))

