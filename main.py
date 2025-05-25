import os
import uuid
import shutil
import imageio_ffmpeg
import telebot
import yt_dlp
from flask import Flask, request, abort
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# === CONFIGURATION ===
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981
USER_FILE = "users.txt"
DOWNLOAD_DIR = "downloads"
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com"

# === SETUP FFmpeg from imageio_ffmpeg ===
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")

# === CLEAN DOWNLOAD DIRECTORY ON START ===
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# === INIT BOT & FLASK ===
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# === USER MANAGEMENT ===
def save_user(user_id):
    if not os.path.exists(USER_FILE):
        open(USER_FILE, "w").close()
    with open(USER_FILE, "r+") as f:
        ids = f.read().splitlines()
        if str(user_id) not in ids:
            f.write(f"{user_id}\n")

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.chat.id
    save_user(user_id)
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    bot.send_message(user_id, f"👋 Hello {username}, send me a TikTok, YouTube, Instagram, Facebook or other link, and I’ll download it for you!")
    if user_id == ADMIN_ID:
        show_admin_panel(user_id)

def show_admin_panel(chat_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Total Users", "📢 Send Ads (broadcast)")
    bot.send_message(chat_id, "Welcome to the Admin Panel", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "📊 Total Users" and msg.chat.id == ADMIN_ID)
def total_users(msg):
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            count = len(f.readlines())
        bot.send_message(msg.chat.id, f"👥 Total users: {count}")
    else:
        bot.send_message(msg.chat.id, "No users yet.")

@bot.message_handler(func=lambda msg: msg.text == "📢 Send Ads (broadcast)" and msg.chat.id == ADMIN_ID)
def ask_broadcast(msg):
    bot.send_message(msg.chat.id, "Send your message (text, photo, video, etc.) to broadcast:")
    bot.register_next_step_handler(msg, broadcast_message)

def broadcast_message(message):
    if not os.path.exists(USER_FILE):
        bot.send_message(message.chat.id, "No users to broadcast to.")
        return
    with open(USER_FILE, "r") as f:
        user_ids = f.read().splitlines()
    sent = 0
    for uid in user_ids:
        try:
            uid = int(uid)
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
        except Exception as e:
            print(f"Error sending to {uid}: {e}")
    bot.send_message(message.chat.id, f"✅ Broadcast finished. Sent to {sent} users.")

# === SUPPORTED URLS ===
def is_supported_url(url):
    platforms = ['tiktok.com', 'youtube.com', 'pinterest.com', 'pin.it', 'youtu.be',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

# === YOUTUBE HANDLING ===
@bot.message_handler(func=lambda msg: is_youtube_url(msg.text))
def handle_youtube_url(msg):
    url = msg.text
    try:
        bot.send_chat_action(msg.chat.id, 'typing')
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
            bot.send_message(msg.chat.id, "No suitable resolutions found.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1])):
            vid_id = str(uuid.uuid4())[:8]
            bot.video_info = getattr(bot, 'video_info', {})
            bot.video_info[vid_id] = {'url': url, 'format_id': fid}
            markup.add(InlineKeyboardButton(res, callback_data=f'dl:{vid_id}'))

        bot.send_message(msg.chat.id, f"Choose quality for: {title}", reply_markup=markup)

    except Exception as e:
        bot.send_message(msg.chat.id, f"Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video(call):
    vid = call.data.split(":")[1]
    if not hasattr(bot, 'video_info') or vid not in bot.video_info:
        bot.answer_callback_query(call.id, "Download expired. Try again.")
        return

    data = bot.video_info[vid]
    url, fmt = data['url'], data['format_id']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")

    try:
        bot.answer_callback_query(call.id, "Downloading...")
        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt'
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(video_path):
            with open(video_path, 'rb') as f:
                bot.send_video(call.message.chat.id, f, reply_to_message_id=call.message.message_id)

            subtitle_path = video_path.replace('.mp4', '.en.srt')
            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub_file:
                    bot.send_document(call.message.chat.id, sub_file, caption="Subtitles")
        else:
            bot.send_message(call.message.chat.id, "Download failed.")

    except Exception as e:
        bot.send_message(call.message.chat.id, f"Error downloading: {e}")
    finally:
        for file in os.listdir(DOWNLOAD_DIR):
            os.remove(os.path.join(DOWNLOAD_DIR, file))

# === OTHER SOCIAL PLATFORMS ===
@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text))
def handle_social_video(msg):
    url = msg.text
    try:
        bot.send_chat_action(msg.chat.id, 'upload_video')
        path, sub_path = download_video_any(url)

        if not path or not os.path.exists(path):
            bot.send_message(msg.chat.id, "⚠️ Video file not found. Please try again.")
            return

        with open(path, 'rb') as f:
            bot.send_video(msg.chat.id, f)

        if sub_path and os.path.exists(sub_path):
            with open(sub_path, 'rb') as sub:
                bot.send_document(msg.chat.id, sub, caption="Subtitles")
    except Exception as e:
        bot.send_message(msg.chat.id, f"Error: {e}")
    finally:
        for file in os.listdir(DOWNLOAD_DIR):
            os.remove(os.path.join(DOWNLOAD_DIR, file))

def download_video_any(url):
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
    ydl_opts = {
        'format': 'best',
        'outtmpl': video_path,
        'quiet': True,
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'convert_subtitles': 'srt',
        'ignoreerrors': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        subtitle_path = video_path.replace('.mp4', '.en.srt')
        return video_path, subtitle_path if os.path.exists(subtitle_path) else None
    except Exception as e:
        print(f"Download error: {e}")
        return None, None

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
def set_webhook():
    bot.set_webhook(url=WEBHOOK_URL)
    return f"Webhook set to {WEBHOOK_URL}", 200

@app.route('/delete_webhook', methods=['GET', 'POST'])
def delete_webhook():
    bot.delete_webhook()
    return "Webhook deleted", 200

# === APP RUN ===
if __name__ == "__main__":
    bot.delete_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
