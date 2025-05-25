import os
import re
import uuid
import json
import shutil
import logging
import requests
import telebot
import yt_dlp
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)

# === CONFIGURATION ===
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981
REQUIRED_CHANNEL = "@transcriberbo"
DOWNLOAD_DIR = "downloads"
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com"
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA"

# === INITIALIZATION ===
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# === MODELS ===
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# === DATA STORES ===
users_file = "users.json"
user_data = {}
user_language_settings = {}
user_transcriptions = {}

if os.path.exists(users_file):
    with open(users_file) as f:
        user_data = json.load(f)

# === HELPER FUNCTIONS ===
def save_user_data():
    with open(users_file, "w") as f:
        json.dump(user_data, f, indent=4)

def is_supported_url(url):
    platforms = [
        'tiktok.com', 'youtube.com', 'youtu.be', 'instagram.com',
        'facebook.com', 'x.com', 'twitter.com', 'pinterest.com', 'pin.it'
    ]
    return any(p in url.lower() for p in platforms)

def react_to_message(message):
    if message.content_type in ['voice', 'audio', 'video', 'video_note']:
        bot.set_message_reaction(message.chat.id, message.id, [telebot.types.ReactionTypeEmoji("👀")])
    elif message.text and is_supported_url(message.text):
        bot.set_message_reaction(message.chat.id, message.id, [telebot.types.ReactionTypeEmoji("👀")])

# === USER MANAGEMENT ===
def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

def check_subscription(user_id):
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

# === MESSAGE HANDLERS ===
@bot.message_handler(commands=['start'])
def start_handler(message):
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return

    if message.from_user.id == ADMIN_ID:
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📊 Total Users", "📢 Broadcast")
        bot.send_message(message.chat.id, "Admin Panel", reply_markup=markup)
    else:
        bot.send_message(
            message.chat.id,
            "👋 Send me a link or media file to download/transcribe!\n"
            "Supported platforms: TikTok, YouTube, Instagram, Facebook, Twitter"
        )

@bot.message_handler(func=lambda m: m.text and is_supported_url(m.text))
def handle_url(message):
    react_to_message(message)
    url = message.text
    
    if 'tiktok.com' in url:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Download", callback_data=f"dl_tiktok|{url}"),
            InlineKeyboardButton("Transcribe", callback_data=f"transcribe_tiktok|{url}")
        )
        bot.send_message(message.chat.id, "Select action:", reply_markup=markup)
    elif 'youtube.com' in url or 'youtu.be' in url:
        handle_youtube_url(message)
    else:
        handle_generic_url(message)

def handle_youtube_url(message):
    try:
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(message.text, download=False)
            formats = [f for f in info['formats'] if f.get('vcodec') != 'none']
        
        markup = InlineKeyboardMarkup()
        for fmt in sorted(formats, key=lambda x: x['height'], reverse=True)[:3]:
            markup.add(InlineKeyboardButton(
                f"{fmt['height']}p", 
                callback_data=f"dl_yt|{fmt['format_id']}|{message.text}"
            ))
        bot.send_message(message.chat.id, "Select quality:", reply_markup=markup)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {str(e)}")

def handle_generic_url(message):
    try:
        path, desc = download_media(message.text)
        with open(path, 'rb') as f:
            if path.endswith('.mp4'):
                bot.send_video(message.chat.id, f, caption=desc)
            else:
                bot.send_document(message.chat.id, f, caption=desc)
        os.remove(path)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Download failed: {str(e)}")

# === DOWNLOAD FUNCTIONS ===
def download_media(url):
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
        'writedescription': True,
        'writeinfojson': True,
        'quiet': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url)
        path = ydl.prepare_filename(info)
        desc = info.get('description', '')
        hashtags = ' '.join([tag for tag in info.get('tags', []) if tag.startswith('#')])
    
    return path, f"{desc}\n\n{hashtags}"

# === TRANSCRIPTION HANDLERS ===
@bot.callback_query_handler(func=lambda c: c.data.startswith("transcribe_tiktok|"))
def transcribe_tiktok(call):
    url = call.data.split("|")[1]
    try:
        path, _ = download_media(url)
        segments, _ = model.transcribe(path)
        text = " ".join(segment.text for segment in segments)
        
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Translate", callback_data=f"translate|{text}"),
            InlineKeyboardButton("Summarize", callback_data=f"summarize|{text}")
        )
        bot.send_message(call.message.chat.id, text, reply_markup=markup)
        os.remove(path)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Transcription failed: {str(e)}")

# === ADMIN FEATURES ===
@bot.message_handler(func=lambda m: m.text == "📊 Total Users" and m.from_user.id == ADMIN_ID)
def show_stats(message):
    bot.send_message(message.chat.id, f"Total users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and m.from_user.id == ADMIN_ID)
def broadcast(message):
    msg = bot.send_message(message.chat.id, "Send broadcast message:")
    bot.register_next_step_handler(msg, process_broadcast)

def process_broadcast(message):
    success = 0
    for user_id in user_data:
        try:
            bot.copy_message(user_id, message.chat.id, message.message_id)
            success += 1
        except Exception:
            continue
    bot.send_message(message.chat.id, f"Broadcast sent to {success} users.")

# === WEBHOOK SETUP ===
@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    abort(403)

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
