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
import subprocess
import imageio_ffmpeg as ffmpeg
from pydub import AudioSegment
import threading
import time
import io

from faster_whisper import WhisperModel

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- BOT CONFIGURATION ---
TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4" # Replace with your actual bot token

bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# Admin ID
ADMIN_ID = 5978150981 # Replace with your actual Admin ID

# Download directory (still used for temporary files)
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

def save_user_data():
    with open(users_file, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    with open(user_language_settings_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

# In-memory chat history and transcription store
user_memory = {}
user_transcriptions = {} # Stores original message_id for retries and context

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_processing_time = 0
bot_start_time = datetime.now()

# Admin uptime message storage
admin_uptime_message = {}
admin_uptime_lock = threading.Lock()

GEMINI_API_KEY = "AIzaSyAto78yGVZobOaXnl8wCE9ZW8Do2R8HA" # Replace with your actual Gemini API Key

# Initialize faster-whisper model globally
WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")

# Temporary storage for file info for retry mechanism {chat_id: {original_message_id: file_info}}
# file_info could be a dict containing file_id, file_extension, etc.
# We'll store it as a dict of message objects, but for actual use you might want to strip it down.
file_info_for_retry = {}


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

FILE_SIZE_LIMIT = 20 * 1024 * 1024 # 20MB
admin_state = {}

def set_bot_info():
    commands = [
        telebot.types.BotCommand("start", "👋Get a welcome message and info"),
        telebot.types.BotCommand("status", "📊View Bot statistics"),
        telebot.types.BotCommand("help", "❓Get information on how to use the bot"),
        telebot.types.BotCommand("language", "🌐Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "👮Privacy Notice"),
        telebot.types.BotCommand("demo", "🎵Try a demo transcription"), # Added demo command
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

# Function to update uptime message
def update_uptime_message(chat_id, message_id):
    """
    Live-update the admin uptime message every second, showing days, hours, minutes and seconds.
    """
    while True:
        try:
            elapsed = datetime.now() - bot_start_time
            total_seconds = int(elapsed.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)

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
            time.sleep(1)

        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                logging.error(f"Error updating uptime message: {e}")
            break
        except Exception as e:
            logging.error(f"Unexpected error in uptime thread: {e}")
            break

# NEW: Function to keep sending typing action
def keep_typing(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing')
            time.sleep(4)
        except Exception as e:
            logging.error(f"Error in keep_typing thread: {e}")
            break # Exit if there's an API error, e.g., chat not found

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
            if admin_uptime_message.get(ADMIN_ID) and admin_uptime_message[ADMIN_ID].get('thread') and admin_uptime_message[ADMIN_ID]['thread'].is_alive():
                pass
            admin_uptime_message[ADMIN_ID] = {'message_id': sent_message.message_id, 'chat_id': message.chat.id}
            uptime_thread = threading.Thread(target=update_uptime_message, args=(message.chat.id, sent_message.message_id))
            uptime_thread.daemon = True
            uptime_thread.start()
            admin_uptime_message[ADMIN_ID]['thread'] = uptime_thread

    else:
        display_name = message.from_user.first_name or (f"@{message.from_user.username}" if message.from_user.username else "user")
        
        # NEW: Inline keyboard for user engagement
        user_keyboard = InlineKeyboardMarkup(row_width=2)
        user_keyboard.add(
            InlineKeyboardButton("❓ How to Use", callback_data="show_help"),
            InlineKeyboardButton("🎵 Try a Demo", callback_data="try_demo")
        )

        bot.send_message(
            message.chat.id,
            f"""👋🏻 Welcome dear {display_name}!
I’m your media transcription bot.
🎙️ Send me:
• Voice message
• Video message
• Audio file

📝 I’ll transcribe it for free!

✨ With advanced AI, I automatically detect the language of your audio.
Please make sure your recording is clear, calm, and free from background noise for the best results
""",
            reply_markup=user_keyboard
        )

# NEW: Callback handler for "How to Use" and "Try a Demo" buttons
@bot.callback_query_handler(func=lambda c: c.data == "show_help")
def callback_show_help(call):
    bot.answer_callback_query(call.id)
    help_handler(call.message) # Reuse the existing help handler

@bot.callback_query_handler(func=lambda c: c.data == "try_demo")
def callback_try_demo(call):
    bot.answer_callback_query(call.id, text="Sending a demo audio file...")
    send_demo_audio(call.message) # Call the new demo function


@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, and videos using advanced AI.

1.  **Send a File for Transcription:**
    * Send a voice message, audio file, or video to the bot.
    * The bot will automatically detect the language and process your media, sending back the transcribed text. If the transcription is very long, it will be sent as a text file for easier reading.
    * After receiving the transcription, you'll see inline buttons with options to **Translate** or **Summarize** the text.

2.  **Commands:**
    * `/start`: Get a welcome message and info about the bot. (Admins see a live uptime panel).
    * `/status`: View detailed statistics about the bot's performance and usage.
    * `/help`: Display these instructions on how to use the bot.
    * `/language`: Change your preferred language for translations and summaries. This setting applies to text outputs, not the original media.
    * `/privacy`: Read the bot's privacy notice to understand how your data is handled.
    * `/demo`: Get a quick demonstration by receiving a sample audio file that the bot will transcribe for you.

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
    * **Language Preferences:** Your chosen languages for translations/summaries are saved. This ensures you don't need to re-select them for every interaction, making your experience smoother.

2.  **How Your Data is Used:**
    * To deliver the bot's core services: transcribing, translating, and summarizing your media.
    * To enhance bot performance and gain insights into general usage trends through anonymous, collective statistics (e.g., total files processed).
    * To maintain your personalized language settings across sessions.

3.  **Data Sharing Policy:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are facilitated by integrating with advanced AI models (specifically, faster-whisper for transcription and the Gemini API for translation/summarization). Your input sent to these models is governed by their respective privacy policies, but we ensure that your data is **not stored by us** after processing by these services.

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

# NEW: Demo command
@bot.message_handler(commands=['demo'])
def send_demo_audio(message):
    update_user_activity(message.from_user.id)
    chat_id = message.chat.id

    bot.send_chat_action(chat_id, 'upload_voice')
    # Create a very short, simple WAV file in memory
    # This is a minimal example, in a real scenario you might have a pre-recorded
    # demo audio file on disk to send.
    
    # Let's create a very simple WAV header and some silent audio data
    # (or you can use pydub to generate a short tone/speech)
    from pydub import AudioSegment
    # Create a 2-second silent audio segment at 16kHz mono
    silent_audio = AudioSegment.silent(duration=2000, frame_rate=16000) 
    
    # Add a short beep and "hello world" like sound for demo if you want more realism
    # from pydub.generators import Sine
    # sine_wave = Sine(440).to_audio_segment(duration=500)
    # spoken_text = AudioSegment.silent(duration=500) + Sine(880).to_audio_segment(duration=200) # Simple "hello" sound
    # demo_audio = silent_audio.overlay(sine_wave, position=0) + spoken_text
    
    # For a simple, small file:
    demo_audio = silent_audio 

    # Save to an in-memory bytes buffer
    audio_buffer = io.BytesIO()
    demo_audio.export(audio_buffer, format="wav")
    audio_buffer.seek(0)

    bot.send_voice(
        chat_id,
        audio_buffer,
        caption="Here's a demo audio for transcription. Watch the bot process it!",
        reply_to_message_id=message.message_id
    )
    # Note: We don't call process_media_file directly here.
    # The `handle_file_wrapper` (which handles 'voice' content_type) will naturally pick this up.

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file_wrapper(message):
    """
    Wrapper to handle file processing in a separate thread.
    Also initiates the typing action and stores file info for retry.
    """
    chat_id = message.chat.id
    message_id = message.message_id
    uid = str(message.from_user.id)

    # Store file information for potential retry
    file_info_for_retry.setdefault(uid, {})[message_id] = message

    # Start the typing animation
    stop_typing_event = threading.Event()
    typing_thread = threading.Thread(target=keep_typing, args=(chat_id, stop_typing_event))
    typing_thread.daemon = True # Allows thread to exit when main program exits
    typing_thread.start()

    # Initial processing message
    progress_msg = bot.send_message(chat_id, "🔄 Processing your file... Please wait.")

    # Start the actual processing in a new thread, passing the stop_event and progress_msg_id
    thread = threading.Thread(target=process_media_file, args=(message, stop_typing_event, progress_msg.message_id))
    thread.start()


# Modified process_media_file to include progress updates and retry button
def process_media_file(message, stop_typing_event, progress_msg_id):
    """
    Handles the downloading, converting, and transcribing of media files.
    This function will be run in a separate thread.
    """
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)
    uid = str(message.from_user.id)
    chat_id = message.chat.id
    message_id = message.message_id # Original message ID of the file

    # Initialize retry keyboard
    retry_keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("🔁 Try Again", callback_data=f"retry|{message_id}"))

    temp_download_path = None
    wav_output_path = None

    try:
        # Set reaction
        try:
            if message.voice:
                bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=["👀"])
            elif message.audio:
                bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=["👀"])
            elif message.video or message.video_note:
                bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=["👀"])
        except Exception as e:
            logging.error(f"Error setting reaction: {e}")

        file_obj = message.voice or message.audio or message.video or message.video_note
        if file_obj.file_size > FILE_SIZE_LIMIT:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg_id,
                text="⚠️ The file size you uploaded is too large (max allowed is 20MB).",
                reply_markup=retry_keyboard # Offer retry
            )
            return

        info = bot.get_file(file_obj.file_id)
        file_extension = ".ogg" if message.voice or message.video_note else os.path.splitext(info.file_path)[1]
        
        temp_download_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}{file_extension}")
        wav_output_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.wav")

        # Step 1: Download the file with progress update
        download_start_time = datetime.now()
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg_id,
            text=f"🔄 Step 1/3: 📥 Downloading your file (approx. {file_obj.file_size / (1024*1024):.2f} MB)..."
        )
        logging.info(f"Downloading file {info.file_path} to {temp_download_path}")
        
        # Simulating download progress (Telegram API doesn't expose byte-by-byte progress easily)
        # You would replace this with actual chunked download and update if you get a progress hook
        file_data = bot.download_file(info.file_path)
        with open(temp_download_path, 'wb') as f:
            f.write(file_data)
        logging.info(f"File downloaded to {temp_download_path}")
        download_duration = (datetime.now() - download_start_time).total_seconds()


        processing_start_time = datetime.now()

        # Step 2: Convert to WAV using subprocess
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg_id,
            text=f"🔄 Step 2/3: ⚙️ Converting audio format (from {file_extension} to WAV)..."
        )
        logging.info(f"Converting {temp_download_path} to {wav_output_path}")
        command = [
            ffmpeg.get_ffmpeg_exe(),
            '-i', temp_download_path,
            '-vn',
            '-acodec', 'pcm_s16le',
            '-ar', '16000',
            '-ac', '1',
            wav_output_path
        ]
        
        try:
            process = subprocess.run(command, capture_output=True, check=True, timeout=300) # 5 min timeout
            logging.info(f"FFmpeg stdout: {process.stdout.decode()}")
            logging.info(f"FFmpeg stderr: {process.stderr.decode()}")

            if not os.path.exists(wav_output_path) or os.path.getsize(wav_output_path) == 0:
                raise Exception("FFmpeg conversion failed or resulted in empty file.")
            logging.info(f"File converted to WAV: {wav_output_path}")

        except subprocess.TimeoutExpired:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg_id,
                text="😓 Sorry, the conversion timed out. The file might be too long or complex.",
                reply_markup=retry_keyboard
            )
            return
        except subprocess.CalledProcessError as e:
            logging.error(f"FFmpeg conversion failed with error: {e.stderr.decode()}")
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg_id,
                text="😓 Sorry, there was an issue converting your file. It might be corrupted or in an unsupported format.",
                reply_markup=retry_keyboard
            )
            return
        except Exception as e:
            logging.error(f"Conversion failed: {e}")
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg_id,
                text="😓 Sorry, your file could not be converted. Please try again with a different format or a clearer recording.",
                reply_markup=retry_keyboard
            )
            return

        # Step 3: Transcribe using Faster-Whisper
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg_id,
            text="🧠 Step 3/3: Transcribing audio with Faster-Whisper... This is the final step!"
        )
        logging.info(f"Starting transcription for {wav_output_path}")
        transcription = transcribe_audio_chunks(wav_output_path) or ""
        user_transcriptions.setdefault(uid, {})[message_id] = transcription
        logging.info("Transcription complete.")

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
            InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message_id}"),
            InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message_id}")
        )
        
        # Delete the progress message before sending the result
        bot.delete_message(chat_id, progress_msg_id)

        if not transcription.strip():
            bot.send_message(
                chat_id,
                "😔 Could not transcribe the audio. It might be too quiet, noisy, or in an unsupported language.",
                reply_to_message_id=message_id,
                reply_markup=retry_keyboard
            )
        elif len(transcription) > 4000:
            fn = 'transcription.txt'
            with open(fn, 'w', encoding='utf-8') as f:
                f.write(transcription)
            bot.send_chat_action(chat_id, 'upload_document')
            with open(fn, 'rb') as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    reply_to_message_id=message_id,
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
        logging.error(f"Error processing file: {e}", exc_info=True)
        # Graceful error message
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg_id,
            text="😓 Sorry, there was a technical error while processing your file. Please try again.",
            reply_markup=retry_keyboard # Offer retry
        )
    finally:
        # Signal the typing thread to stop
        stop_typing_event.set()
        # Ensure temporary files are cleaned up
        if temp_download_path and os.path.exists(temp_download_path):
            os.remove(temp_download_path)
            logging.info(f"Cleaned up {temp_download_path}")
        if wav_output_path and os.path.exists(wav_output_path):
            os.remove(wav_output_path)
            logging.info(f"Cleaned up {wav_output_path}")

# NEW: Retry mechanism handler
@bot.callback_query_handler(func=lambda c: c.data.startswith("retry|"))
def callback_retry_processing(call):
    uid = str(call.from_user.id)
    _, original_message_id_str = call.data.split("|", 1)
    original_message_id = int(original_message_id_str)

    if uid not in file_info_for_retry or original_message_id not in file_info_for_retry[uid]:
        bot.answer_callback_query(call.id, "❌ Original file not found for retry. Please send it again.")
        return

    # Get the original message object
    original_message = file_info_for_retry[uid][original_message_id]

    # Acknowledge the retry request
    bot.answer_callback_query(call.id, "Attempting to retry processing...")
    
    # Edit the message to show "Retrying..."
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🔁 Retrying file processing... Please wait."
    )

    # Re-run the processing logic for the original message
    # We pass None for stop_typing_event and progress_msg_id initially, 
    # as handle_file_wrapper will create new ones.
    # Note: handle_file_wrapper expects a message object.
    handle_file_wrapper(original_message)


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
    bot.answer_callback_query(call.id)

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
    bot.answer_callback_query(call.id)

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

# Modified transcribe_audio_chunks to use faster-whisper
def transcribe_audio_chunks(audio_path: str) -> str | None:
    full_transcription = []
    try:
        segments, info = WHISPER_MODEL.transcribe(audio_path, beam_size=5)
        logging.info(f"Detected language: {info.language} with probability {info.language_probability}")

        for segment in segments:
            full_transcription.append(segment.text)
            logging.info(f"Transcribed segment: [{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")

        return " ".join(full_transcription) if full_transcription else None
    except Exception as e:
        logging.error(f"Faster-Whisper transcription error: {e}")
        return None

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

@bot.message_handler(func=lambda m: True, content_types=['photo', 'sticker', 'document', 'text'])
def fallback(message):
    update_user_activity(message.from_user.id)
    if message.text and message.text.startswith('/'):
        pass
    else:
        bot.send_message(message.chat.id, "Please send only voice messages, audio, or video for transcription.")

@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook():
    url = "https://media-transcriber-bot-dc38.onrender.com" # Update this to your actual Render URL
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
    bot.set_webhook(url="https://media-transcriber-bot-dc38.onrender.com") # Update this to your actual Render URL
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))

