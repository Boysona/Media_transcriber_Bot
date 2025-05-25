import os
import uuid
import shutil
import imageio_ffmpeg
import telebot
import yt_dlp
import re
import logging
import requests
import json
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === CONFIGURATION ===
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981
DOWNLOAD_DIR = "downloads"
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com" # Ensure this is the correct URL for your deployed bot
REQUIRED_CHANNEL = "@transcriberbo" # From the second bot

# === SETUP FFmpeg from imageio_ffmpeg ===
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")

# === CLEAN DOWNLOAD DIRECTORY ON START ===
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# === INIT BOT & FLASK ===
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True) # Ensure threaded is True for webhook

# Whisper model for transcription
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# User tracking file (using JSON for better structure)
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

GEMINI_API_KEY = "YOUR_GEMINI_API_KEY" # Replace with your actual Gemini API Key

def ask_gemini(user_id, user_message):
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    history = user_memory[user_id][-10:]
    parts = [{"text": msg["text"]} for msg in history]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        resp = requests.post(url, headers={'Content-Type': 'application/json'}, json={"contents": [{"parts": parts}]})
        resp.raise_for_status() # Raise an exception for bad status codes
        result = resp.json()
        if "candidates" in result:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            user_memory[user_id].append({"role": "model", "text": reply})
            return reply
        return "Error: " + json.dumps(result)
    except requests.exceptions.RequestException as e:
        logging.error(f"Gemini API request failed: {e}")
        return f"Error: Failed to connect to Gemini API. {e}"

FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB
admin_state = {}

def set_bot_info():
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "View instructions"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice"),
    ]
    bot.set_my_commands(commands)

    bot.set_my_short_description(
        "Got media files? Let this free bot transcribe, summarize, and translate them in seconds!"
    )

    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, TikTok videos, audio files, and videos—free and in multiple languages.

     🔥Enjoy free usage and start now!👌🏻"""
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
        telebot.types.InlineKeyboardButton("Click here to join the channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")
    )
    bot.send_message(
        chat_id,
        "This bot only works when you join the channel 👉🏻 @transcriberbo. Please join the channel first, then come back to use the bot.🥰",
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
• Send me:
• Voice message
• Video message
• Audio file
• TikTok video link
• or other social media links to download/transcribe for free"""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    help_text = (
        """ℹ️ How to use this bot:

This bot transcribes voice messages, audio files, videos, and social media links.

1.  **Join the Channel:** Make sure you've joined our channel: https://t.me/transcriberbo.
2.  **Send a File/Link:** Send a voice message, audio, video, or a supported social media URL.
3.  **Receive Transcription/Download:**
    -   For TikTok and other supported links, you'll see buttons to **Download** (with description) or **Transcribe**.
    -   For audio/video files, the bot will directly transcribe.
4.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.
5.  **Commands:**
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
    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and social media links you send are temporarily processed for transcription/download. These files are **deleted immediately** after processing. We do not store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: transcription, translation, and summarization, and media download.
    * To improve bot performance and understand usage patterns through anonymous, aggregated statistics (e.g., total files processed).
    * To set your preferred language for future interactions.

3.  **Data Sharing:**
    * We **do not share** your personal data, media files, or transcriptions with any third parties.
    * Transcription, translation, and summarization are performed using integrated AI models (Whisper and Gemini API). Your text input to these models is handled according to their respective privacy policies, but we do not store this data after processing.

4.  **Data Retention:**
    * Media files are deleted immediately after transcription/download.
    * Transcriptions are held temporarily in memory.
    * User IDs and language preferences are retained to maintain your settings and usage statistics. You can always delete your data by stopping using the bot or contacting the bot administrator.

By using this bot, you agree to the terms outlined in this Privacy Notice.

If you have any questions, please contact the bot administrator."""
    )
    bot.send_message(message.chat.id, privacy_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_handler(message):
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
    for uid_str in user_data: # Iterate through string keys
        try:
            uid = int(uid_str) # Convert to int for bot.copy_message
            bot.copy_message(uid, message.chat.id, message.message_id)
            success += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Error sending broadcast to {uid_str}: {e}")
            fail += 1
    bot.send_message(
        message.chat.id,
        f"✅ Broadcast finished.\nSuccessful: {success}\nFailed: {fail}"
    )

# === URL HANDLING ===
TIKTOK_REGEX = re.compile(r'(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+')
YOUTUBE_REGEX = re.compile(r'(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)[^\s]+')
INSTAGRAM_REGEX = re.compile(r'(https?://)?(www\.)?instagram\.com/(p|reel|tv)/[^\s]+')
FACEBOOK_REGEX = re.compile(r'(https?://)?(www\.)?(facebook\.com|fb\.watch)/[^\s]+')
TWITTER_X_REGEX = re.compile(r'(https?://)?(www\.)?(twitter\.com|x\.com)/[^\s]+')
PINTEREST_REGEX = re.compile(r'(https?://)?(www\.)?(pinterest\.com|pin\.it)/[^\s]+')
SNAPCHAT_REGEX = re.compile(r'(https?://)?(www\.)?snapchat\.com/(t|add)/[^\s]+') # General snapchat public link

def get_platform_buttons(url, message_id):
    markup = InlineKeyboardMarkup(row_width=2)
    if TIKTOK_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download TikTok", callback_data=f"download_tiktok|{url}"),
            InlineKeyboardButton("Transcribe TikTok", callback_data=f"transcribe_tiktok|{url}|{message_id}")
        )
    elif YOUTUBE_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download YouTube", callback_data=f"download_youtube|{url}"),
            InlineKeyboardButton("Transcribe YouTube", callback_data=f"transcribe_youtube|{url}|{message_id}")
        )
    elif INSTAGRAM_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download Instagram", callback_data=f"download_generic|{url}"),
            InlineKeyboardButton("Transcribe Instagram", callback_data=f"transcribe_generic|{url}|{message_id}")
        )
    elif FACEBOOK_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download Facebook", callback_data=f"download_generic|{url}"),
            InlineKeyboardButton("Transcribe Facebook", callback_data=f"transcribe_generic|{url}|{message_id}")
        )
    elif TWITTER_X_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download X/Twitter", callback_data=f"download_generic|{url}"),
            InlineKeyboardButton("Transcribe X/Twitter", callback_data=f"transcribe_generic|{url}|{message_id}")
        )
    elif PINTEREST_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download Pinterest", callback_data=f"download_generic|{url}"),
            InlineKeyboardButton("Transcribe Pinterest", callback_data=f"transcribe_generic|{url}|{message_id}")
        )
    elif SNAPCHAT_REGEX.search(url):
        markup.add(
            InlineKeyboardButton("Download Snapchat", callback_data=f"download_generic|{url}"),
            InlineKeyboardButton("Transcribe Snapchat", callback_data=f"transcribe_generic|{url}|{message_id}")
        )
    return markup

@bot.message_handler(regexp=r'https?://[^\s]+')
def handle_urls(message):
    url = re.search(r'https?://[^\s]+', message.text).group(0)
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    markup = get_platform_buttons(url, message.message_id)

    if markup.keyboard:
        bot.send_message(message.chat.id, "Link detected. Choose an action:", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "Unsupported link or no specific action for this link.")

# === Callback Handlers for Downloads and Transcriptions ===

@bot.callback_query_handler(func=lambda c: c.data.startswith("download_tiktok|"))
def callback_download_tiktok(call):
    global total_tiktok_downloads
    _, url = call.data.split("|", 1)
    bot.edit_message_text("👀 Processing your TikTok download...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4',
            'writedescription': True, # To fetch description
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_path = ydl.prepare_filename(info)
            caption = info.get('description', 'No description found.')
            title = info.get('title', 'Video')
            hashtags = " ".join([f"#{tag}" for tag in info.get('tags', [])]) if info.get('tags') else ""
            full_caption = f"*{title}*\n\n{caption}\n\n{hashtags}".strip()

        if os.path.exists(video_path):
            with open(video_path, 'rb') as video_file:
                bot.send_chat_action(call.message.chat.id, 'upload_video')
                bot.send_video(call.message.chat.id, video_file, caption=full_caption, parse_mode='Markdown')
            total_tiktok_downloads += 1
        else:
            bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok video.")
    except Exception as e:
        logging.error(f"TikTok download error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok video.")
    finally:
        if 'video_path' in locals() and os.path.exists(video_path):
            os.remove(video_path)
        bot.delete_message(call.message.chat.id, call.message.message_id) # Delete the "Processing" message

@bot.callback_query_handler(func=lambda c: c.data.startswith("transcribe_tiktok|"))
def callback_transcribe_tiktok(call):
    _, url, message_id_str = call.data.split("|", 2)
    message_id = int(message_id_str)
    bot.edit_message_text("👀 Transcribing your TikTok video...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestaudio/best', # Download only audio for transcription
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            audio_path_base = ydl.prepare_filename(info)
            audio_path = f"{os.path.splitext(audio_path_base)[0]}.mp3" # Ensure .mp3 extension

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(audio_path) or ""
        uid = str(call.from_user.id)

        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        global total_processing_time
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup()
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
        if 'audio_path' in locals() and os.path.exists(audio_path):
            os.remove(audio_path)
        bot.delete_message(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("download_youtube|"))
def callback_download_youtube(call):
    _, url = call.data.split("|", 1)
    bot.edit_message_text("👀 Preparing YouTube download options...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            formats = info.get('formats', [])

        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') <= 1080}

        if not resolutions:
            bot.send_message(call.message.chat.id, "No suitable resolutions found for download.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1])):
            vid_id = str(uuid.uuid4())[:8]
            bot.video_info = getattr(bot, 'video_info', {})
            bot.video_info[vid_id] = {'url': url, 'format_id': fid}
            markup.add(InlineKeyboardButton(res, callback_data=f'dl:{vid_id}'))

        bot.send_message(call.message.chat.id, f"Choose quality for: {title}", reply_markup=markup)

    except Exception as e:
        logging.error(f"YouTube download option error: {e}")
        bot.send_message(call.message.chat.id, f"Error: {e}")
    finally:
        bot.delete_message(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video_selected_quality(call):
    vid = call.data.split(":")[1]
    if not hasattr(bot, 'video_info') or vid not in bot.video_info:
        bot.answer_callback_query(call.id, "Download expired. Try again.")
        return

    data = bot.video_info[vid]
    url, fmt = data['url'], data['format_id']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")

    bot.edit_message_text("👀 Downloading YouTube video...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'writedescription': True, # To fetch description
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            caption = info.get('description', 'No description found.')
            title = info.get('title', 'Video')
            hashtags = " ".join([f"#{tag}" for tag in info.get('tags', [])]) if info.get('tags') else ""
            full_caption = f"*{title}*\n\n{caption}\n\n{hashtags}".strip()

        if os.path.exists(video_path):
            with open(video_path, 'rb') as f:
                bot.send_video(call.message.chat.id, f, reply_to_message_id=call.message.message_id, caption=full_caption, parse_mode='Markdown')

            subtitle_path = video_path.replace('.mp4', '.en.srt')
            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub_file:
                    bot.send_document(call.message.chat.id, sub_file, caption="Subtitles")
        else:
            bot.send_message(call.message.chat.id, "Download failed.")

    except Exception as e:
        logging.error(f"Error downloading YouTube video: {e}")
        bot.send_message(call.message.chat.id, f"Error downloading: {e}")
    finally:
        for file in os.listdir(DOWNLOAD_DIR):
            os.remove(os.path.join(DOWNLOAD_DIR, file))
        bot.delete_message(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("transcribe_youtube|"))
def callback_transcribe_youtube(call):
    _, url, message_id_str = call.data.split("|", 2)
    message_id = int(message_id_str)
    bot.edit_message_text("👀 Transcribing your YouTube video...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestaudio/best', # Download only audio for transcription
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            audio_path_base = ydl.prepare_filename(info)
            audio_path = f"{os.path.splitext(audio_path_base)[0]}.mp3"

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(audio_path) or ""
        uid = str(call.from_user.id)
        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        global total_processing_time
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message_id}")
        )

        if len(transcription) > 4000:
            fn = 'youtube_transcription.txt'
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
        logging.error(f"YouTube transcribe error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to transcribe YouTube video.")
    finally:
        if 'audio_path' in locals() and os.path.exists(audio_path):
            os.remove(audio_path)
        bot.delete_message(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("download_generic|"))
def callback_download_generic(call):
    _, url = call.data.split("|", 1)
    bot.edit_message_text("👀 Processing your download...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4',
            'writedescription': True,
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_path = ydl.prepare_filename(info)
            caption = info.get('description', 'No description found.')
            title = info.get('title', 'Video')
            hashtags = " ".join([f"#{tag}" for tag in info.get('tags', [])]) if info.get('tags') else ""
            full_caption = f"*{title}*\n\n{caption}\n\n{hashtags}".strip()

        if os.path.exists(video_path):
            with open(video_path, 'rb') as video_file:
                bot.send_chat_action(call.message.chat.id, 'upload_video')
                bot.send_video(call.message.chat.id, video_file, caption=full_caption, parse_mode='Markdown')
        else:
            bot.send_message(call.message.chat.id, "⚠️ Failed to download video.")
    except Exception as e:
        logging.error(f"Generic download error for {url}: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to download video from this platform.")
    finally:
        if 'video_path' in locals() and os.path.exists(video_path):
            os.remove(video_path)
        bot.delete_message(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("transcribe_generic|"))
def callback_transcribe_generic(call):
    _, url, message_id_str = call.data.split("|", 2)
    message_id = int(message_id_str)
    bot.edit_message_text("👀 Transcribing this content...", chat_id=call.message.chat.id, message_id=call.message.message_id)

    try:
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'format': 'bestaudio/best',
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            audio_path_base = ydl.prepare_filename(info)
            audio_path = f"{os.path.splitext(audio_path_base)[0]}.mp3"

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe(audio_path) or ""
        uid = str(call.from_user.id)
        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        processing_time = (datetime.now() - processing_start_time).total_seconds()
        global total_processing_time
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message_id}")
        )

        if len(transcription) > 4000:
            fn = 'generic_transcription.txt'
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
        logging.error(f"Generic transcribe error for {url}: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to transcribe this content.")
    finally:
        if 'audio_path' in locals() and os.path.exists(audio_path):
            os.remove(audio_path)
        bot.delete_message(call.message.chat.id, call.message.message_id)


@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    bot.send_reaction(message.chat.id, message.message_id, "👀") # React with eye icon

    info = bot.get_file(file_obj.file_id)
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg") # Use ogg for voice/video_note, mp3 for audio
    if message.audio:
        local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp3")

    bot.send_chat_action(message.chat.id, 'typing')

    try:
        data = bot.download_file(info.file_path)
        with open(local_path, 'wb') as f:
            f.write(data)

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

LANGUAGES = [
    {"name": "English", "flag": "🇬🇧"}, {"name": "Chinese", "flag": "🇨🇳"}, {"name": "Spanish", "flag": "🇪🇸"},
    {"name": "Hindi", "flag": "🇮🇳"}, {"name": "Arabic", "flag": "🇸🇦"}, {"name": "French", "flag": "🇫🇷"},
    {"name": "Bengali", "flag": "🇧🇩"}, {"name": "Russian", "flag": "🇷🇺"}, {"name": "Portuguese", "flag": "🇵🇹"},
    {"name": "Urdu", "flag": "🇵🇰"}, {"name": "German", "flag": "🇩🇪"}, {"name": "Japanese", "flag": "🇯🇵"},
    {"name": "Korean", "flag": "🇰🇷"}, {"name": "Vietnamese", "flag": "🇻🇳"}, {"name": "Turkish", "flag": "🇹🇷"},
    {"name": "Italian", "flag": "🇮🇹"}, {"name": "Thai", "flag": "🇹🇭"}, {"name": "Swahili", "flag": "🇰🇪"},
    {"name": "Dutch", "flag": "🇳🇱"}, {"name": "Polish", "flag": "🇵🇱"}, {"name": "Ukrainian", "flag": "🇺🇦"},
    {"name": "Indonesian", "flag": "🇮🇩"}, {"name": "Malay", "flag": "🇲🇾"}, {"name": "Filipino", "flag": "🇵🇭"},
    {"name": "Persian", "flag": "🇮🇷"}, {"name": "Amharic", "flag": "🇪🇹"}, {"name": "Somali", "flag": "🇸🇴"},
    {"name": "Swedish", "flag": "🇸🇪"}, {"name": "Norwegian", "flag": "🇳🇴"}, {"name": "Danish", "flag": "🇩🇰"},
    {"name": "Finnish", "flag": "🇫🇮"}, {"name": "Greek", "flag": "🇬🇷"}, {"name": "Hebrew", "flag": "🇮🇱"},
    {"name": "Czech", "flag": "🇨🇿"}, {"name": "Hungarian", "flag": "🇭🇺"}, {"name": "Romanian", "flag": "🇷🇴"},
    {"name": "Nepali", "flag": "🇳🇵"}, {"name": "Sinhala", "flag": "🇱🇰"}, {"name": "Tamil", "flag": "🇮🇳"},
    {"name": "Telugu", "flag": "🇮🇳"}, {"name": "Kannada", "flag": "🇮🇳"}, {"name": "Malayalam", "flag": "🇮🇳"},
    {"name": "Gujarati", "flag": "🇮🇳"}, {"name": "Punjabi", "flag": "🇮🇳"}, {"name": "Marathi", "flag": "🇮🇳"},
    {"name": "Oriya", "flag": "🇮🇳"}, {"name": "Assamese", "flag": "🇮🇳"}, {"name": "Khmer", "flag": "🇰🇭"},
    {"name": "Lao", "flag": "🇱🇦"}, {"name": "Burmese", "flag": "🇲🇲"}, {"name": "Georgian", "flag": "🇬🇪"},
    {"name": "Armenian", "flag": "🇦🇲"}, {"name": "Azerbaijani", "flag": "🇦🇿"}, {"name": "Kazakh", "flag": "🇰🇿"},
    {"name": "Uzbek", "flag": "🇺🇿"}, {"name": "Kyrgyz", "flag": "🇰🇬"}, {"name": "Tajik", "flag": "🇹🇯"},
    {"name": "Turkmen", "flag": "🇹🇲"}, {"name": "Mongolian", "flag": "🇲🇳"}, {"name": "Estonian", "flag": "🇪🇪"},
    {"name": "Latvian", "flag": "🇱🇻"}, {"name": "Lithuanian", "flag": "🇱🇹"},
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
        bot.send_message(message.chat.id, f"Error during translation: {translated}", reply_to_message_id=message_id)
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
        bot.send_message(message.chat.id, f"Error during summarization: {summary}", reply_to_message_id=message_id)
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
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

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
    # React with 👀 for any file or link not explicitly handled yet, as a generic reaction
    # This also covers cases where a URL is sent but not directly for a recognized platform
    if message.text and re.match(r'https?://[^\s]+', message.text):
        bot.send_reaction(message.chat.id, message.message_id, "👀")
        bot.send_message(message.chat.id, "I received a link, but it seems to be unsupported for direct download/transcription or I've already provided options for it. Please send only voice, audio, video, or a supported social media link if you're looking for specific actions.")
    else:
        bot.send_message(message.chat.id, "Please send only voice, audio, video, or a supported social media link.")


# === WEBHOOK ENDPOINTS ===
@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

@app.route('/set_webhook', methods=['GET', 'POST'])
def set_webhook_route(): # Renamed to avoid conflict with bot.set_webhook
    bot.set_webhook(url=WEBHOOK_URL)
    return f"Webhook set to {WEBHOOK_URL}", 200

@app.route('/delete_webhook', methods=['GET', 'POST'])
def delete_webhook_route(): # Renamed to avoid conflict with bot.delete_webhook
    bot.delete_webhook()
    return "Webhook deleted", 200

# === APP RUN ===
if __name__ == "__main__":
    bot.delete_webhook()
    set_bot_info()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
