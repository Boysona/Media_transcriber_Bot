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
import imageio_ffmpeg # For FFmpeg setup

# --- CONFIGURATION ---
# Replace with your actual bot token
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q" 
# Replace with your Telegram Admin ID
ADMIN_ID = 5978150981 
REQUIRED_CHANNEL = "@transcriberbo" # Replace with your required channel username
# Replace with your Render.com or other deployment URL
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com"
# Replace with your Gemini API Key
GEMINI_API_KEY = "AIzaSyAto78yGVZobxOwPXnl8wCE9ZW8Do2R8HA" 

# --- SETUP FFmpeg from imageio_ffmpeg ---
# This ensures yt_dlp can find ffmpeg for video processing
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")

# --- DIRECTORY & FILE MANAGEMENT ---
DOWNLOAD_DIR = "downloads"
USERS_FILE = "users.json"
USER_LANGUAGE_SETTINGS_FILE = 'user_language_settings.json'

# Clean download directory on start
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- BOT & FLASK INIT ---
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# --- GLOBAL BOT DATA ---
# User tracking data
user_data = {}
if os.path.exists(USERS_FILE):
    with open(USERS_FILE, 'r') as f:
        try:
            user_data = json.load(f)
        except json.JSONDecodeError:
            user_data = {}

# User-specific language settings
user_language_settings = {}
if os.path.exists(USER_LANGUAGE_SETTINGS_FILE):
    with open(USER_LANGUAGE_SETTINGS_FILE, 'r') as f:
        try:
            user_language_settings = json.load(f)
        except json.JSONDecodeError:
            user_language_settings = {}

# In-memory chat history for Gemini API
user_memory = {}
# Store transcriptions per user and message ID for post-processing actions
user_transcriptions = {} # Format: {user_id: {message_id: "transcription_text"}}
# Admin state for broadcast feature
admin_state = {}

# Statistics counters (global variables)
total_files_processed = 0
total_audio_files = 0
total_voice_clips = 0
total_videos = 0
total_tiktok_downloads = 0
total_processing_time = 0  # in seconds
processing_start_time = None

# --- WHISPER MODEL INIT ---
# You might consider using a larger model for better accuracy if resources allow, e.g., "base", "small"
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8") 

# --- CONSTANTS ---
FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20MB

# TikTok link detection regex
TIKTOK_REGEX = re.compile(r'(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+')

# --- HELPER FUNCTIONS ---
def save_user_data():
    """Saves user activity data to a JSON file."""
    with open(USERS_FILE, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    """Saves user language preferences to a JSON file."""
    with open(USER_LANGUAGE_SETTINGS_FILE, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

def update_user_activity(user_id):
    """Updates the last activity timestamp for a user."""
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()

def check_subscription(user_id):
    """Checks if a user is subscribed to the required channel."""
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error checking subscription for user {user_id}: {e}")
        return False

def send_subscription_message(chat_id):
    """Sends a message prompting the user to join the required channel."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("Click here to join the channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")
    )
    bot.send_message(
        chat_id,
        "This bot only works when you join the channel 👉🏻 @transcriberbo. Please join the channel first, then come back to use the bot.🥰",
        reply_markup=markup
    )

def is_youtube_url(url):
    """Checks if a given URL is a YouTube URL."""
    return 'youtube.com/' in url.lower() or 'youtu.be/' in url.lower()

def is_supported_url(url):
    """Checks if a URL is from a generally supported platform for direct download."""
    platforms = [
        'tiktok.com', 'youtube.com', 'youtu.be', 'pinterest.com', 'pin.it',
        'instagram.com', 'snapchat.com', 'facebook.com', 'x.com', 'twitter.com'
    ]
    return any(p in url.lower() for p in platforms)

def transcribe_audio_file(path: str) -> str | None:
    """Transcribes an audio file using Whisper."""
    try:
        segments, _ = model.transcribe(path, beam_size=1)
        return " ".join(segment.text for segment in segments)
    except Exception as e:
        logging.error(f"Transcription error: {e}")
        return None

def ask_gemini(user_id, user_message):
    """Sends a message to the Gemini API and returns the response."""
    user_memory.setdefault(user_id, []).append({"role": "user", "text": user_message})
    # Keep history limited to recent messages
    history = user_memory[user_id][-10:] 
    parts = [{"text": msg["text"]} for msg in history]
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}" # Using gemini-pro for better performance
    headers = {'Content-Type': 'application/json'}
    payload = {"contents": [{"parts": parts}]}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30) # Add timeout
        resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        result = resp.json()

        if "candidates" in result:
            reply = result['candidates'][0]['content']['parts'][0]['text']
            user_memory[user_id].append({"role": "model", "text": reply})
            return reply
        else:
            logging.error(f"Gemini API error: No candidates in response. {json.dumps(result)}")
            return "Error: Could not get a valid response from AI. Please try again."
    except requests.exceptions.RequestException as e:
        logging.error(f"Gemini API request failed: {e}")
        return f"Error: Failed to connect to AI service. {e}"
    except json.JSONDecodeError as e:
        logging.error(f"Gemini API response JSON decode error: {e}, Response: {resp.text}")
        return "Error: Malformed response from AI service."

# --- LANGUAGE SELECTION & HANDLING ---

# List of common languages with emojis (ordered by approximate global prevalence/popularity)
LANGUAGES = [
    {"name": "English", "flag": "🇬🇧"}, {"name": "Chinese", "flag": "🇨🇳"},
    {"name": "Spanish", "flag": "🇪🇸"}, {"name": "Hindi", "flag": "🇮🇳"},
    {"name": "Arabic", "flag": "🇸🇦"}, {"name": "French", "flag": "🇫🇷"},
    {"name": "Bengali", "flag": "🇧🇩"}, {"name": "Russian", "flag": "🇷🇺"},
    {"name": "Portuguese", "flag": "🇵🇹"}, {"name": "Urdu", "flag": "🇵🇰"},
    {"name": "German", "flag": "🇩🇪"}, {"name": "Japanese", "flag": "🇯🇵"},
    {"name": "Korean", "flag": "🇰🇷"}, {"name": "Vietnamese", "flag": "🇻🇳"},
    {"name": "Turkish", "flag": "🇹🇷"}, {"name": "Italian", "flag": "🇮🇹"},
    {"name": "Thai", "flag": "🇹🇭"}, {"name": "Swahili", "flag": "🇰🇪"},
    {"name": "Dutch", "flag": "🇳🇱"}, {"name": "Polish", "flag": "🇵🇱"},
    {"name": "Ukrainian", "flag": "🇺🇦"}, {"name": "Indonesian", "flag": "🇮🇩"},
    {"name": "Malay", "flag": "🇲🇾"}, {"name": "Filipino", "flag": "🇵🇭"},
    {"name": "Persian", "flag": "🇮🇷"}, {"name": "Amharic", "flag": "🇪🇹"},
    {"name": "Somali", "flag": "🇸🇴"}, {"name": "Swedish", "flag": "🇸🇪"},
    {"name": "Norwegian", "flag": "🇳🇴"}, {"name": "Danish", "flag": "🇩🇰"},
    {"name": "Finnish", "flag": "🇫🇮"}, {"name": "Greek", "flag": "🇬🇷"},
    {"name": "Hebrew", "flag": "🇮🇱"}, {"name": "Czech", "flag": "🇨🇿"},
    {"name": "Hungarian", "flag": "🇭🇺"}, {"name": "Romanian", "flag": "🇷🇴"},
    {"name": "Nepali", "flag": "🇳🇵"}, {"name": "Sinhala", "flag": "🇱🇰"},
    {"name": "Tamil", "flag": "🇮🇳"}, {"name": "Telugu", "flag": "🇮🇳"},
    {"name": "Kannada", "flag": "🇮🇳"}, {"name": "Malayalam", "flag": "🇮🇳"},
    {"name": "Gujarati", "flag": "🇮🇳"}, {"name": "Punjabi", "flag": "🇮🇳"},
    {"name": "Marathi", "flag": "🇮🇳"}, {"name": "Oriya", "flag": "🇮🇳"},
    {"name": "Assamese", "flag": "🇮🇳"}, {"name": "Khmer", "flag": "🇰🇭"},
    {"name": "Lao", "flag": "🇱🇦"}, {"name": "Burmese", "flag": "🇲🇲"},
    {"name": "Georgian", "flag": "🇬🇪"}, {"name": "Armenian", "flag": "🇦🇲"},
    {"name": "Azerbaijani", "flag": "🇦🇿"}, {"name": "Kazakh", "flag": "🇰🇿"},
    {"name": "Uzbek", "flag": "🇺🇿"}, {"name": "Kyrgyz", "flag": "🇰🇬"},
    {"name": "Tajik", "flag": "🇹🇯"}, {"name": "Turkmen", "flag": "🇹🇲"},
    {"name": "Mongolian", "flag": "🇲🇳"}, {"name": "Estonian", "flag": "🇪🇪"},
    {"name": "Latvian", "flag": "🇱🇻"}, {"name": "Lithuanian", "flag": "🇱🇹"},
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

def do_translate_or_summarize(message, uid, lang, message_id, action_type):
    """Handles the translation or summarization logic."""
    original_text = user_transcriptions.get(uid, {}).get(message_id, "")
    if not original_text:
        bot.send_message(message.chat.id, "❌ No transcription available for this specific message to process.")
        return

    if action_type == "translate":
        prompt = f"Translate the following text into {lang}. Provide only the translated text, with no additional notes, explanations, or introductory/concluding remarks:\n\n{original_text}"
        caption_prefix = f"Translation to {lang}"
        filename = "translation.txt"
    elif action_type == "summarize":
        prompt = f"Summarize the following text in {lang}. Provide only the summarized text, with no additional notes, explanations, or different versions:\n\n{original_text}"
        caption_prefix = f"Summary in {lang}"
        filename = "summary.txt"
    else:
        return

    bot.send_chat_action(message.chat.id, 'typing')
    processed_text = ask_gemini(uid, prompt)

    if processed_text.startswith("Error:"):
        bot.send_message(message.chat.id, f"Error during {action_type}: {processed_text}")
        return

    if len(processed_text) > 4000:
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(processed_text)
        bot.send_chat_action(message.chat.id, 'upload_document')
        with open(file_path, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption=caption_prefix, reply_to_message_id=message_id)
        os.remove(file_path)
    else:
        bot.send_message(message.chat.id, processed_text, reply_to_message_id=message_id)

# --- BOT COMMAND HANDLERS ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    if user_id not in user_data:
        user_data[user_id] = datetime.now().isoformat()
        save_user_data()

    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    # Admin panel for admin ID
    if message.from_user.id == ADMIN_ID:
        keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("📊 Total Users", "📢 Send Ads (broadcast)")
        bot.send_message(message.chat.id, "Welcome to the Admin Panel", reply_markup=keyboard)
    else:
        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        bot.send_message(
            message.chat.id,
            f"""👋 Hello {username}, send me a TikTok, YouTube, Instagram, Facebook or other link, or a voice message, video, or audio file, and I’ll process it for you!

• Send me:
• Voice message
• Video message
• Audio file
• TikTok video link
• YouTube, Instagram, Facebook or other video link
• to transcribe, download, translate or summarize for free!"""
        )

@bot.message_handler(commands=['help'])
def help_handler(message):
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    help_text = (
        """ℹ️ How to use this bot:

This bot downloads media from various platforms and transcribes voice messages, audio files, videos, and TikTok video links. It can also translate and summarize transcriptions.

1.  **Join the Channel:** Make sure you've joined our channel: https://t.me/transcriberbo.
2.  **Send a File/Link:** Send a voice message, audio, video, or a supported URL (TikTok, YouTube, Instagram, Facebook, etc.).
3.  **Receive Processing:** The bot will either download the media or process it for transcription.
4.  **TikTok/YouTube Options:** For TikTok/YouTube links, you can choose to **Download** the video or **Transcribe** it (for TikTok).
5.  **Post-Transcription Actions:** After transcription, you'll see options to **Translate** or **Summarize** the text.
6.  **Commands:**
    -   `/start`: Restart the bot.
    -   `/status`: View bot statistics.
    -   `/help`: Display these instructions.
    -   `/language`: Change your preferred language for translate/summarize.
    -   `/privacy`: View the bot's privacy notice.

Enjoy!"""
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_notice_handler(message):
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    privacy_text = (
        """**Privacy Notice**

Your privacy is important to us. Here's how this bot handles your data:

1.  **Data We Process:**
    * **Media Files:** Voice messages, audio files, video files, and video links you send are temporarily processed for transcription or download. These files are **deleted immediately** after processing. We do not store your media content.
    * **Transcriptions:** The text transcriptions generated from your media are stored temporarily in memory for subsequent actions (like translation or summarization) for a limited time (e.g., until a new transcription is made or the bot is restarted). We do not permanently store your transcription data on our servers.
    * **User IDs:** Your Telegram User ID is stored to manage your language preferences and track basic activity (like last seen) to improve bot service and provide aggregated usage statistics. This ID is not linked to any personal identifying information outside of Telegram.
    * **Language Preferences:** Your chosen language for translations and summaries is stored so you don't have to select it each time.

2.  **How We Use Your Data:**
    * To provide the core functionality of the bot: media download, transcription, translation, and summarization.
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
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

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
        f"▫️ Total Users Today: {active_today}\n"
        f"▫️ Total Registered Users: {len(user_data)}\n\n"
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

@bot.message_handler(commands=['language'])
def select_language_command(message):
    uid = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    markup = generate_language_keyboard("set_lang")
    bot.send_message(
        message.chat.id,
        "Please select your preferred language for future translations and summaries:",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == "📊 Total Users" and m.from_user.id == ADMIN_ID)
def admin_total_users(message):
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            try:
                users = json.load(f)
                count = len(users)
            except json.JSONDecodeError:
                count = 0
        bot.send_message(message.chat.id, f"👥 Total registered users: {count}")
    else:
        bot.send_message(message.chat.id, "No users yet.")

@bot.message_handler(func=lambda m: m.text == "📢 Send Ads (broadcast)" and m.from_user.id == ADMIN_ID)
def ask_broadcast(message):
    admin_state[message.from_user.id] = 'awaiting_broadcast'
    bot.send_message(message.chat.id, "Send your message (text, photo, video, etc.) to broadcast:")

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast',
    content_types=['text', 'photo', 'video', 'audio', 'document', 'voice']
)
def broadcast_message(message):
    admin_state[message.from_user.id] = None # Reset admin state
    if not os.path.exists(USERS_FILE):
        bot.send_message(message.chat.id, "No users to broadcast to.")
        return

    with open(USERS_FILE, "r") as f:
        try:
            user_ids = json.load(f).keys()
        except json.JSONDecodeError:
            user_ids = []

    sent = 0
    failed = 0
    for uid_str in user_ids:
        try:
            uid = int(uid_str)
            # Skip sending to self during broadcast test
            if uid == message.chat.id:
                continue

            if message.text:
                bot.send_message(uid, message.text)
            elif message.photo:
                bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption or "")
            elif message.video:
                bot.send_video(uid, message.video.file_id, caption=message.caption or "")
            elif message.audio:
                bot.send_audio(uid, message.audio.file_id, caption=message.caption or "")
            elif message.voice:
                bot.send_voice(uid, message.voice.file_id)
            elif message.document:
                bot.send_document(uid, message.document.file_id, caption=message.caption or "")
            sent += 1
        except telebot.apihelper.ApiTelegramException as e:
            logging.warning(f"Failed to send broadcast to user {uid_str}: {e}")
            failed += 1
        except Exception as e:
            logging.error(f"Unexpected error during broadcast to user {uid_str}: {e}")
            failed += 1
    bot.send_message(message.chat.id, f"✅ Broadcast finished. Sent to {sent} users. Failed: {failed}.")

# --- MEDIA HANDLERS ---

@bot.message_handler(func=lambda msg: is_youtube_url(msg.text))
def handle_youtube_url(msg):
    update_user_activity(msg.from_user.id)
    if not check_subscription(msg.from_user.id):
        return send_subscription_message(msg.chat.id)

    url = msg.text
    try:
        bot.send_chat_action(msg.chat.id, 'typing')
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'nocheckcertificate': True, # Bypass SSL certificate issues
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            formats = info.get('formats', [])

        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') <= 1080 and f.get('ext') == 'mp4'} # Filter for mp4 and reasonable quality

        if not resolutions:
            bot.send_message(msg.chat.id, "No suitable MP4 resolutions found for download.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1])):
            vid_id = str(uuid.uuid4())[:8]
            # Store video info directly on the bot object for callback access
            if not hasattr(bot, 'video_info'):
                bot.video_info = {}
            bot.video_info[vid_id] = {'url': url, 'format_id': fid}
            markup.add(InlineKeyboardButton(res, callback_data=f'dl_yt:{vid_id}'))

        bot.send_message(msg.chat.id, f"Choose quality for: **{title}**", reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"YouTube URL handling error: {e}")
        bot.send_message(msg.chat.id, f"Error processing YouTube link: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_yt:'))
def download_youtube_video(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    vid_id = call.data.split(":")[1]
    if not hasattr(bot, 'video_info') or vid_id not in bot.video_info:
        bot.answer_callback_query(call.id, "Download link expired. Please send the YouTube link again.")
        return

    data = bot.video_info[vid_id]
    url, fmt = data['url'], data['format_id']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")

    try:
        bot.answer_callback_query(call.id, "Downloading...")
        bot.send_chat_action(call.message.chat.id, 'upload_video')

        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'merge_output_format': 'mp4',
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'retries': 5, # Add retries for robustness
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(video_path):
            with open(video_path, 'rb') as f:
                bot.send_video(call.message.chat.id, f, reply_to_message_id=call.message.message_id)

            subtitle_path_en = video_path.replace('.mp4', '.en.srt')
            subtitle_path_auto = video_path.replace('.mp4', '.en.vtt') # yt-dlp might produce vtt for auto subs
            
            sent_subtitles = False
            if os.path.exists(subtitle_path_en):
                with open(subtitle_path_en, 'rb') as sub_file:
                    bot.send_document(call.message.chat.id, sub_file, caption="English Subtitles (SRT)")
                sent_subtitles = True
            if os.path.exists(subtitle_path_auto): # Check for auto-generated VTT
                # Convert VTT to SRT if necessary before sending
                srt_path_auto = subtitle_path_auto.replace('.vtt', '.srt')
                try:
                    # Simple VTT to SRT conversion (can be more robust)
                    with open(subtitle_path_auto, 'r', encoding='utf-8') as f_vtt, \
                         open(srt_path_auto, 'w', encoding='utf-8') as f_srt:
                        srt_content = f_vtt.read()
                        # Remove WebVTT header and convert timestamp format
                        srt_content = re.sub(r'WEBVTT\n\n', '', srt_content)
                        srt_content = re.sub(r'(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})', 
                                             r'\1:\2:\3,\4 --> \5:\6:\7,\8', srt_content)
                        # Add sequence numbers
                        segments = srt_content.strip().split('\n\n')
                        final_srt = []
                        for i, seg in enumerate(segments):
                            final_srt.append(str(i + 1))
                            final_srt.append(seg)
                        f_srt.write('\n\n'.join(final_srt))
                    
                    with open(srt_path_auto, 'rb') as sub_file:
                        bot.send_document(call.message.chat.id, sub_file, caption="Auto-generated Subtitles (SRT)")
                    os.remove(srt_path_auto) # Clean up converted srt
                    sent_subtitles = True
                except Exception as sub_convert_e:
                    logging.warning(f"Failed to convert VTT to SRT or send auto subs: {sub_convert_e}")

            if not sent_subtitles:
                bot.send_message(call.message.chat.id, "No subtitles found or generated for this video.")
        else:
            bot.send_message(call.message.chat.id, "Download failed or video file not found.")

    except Exception as e:
        logging.error(f"Error downloading YouTube video in callback: {e}")
        bot.send_message(call.message.chat.id, f"Error downloading: {e}")
    finally:
        # Clean up downloaded files
        for file in os.listdir(DOWNLOAD_DIR):
            file_path_to_remove = os.path.join(DOWNLOAD_DIR, file)
            try:
                os.remove(file_path_to_remove)
            except OSError as e:
                logging.error(f"Error removing file {file_path_to_remove}: {e}")
        # Clear specific video_info to prevent memory leak
        if hasattr(bot, 'video_info') and vid_id in bot.video_info:
            del bot.video_info[vid_id]


@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text) and not TIKTOK_REGEX.search(msg.text))
def handle_other_social_video(msg):
    """Handles direct download for non-YouTube/TikTok social media links."""
    update_user_activity(msg.from_user.id)
    if not check_subscription(msg.from_user.id):
        return send_subscription_message(msg.chat.id)

    url = msg.text
    bot.send_chat_action(msg.chat.id, 'upload_video')

    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Prioritize mp4
        'outtmpl': video_path,
        'quiet': True,
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'convert_subtitles': 'srt',
        'ignoreerrors': True,
        'nocheckcertificate': True,
        'retries': 5,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info) # Get actual downloaded file path
            
        if os.path.exists(downloaded_file):
            with open(downloaded_file, 'rb') as f:
                bot.send_video(msg.chat.id, f)

            # Check for subtitles
            subtitle_path = downloaded_file.replace('.mp4', '.en.srt')
            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub:
                    bot.send_document(msg.chat.id, sub, caption="Subtitles")
        else:
            bot.send_message(msg.chat.id, "⚠️ Video file not found or download failed. Please try again.")

    except Exception as e:
        logging.error(f"Error handling social video: {e}")
        bot.send_message(msg.chat.id, f"Error processing link: {e}")
    finally:
        # Clean up downloaded files
        for file in os.listdir(DOWNLOAD_DIR):
            file_path_to_remove = os.path.join(DOWNLOAD_DIR, file)
            try:
                os.remove(file_path_to_remove)
            except OSError as e:
                logging.error(f"Error removing file {file_path_to_remove}: {e}")

@bot.message_handler(func=lambda m: m.text and TIKTOK_REGEX.search(m.text))
def tiktok_link_handler(message):
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    url = TIKTOK_REGEX.search(message.text).group(0)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Download", callback_data=f"tiktok_download|{url}"),
        InlineKeyboardButton("Transcribe", callback_data=f"tiktok_transcribe|{url}|{message.message_id}")
    )
    bot.send_message(message.chat.id, "TikTok link detected—choose an action:", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("tiktok_download|"))
def callback_download_tiktok(call):
    global total_tiktok_downloads
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    _, url = call.data.split("|", 1)
    bot.answer_callback_query(call.id, "Downloading TikTok video...")
    bot.send_chat_action(call.message.chat.id, 'upload_video')

    temp_file_path = None
    try:
        # Use a temporary file name that includes ext
        temp_file_name = f"{uuid.uuid4()}.mp4" 
        temp_file_path = os.path.join(DOWNLOAD_DIR, temp_file_name)

        ydl_opts = {
            'outtmpl': temp_file_path,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'quiet': True,
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'nocheckcertificate': True,
            'retries': 5,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # The actual downloaded file might have a different name/extension if merge is used
            # We don't need info.get('description') as TikTok usually embeds it
            
        # Check if the file exists and has a non-zero size
        if os.path.exists(temp_file_path) and os.path.getsize(temp_file_path) > 0:
            with open(temp_file_path, 'rb') as video:
                bot.send_video(call.message.chat.id, video, caption="TikTok Video")
            total_tiktok_downloads += 1
        else:
            bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok video. The file might be empty or missing.")

    except Exception as e:
        logging.error(f"TikTok download error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok video. This video might not be downloadable or an error occurred.")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


@bot.callback_query_handler(func=lambda c: c.data.startswith("tiktok_transcribe|"))
def callback_transcribe_tiktok(call):
    global total_files_processed, total_processing_time
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    _, url, message_id_str = call.data.split("|", 2)
    message_id = int(message_id_str)
    
    bot.answer_callback_query(call.id, "Transcribing TikTok video...")
    bot.send_chat_action(call.message.chat.id, 'typing')

    temp_file_path = None
    try:
        temp_file_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")
        ydl_opts = {
            'outtmpl': temp_file_path,
            'format': 'bestaudio/best', # Get best audio for transcription
            'quiet': True,
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'nocheckcertificate': True,
            'retries': 5,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        if not os.path.exists(temp_file_path) or os.path.getsize(temp_file_path) == 0:
            bot.send_message(call.message.chat.id, "⚠️ Failed to download TikTok audio for transcription. The file might be empty or missing.")
            return

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe_audio_file(temp_file_path) or "Transcription failed or returned empty."
        
        user_transcriptions.setdefault(uid, {})[message_id] = transcription

        total_files_processed += 1
        processing_time = (datetime.now() - processing_start_time).total_seconds()
        total_processing_time += processing_time

        buttons = InlineKeyboardMarkup()
        buttons.add(
            InlineKeyboardButton("Translate ", callback_data=f"btn_translate|{message_id}"),
            InlineKeyboardButton("Summarize ", callback_data=f"btn_summarize|{message_id}")
        )

        if len(transcription) > 4000:
            fn = os.path.join(DOWNLOAD_DIR, 'tiktok_transcription.txt')
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
                reply_markup=buttons,
                reply_to_message_id=message_id # Reply to the original message that contained the link
            )
    except Exception as e:
        logging.error(f"TikTok transcribe error: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Failed to transcribe TikTok.")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

@bot.message_handler(content_types=['voice', 'audio', 'video', 'video_note'])
def handle_file(message):
    global total_files_processed, total_audio_files, total_voice_clips, total_videos, total_processing_time
    uid = str(message.from_user.id)
    update_user_activity(uid)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)

    file_obj = message.voice or message.audio or message.video or message.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(message.chat.id, "The file size you uploaded is too large (max allowed is 20MB).")

    info = bot.get_file(file_obj.file_id)
    # Using a unique name for each download to prevent conflicts
    file_extension = 'ogg' if message.voice else 'mp3' if message.audio else 'mp4'
    local_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.{file_extension}")
    
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        downloaded_file_data = bot.download_file(info.file_path)
        with open(local_path, 'wb') as f:
            f.write(downloaded_file_data)
        
        # Ensure the file exists and has content before transcribing
        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            bot.send_message(message.chat.id, "⚠️ Failed to download file for transcription. It might be empty or missing.")
            return

        global processing_start_time
        processing_start_time = datetime.now()

        transcription = transcribe_audio_file(local_path) or "Transcription failed or returned empty."
        
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

# --- CALLBACK HANDLERS FOR LANGUAGE SETTING, TRANSLATE & SUMMARIZE ---

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_lang|"))
def callback_set_language(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

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
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message. It might be too old.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, f"Translating to {preferred_lang}...")
        do_translate_or_summarize(call.message, uid, preferred_lang, message_id, "translate")
    else:
        markup = generate_language_keyboard("translate_to", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want to translate into:",
            reply_markup=markup
        )
    # No need to answer callback query again if it was answered by do_translate_or_summarize

@bot.callback_query_handler(func=lambda c: c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)

    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ No transcription found for this message. It might be too old.")
        return

    preferred_lang = user_language_settings.get(uid)
    if preferred_lang:
        bot.answer_callback_query(call.id, f"Summarizing in {preferred_lang}...")
        do_translate_or_summarize(call.message, uid, preferred_lang, message_id, "summarize")
    else:
        markup = generate_language_keyboard("summarize_in", message_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Please select the language you want the summary in:",
            reply_markup=markup
        )
    # No need to answer callback query again if it was answered by do_translate_or_summarize


@bot.callback_query_handler(func=lambda c: c.data.startswith("translate_to|"))
def callback_translate_to(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    # Update preferred language for future use
    user_language_settings[uid] = lang
    save_user_language_settings()
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Translating to **{lang}**...",
        parse_mode="Markdown"
    )

    if message_id:
        do_translate_or_summarize(call.message, uid, lang, message_id, "translate")
    else:
        bot.send_message(call.message.chat.id, "❌ No transcription found to translate. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id) # Answer callback to remove "loading" on button

@bot.callback_query_handler(func=lambda c: c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    uid = str(call.from_user.id)
    update_user_activity(uid)
    if not check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "Please subscribe to the channel first.")
        return send_subscription_message(call.message.chat.id)

    parts = call.data.split("|")
    lang = parts[1]
    message_id = int(parts[2]) if len(parts) > 2 else None

    # Update preferred language for future use
    user_language_settings[uid] = lang
    save_user_language_settings()

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Summarizing in **{lang}**...",
        parse_mode="Markdown"
    )

    if message_id:
        do_translate_or_summarize(call.message, uid, lang, message_id, "summarize")
    else:
        bot.send_message(call.message.chat.id, "❌ No transcription found to summarize. Please use the inline buttons on the transcription.")
    bot.answer_callback_query(call.id) # Answer callback to remove "loading" on button


@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'sticker', 'document'])
def fallback(message):
    update_user_activity(message.from_user.id)
    if not check_subscription(message.from_user.id):
        return send_subscription_message(message.chat.id)
    bot.send_message(message.chat.id, "Please send a supported link (TikTok, YouTube, Instagram, Facebook, X, etc.) or a voice, audio, or video file.")


# --- WEBHOOK ENDPOINTS ---
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
def set_webhook_route():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logging.error(f"Error setting webhook: {e}")
        return f"Error setting webhook: {e}", 500

@app.route('/delete_webhook', methods=['GET', 'POST'])
def delete_webhook_route():
    try:
        bot.delete_webhook()
        return "Webhook deleted", 200
    except Exception as e:
        logging.error(f"Error deleting webhook: {e}")
        return f"Error deleting webhook: {e}", 500

# --- APP RUN ---
if __name__ == "__main__":
    # Ensure download directory is clean on startup
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    # Set bot commands and description (Telegram Bot API calls)
    bot.set_my_commands([
        telebot.types.BotCommand("start", "Restart the bot and view welcome message"),
        telebot.types.BotCommand("status", "View Bot statistics and user activity"),
        telebot.types.BotCommand("help", "View detailed instructions on how to use the bot"),
        telebot.types.BotCommand("language", "Change preferred language for translate/summarize"),
        telebot.types.BotCommand("privacy", "View privacy notice about data handling"),
    ])
    bot.set_my_short_description("Got media files? Let this free bot transcribe, summarize, and translate them in seconds!")
    bot.set_my_description(
        """This bot quickly transcribes, summarizes, and translates voice messages, TikTok videos, audio files, and videos—free and in multiple languages.

🔥Enjoy free usage and start now!👌🏻"""
    )
    
    # Delete existing webhook and set new one
    try:
        bot.delete_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook successfully set to {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Failed to set webhook on startup: {e}")

    # Run the Flask app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
