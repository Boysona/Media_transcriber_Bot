import shutil
import telebot
import yt_dlp
import os
import uuid
from flask import Flask, request, abort
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ReplyKeyboardRemove

# --- Configuration ---
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID = 5978150981 # Replace with your actual admin ID
USER_FILE = "users.txt"
DOWNLOAD_DIR = "downloads"
WEBHOOK_URL = "https://media-transcriber-bot.onrender.com" # Ensure this matches your deployment URL

# --- Flask App and Bot Initialization ---
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# --- Setup Download Directory ---
if os.path.exists(DOWNLOAD_DIR):
    shutil.rmtree(DOWNLOAD_DIR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- Helper Functions ---
def save_user(user_id):
    """Saves a user ID to the users.txt file if it's not already there."""
    if not os.path.exists(USER_FILE):
        open(USER_FILE, "w").close()
    with open(USER_FILE, "r+") as f:
        ids = f.read().splitlines()
        if str(user_id) not in ids:
            f.write(f"{user_id}\n")

def cleanup_downloaded_files(file_path):
    """Removes a file and its associated subtitle file if they exist."""
    if file_path and os.path.exists(file_path):
        os.remove(file_path)
    subtitle_path = file_path.replace('.mp4', '.en.srt') if file_path else None
    if subtitle_path and os.path.exists(subtitle_path):
        os.remove(subtitle_path)

def set_bot_profile_details():
    """Sets the bot's description, short description, and commands."""
    try:
        # set_my_description
        bot.set_my_description(
            description="I am a media downloader bot. Send me links from various platforms like TikTok, YouTube, Instagram, and Facebook, and I'll download the media for you, including video descriptions and subtitles where available."
        )
        print("Bot description set successfully.")

        # set_my_short_description
        bot.set_my_short_description(
            short_description="Your go-to bot for downloading media from popular platforms!"
        )
        print("Bot short description set successfully.")

        # set_my_commands
        commands = [
            BotCommand("start", "👋 Get a welcome message and info."),
            BotCommand("help", "❓ Get information on how to use the bot."),
        ]
        bot.set_my_commands(commands)
        print("Bot commands set successfully.")

    except Exception as e:
        print(f"Error setting bot profile details: {e}")

# --- Message Handlers ---
@bot.message_handler(commands=['start'])
def start_handler(message):
    """Handles the /start command."""
    user_id = message.chat.id
    save_user(user_id)
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    text = (
        f"👋 Salam {username}, I'm your friendly media downloader bot!\n"
        "Send me a link from TikTok, YouTube, Instagram, Facebook, or other supported platforms, "
        "and I’ll download it for you.\n\n"
        "For more info, use /help."
    )
    bot.send_message(user_id, text)
    if user_id == ADMIN_ID:
        show_admin_panel(user_id)

@bot.message_handler(commands=['help'])
def help_handler(message):
    """Handles the /help command."""
    help_text = (
        "❓ **How to use me:**\n"
        "1. Simply send me a link to a video from platforms like TikTok, YouTube, Instagram, Facebook, X (Twitter), etc.\n"
        "2. I will process the link and send you the downloadable media.\n"
        "3. For YouTube links, I'll give you options to choose the video quality.\n"
        "4. I'll also try to fetch descriptions, including hashtags, and subtitles if available.\n\n"
        "**Supported Platforms:** TikTok, YouTube, Instagram, Facebook, X (Twitter), and many more supported by yt-dlp!\n\n"
        "If you encounter any issues, please ensure your link is correct and publicly accessible."
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

def show_admin_panel(chat_id):
    """Displays the admin panel keyboard."""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📊 Total Users", "📢 Send Ads (broadcast)")
    bot.send_message(chat_id, "Welcome to Admin Panel", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "📊 Total Users" and msg.chat.id == ADMIN_ID)
def total_users(msg):
    """Sends the total number of users to the admin."""
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            count = len(f.readlines())
        bot.send_message(msg.chat.id, f"👥 Total users: {count}")
    else:
        bot.send_message(msg.chat.id, "No users yet.")
    # Remove admin panel keyboard after use
    bot.send_message(msg.chat.id, "Admin panel closed.", reply_markup=ReplyKeyboardRemove())


@bot.message_handler(func=lambda msg: msg.text == "📢 Send Ads (broadcast)" and msg.chat.id == ADMIN_ID)
def ask_broadcast(msg):
    """Asks the admin for the message to broadcast."""
    bot.send_message(msg.chat.id, "Send your message (text, photo, video, etc.) to broadcast:")
    bot.register_next_step_handler(msg, broadcast_message)

def broadcast_message(message):
    """Broadcasts a message to all saved users."""
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
    # Remove admin panel keyboard after use
    bot.send_message(message.chat.id, "Admin panel closed.", reply_markup=ReplyKeyboardRemove())

def is_supported_url(url):
    """Checks if the URL contains keywords for supported platforms."""
    platforms = ['tiktok.com', 'youtube.com', 'youtu.be', 'pinterest.com', 'pin.it',
                 'instagram.com', 'snapchat.com', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com']
    return any(p in url.lower() for p in platforms)

def is_youtube_url(url):
    """Checks if the URL is a YouTube URL."""
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

@bot.message_handler(func=lambda msg: is_youtube_url(msg.text))
def handle_youtube_url(msg):
    """Handles YouTube URLs by offering quality selection."""
    url = msg.text
    try:
        # React to the message
        bot.set_message_reaction(chat_id=msg.chat.id, message_id=msg.message_id, reaction="👀")
        bot.send_chat_action(msg.chat.id, 'typing')

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True, # We only want to extract info, not download yet
            'force_generic_extractor': False, # Allow platform-specific extractors
            'retries': 5,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        if not isinstance(info, dict):
            bot.send_message(msg.chat.id, "Could not extract video information from this YouTube link. Please check the URL or try again later.")
            print(f"Failed to extract info from YouTube URL (not a dict): {url}")
            return

        title = info.get('title', 'Video')
        description = info.get('description', 'No description available.')
        formats = info.get('formats', [])

        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') and f.get('height') <= 1080}

        if not resolutions:
            bot.send_message(msg.chat.id, "No suitable video qualities found for this YouTube video.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resolutions.items(), key=lambda x: int(x[0][:-1])):
            vid_id = str(uuid.uuid4())[:8]
            # Store video info with a unique ID
            bot.video_info = getattr(bot, 'video_info', {})
            bot.video_info[vid_id] = {'url': url, 'format_id': fid, 'description': description}
            markup.add(InlineKeyboardButton(res, callback_data=f'dl:{vid_id}'))

        bot.send_message(msg.chat.id, f"Choose quality for: **{title}**", reply_markup=markup, parse_mode='Markdown')

    except yt_dlp.utils.DownloadError as e:
        bot.send_message(msg.chat.id, f"Error processing YouTube link: The video might be private, geo-restricted, or does not exist. ({e})")
        print(f"YouTube processing error (DownloadError) for {url}: {e}")
    except Exception as e:
        bot.send_message(msg.chat.id, f"An unexpected error occurred while processing your YouTube link: {e}")
        print(f"YouTube processing error: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video(call):
    """Handles callback queries for downloading YouTube videos."""
    vid = call.data.split(":")[1]
    # Check if the stored info exists
    if not hasattr(bot, 'video_info') or vid not in bot.video_info:
        bot.answer_callback_query(call.id, "Download link expired or not found. Please try sending the link again.")
        return

    data = bot.video_info[vid]
    url, fmt, description = data['url'], data['format_id'], data['description']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")

    try:
        bot.answer_callback_query(call.id, "Downloading...")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Downloading your video... Please wait. This might take a moment.",
            reply_markup=None # Remove the inline keyboard
        )
        bot.send_chat_action(call.message.chat.id, 'upload_video')

        ydl_opts = {
            'format': fmt,
            'outtmpl': video_path,
            'quiet': True,
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'ignoreerrors': True,
            'retries': 5, # Add retries for robustness
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
            raise Exception("Video download failed or resulted in an empty file.")

        caption = f"Description:\n{description[:1020]}{'...' if len(description) > 1020 else ''}" # Limit caption length
        with open(video_path, 'rb') as f:
            bot.send_video(
                call.message.chat.id,
                f,
                caption=caption,
                reply_to_message_id=call.message.message_id
            )

        subtitle_path = video_path.replace('.mp4', '.en.srt')
        if os.path.exists(subtitle_path) and os.path.getsize(subtitle_path) > 0:
            with open(subtitle_path, 'rb') as sub_file:
                bot.send_document(call.message.chat.id, sub_file, caption="Subtitles")

    except yt_dlp.utils.DownloadError as e:
        bot.send_message(call.message.chat.id, f"Failed to download video: The video might be private, geo-restricted, or no suitable format was found. ({e})")
        print(f"YouTube download error (DownloadError) for {url}: {e}")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"An unexpected error occurred during video download: {e}")
        print(f"YouTube download error for {url}: {e}")
    finally:
        # Clean up files after sending
        cleanup_downloaded_files(video_path)
        # Remove the stored video info
        if vid in bot.video_info:
            del bot.video_info[vid]


@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text))
def handle_social_video(msg):
    """Handles social media URLs (TikTok, Instagram, Facebook, etc.)."""
    url = msg.text
    video_path = None # Initialize video_path to None
    try:
        # React to the message
        bot.set_message_reaction(chat_id=msg.chat.id, message_id=msg.message_id, reaction="👀")
        bot.send_chat_action(msg.chat.id, 'upload_video')

        basename = str(uuid.uuid4())
        video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")
        
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': video_path,
            'quiet': True,
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'convert_subtitles': 'srt',
            'ignoreerrors': True,
            'retries': 5,
        }
        
        description = "No description available."
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True) # Set download to True to directly download
            
        if not isinstance(info, dict):
            raise ValueError("Failed to extract video information (not a dictionary). The link might be invalid or unsupported.")

        description = info.get('description', 'No description available.')

        if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
            raise Exception("Video download failed or resulted in an empty file.")

        caption = f"Description:\n{description[:1020]}{'...' if len(description) > 1020 else ''}" # Limit caption length
        with open(video_path, 'rb') as f:
            bot.send_video(msg.chat.id, f, caption=caption)
        
        subtitle_path = video_path.replace('.mp4', '.en.srt')
        if os.path.exists(subtitle_path) and os.path.getsize(subtitle_path) > 0:
            with open(subtitle_path, 'rb') as sub:
                bot.send_document(msg.chat.id, sub, caption="Subtitles")

    except yt_dlp.utils.DownloadError as e:
        bot.send_message(msg.chat.id, f"Error downloading social media video: The video might be private, geo-restricted, or does not exist. ({e})")
        print(f"Social media download error (DownloadError) for {url}: {e}")
    except ValueError as e:
        bot.send_message(msg.chat.id, f"Could not process this link: {e}")
        print(f"Value error for {url}: {e}")
    except Exception as e:
        bot.send_message(msg.chat.id, f"An unexpected error occurred while downloading social media video: {e}")
        print(f"Social media download error for {url}: {e}")
    finally:
        # Clean up files after sending, only if video_path was set
        if video_path:
            cleanup_downloaded_files(video_path)

# --- Webhook and Flask Setup ---
@app.route('/', methods=['POST'])
def webhook():
    """Handles incoming Telegram updates via webhook."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

@app.route('/set_webhook', methods=['GET', 'POST'])
def set_webhook_route():
    """Route to manually set the webhook."""
    bot.set_webhook(url=WEBHOOK_URL)
    return f"Webhook set to {WEBHOOK_URL}", 200

@app.route('/delete_webhook', methods=['GET', 'POST'])
def delete_webhook_route():
    """Route to manually delete the webhook."""
    bot.delete_webhook()
    return "Webhook deleted", 200

# --- Main Execution ---
if __name__ == "__main__":
    print("Starting bot...")
    bot.delete_webhook() # Ensure no old webhooks are active
    bot.set_webhook(url=WEBHOOK_URL) # Set the new webhook
    set_bot_profile_details() # Set bot description and commands on startup
    print("Bot is running and webhook is set.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

