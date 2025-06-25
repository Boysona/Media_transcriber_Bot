import logging
import requests
import time
import os # Import os for os.environ.get
from flask import Flask, request, abort
from pymongo import MongoClient
import telebot
from telebot import types

# ======= API Tokens & Config =======
BOT_TOKEN = "7790991731:AAFl7aS2kw4zONbxFi2XzWPRiWBA5T52Pyg"
ASSEMBLYAI_API_KEY = "6dab0a0669624f44afa50d679242e473"
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com/"
ADMIN_ID = 5978150981 # Your specified admin ID

# ======= MongoDB Setup =======
# FIX: Corrected the password in the MONGO_URI
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(MONGO_URI)
db = client["telegram_bot_db"]
users_collection = db["users"]

# ======= In-memory User Lang Map & Admin State =======
user_lang = {}
# To keep track if the admin is in broadcast mode
admin_broadcast_state = {}

# ======= Supported Languages with Flags =======
LANGUAGES = {
    "English 🇬🇧": "en", "Deutsch 🇩🇪": "de", "Русский 🇷🇺": "ru", "فارسى 🇮🇷": "fa",
    "Indonesia 🇮🇩": "id", "Казакша 🇰🇿": "kk", "Azerbaycan 🇦🇿": "az", "Italiano 🇮🇹": "it",
    "Türkçe 🇹🇷": "tr", "Български 🇧🇬": "bg", "Sroski 🇷🇸": "sr", "Français 🇫🇷": "fr",
    "العربية 🇸🇦": "ar", "Español 🇪🇸": "es", "اردو 🇵🇰": "ur", "ไทย 🇹🇭": "th",
    "Tiếng Việt 🇻🇳": "vi", "日本語 🇯🇵": "ja", "한국어 🇰🇷": "ko", "中文 🇨🇳": "zh",
    "Nederlands 🇳🇱": "nl", "Svenska 🇸🇪": "sv", "Norsk 🇳🇴": "no", "Dansk 🇩🇰": "da",
    "Suomi 🇫🇮": "fi", "Polski 🇵🇱": "pl", "Cestina 🇨🇿": "cs", "Magyar 🇭🇺": "hu",
    "Română 🇷🇴": "ro", "Melayu 🇲🇾": "ms", "O'zbekcha 🇺🇿": "uz", "Tagalog 🇵🇭": "tl",
    "Português 🇵🇹": "pt", "हिन्दी 🇮🇳": "hi"
}

# ======= Build Keyboard =======
def build_language_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    row = []
    for i, name in enumerate(LANGUAGES.keys(), 1):
        row.append(types.KeyboardButton(name))
        if i % 3 == 0:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    return markup

def build_admin_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("Send Broadcast"))
    markup.add(types.KeyboardButton("Total Users"))
    return markup

# ======= App Init =======
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# ======= /start & /language =======
@bot.message_handler(commands=['start', 'language'])
def send_language_prompt(message):
    chat_id = message.chat.id

    # Check if the user is the admin
    if chat_id == ADMIN_ID:
        bot.send_message(
            chat_id,
            "👋 Welcome, Admin! Choose an option:",
            reply_markup=build_admin_keyboard()
        )
    else:
        bot.send_message(
            chat_id,
            "👋 Welcome! Choose your Media (Voice, Audio, Video) file language using the buttons below:",
            reply_markup=build_language_keyboard()
        )

# ======= Save Lang Choice =======
@bot.message_handler(func=lambda msg: msg.text in LANGUAGES)
def save_user_language(message):
    chat_id = message.chat.id
    # Only save language if not admin or if admin chose a language (though admin will typically use admin buttons)
    if chat_id != ADMIN_ID:
        lang_code = LANGUAGES[message.text]
        user_lang[chat_id] = lang_code

        # Save to MongoDB
        users_collection.update_one(
            {"chat_id": chat_id},
            {"$set": {"language": lang_code}},
            upsert=True
        )

        # Updated message as per request
        bot.send_message(
            chat_id,
            f"✅ Transcription Language Set: {message.text}\n\n"
            "🎙️ Please send your voice message, audio file, or video note, and I’ll transcribe it for you with precision.\n\n"
            "📁 Supported file size: Up to 20MB\n\n"
            "📞 Need help? Contact: @Zack_3d"
        )
    else:
        # If admin accidentally taps a language, redirect to admin menu
        bot.send_message(
            chat_id,
            "Admin, please use the admin options.",
            reply_markup=build_admin_keyboard()
        )

# ======= Admin Panel Handlers =======
@bot.message_handler(func=lambda message: message.chat.id == ADMIN_ID and message.text == "Total Users")
def handle_total_users(message):
    total_users = users_collection.count_documents({})
    bot.send_message(message.chat.id, f"Total users registered: {total_users}")
    # After showing total users, reset admin state or give option to return to admin menu
    bot.send_message(
        message.chat.id,
        "What else, Admin?",
        reply_markup=build_admin_keyboard()
    )


@bot.message_handler(func=lambda message: message.chat.id == ADMIN_ID and message.text == "Send Broadcast")
def handle_send_broadcast(message):
    admin_broadcast_state[message.chat.id] = True
    bot.send_message(message.chat.id, "Okay, Admin. Send me the message (text, photo, video, document, etc.) you want to broadcast to all users. To cancel, type /cancel_broadcast")

@bot.message_handler(commands=['cancel_broadcast'], func=lambda message: message.chat.id == ADMIN_ID and message.chat.id in admin_broadcast_state)
def cancel_broadcast(message):
    del admin_broadcast_state[message.chat.id]
    bot.send_message(
        message.chat.id,
        "Broadcast cancelled. What else, Admin?",
        reply_markup=build_admin_keyboard()
    )


@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'voice'],
                     func=lambda message: message.chat.id == ADMIN_ID and admin_broadcast_state.get(message.chat.id, False))
def handle_broadcast_message(message):
    del admin_broadcast_state[message.chat.id] # Exit broadcast state after receiving the message
    
    bot.send_message(message.chat.id, "Broadcasting your message now...")
    
    # Corrected: Fetch all distinct chat_ids from the collection
    all_users_chat_ids = users_collection.distinct("chat_id")

    sent_count = 0
    failed_count = 0

    for user_chat_id in all_users_chat_ids:
        if user_chat_id == ADMIN_ID: # Don't send broadcast to admin themselves
            continue

        try:
            # Using copy_message to handle various content types more elegantly
            bot.copy_message(user_chat_id, message.chat.id, message.message_id)
            
            sent_count += 1
            time.sleep(0.1) # Small delay to avoid hitting Telegram's flood limits

        except telebot.apihelper.ApiTelegramException as e:
            logging.error(f"Failed to send broadcast to user {user_chat_id}: {e}")
            failed_count += 1
        except Exception as e:
            logging.error(f"Unexpected error broadcasting to user {user_chat_id}: {e}")
            failed_count += 1

    bot.send_message(message.chat.id, f"Broadcast complete! Successfully sent to {sent_count} users. Failed for {failed_count} users.")
    bot.send_message(
        message.chat.id,
        "What else, Admin?",
        reply_markup=build_admin_keyboard()
    )

# ======= Media Handler =======
# Ensure this handler is processed AFTER admin specific handlers
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document', 'text']) # Added 'text' to catch non-admin text messages
def handle_media_and_text(message):
    chat_id = message.chat.id

    # If it's the admin, and they are NOT in broadcast state, redirect them to admin menu
    if chat_id == ADMIN_ID and not admin_broadcast_state.get(chat_id, False):
        bot.send_message(
            chat_id,
            "Admin, please use the admin options.",
            reply_markup=build_admin_keyboard()
        )
        return # Stop processing this message further for admin

    # If it's a regular user sending text or if admin is sending text but not in broadcast mode,
    # or if the message content type is not intended for media processing, handle as normal text
    if message.content_type == 'text':
        # You might want to add a default response for regular user text messages here
        bot.send_message(chat_id, "I can only process voice, audio, video, or document files for transcription. Please send one of those, or use /language to change your language settings.")
        return


    # Original media handling logic for non-admin users or if content_type is media
    # Retrieve user language from MongoDB (and update in-memory cache)
    user_data = users_collection.find_one({"chat_id": chat_id})
    if user_data and "language" in user_data:
        user_lang[chat_id] = user_data["language"] # Update in-memory cache
    else:
        # If no language is found, prompt user to select one
        bot.send_message(chat_id, "❗ Please select a language first using /language before sending a file.")
        return

    lang_code = user_lang[chat_id]

    try:
        bot.send_chat_action(chat_id, "typing")
        processing_msg = bot.reply_to(message, "⏳ Processing...") # Reply to the original message

        # Ensure we get the file_id correctly for different media types
        file_id = None
        if message.voice:
            file_id = message.voice.file_id
        elif message.audio:
            file_id = message.audio.file_id
        elif message.video:
            file_id = message.video.file_id
        elif message.document:
            file_id = message.document.file_id
        
        if not file_id:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message, "Unsupported file type. Please send a voice, audio, video, or document file.") # Reply to the original message
            return

        file_info = bot.get_file(file_id)
        if file_info.file_size > 20 * 1024 * 1024:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message, "⚠️ File is too large. Max size is 20MB.") # Reply to the original message
            return

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        file_data = requests.get(file_url).content

        upload_res = requests.post(
            "https://api.assemblyai.com/v2/upload",
            headers={"authorization": ASSEMBLYAI_API_KEY},
            data=file_data
        )
        audio_url = upload_res.json().get('upload_url')
        if not audio_url:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message, "❌ Failed to upload file.") # Reply to the original message
            return

        transcript_res = requests.post(
            "https://api.assemblyai.com/v2/transcript",
            headers={
                "authorization": ASSEMBLYAI_API_KEY,
                "content-type": "application/json"
            },
            json={
                "audio_url": audio_url,
                "language_code": lang_code,
                "speech_model": "best"
            }
        )

        res_json = transcript_res.json()
        transcript_id = res_json.get("id")
        if not transcript_id:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message, f"❌ Transcription error: {res_json.get('error', 'Unknown')}") # Reply to the original message
            return

        polling_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
        while True:
            res = requests.get(polling_url, headers={"authorization": ASSEMBLYAI_API_KEY}).json()
            if res['status'] in ['completed', 'error']:
                break
            time.sleep(2)

        bot.delete_message(chat_id, processing_msg.message_id)

        if res['status'] == 'completed':
            text = res.get("text", "")
            if not text:
                bot.reply_to(message, "ℹ️ No transcription text was returned.") # Reply to the original message
            elif len(text) <= 4000:
                bot.reply_to(message, text) # Reply to the original message
            else:
                # Create a file-like object in memory instead of writing to disk
                import io
                transcript_file = io.BytesIO(text.encode("utf-8"))
                transcript_file.name = "transcript.txt" # Set a filename
                bot.reply_to(message, "Natiijada qoraalka ayaa ka dheer 4000 oo xaraf, halkan ka degso fayl ahaan:", document=transcript_file) # Reply to the original message and send as document
        else:
            bot.reply_to(message, "❌ Sorry, transcription failed.") # Reply to the original message

    except Exception as e:
        logging.error(f"Error handling media: {e}")
        bot.reply_to(message, f"⚠️ An error occurred: {str(e)}") # Reply to the original message


# ======= Webhook Routes =======

@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method in ("GET", "HEAD"):
        return "OK", 200
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if content_type and content_type.startswith("application/json"):
            update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
            bot.process_new_updates([update])
            return "", 200
    return abort(403)

@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook_route():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        return f"Failed to set webhook: {e}", 500

@app.route("/delete_webhook", methods=["GET", "POST"])
def delete_webhook_route():
    try:
        bot.delete_webhook()
        return "Webhook deleted.", 200
    except Exception as e:
        logging.error(f"Failed to delete webhook: {e}")
        return f"Failed to delete webhook: {e}", 500

def set_webhook_on_startup():
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set successfully to {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Failed to set webhook on startup: {e}")

def connect_to_mongodb():
    """
    Function to explicitly handle MongoDB connection attempts and ensure it's established.
    """
    global client, db, users_collection
    try:
        client = MongoClient(MONGO_URI)
        # The ismaster command is cheap and does not require auth.
        # It lets us check if the MongoDB server is available.
        client.admin.command('ismaster')
        db = client["telegram_bot_db"]
        users_collection = db["users"]
        logging.info("Connected to MongoDB successfully!")
    except Exception as e:
        logging.error(f"Could not connect to MongoDB: {e}")
        # Depending on your deployment, you might want to exit or retry here.
        # For a simple web app, letting it crash might be acceptable if it can be restarted.
        # For this example, we'll just log and let it potentially proceed with limited functionality.

def set_bot_info_and_startup():
    connect_to_mongodb()
    set_webhook_on_startup()

if __name__ == "__main__":
    # Ensure MongoDB connection is attempted before starting the Flask app
    set_bot_info_and_startup() 
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
