import logging
import requests
import time
import os
from flask import Flask, request, abort
from pymongo import MongoClient
import telebot
from telebot import types

# ======= API Tokens & Config =======
BOT_TOKEN = "7770743573:AAHHnK_Ameb8GkqgvK3LQUp3l0dN3njecN4"
ASSEMBLYAI_API_KEY = "6dab0a0669624f44afa50d679242e473"
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com/"
ADMIN_ID = 5978150981 # Your specified admin ID

# ======= MongoDB Setup =======
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7s.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(MONGO_URI)
db = client["telegram_bot_db"]
users_collection = db["users"]

# ======= In-memory User Lang Map & Admin State =======
user_lang = {}
admin_broadcast_state = {}

# ======= Premium / Stars Config =======
# Define premium tiers with associated stars and usage limits (in seconds, for example)
# For simplicity, let's say 1 star = 1 minute of transcription time for now.
# We'll use file count as the limit for this example, as mentioned in your request "30 daqiiqo files isku dae koodu gaarayo"
PREMIUM_TIERS = {
    "100_stars": {"stars": 100, "duration_limit_minutes": 30, "price_display": "3€ 🚀"},
    "150_stars": {"stars": 150, "duration_limit_minutes": 60, "price_display": "5€ 😎"},
    "250_stars": {"stars": 250, "duration_limit_minutes": 120, "price_display": "7€ 👑"},
}
FREE_TRANSCRIPT_LIMIT_MINUTES = 30 # User gets 30 minutes of free transcription time

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

def build_stars_purchase_keyboard(user_balance=0):
    markup = types.InlineKeyboardMarkup()
    for tier_id, tier_info in PREMIUM_TIERS.items():
        button_text = f"{tier_info['stars']} ⭐ - {tier_info['price_display']}"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"buy_stars_{tier_id}"))
    markup.row(types.InlineKeyboardButton(f"Your Balance: {user_balance} ⭐", callback_data="balance_info")) # Display balance
    return markup

def build_confirm_purchase_keyboard(tier_id, stars_to_buy):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"Confirm and Pay ⭐ {stars_to_buy}", callback_data=f"confirm_purchase_{tier_id}"))
    markup.add(types.InlineKeyboardButton("Cancel", callback_data="cancel_purchase"))
    return markup

# ======= App Init =======
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# Helper to get or create user document
def get_user_data(chat_id):
    user_data = users_collection.find_one({"chat_id": chat_id})
    if not user_data:
        # Initialize with free limit and 0 stars
        user_data = {
            "chat_id": chat_id,
            "language": None,
            "transcribed_minutes": 0,
            "stars_balance": 0
        }
        users_collection.insert_one(user_data)
    return user_data

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
        # Ensure user data exists and is initialized
        get_user_data(chat_id)
        bot.send_message(
            chat_id,
            "👋 Welcome! Choose your Media (Voice, Audio, Video) file language using the buttons below:",
            reply_markup=build_language_keyboard()
        )

# ======= Save Lang Choice =======
@bot.message_handler(func=lambda msg: msg.text in LANGUAGES)
def save_user_language(message):
    chat_id = message.chat.id
    if chat_id != ADMIN_ID:
        lang_code = LANGUAGES[message.text]
        user_lang[chat_id] = lang_code

        users_collection.update_one(
            {"chat_id": chat_id},
            {"$set": {"language": lang_code}},
            upsert=True
        )

        bot.send_message(
            chat_id,
            f"✅ Transcription Language Set: {message.text}\n\n"
            "🎙️ Please send your voice message, audio file, or video note, and I’ll transcribe it for you with precision.\n\n"
            "📁 Supported file size: Up to 20MB\n\n"
            "📞 Need help? Contact: @Zack_3d"
        )
    else:
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
    del admin_broadcast_state[message.chat.id]
    
    bot.send_message(message.chat.id, "Broadcasting your message now...")
    
    all_users_chat_ids = users_collection.distinct("chat_id")

    sent_count = 0
    failed_count = 0

    for user_chat_id in all_users_chat_ids:
        if user_chat_id == ADMIN_ID:
            continue

        try:
            bot.copy_message(user_chat_id, message.chat.id, message.message_id)
            sent_count += 1
            time.sleep(0.1)

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
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document', 'text'])
def handle_media_and_text(message):
    chat_id = message.chat.id

    if chat_id == ADMIN_ID and not admin_broadcast_state.get(chat_id, False):
        bot.send_message(
            chat_id,
            "Admin, please use the admin options.",
            reply_markup=build_admin_keyboard()
        )
        return

    if message.content_type == 'text':
        bot.send_message(chat_id, "I can only process voice, audio, video, or document files for transcription. Please send one of those, or use /language to change your language settings.")
        return

    user_data = get_user_data(chat_id)
    if not user_data.get("language"):
        bot.send_message(chat_id, "❗ Please select a language first using /language before sending a file.")
        return

    lang_code = user_data["language"]

    # Check transcription usage
    # We will approximate duration by file size for now or assume a fixed duration for each file if not easily obtainable from Telegram API
    # For a more accurate approach, you'd need to extract audio duration from the file metadata which can be complex.
    # Let's assume each file counts as 'X' minutes for the free tier, or that we track actual audio minutes.
    # For this example, let's track 'transcribed_minutes' based on estimated duration or file count.
    
    # Get file duration (if available) - Telegram provides duration for voice and audio messages
    duration_seconds = 0
    if message.voice and message.voice.duration:
        duration_seconds = message.voice.duration
    elif message.audio and message.audio.duration:
        duration_seconds = message.audio.duration
    elif message.video and message.video.duration:
        duration_seconds = message.video.duration

    estimated_minutes = duration_seconds / 60 if duration_seconds > 0 else 1 # Assume 1 minute if duration is not available (e.g., for documents)

    current_transcribed_minutes = user_data.get("transcribed_minutes", 0)
    current_stars_balance = user_data.get("stars_balance", 0)

    # Check if user has exceeded free limit and doesn't have enough stars
    if current_transcribed_minutes + estimated_minutes > FREE_TRANSCRIPT_LIMIT_MINUTES and current_stars_balance < estimated_minutes:
        bot.reply_to(message, 
                     f"Ops! Waxaad gaartay xadkaaga bilaashka ah ({FREE_TRANSCRIPT_LIMIT_MINUTES} daqiiqo). Si aad u sii wadato isticmaalka, fadlan iibso Stars. Hada waxaad isticmaashay: {current_transcribed_minutes:.2f} daqiiqo.")
        bot.send_message(chat_id, "Fadlan dooro xaddiga Stars-ka ee aad rabto inaad iibsato:", reply_markup=build_stars_purchase_keyboard(current_stars_balance))
        return

    try:
        bot.send_chat_action(chat_id, "typing")
        processing_msg = bot.reply_to(message, "⏳ Processing...")

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
            bot.reply_to(message, "Unsupported file type. Please send a voice, audio, video, or document file.")
            return

        file_info = bot.get_file(file_id)
        if file_info.file_size > 20 * 1024 * 1024:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message, "⚠️ File is too large. Max size is 20MB.")
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
            bot.reply_to(message, "❌ Failed to upload file.")
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
            bot.reply_to(message, f"❌ Transcription error: {res_json.get('error', 'Unknown')}")
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
                bot.reply_to(message, "ℹ️ No transcription text was returned.")
            elif len(text) <= 4000:
                bot.reply_to(message, text)
            else:
                import io
                transcript_file = io.BytesIO(text.encode("utf-8"))
                transcript_file.name = "transcript.txt"
                bot.reply_to(message, "Natiijada qoraalka ayaa ka dheer 4000 oo xaraf, halkan ka degso fayl ahaan:", document=transcript_file)
            
            # Update user's transcribed minutes after successful transcription
            users_collection.update_one(
                {"chat_id": chat_id},
                {"$inc": {"transcribed_minutes": estimated_minutes}}
            )
            # If stars were used, decrement them
            if current_transcribed_minutes + estimated_minutes > FREE_TRANSCRIPT_LIMIT_MINUTES:
                stars_to_deduct = estimated_minutes # Assuming 1 star per minute
                users_collection.update_one(
                    {"chat_id": chat_id},
                    {"$inc": {"stars_balance": -stars_to_deduct}} # Deduct stars
                )
                bot.send_message(chat_id, f"Isticmaalkaaga hadda waxuu gaaray {current_transcribed_minutes + estimated_minutes:.2f} daqiiqo. Waxaa kaa go'ay {stars_to_deduct} ⭐. Haraagaaga Stars: {current_stars_balance - stars_to_deduct} ⭐.")

        else:
            bot.reply_to(message, "❌ Sorry, transcription failed.")

    except Exception as e:
        logging.error(f"Error handling media: {e}")
        bot.reply_to(message, f"⚠️ An error occurred: {str(e)}")

# ======= Callback Query Handler for Stars Purchase =======
@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_stars_") or call.data.startswith("confirm_purchase_") or call.data == "cancel_purchase" or call.data == "balance_info")
def handle_stars_callback(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    user_data = get_user_data(chat_id)
    current_stars_balance = user_data.get("stars_balance", 0)

    if call.data.startswith("buy_stars_"):
        tier_id = call.data.replace("buy_stars_", "")
        tier_info = PREMIUM_TIERS.get(tier_id)
        if tier_info:
            stars_to_buy = tier_info["stars"]
            price_display = tier_info["price_display"]
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"Do you want to buy Support AudioMessBot in Voice message to text for {stars_to_buy} Stars?\n\nYour Balance: {current_stars_balance} ⭐",
                reply_markup=build_confirm_purchase_keyboard(tier_id, stars_to_buy)
            )
        else:
            bot.answer_callback_query(call.id, "Invalid tier selected.")
    
    elif call.data.startswith("confirm_purchase_"):
        tier_id = call.data.replace("confirm_purchase_", "")
        tier_info = PREMIUM_TIERS.get(tier_id)
        if tier_info:
            stars_to_add = tier_info["stars"]
            # Here you would integrate with your payment gateway (e.g., Stripe, PayGo)
            # For this example, we'll simulate a successful purchase and add stars.
            # In a real scenario, this would be triggered AFTER successful payment.

            # Simulate payment success
            users_collection.update_one(
                {"chat_id": chat_id},
                {"$inc": {"stars_balance": stars_to_add}},
                upsert=True
            )
            updated_user_data = get_user_data(chat_id)
            new_balance = updated_user_data.get("stars_balance", 0)

            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ Purchase successful! You have added {stars_to_add} ⭐.\nYour new balance is {new_balance} ⭐."
            )
            bot.send_message(chat_id, "Waad ku mahadsan tahay taageeradaada! Hadda waxaad sii wadan kartaa isticmaalka.")
        else:
            bot.answer_callback_query(call.id, "Invalid tier for confirmation.")

    elif call.data == "cancel_purchase":
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="Iibsashada waa la joojiyay. Haddii aad beddesho maskaxdaada, waxaad mar kale iibsan kartaa Stars.",
            reply_markup=build_stars_purchase_keyboard(current_stars_balance) # Go back to stars selection
        )
        bot.answer_callback_query(call.id, "Purchase cancelled.")

    elif call.data == "balance_info":
        bot.answer_callback_query(call.id, f"Your current Stars balance: {current_stars_balance} ⭐")


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
    global client, db, users_collection
    try:
        client = MongoClient(MONGO_URI)
        client.admin.command('ismaster')
        db = client["telegram_bot_db"]
        users_collection = db["users"]
        logging.info("Connected to MongoDB successfully!")
    except Exception as e:
        logging.error(f"Could not connect to MongoDB: {e}")

def set_bot_info_and_startup():
    connect_to_mongodb()
    set_webhook_on_startup()

if __name__ == "__main__":
    set_bot_info_and_startup() 
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

