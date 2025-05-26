import os
import re
import uuid
import shutil
import logging
import requests
import telebot
import json
import yt_dlp
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Bot configuration
TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)

# File paths and directories
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
USER_FILE = "users.json"
USER_LANG_FILE = "user_language_settings.json"

# Initialize Whisper model
whisper_model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# Gemini API configuration
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"
FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB

# Global statistics
stats = {
    'total_files': 0,
    'audio_files': 0,
    'voice_clips': 0,
    'videos': 0,
    'url_downloads': 0,
    'processing_time': 0
}

# User data management
user_data = {}
user_lang = {}
bot.icon_messages = {}  # For download status icons

# Load existing data
for file, data in [(USER_FILE, user_data), (USER_LANG_FILE, user_lang)]:
    if os.path.exists(file):
        try:
            with open(file, 'r') as f:
                data.update(json.load(f))
        except (json.JSONDecodeError, FileNotFoundError):
            pass

def save_data(file_name, data):
    with open(file_name, 'w') as f:
        json.dump(data, f, indent=4)

# --------------------------
# Common Utility Functions
# --------------------------
def update_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_data(USER_FILE, user_data)

def is_supported_url(url):
    platforms = ['tiktok.com', 'youtube.com', 'pinterest.com', 'pin.it',
                 'youtu.be', 'instagram.com', 'facebook.com']
    return any(p in url.lower() for p in platforms)

# --------------------------
# Core Bot Functionality
# --------------------------
def set_bot_commands():
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("help", "Usage instructions"),
        telebot.types.BotCommand("status", "Bot statistics"),
        telebot.types.BotCommand("language", "Set preferred language"),
        telebot.types.BotCommand("privacy", "Privacy policy")
    ]
    bot.set_my_commands(commands)
    
    bot.set_my_description(
        "🔹 Transcribe media files & download videos from social media\n"
        "• Transcribe audio/video files\n"
        "• Download from YouTube/TikTok/Instagram\n"
        "• Multi-language support"
    )
    
    bot.set_my_short_description("Media transcription & social media download bot")

# --------------------------
# Message Handlers
# --------------------------
@bot.message_handler(commands=['start'])
def start_handler(message):
    update_activity(message.from_user.id)
    name = message.from_user.first_name
    text = (
        f"👋 Welcome {name}!\n\n"
        "I can:\n"
        "• Transcribe audio/video files\n"
        "• Download videos from social media\n\n"
        "Send me a media file or social media link to start!"
    )
    bot.send_message(message.chat.id, text)
    if message.from_user.id == ADMIN_ID:
        show_admin_panel(message.chat.id)

@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        "📖 How to use this bot:\n\n"
        "For transcription:\n"
        "• Send voice/audio/video files (max 20MB)\n"
        "• Get text transcription\n"
        "• Translate/summarize options\n\n"
        "For video downloads:\n"
        "• Send supported links (YouTube/TikTok/Instagram)\n"
        "• Choose quality (YouTube)\n"
        "• Get video with description\n\n"
        "Commands:\n"
        "/start - Restart bot\n"
        "/status - Usage statistics\n"
        "/language - Set preferred language\n"
        "/privacy - View privacy policy"
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(content_types=['voice', 'audio', 'video'])
def handle_media(message):
    update_activity(message.from_user.id)
    file_obj = message.voice or message.audio or message.video
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "File too large (max 20MB)")
    
    # Transcription handling
    process_media_file(message)

@bot.message_handler(func=lambda msg: is_supported_url(msg.text))
def handle_url(message):
    update_activity(message.from_user.id)
    # Show processing icon
    icon_msg = bot.send_message(message.chat.id, "👀")
    bot.icon_messages[message.chat.id] = icon_msg.message_id
    
    if 'youtube.com' in message.text or 'youtu.be' in message.text:
        handle_youtube(message)
    else:
        handle_social_media(message)

# --------------------------
# Transcription Functionality
# --------------------------
def process_media_file(message):
    try:
        file_info = bot.get_file(message.voice.file_id if message.voice else
                               message.audio.file_id if message.audio else
                               message.video.file_id)
        file_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
        
        # Download and process file
        downloaded_file = bot.download_file(file_info.file_path)
        with open(file_path, 'wb') as f:
            f.write(downloaded_file)
        
        # Transcribe with Whisper
        segments, _ = whisper_model.transcribe(file_path, beam_size=1)
        transcription = " ".join(segment.text for segment in segments)
        
        # Handle transcription results
        handle_transcription_result(message, transcription)
        
        # Update statistics
        stats['total_files'] += 1
        if message.voice: stats['voice_clips'] += 1
        elif message.audio: stats['audio_files'] += 1
        else: stats['videos'] += 1

    except Exception as e:
        bot.send_message(message.chat.id, f"Error processing file: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

def handle_transcription_result(message, transcription):
    # Create response with options
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Translate", callback_data=f"trans_{message.message_id}"),
        InlineKeyboardButton("Summarize", callback_data=f"summ_{message.message_id}")
    )
    
    if len(transcription) > 4000:
        with open("transcription.txt", "w") as f:
            f.write(transcription)
        with open("transcription.txt", "rb") as f:
            bot.send_document(message.chat.id, f, caption="Your transcription", reply_markup=keyboard)
        os.remove("transcription.txt")
    else:
        bot.send_message(message.chat.id, transcription, reply_markup=keyboard)

# --------------------------
# Download Functionality
# --------------------------
def handle_youtube(message):
    try:
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(message.text, download=False)
        
        # Create quality selection
        keyboard = InlineKeyboardMarkup()
        for fmt in info['formats'][:3]:  # First 3 formats
            keyboard.add(InlineKeyboardButton(
                fmt['format'], 
                callback_data=f"yt_{fmt['format_id']}"
            ))
        
        bot.send_message(message.chat.id, "Choose quality:", reply_markup=keyboard)
    
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {str(e)}")

def handle_social_media(message):
    try:
        file_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")
        ydl_opts = {
            'outtmpl': file_path,
            'quiet': True,
            'format': 'best'
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([message.text])
        
        with open(file_path, 'rb') as f:
            bot.send_video(message.chat.id, f, caption="Downloaded Video")
        
        stats['url_downloads'] += 1
    except Exception as e:
        bot.send_message(message.chat.id, f"Download error: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        if message.chat.id in bot.icon_messages:
            bot.delete_message(message.chat.id, bot.icon_messages[message.chat.id])

# --------------------------
# Admin Functionality
# --------------------------
def show_admin_panel(chat_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Statistics", "📢 Broadcast")
    bot.send_message(chat_id, "Admin Panel", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📊 Statistics" and m.from_user.id == ADMIN_ID)
def show_stats(message):
    active_users = len([u for u in user_data.values() if 
        (datetime.now() - datetime.fromisoformat(u)).days < 1])
    
    text = (
        f"👥 Users: {len(user_data)}\n"
        f"🔄 Active Today: {active_users}\n"
        f"📁 Files Processed: {stats['total_files']}\n"
        f"🎧 Audio Files: {stats['audio_files']}\n"
        f"🎤 Voice Messages: {stats['voice_clips']}\n"
        f"📹 Videos: {stats['videos']}\n"
        f"🔗 URL Downloads: {stats['url_downloads']}\n"
        f"⏱ Total Processing Time: {stats['processing_time']}s"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and m.from_user.id == ADMIN_ID)
def handle_broadcast(message):
    bot.send_message(message.chat.id, "Send your broadcast message:")
    bot.register_next_step_handler(message, process_broadcast)

def process_broadcast(message):
    success = 0
    for user_id in user_data:
        try:
            bot.copy_message(user_id, message.chat.id, message.message_id)
            success += 1
        except:
            continue
    bot.send_message(message.chat.id, f"Broadcast sent to {success} users")

# --------------------------
# Webhook Configuration
# --------------------------
@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    abort(403)

if __name__ == '__main__':
    set_bot_commands()
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    # For production with webhook
    bot.remove_webhook()
    bot.set_webhook(url="https://media-transcriber-bot.onrender.com/")  # Set your actual webhook URL
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

    # For local testing without webhook
    # bot.polling()
