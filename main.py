import shutil
import telebot
import yt_dlp
import os
import uuid
from flask import Flask, request, abort
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import imageio_ffmpeg  # Isticmaalka imageio-ffmpeg

BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981
USER_FILE = "users.txt"
DOWNLOAD_DIR = "downloads"
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com"

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# Dejinta sharaxaadda buuxda (full description) iyo sharaxaadda gaaban (short description)
try:
    # Full description
    bot.set_my_description(
        "I am a media downloader bot. Send me links from various platforms like TikTok, YouTube, Instagram, Pinterest, and Facebook, and I'll download the media for you, including video descriptions and subtitles where available.",
        language_code='en'
    )
    # Short description (about text)
    bot.set_my_short_description(
        "Your go-to this bot for downloading media from popular platforms!",
        language_code='en'
    )
    # Dejinta amarrada /start iyo /help
    commands = [
        telebot.types.BotCommand("/start", "👋Get a welcome message and info"),
        telebot.types.BotCommand("/help", "❓Get information on how to use the bot")
    ]
    bot.set_my_commands(commands)
except Exception as e:
    print(f"Error setting bot description or commands: {e}")

# Haddii folder-ka downloads uu horay u jiray, tirtir ka dibna dib u abuuro
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
    # Isticmaal magaca hore ee user-ka (first_name), meeshii @username laga xusi lahaa
    name = message.from_user.first_name
    welcome_text = (
        f"👋 Salam {name}, I'm your friendly media downloader bot!\n"
        "Send me a link from TikTok, YouTube, Instagram, Facebook, or other supported platforms, and I’ll download it for you.\n\n"
        "For more info, use /help."
    )
    bot.send_message(user_id, welcome_text)
    if user_id == ADMIN_ID:
        show_admin_panel(user_id)

@bot.message_handler(commands=['help'])
def help_handler(message):
    user_id = message.chat.id
    help_text = (
        "Here are some instructions:\n"
        "/start - Begin using the bot\n"
        "/help - Get help and instructions\n"
        "Send me a supported media link (TikTok, YouTube, Instagram, Pinterest, Facebook, etc.), and I'll download the media (including video descriptions and subtitles where available)."
    )
    bot.send_message(user_id, help_text)

def show_admin_panel(chat_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Total Users", "📢 Send Ads (broadcast)")
    bot.send_message(chat_id, "Welcome to Admin Panel", reply_markup=markup)

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
    bot.send_message(msg.chat.id, "Send your broadcast message (text, photo, video, etc.):")
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

def is_supported_url(url):
    platforms = ['tiktok.com', 'youtube.com', 'pinterest.com', 'pin.it', 'youtu.be',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

# Markii link la soo diro, bot-ka wuu falcelin doonaa isagoo diraya emoji 👀
@bot.message_handler(func=lambda msg: is_supported_url(msg.text))
def react_with_icon(msg):
    bot.send_message(msg.chat.id, "👀")
    # Kadib u gudub ka shaqeynta link-ga
    if is_youtube_url(msg.text):
        handle_youtube_url(msg)
    else:
        handle_social_video(msg)

@bot.message_handler(func=lambda msg: is_youtube_url(msg.text))
def handle_youtube_url(msg):
    url = msg.text
    try:
        bot.send_chat_action(msg.chat.id, 'typing')
        # Soo saar macluumaadka video-ga adigoo adeegsanaya yt_dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            # Isticmaal ffmpeg-ka imageio-ffmpeg si uusan u baahnayn ffmpeg-ka caadiga ah
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            description = info.get('description', '')  # Qoraalka sharaxaadda
            formats = info.get('formats', [])

        # Diyaari doorashooyinka tayooyinka ilaa 1080p
        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') <= 1080}

        if not resolutions:
            bot.send_message(msg.chat.id, "No suitable resolutions found.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1])):
            vid_id = str(uuid.uuid4())[:8]
            bot.video_info = getattr(bot, 'video_info', {})
            bot.video_info[vid_id] = {'url': url, 'format_id': fid, 'description': description}
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
    url, fmt, description = data['url'], data['format_id'], data.get('description', '')
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
            'convert_subtitles': 'srt',
            # Isticmaal ffmpeg-ka imageio-ffmpeg
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Dir fiidiyowga
        with open(video_path, 'rb') as f:
            bot.send_video(call.message.chat.id, f, reply_to_message_id=call.message.message_id)

        # Dir qoraalka sharaxaadda (description) haddii uu jiro
        if description:
            bot.send_message(call.message.chat.id, f"Description:\n{description}")

        # Haddii subtitles la helay, dir
        subtitle_path = video_path.replace('.mp4', '.en.srt')
        if os.path.exists(subtitle_path):
            with open(subtitle_path, 'rb') as sub_file:
                bot.send_document(call.message.chat.id, sub_file, caption="Subtitles")

    except Exception as e:
        bot.send_message(call.message.chat.id, f"Error downloading: {e}")
    finally:
        for file in os.listdir(DOWNLOAD_DIR):
            os.remove(os.path.join(DOWNLOAD_DIR, file))

@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text))
def handle_social_video(msg):
    url = msg.text
    try:
        bot.send_chat_action(msg.chat.id, 'upload_video')
        path, sub_path, description = download_video_any(url)
        if path:
            with open(path, 'rb') as f:
                bot.send_video(msg.chat.id, f)
            if description:
                bot.send_message(msg.chat.id, f"Description:\n{description}")
            if sub_path and os.path.exists(sub_path):
                with open(sub_path, 'rb') as sub:
                    bot.send_document(msg.chat.id, sub, caption="Subtitles")
        else:
            bot.send_message(msg.chat.id, "An error occurred during download.")
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
        'ignoreerrors': True,
        # Isticmaal ffmpeg-ka imageio-ffmpeg
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            description = info.get('description', '')
        subtitle_path = video_path.replace('.mp4', '.en.srt')
        return video_path, subtitle_path if os.path.exists(subtitle_path) else None, description
    except Exception as e:
        print(f"Download error: {e}")
        return None, None, ""

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

if __name__ == "__main__":
    bot.delete_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
