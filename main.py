import shutil
import telebot
import yt_dlp
import os
import uuid
import re # Import regex module
from flask import Flask, request, abort
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand

# --- Configuration ---
BOT_TOKEN = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q" # REPLACE WITH YOUR BOT TOKEN
ADMIN_ID = 5978150981  # Replace with your actual admin ID
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
        try:
            os.remove(file_path)
            print(f"Cleaned up video file: {file_path}")
        except OSError as e:
            print(f"Error removing video file {file_path}: {e}")
    
    # Check for subtitle file with common extensions
    if file_path:
        base, _ = os.path.splitext(file_path)
        for ext in ['.srt', '.vtt', '.en.srt', '.en.vtt']: # Add common subtitle extensions
            subtitle_path = f"{base}{ext}"
            if os.path.exists(subtitle_path):
                try:
                    os.remove(subtitle_path)
                    print(f"Cleaned up subtitle file: {subtitle_path}")
                except OSError as e:
                    print(f"Error removing subtitle file {subtitle_path}: {e}")

def set_bot_profile_details():
    """Sets the bot's description, short description, and commands."""
    try:
        bot.set_my_description(
            description="I am a media downloader bot. Send me links from various platforms like TikTok, YouTube, Instagram, and Facebook, and I'll download the media for you, including video descriptions and subtitles where available."
        )
        print("Bot description set successfully.")

        bot.set_my_short_description(
            short_description="Your go-to bot for downloading media from popular platforms!"
        )
        print("Bot short description set successfully.")

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
    for uid_str in user_ids: # Iterate over string user IDs
        try:
            uid = int(uid_str)
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
            print(f"Error sending to user {uid_str}: {e}")
    bot.send_message(message.chat.id, f"✅ Broadcast finished. Sent to {sent} users.")

def is_supported_url(url):
    """Checks if the URL contains keywords for commonly supported platforms by yt-dlp."""
    # A more general check for common video platforms
    common_platforms = [
        'tiktok.com', 'youtube.com', 'youtu.be', 'instagram.com',
        'facebook.com', 'fb.watch', 'x.com', 'twitter.com',
        'dailymotion.com', 'vimeo.com', 'twitch.tv', 'soundcloud.com',
        'pinterest.com', 'pin.it'
    ]
    return any(p in url.lower() for p in common_platforms)

def is_youtube_url(url):
    """Checks if the URL is a YouTube URL using regex for accuracy."""
    youtube_regex = r"(?:http(?:s)?:\/\/)?(?:www\.)?(?:m\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=|embed\/|v\/|)([\w-]{11})(?:\S+)?"
    return re.match(youtube_regex, url) is not None

@bot.message_handler(func=lambda msg: is_youtube_url(msg.text))
def handle_youtube_url(msg):
    """Handles YouTube URLs by offering quality selection."""
    url = msg.text
    try:
        bot.set_message_reaction(chat_id=msg.chat.id, message_id=msg.message_id, reaction="👀")
        bot.send_chat_action(msg.chat.id, 'typing')
        processing_message = bot.send_message(msg.chat.id, "Analyzing YouTube link... Please wait.")

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'force_generic_extractor': False,
            'format': 'bestvideo+bestaudio/best', # Ensure we get all formats for resolution info
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not isinstance(info, dict):
                bot.edit_message_text(chat_id=msg.chat.id, message_id=processing_message.message_id, 
                                      text="Failed to analyze YouTube link. The video might be private or geo-restricted.")
                print(f"YouTube info extraction failed: {info}")
                return

            title = info.get('title', 'Video')
            description = info.get('description', 'No description available.')
            formats = info.get('formats', [])

        resolutions = {f'{f["height"]}p': f['format_id']
                       for f in formats if f.get('vcodec') != 'none' and f.get('height') and f.get('height') <= 1080}

        if not resolutions:
            bot.edit_message_text(chat_id=msg.chat.id, message_id=processing_message.message_id, 
                                  text="No suitable video qualities found for download.")
            return

        markup = InlineKeyboardMarkup(row_width=3)
        # Sort resolutions numerically
        sorted_resolutions = sorted(resolutions.items(), key=lambda item: int(item[0][:-1]))
        for res, fid in sorted_resolutions:
            vid_id = str(uuid.uuid4())[:8]
            # Store video info with a unique ID for later callback
            if not hasattr(bot, 'video_info'):
                bot.video_info = {}
            bot.video_info[vid_id] = {'url': url, 'format_id': fid, 'description': description, 'title': title}
            markup.add(InlineKeyboardButton(res, callback_data=f'dl:{vid_id}'))

        bot.edit_message_text(chat_id=msg.chat.id, message_id=processing_message.message_id, 
                              text=f"Choose quality for: {title}", reply_markup=markup)

    except yt_dlp.utils.DownloadError as e:
        bot.edit_message_text(chat_id=msg.chat.id, message_id=processing_message.message_id, 
                              text=f"Error processing YouTube link: {e}\n\nPlease check the link and try again.")
        print(f"YouTube DownloadError: {e}")
    except Exception as e:
        bot.edit_message_text(chat_id=msg.chat.id, message_id=processing_message.message_id, 
                              text=f"An unexpected error occurred: {e}")
        print(f"YouTube processing general error: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def download_youtube_video(call):
    """Handles callback queries for downloading YouTube videos."""
    vid = call.data.split(":")[1]
    
    if not hasattr(bot, 'video_info') or vid not in bot.video_info:
        bot.answer_callback_query(call.id, "Download expired or not found. Please try sending the link again.")
        return

    data = bot.video_info[vid]
    url, fmt, description, title = data['url'], data['format_id'], data['description'], data['title']
    basename = str(uuid.uuid4())
    video_path = os.path.join(DOWNLOAD_DIR, f"{basename}.mp4")

    download_message = None # Initialize to None
    try:
        bot.answer_callback_query(call.id, "Starting download...")
        download_message = bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Downloading '{title}'... Please wait. This might take a moment.",
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
            'retries': 5,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found after download: {video_path}")

        caption = f"Description:\n{description[:1000]}{'...' if len(description) > 1000 else ''}" # Limit caption length for Telegram
        with open(video_path, 'rb') as f:
            bot.send_video(
                call.message.chat.id,
                f,
                caption=caption,
                reply_to_message_id=call.message.message_id,
                supports_streaming=True # Enable streaming
            )
        
        # Try to send subtitles
        base_video_path = os.path.splitext(video_path)[0]
        subtitle_found = False
        for sub_ext in ['.en.srt', '.srt', '.en.vtt', '.vtt']: # Check for common subtitle extensions
            subtitle_path = f"{base_video_path}{sub_ext}"
            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub_file:
                    bot.send_document(call.message.chat.id, sub_file, caption="Subtitles")
                subtitle_found = True
                break
        
        if not subtitle_found:
            print(f"No English subtitles found for {title} at {subtitle_path_base}.en.srt")
            
        if download_message: # Update message after successful send
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=download_message.message_id,
                text=f"Download complete for '{title}'!",
                reply_markup=None
            )

    except yt_dlp.utils.DownloadError as e:
        error_msg = f"Error downloading video: {e}\n\nPlease try again or use a different quality."
        if download_message:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=download_message.message_id, text=error_msg)
        else:
            bot.send_message(call.message.chat.id, error_msg)
        print(f"YouTube download error for {url}: {e}")
    except FileNotFoundError:
        error_msg = "The video file was not found after download. It might have failed silently. Please try again."
        if download_message:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=download_message.message_id, text=error_msg)
        else:
            bot.send_message(call.message.chat.id, error_msg)
        print(f"File not found error after download for {url}.")
    except Exception as e:
        error_msg = f"An unexpected error occurred during download: {e}"
        if download_message:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=download_message.message_id, text=error_msg)
        else:
            bot.send_message(call.message.chat.id, error_msg)
        print(f"YouTube download general error for {url}: {e}")
    finally:
        cleanup_downloaded_files(video_path)
        if hasattr(bot, 'video_info') and vid in bot.video_info:
            del bot.video_info[vid]


@bot.message_handler(func=lambda msg: is_supported_url(msg.text) and not is_youtube_url(msg.text))
def handle_social_video(msg):
    """Handles social media URLs (TikTok, Instagram, Facebook, etc.)."""
    url = msg.text
    video_path = None # Initialize video_path to None
    download_message = None # Initialize to None
    try:
        bot.set_message_reaction(chat_id=msg.chat.id, message_id=msg.message_id, reaction="👀")
        bot.send_chat_action(msg.chat.id, 'upload_video')
        download_message = bot.send_message(msg.chat.id, "Starting social media download... Please wait.")

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
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False) # First, extract info without downloading
                
                # IMPORTANT: Check if info is a dictionary
                if isinstance(info, dict):
                    description = info.get('description', 'No description available.')
                    title = info.get('title', 'Social Media Video')
                else:
                    # If info is not a dict, it's likely an error string from yt-dlp
                    print(f"yt-dlp extract_info for social media did not return a dictionary for {url}: {info}")
                    description = "Could not retrieve detailed description."
                    title = "Social Media Video" # Fallback title

                # Now download the video using the same ydl instance and options
                bot.edit_message_text(chat_id=msg.chat.id, message_id=download_message.message_id, 
                                      text=f"Downloading '{title}'... This may take a moment.")
                ydl.download([url])

        except yt_dlp.utils.DownloadError as e:
            error_msg = f"Failed to download video from social media: {e}\n\nPlease check the link and try again."
            if download_message:
                bot.edit_message_text(chat_id=msg.chat.id, message_id=download_message.message_id, text=error_msg)
            else:
                bot.send_message(msg.chat.id, error_msg)
            print(f"Social media DownloadError for {url}: {e}")
            return # Exit if download failed
        except Exception as e:
            error_msg = f"An error occurred while processing the social media link: {e}"
            if download_message:
                bot.edit_message_text(chat_id=msg.chat.id, message_id=download_message.message_id, text=error_msg)
            else:
                bot.send_message(msg.chat.id, error_msg)
            print(f"Social media general error for {url}: {e}")
            return # Exit if info extraction or initial download failed

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found after download: {video_path}")

        caption = f"Description:\n{description[:1000]}{'...' if len(description) > 1000 else ''}" # Limit caption length for Telegram
        with open(video_path, 'rb') as f:
            bot.send_video(msg.chat.id, f, caption=caption, supports_streaming=True)
        
        # Try to send subtitles
        base_video_path = os.path.splitext(video_path)[0]
        subtitle_found = False
        for sub_ext in ['.en.srt', '.srt', '.en.vtt', '.vtt']:
            subtitle_path = f"{base_video_path}{sub_ext}"
            if os.path.exists(subtitle_path):
                with open(subtitle_path, 'rb') as sub:
                    bot.send_document(msg.chat.id, sub, caption="Subtitles")
                subtitle_found = True
                break
        
        if not subtitle_found:
            print(f"No English subtitles found for {url}")

        if download_message: # Update message after successful send
            bot.edit_message_text(
                chat_id=msg.chat.id,
                message_id=download_message.message_id,
                text=f"Download complete for '{title}'!",
                reply_markup=None
            )

    except Exception as e:
        error_msg = f"An unexpected error occurred during social media video handling: {e}"
        if download_message:
            bot.edit_message_text(chat_id=msg.chat.id, message_id=download_message.message_id, text=error_msg)
        else:
            bot.send_message(msg.chat.id, error_msg)
        print(f"Social media download general error for {url}: {e}")
    finally:
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
    # It's good practice to delete and then set webhook to ensure it's fresh
    # This also handles cases where the URL might have changed in deployment
    bot.delete_webhook() 
    bot.set_webhook(url=WEBHOOK_URL) 
    set_bot_profile_details()
    print("Bot is running and webhook is set.")
    # Use 0.0.0.0 to listen on all available interfaces for Render deployment
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

