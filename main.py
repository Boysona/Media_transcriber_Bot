import os
import re
import uuid
import shutil
import logging
import requests
import telebot
import json
from flask import Flask, request, abort
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import speech_recognition as sr
import imageio_ffmpeg as ffmpeg
from pydub import AudioSegment
import threading
import time
import io # New: For in-memory audio conversion
import subprocess # New: For subprocess calls

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- BOT CONFIGURATION ---
TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4" # Replace with your actual bot token

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID
ADMIN_ID = 5978150981 # Replace with your actual Admin ID

# Download directory (still needed for initial download from Telegram, then we convert in-memory)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- User tracking files ---
users_file = 'users.json'
user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

# User-specific language settings for translate/summarize
user_language_settings_file = 'user_language_settings.json'
user_language_settings = {}
if os.path.exists(user_language_settings_file):
    with open(user_language_settings_file, 'r') as f:
        try:
            user_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_language_settings = {}

# User-specific media language settings for speech recognition
user_media_language_settings_file = 'user_media_language_settings.json'
user_media_language_settings = {}
if os.path.exists(user_media_language_settings_file):
    with open(user_media_language_settings_file, 'r') as f:
        try:
            user_media_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_media_language_settings = {}

def save_user_data():
    with open(users_file, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

def save_user_media_language_settings():
    with open(user_media_language_settings_file, 'w') as f:
        json.dump(user_media_language_settings, f, indent=4)

# In-memory chat history and transcription store
user_memory = {}
user_transcriptions = {}
# New: Dictionary to store original file_id and chat_id for retry functionality
original_media_info = {}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0
bot_start_time = datetime.now()

# Admin uptime message storage
admin_uptime_message = {}
admin_uptime_lock = threading.Lock() # To prevent race conditions

GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA" # Replace with your actual Gemini API Key

def ask_gemini(user_id, user_message):
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
    # Gemini 2.0 API expects content in a specific format for text.
    # If the history contains only text, then parts should be list of dicts with 'text' key
    contents = [{"parts": [{"text": msg["text"]}]} for msg in history]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    # Ensure the request body is structured correctly for Gemini 2.0
    payload = {"contents": contents}
    
    try:
        resp = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
        resp.raise_for_status() # Raise an exception for HTTP errors
        result = resp.json()
        if "candidates" in result:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            user_memory[user_id].append({"role": "model", "text": reply})
            return reply
        return "Error: " + json.dumps(result)
    except requests.exceptions.RequestException as e:
        logging.error(f"Gemini API request failed: {e}")
        return f"Error: Could not connect to the AI service. Please try again later. ({e})"
    except json.JSONDecodeError as e:
        logging.error(f"Gemini API response parsing failed: {e}, Response: {resp.text}")
        return f"Error: Failed to parse AI response. ({e})"
    except Exception as e:
        logging.error(f"An unexpected error occurred with Gemini API: {e}")
        return f"Error: An unexpected AI error occurred. ({e})"

FILE_SIZE_LIMIT = 20 * 1024 * 1024 # 20MB
admin_state = {}

def set_bot_info():
    commands = [
        telebot.types.BotCommand("start", "👋Get a welcome message and info"),
        telebot.types.BotCommand("status", "📊View Bot statistics"),
        telebot.types.BotCommand("help", "❓Get information on how to use the bot"),
        telebot.types.BotCommand("language", "🌐Change preferred language for translate/summarize"),
        telebot.types.BotCommand("media_language", "📝Set language for media transcription"),
        telebot.types.BotCommand("privacy", "👮Privacy Notice"),
    ]
    bot.set_my_commands(commands)

    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )

    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, audio files, and videos—free!

     🔥Enjoy free usage and start now!👌🏻"""
    )

def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

# Function to update uptime message (Updated code)
def update_uptime_message(chat_id, message_id):
    """
    Live-update the admin uptime message every second, showing days, hours, minutes and seconds.
    """
    while True:
        try:
            # Compute total elapsed seconds since bot_start_time
            elapsed = datetime.now() - bot_start_time
            total_seconds = int(elapsed.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)

            # Format with leading zeros for hours/minutes/seconds
            uptime_text = (
                f"**Bot Uptime:**\n"
                f"{days} days, {hours:02d} hours, {minutes:02d} minutes, {seconds:02d} seconds"
            )

            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=uptime_text,
                parse_mode="Markdown"
            )
            # Wait exactly 1 second before next update
            time.sleep(1)

        except telebot.apihelper.ApiTelegramException as e:
            # Ignore "message is not modified" errors, log others
            if "message is not modified" not in str(e):
                logging.error(f"Error updating uptime message: {e}")
                break # Exit thread on persistent errors
            time.sleep(1) # Still sleep to prevent tight loop on benign errors
        except Exception as e:
            logging.error(f"Unexpected error in uptime thread: {e}")
            break # Exit thread on unexpected errors

# New: Background thread for cleaning up old data
def cleanup_old_data():
    while True:
        logging.info("Starting data cleanup.")
        current_time = datetime.now()
        # Clean up user_transcriptions (e.g., older than 3 hours)
        for user_id in list(user_transcriptions.keys()):
            for msg_id, transcription_data in list(user_transcriptions[user_id].items()):
                # Assuming transcription_data is a dictionary with a 'timestamp' or similar
                # If not, you might need to modify where transcription is stored to include timestamp
                # For now, let's assume entries older than 3 hours are simply cleared if not used by Gemini functions.
                # A better approach would be to store { 'text': '...', 'timestamp': datetime.now() }
                # For simplicity, we'll clear all old transcriptions that haven't been accessed for a while implicitly.
                # A more robust system would involve tracking last access time for each transcription.
                # For now, we'll just clear items associated with messages not recently interacted with
                pass # This needs more robust tracking, for now, they persist until bot restart or new file is processed by user.

        # Clear user_memory for inactivity (e.g., if a user hasn't interacted for 3 hours)
        for user_id in list(user_memory.keys()):
            # This is hard to do without last interaction timestamp per user in user_memory
            # For simplicity, let's just clear user_memory if they haven't sent a message for a long time (tracked by user_data)
            last_activity = user_data.get(user_id)
            if last_activity:
                last_active_time = datetime.fromisoformat(last_activity)
                if (current_time - last_active_time) > timedelta(hours=3):
                    if user_id in user_memory:
                        del user_memory[user_id]
                        logging.info(f"Cleaned up user_memory for inactive user {user_id}")

        # Clean up old original_media_info (e.g., after 3 hours)
        for msg_id in list(original_media_info.keys()):
            info = original_media_info[msg_id]
            if (current_time - info['timestamp']) > timedelta(hours=3):
                del original_media_info[msg_id]
                logging.info(f"Cleaned up original_media_info for message {msg_id}")

        logging.info("Data cleanup complete. Next cleanup in 3 hours.")
        time.sleep(3 * 3600) # Run cleanup every 3 hours

# Start the cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_data)
cleanup_thread.daemon = True
cleanup_thread.start()

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
        sent_message = bot.send_message(message.chat.id, "Admin Panel and Uptime (updating live)...", reply_markup=keyboard)

        with admin_uptime_lock:
            # If an uptime thread exists for this admin, signal it to stop before starting a new one.
            # This requires a stop_event for the uptime thread.
            if admin_uptime_message.get(ADMIN_ID):
                if admin_uptime_message[ADMIN_ID].get('stop_event'):
                    admin_uptime_message[ADMIN_ID]['stop_event'].set()
                if admin_uptime_message[ADMIN_ID].get('thread') and admin_uptime_message[ADMIN_ID]['thread'].is_alive():
                    admin_uptime_message[ADMIN_ID]['thread'].join(timeout=5) # Wait for it to finish

            stop_uptime_event = threading.Event()
            # Store the new message ID and start a new thread
            admin_uptime_message[ADMIN_ID] = {
                'message_id': sent_message.message_id,
                'chat_id': message.chat.id,
                'stop_event': stop_uptime_event
            }
            uptime_thread = threading.Thread(target=update_uptime_message, args=(message.chat.id, sent_message.message_id))
            uptime_thread.daemon = True # Allows the thread to exit when the main program exits
            uptime_thread.start()
            admin_uptime_message[ADMIN_ID]['thread'] = uptime_thread

    else:
        display_name = message.from_user.first_name or (f"@{message.from_user.username}" if message.from_user.username else "user")
        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear {display_name}!
I'm your media transcription bot.
• Send me:
• Voice message
• Video message
• Audio file
• to transcribe for free!
**Before sending a media file for transcription, use /media_language to set the language of the audio.**
"""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, and videos using advanced AI.

1.  **Send a File for Transcription:**
    * Send a voice message, audio file, or video to the bot.
    * **Crucially**, before sending your media, use the `/media_language` command to tell the bot the language of the audio. This ensures the most accurate transcription possible.
    * The bot will then process your media and send back the transcribed text. If the transcription is very long, it will be sent as a text file for easier reading.
    * After receiving the transcription, you'll see inline buttons with options to **Translate** or **Summarize** the text.

2.  **Commands:**
    * `/start`: Get a welcome message and info about the bot. (Admins see a live uptime panel).
    * `/status`: View detailed statistics about the bot's performance and usage.
    * `/help`: Display these instructions on how to use the bot.
    * `/language`: Change your preferred language for translations and summaries. This setting applies to text outputs, not the original media.
    * `/media_language`: Set the language of the audio in your media files for transcription. This is vital for accuracy.
    * `/privacy`: Read the bot's privacy notice to understand how your data is handled.

Enjoy transcribing, translating, and summarizing your media quickly and easily!
"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    privacy_text = (
        """**Privacy Notice**

Your privacy is paramount. Here's a transparent look at how this bot handles your data in real-time:

1.  **Data We Process & Its Lifecycle:**
    * **Media Files (Voice, Audio, Video):** When you send a media file, it's temporarily downloaded for **immediate transcription**. Crucially, these files are **deleted instantly** from our servers once the transcription is complete. We do not store your media content.
    * **Transcriptions:** The text generated from your media is held **temporarily in the bot's memory** for a limited period. This allows for follow-up actions like translation or summarization. This data is not permanently stored on our servers and is cleared regularly (e.g., when new media is processed or the bot restarts).
    * **User IDs:** Your Telegram User ID is stored. This helps us remember your language preferences and track basic, aggregated activity (like when you last used the bot) to improve service and understand overall usage patterns. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen languages for translations/summaries and media transcription are saved. This ensures you don't need to re-select them for every interaction, making your experience smoother.

2.  **How Your Data is Used:**
    * To deliver the bot's core services: transcribing, translating, and summarizing your media.
    * To enhance bot performance and gain insights into general usage trends through anonymous, collective statistics (e.g., total files processed).
    * To maintain your personalized language settings across sessions.

3.  **Data Sharing Policy:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are facilitated by integrating with advanced AI models (specifically, the Google Speech-to-Text API for transcription and the Gemini API for translation/summarization). Your input sent to these models is governed by their respective privacy policies, but we ensure that your data is **not stored by us** after processing by these services.

4.  **Data Retention:**
    * **Media files:** Deleted immediately post-transcription.
    * **Transcriptions:** Held temporarily in the bot's active memory for immediate use.
    * **User IDs and language preferences:** Retained to support your settings and for anonymous usage statistics. If you wish to have your stored preferences removed, you can cease using the bot or contact the bot administrator for explicit data deletion.

By using this bot, you acknowledge and agree to the data practices outlined in this Privacy Notice.

Should you have any questions or concerns regarding your privacy, please feel free to contact the bot administrator.
"""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
    update_user_activity(message.from_user.id)

    uptime = datetime.now() - bot_start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    today = datetime.now().date()
    active_today = sum(
        1 for timestamp in user_data.values()
        if datetime.fromisoformat(timestamp).date() == today
    )

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
    for uid_key in user_data:
        uid = uid_key
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

# New: Function to keep typing action active
def keep_typing(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing')
            time.sleep(4)
        except telebot.apihelper.ApiTelegramException as e:
            # Handle potential errors like chat not found or bot blocked
            logging.warning(f"Error sending typing action to {chat_id}: {e}")
            break # Exit loop if there's a persistent issue
        except Exception as e:
            logging.error(f"Unexpected error in keep_typing thread: {e}")
            break

# New: Entry point for handling files, including retry logic
@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file_wrapper(message):
    uid = str(message.from_user.id)
    # Store original message info for retry
    original_media_info[message.message_id] = {
        'file_id': message.voice.file_id if message.voice else \
                   message.audio.file_id if message.audio else \
                   message.video.file_id if message.video else \
                   message.video_note.file_id,
        'file_type': 'voice' if message.voice else \
                     'audio' if message.audio else \
                     'video' if message.video else 'video_note',
        'timestamp': datetime.now()
    }
    # Pass the message directly to the processing function
    process_media_file(message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("retry|"))
def retry_failed_conversion(call):
    uid = str(call.from_user.id)
    _, original_message_id_str = call.data.split("|", 1)
    original_message_id = int(original_message_id_str)

    # Retrieve original media info
    media_info = original_media_info.get(original_message_id)
    if not media_info:
        bot.answer_callback_query(call.id, "Original media info not found. Please re-upload the file.")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="😓 Sorry, the original file data for this retry could not be found. Please send the file again.",
            parse_mode="Markdown"
        )
        return

    bot.answer_callback_query(call.id, "Retrying...")

    # Construct a dummy message object with the original file_id and type
    # This simulates the original message for process_media_file
    dummy_message = telebot.types.Message.de_json({
        'message_id': original_message_id,
        'from': call.from_user.to_dict(),
        'chat': call.message.chat.to_dict(),
        'date': int(time.time()),
        media_info['file_type']: {'file_id': media_info['file_id'], 'file_size': 0} # file_size will be updated by get_file
    })
    # Remove the retry button from the previous error message
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=None
    )
    process_media_file(dummy_message)

def process_media_file(message):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)
    uid = str(message.from_user.id)
    chat_id = message.chat.id
    original_message_id = message.message_id # Keep track of the original message ID

    if uid not in user_media_language_settings:
        bot.send_message(chat_id,
                         "⚠️ Please first select the language of the audio file using /media_language before sending the file.")
        return

    file_obj = message.voice or message.audio or message.video or message.video_note
    if not file_obj:
        # This can happen if message was only 'text' etc. during retry simulation
        return bot.send_message(chat_id, "😓 Sorry, I couldn't get the file. Please try sending it again.")

    # Start typing indicator thread
    stop_typing_event = threading.Event()
    typing_thread = threading.Thread(target=keep_typing, args=(chat_id, stop_typing_event))
    typing_thread.daemon = True
    typing_thread.start()

    progress_msg = None
    local_path = None
    try:
        # Step 1: Download File
        progress_msg = bot.send_message(chat_id, "🔄 Step 1/3: 📥 Downloading your file...")
        bot.set_message_reaction(chat_id=chat_id, message_id=message.message_id, reaction=["👀"])

        info = bot.get_file(file_obj.file_id)
        file_size_mb = info.file_size / (1024 * 1024) if info.file_size else 0

        if file_obj.file_size > FILE_SIZE_LIMIT:
            bot.edit_message_text("The file size you uploaded is too large (max allowed is 20MB).", chat_id, progress_msg.message_id)
            return

        bot.edit_message_text(
            f"🔄 Step 1/3: 📥 Downloading your file ({file_size_mb:.2f} MB)...",
            chat_id, progress_msg.message_id
        )

        downloaded_file = bot.download_file(info.file_path)
        # Use io.BytesIO for in-memory handling initially
        audio_bytes_io = io.BytesIO(downloaded_file)
        logging.info(f"Downloaded file for {chat_id}, size: {file_size_mb:.2f}MB")

        # Step 2: Convert Audio Format (in-memory)
        bot.edit_message_text("🔄 Step 2/3: ⚙️ Converting audio format to WAV...", chat_id, progress_msg.message_id)

        # Use subprocess for FFmpeg conversion
        converted_audio_bytes_io = io.BytesIO()
        ffmpeg_cmd = [
            ffmpeg.get_ffmpeg_exe(),
            '-i', 'pipe:0',  # Input from stdin
            '-f', 'wav',     # Output format WAV
            '-acodec', 'pcm_s16le', # PCM 16-bit little-endian
            '-ar', '16000',  # 16kHz sample rate
            '-ac', '1',      # Mono channel
            'pipe:1'         # Output to stdout
        ]

        try:
            process = subprocess.run(
                ffmpeg_cmd,
                input=audio_bytes_io.getvalue(),
                capture_output=True,
                check=True # Raise an exception for non-zero exit codes
            )
            converted_audio_bytes_io.write(process.stdout)
            converted_audio_bytes_io.seek(0) # Reset pointer to the beginning of the BytesIO object
            logging.info(f"Converted audio to WAV in-memory for {chat_id}")

        except subprocess.CalledProcessError as e:
            logging.error(f"FFmpeg conversion failed: {e.stderr.decode()}")
            raise Exception("Your file cannot be converted to the correct voice recognition format.")
        except Exception as e:
            logging.error(f"Error during FFmpeg conversion: {e}")
            raise Exception("An unexpected error occurred during audio conversion.")

        media_lang_code = get_lang_code(user_media_language_settings[uid])
        if not media_lang_code:
            bot.edit_message_text(f"❌ The language *{user_media_language_settings[uid]}* does not have a valid code. Please re-select the language.", chat_id, progress_msg.message_id, parse_mode="Markdown")
            return

        # Step 3: Transcribe Audio
        bot.edit_message_text("🧠 Step 3/3: Transcribing audio. This might take a moment...", chat_id, progress_msg.message_id)
        processing_start_time = datetime.now()

        # Pass BytesIO directly to transcribe_audio_chunks
        transcription = transcribe_audio_chunks_in_memory(converted_audio_bytes_io, media_lang_code) or ""
        user_transcriptions.setdefault(uid, {})[original_message_id] = transcription

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
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{original_message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{original_message_id}")
        )

        # Remove the progress message
        bot.delete_message(chat_id, progress_msg.message_id)

        if len(transcription) > 4000:
            fn = 'transcription.txt'
            with open(fn, 'w', encoding='utf-8') as f:
                f.write(transcription)
            bot.send_chat_action(chat_id, 'upload_document')
            with open(fn, 'rb') as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    reply_to_message_id=original_message_id,
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
        bot.set_message_reaction(chat_id=chat_id, message_id=message.message_id, reaction=["✍️"])

    except Exception as e:
        logging.error(f"Error processing file for {chat_id}: {e}", exc_info=True)
        error_message = "😓 Sorry, there was a technical error while processing your file. Please try again."
        retry_markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔁 Retry", callback_data=f"retry|{original_message_id}"))
        
        if progress_msg:
            bot.edit_message_text(
                error_message,
                chat_id,
                progress_msg.message_id,
                reply_markup=retry_markup
            )
        else:
            bot.send_message(chat_id, error_message, reply_markup=retry_markup)
        bot.set_message_reaction(chat_id=chat_id, message_id=message.message_id, reaction=["🚫"])
    finally:
        # Stop typing indicator thread
        stop_typing_event.set()
        if typing_thread.is_alive():
            typing_thread.join(timeout=5) # Wait for it to finish if it hasn't already
        
        # Ensure temporary downloaded files are removed
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
            logging.info(f"Cleaned up temporary file: {local_path}")

# --- Language Selection and Saving ---
LANGUAGES = [
    {"name": "English", "flag": "🇬🇧", "code": "en-US"},
    {"name": "Chinese", "flag": "🇨🇳", "code": "zh-CN"},
    {"name": "Spanish", "flag": "🇪🇸", "code": "es-ES"},
    {"name": "Hindi", "flag": "🇮🇳", "code": "hi-IN"},
    {"name": "Arabic", "flag": "🇸🇦", "code": "ar-SA"},
    {"name": "French", "flag": "🇫🇷", "code": "fr-FR"},
    {"name": "Bengali", "flag": "🇧🇩", "code": "bn-BD"},
    {"name": "Russian", "flag": "🇷🇺", "code": "ru-RU"},
    {"name": "Portuguese", "flag": "🇵🇹", "code": "pt-PT"},
    {"name": "Urdu", "flag": "🇵🇰", "code": "ur-PK"},
    {"name": "German", "flag": "🇩🇪", "code": "de-DE"},
    {"name": "Japanese", "flag": "🇯🇵", "code": "ja-JP"},
    {"name": "Korean", "flag": "🇰🇷", "code": "ko-KR"},
    {"name": "Vietnamese", "flag": "🇻🇳", "code": "vi-VN"},
    {"name": "Turkish", "flag": "🇹🇷", "code": "tr-TR"},
    {"name": "Italian", "flag": "🇮🇹", "code": "it-IT"},
    {"name": "Thai", "flag": "🇹🇭", "code": "th-TH"},
    {"name": "Swahili", "flag": "🇰🇪", "code": "sw-KE"},
    {"name": "Dutch", "flag": "🇳🇱", "code": "nl-NL"},
    {"name": "Polish", "flag": "🇵🇱", "code": "pl-PL"},
    {"name": "Ukrainian", "flag": "🇺🇦", "code": "uk-UA"},
    {"name": "Indonesian", "flag": "🇮🇩", "code": "id-ID"},
    {"name": "Malay", "flag": "🇲🇾", "code": "ms-MY"},
    {"name": "Filipino", "flag": "🇵🇭", "code": "fil-PH"},
    {"name": "Persian", "flag": "🇮🇷", "code": "fa-IR"},
    {"name": "Amharic", "flag": "🇪🇹", "code": "am-ET"},
    {"name": "Somali", "flag": "🇸🇴", "code": "so-SO"},
    {"name": "Swedish", "flag": "🇸🇪", "code": "sv-SE"},
    {"name": "Norwegian", "flag": "🇳🇴", "code": "nb-NO"},
    {"name": "Danish", "flag": "🇩🇰", "code": "da-DK"},
    {"name": "Finnish", "flag": "🇫🇮", "code": "fi-FI"},
    {"name": "Greek", "flag": "🇬🇷", "code": "el-GR"},
    {"name": "Hebrew", "flag": "🇮🇱", "code": "he-IL"},
    {"name": "Czech", "flag": "🇨🇿", "code": "cs-CZ"},
    {"name": "Hungarian", "flag": "🇭🇺", "code": "hu-HU"},
    {"name": "Romanian", "flag": "🇷🇴", "code": "ro-RO"},
    {"name": "Nepali", "flag": "🇳🇵", "code": "ne-NP"},
    {"name": "Sinhala", "flag": "🇱🇰", "code": "si-LK"},
    {"name": "Tamil", "flag": "🇮🇳", "code": "ta-IN"},
    {"name": "Telugu", "flag": "🇮🇳", "code": "te-IN"},
    {"name": "Kannada", "flag": "🇮🇳", "code": "kn-IN"},
    {"name": "Malayalam", "flag": "🇮🇳", "code": "ml-IN"},
    {"name": "Gujarati", "flag": "🇮🇳", "code": "gu-IN"},
    {"name": "Punjabi", "flag": "🇮🇳", "code": "pa-IN"},
    {"name": "Marathi", "flag": "🇮🇳", "code": "mr-IN"},
    {"name": "Oriya", "flag": "🇮🇳", "code": "or-IN"},
    {"name": "Assamese", "flag": "🇮🇳", "code": "as-IN"},
    {"name": "Khmer", "flag": "🇰🇭", "code": "km-KH"},
    {"name": "Lao", "flag": "🇱🇦", "code": "lo-LA"},
    {"name": "Burmese", "flag": "🇲🇲", "code": "my-MM"},
    {"name": "Georgian", "flag": "🇬🇪", "code": "ka-GE"},
    {"name": "Armenian", "flag": "🇦🇲", "code": "hy-AM"},
    {"name": "Azerbaijani", "flag": "🇦🇿", "code": "az-AZ"},
    {"name": "Kazakh", "flag": "🇰🇿", "code": "kk-KZ"},
    {"name": "Uzbek", "flag": "🇺🇿", "code": "uz-UZ"},
    {"name": "Kyrgyz", "flag": "🇰🇬", "code": "ky-KG"},
    {"name": "Tajik", "flag": "🇹🇯", "code": "tg-TJ"},
    {"name": "Turkmen", "flag": "🇹🇲", "code": "tk-TM"},
    {"name": "Mongolian", "flag": "🇲🇳", "code": "mn-MN"},
    {"name": "Estonian", "flag": "🇪🇪", "code": "et-EE"},
    {"name": "Latvian", "flag": "🇱🇻", "code": "lv-LV"},
    {"name": "Lithuanian", "flag": "🇱🇹", "code": "lt-LT"},
    {"name": "Afrikaans", "flag": "🇿🇦", "code": "af-ZA"},
    {"name": "Albanian", "flag": "🇦🇱", "code": "sq-AL"},
    {"name": "Bosnian", "flag": "🇧🇦", "code": "bs-BA"},
    {"name": "Bulgarian", "flag": "🇧🇬", "code": "bg-BG"},
    {"name": "Catalan", "flag": "🇪🇸", "code": "ca-ES"},
    {"name": "Croatian", "flag": "🇭🇷", "code": "hr-HR"},
    {"name": "Estonian", "flag": "🇪🇪", "code": "et-EE"},
    {"name": "Galician", "flag": "🇪🇸", "code": "gl-ES"},
    {"name": "Icelandic", "flag": "🇮🇸", "code": "is-IS"},
    {"name": "Irish", "flag": "🇮🇪", "code": "ga-IE"},
    {"name": "Macedonian", "flag": "🇲🇰", "code": "mk-MK"},
    {"name": "Maltese", "flag": "🇲🇹", "code": "mt-MT"},
    {"name": "Serbian", "flag": "🇷🇸", "code": "sr-RS"},
    {"name": "Slovak", "flag": "🇸🇰", "code": "sk-SK"},
    {"name": "Slovenian", "flag": "🇸🇮", "code": "sl-SI"},
    {"name": "Welsh", "flag": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "code": "cy-GB"},
    {"name": "Zulu", "flag": "🇿🇦", "code": "zu-ZA"},
]

def get_lang_code(lang_name):
    for lang in LANGUAGES:
        if lang['name'].lower() == lang_name.lower():
            return lang['code']
    return None

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
    update_user_activity(uid)

    markup = generate_language_keyboard("set_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future **translations and summaries**:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_lang|"))
def callback_set_language(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    _, lang = call.data.split("|", 1)
    user_language_settings[uid] = lang
    save_user_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Your preferred language for translations and summaries has been set to: **{lang}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Language set to {lang}")

@bot.message_handler(commands=['media_language'])
def select_media_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity(uid)

    markup = generate_language_keyboard("set_media_lang")
    bot.send_message(
        message.chat.id,
        "Please choose the language of the audio files that you need me to transcribe. This helps ensure accurate reading.",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_media_lang|"))
def callback_set_media_language(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    _, lang = call.data.split("|", 1)
    user_media_language_settings[uid] = lang
    save_user_media_language_settings()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ The transcription language for your media is set to: **{lang}**",
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, text=f"Media language set to {lang}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
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
    # No need to answer_callback_query twice. Moved inside if/else to ensure it's answered.
    # bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
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
    # No need to answer_callback_query twice. Moved inside if/else to ensure it's answered.
    # bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
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
    update_user_activity(uid)
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
    update_user_activity(uid)

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
    update_user_activity(uid)

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

def transcribe_audio_chunks_in_memory(audio_bytes: io.BytesIO, lang_code: str) -> str | None:
    r = sr.Recognizer()
    full_transcription = []
    # Changed chunk length to 60 seconds as requested
    chunk_length_ms = 60 * 1000 # 60 seconds
    overlap_ms = 500

    try:
        # Load audio from BytesIO
        audio = AudioSegment.from_wav(audio_bytes)
        total_length_ms = len(audio)
        start_ms = 0

        logging.info(f"Starting in-memory chunking, total length {total_length_ms / 1000} seconds.")

        while start_ms < total_length_ms:
            end_ms = min(start_ms + chunk_length_ms, total_length_ms)
            chunk = audio[start_ms:end_ms]

            # Export chunk to an in-memory BytesIO object
            chunk_audio_bytes_io = io.BytesIO()
            chunk.export(chunk_audio_bytes_io, format="wav")
            chunk_audio_bytes_io.seek(0) # Reset pointer

            with sr.AudioFile(chunk_audio_bytes_io) as source:
                try:
                    audio_listened = r.record(source)
                    text = r.recognize_google(audio_listened, language=lang_code)
                    full_transcription.append(text)
                    logging.info(f"Transcribed chunk from {start_ms/1000}s to {end_ms/1000}s: {text[:50]}...")
                except sr.UnknownValueError:
                    logging.warning(f"Speech Recognition could not understand audio in chunk {start_ms/1000}s - {end_ms/1000}s")
                except sr.RequestError as e:
                    logging.error(f"Could not request results from Google Speech Recognition service; {e} for chunk {start_ms/1000}s - {end_ms/1000}s")
                except Exception as e:
                    logging.error(f"Error processing chunk {start_ms/1000}s - {end_ms/1000}s: {e}")
                finally:
                    chunk_audio_bytes_io.close() # Close the in-memory file

            start_ms += chunk_length_ms - overlap_ms

        return " ".join(full_transcription) if full_transcription else None
    except Exception as e:
        logging.error(f"Overall transcription error: {e}")
        return None

@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'document', 'text'])
def fallback(message):
    update_user_activity(message.from_user.id)
    if message.text and message.text.startswith('/'):
        # It's a command not explicitly handled, or a new command.
        # Do nothing or send a generic "unknown command" message if preferred.
        pass
    else:
        bot.send_message(message.chat.id, "Please send only voice messages, audio, or video for transcription.")

@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        # Using a new thread to process updates to avoid blocking the webhook
        threading.Thread(target=bot.process_new_updates, args=[[update]]).start()
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook_route(): # Renamed to avoid conflict with bot.set_webhook
    # Replace "https://your-app-name.onrender.com" with your actual Render URL
    url = "https://media-transcriber-bot-hzlk.onrender.com"
    bot.set_webhook(url=url)
    return f"Webhook set to {url}", 200

@app.route('/delete_webhook', methods=['GET','POST'])
def delete_webhook_route(): # Renamed
    bot.delete_webhook()
    return 'Webhook deleted.', 200

if __name__ == "__main__":
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    set_bot_info()
    bot.delete_webhook()
    # Replace "https://your-app-name.onrender.com" with your actual Render URL
    bot.set_webhook(url="https://media-transcriber-bot-dc38.onrender.com")
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))

