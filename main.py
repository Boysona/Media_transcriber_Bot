import logging
import requests
import time
import os
import io # For handling text files in memory
from flask import Flask, request, abort, jsonify # Added jsonify for backend endpoint
from pymongo import MongoClient
import telebot
from telebot import types

# ======= API Tokens & Config =======
BOT_TOKEN = "7790991731:AAFl7aS2kw4zONbxFi2XzWPRiWBA5T52Pyg"
ASSEMBLYAI_API_KEY = "6dab0a0669624f44afa50d679242e473"
WEBHOOK_URL = "https://media-transcriber-bot-67hc.onrender.com/"
ADMIN_ID = 5978150981 # Your specified admin ID

# Stripe Integration - IMPORTANT: This is for your backend server.
# Bot-ka si toos ah ugama qaban karo lacag bixinta Stripe.
# Waxaad u baahan doontaa server backend gaar ah oo la xiriira Stripe.
# Halkan waxaan ku isticmaalaynaa URL-ka tusaale ahaan.
STRIPE_BACKEND_CREATE_CHECKOUT_URL = "https://your-stripe-backend.com/create-checkout-session"
STRIPE_BACKEND_PAYMENT_SUCCESS_URL = "https://your-bot-url.com/payment-success-webhook" # Tusaale URL webhook-kaaga bot-ka

# ======= MongoDB Setup =======
MONGO_URI = "mongodb-cluster-uri-kaaga" # Hubi in kani sax yahay
client = MongoClient(MONGO_URI)
db = client["telegram_bot_db"]
users_collection = db["users"]

# ======= In-memory User Lang Map & Admin State =======
user_lang = {}
admin_broadcast_state = {}

# ======= Premium / Stars Config =======
# Waxaan ku haynaa qiimaha euros, laakin Stripe wuxuu u baahan yahay 'cents' ama 'smallest unit'
PREMIUM_TIERS = {
    "100_stars": {"stars": 100, "duration_limit_minutes": 30, "price_display": "3€ 🚀", "price_amount_cents": 300, "currency": "EUR"},
    "150_stars": {"stars": 150, "duration_limit_minutes": 60, "price_display": "5€ 😎", "price_amount_cents": 500, "currency": "EUR"},
    "250_stars": {"stars": 250, "duration_limit_minutes": 120, "price_display": "7€ 👑", "price_amount_cents": 700, "currency": "EUR"},
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
    "Suomi 🇫🇮": "fi", "Polski 🇵🇱": "pl",
    "Cestina 🇨🇿": "cs", "Magyar 🇭🇺": "hu", "Română 🇷🇴": "ro", "Melayu 🇲🇾": "ms",
    "O'zbekcha 🇺🇿": "uz", "Tagalog 🇵🇭": "tl", "Português 🇵🇹": "pt", "हिन्दी 🇮🇳": "hi"
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
    markup.row(types.InlineKeyboardButton(f"Your Balance: {user_balance} ⭐", callback_data="balance_info"))
    return markup

# ======= App Init =======
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# Helper to get or create user document
def get_user_data(chat_id):
    user_data = users_collection.find_one({"chat_id": chat_id})
    if not user_data:
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

    if chat_id == ADMIN_ID:
        bot.send_message(
            chat_id,
            "👋 Welcome, Admin! Choose an option:",
            reply_markup=build_admin_keyboard()
        )
    else:
        get_user_data(chat_id) # Ensure user data is initialized
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
        bot.send_message(chat_id, "Waxaan farsamayn karaa oo kaliya codka, maqalka, fiidyowga, ama faylasha dukumentiyada si aan u qoro. Fadlan soo dir mid ka mid ah, ama isticmaal /language si aad u bedesho goobaha luqaddaada.")
        return

    user_data = get_user_data(chat_id)
    if not user_data.get("language"):
        bot.send_message(chat_id, "❗ Fadlan marka hore dooro luqad adigoo isticmaalaya /language ka hor inta aadan soo dirin fayl.")
        return

    lang_code = user_data["language"]

    duration_seconds = 0
    if message.voice and message.voice.duration:
        duration_seconds = message.voice.duration
    elif message.audio and message.audio.duration:
        duration_seconds = message.audio.duration
    elif message.video and message.video.duration:
        duration_seconds = message.video.duration

    # If duration is 0 (e.g., for document), estimate 1 minute to count against usage
    estimated_minutes = duration_seconds / 60 if duration_seconds > 0 else 1 

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
            bot.reply_to(message, "Nooca faylka lama taageero. Fadlan soo dir cod, maqal, muuqaal, ama fayl dukumenti ah.")
            return

        file_info = bot.get_file(file_id)
        if file_info.file_size > 20 * 1024 * 1024:
            bot.delete_message(chat_id, processing_msg.message_id)
            bot.reply_to(message, "⚠️ Faylku wuu weyn yahay. Xaddiga ugu sarreeya waa 20MB.")
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
            bot.reply_to(message, "❌ Ku shubista faylka way guuldarraysatay.")
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
            bot.reply_to(message, f"❌ Khalad ku yimid qoraalka: {res_json.get('error', 'Unknown')}")
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
                bot.reply_to(message, "ℹ️ Ma jiro qoraal qorneed oo soo laabtay.")
            elif len(text) <= 2000: # Xadka cusub ee qoraalka fariinta tooska ah
                bot.reply_to(message, text)
            else:
                # Create a file-like object in memory for .txt file
                transcript_file = io.BytesIO(text.encode("utf-8"))
                transcript_file.name = "transcript.txt" # Set a filename
                bot.reply_to(message, "Natiijada qoraalka ayaa ka dheer 2000 oo xaraf, halkan ka degso fayl ahaan:", document=transcript_file)
            
            # Update user's transcribed minutes after successful transcription
            users_collection.update_one(
                {"chat_id": chat_id},
                {"$inc": {"transcribed_minutes": estimated_minutes}}
            )
            # If stars were used, decrement them
            if current_transcribed_minutes + estimated_minutes > FREE_TRANSCRIPT_LIMIT_MINUTES:
                stars_to_deduct = estimated_minutes # Assuming 1 star per minute
                # Make sure stars_to_deduct does not exceed current_stars_balance if stars were used
                actual_stars_deducted = min(stars_to_deduct, current_stars_balance)
                users_collection.update_one(
                    {"chat_id": chat_id},
                    {"$inc": {"stars_balance": -actual_stars_deducted}}
                )
                bot.send_message(chat_id, f"Isticmaalkaaga hadda waxuu gaaray {current_transcribed_minutes + estimated_minutes:.2f} daqiiqo. Waxaa kaa go'ay {actual_stars_deducted} ⭐. Haraagaaga Stars: {current_stars_balance - actual_stars_deducted} ⭐.")

        else:
            bot.reply_to(message, "❌ Nasiib darro, qoraalkii wuu guuldarraystay.")

    except Exception as e:
        logging.error(f"Error handling media: {e}")
        bot.reply_to(message, f"⚠️ Waxa dhacay khalad: {str(e)}")

# ======= Callback Query Handler for Stars Purchase (Stripe via Backend) =======
@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_stars_") or call.data == "balance_info")
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

            # Halkaan, bot-ku wuxuu dalab u dirayaa server-kaaga backend si uu u abuuro Stripe Checkout Session.
            # Kadib wuxuu user-ka u soo bandhigayaa link-ga lacag bixinta.
            send_payment_link_to_user(chat_id, tier_id, stars_to_buy, price_display, message_id)
            bot.answer_callback_query(call.id, f"Diyaarinaya lacag bixinta {stars_to_buy} ⭐...")
        else:
            bot.answer_callback_query(call.id, "Tier-ka la doortay ma saxna.")
    
    elif call.data == "balance_info":
        bot.answer_callback_query(call.id, f"Haraagaaga Stars ee hadda: {current_stars_balance} ⭐")


def send_payment_link_to_user(chat_id, tier_id, stars_to_buy, price_display, message_id):
    try:
        # Dalab u dir server-kaaga backend si uu u abuuro Stripe Checkout Session
        response = requests.post(
            STRIPE_BACKEND_CREATE_CHECKOUT_URL,
            json={
                "chat_id": chat_id,
                "tier_id": tier_id,
                "stars_to_buy": stars_to_buy,
                "price_display": price_display,
                "success_redirect_url": f"{WEBHOOK_URL}payment-success/{chat_id}/{tier_id}", # URL bot-kaagu kuugu sheegayo guusha
                "cancel_redirect_url": f"{WEBHOOK_URL}payment-cancel/{chat_id}" # URL bot-kaagu kuugu sheegayo joojinta
            }
        )
        response.raise_for_status() # Wixii khalad ah ee HTTP ah ku kicin doona
        checkout_data = response.json()
        checkout_url = checkout_data.get("checkout_url")

        if checkout_url:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(f"Iibso {stars_to_buy} ⭐ ({price_display})", url=checkout_url))
            
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"Fadlan guji badhanka hoose si aad u dhamaystirto iibsigaaga {stars_to_buy} ⭐:\n\n"
                     f"Qiimo: {price_display}",
                reply_markup=markup
            )
        else:
            bot.send_message(chat_id, "Waxa dhacay khalad markii la abuurayay link-ga lacag bixinta. Fadlan isku day hadhow.")
            logging.error(f"Stripe backend did not return checkout_url: {checkout_data}")

    except requests.exceptions.RequestException as e:
        logging.error(f"Error connecting to Stripe backend: {e}")
        bot.send_message(chat_id, "Waxa jirta cilad xagga xiriirka ah ee nidaamka lacag bixinta. Fadlan isku day mar kale.")
    except Exception as e:
        logging.error(f"Unexpected error in send_payment_link_to_user: {e}")
        bot.send_message(chat_id, "Waxa dhacay khalad aan la filayn. Fadlan la xiriir caawimaada.")


# ======= Webhook for Payment Success/Cancel from Backend =======
# Kani waa endpoint-ka bot-kaaga uu ka heli doono fariin server-kaaga backend
# markii lacag bixinta Stripe la dhammaystiro.
@app.route("/payment-success/<int:chat_id>/<string:tier_id>", methods=["GET"])
def handle_payment_success_webhook(chat_id, tier_id):
    try:
        tier_info = PREMIUM_TIERS.get(tier_id)
        if not tier_info:
            logging.error(f"Payment success webhook: Invalid tier_id {tier_id} for chat_id {chat_id}")
            return "Invalid Tier ID", 400

        stars_to_add = tier_info["stars"]
        
        # Ku dar Stars-ka user-ka MongoDB
        users_collection.update_one(
            {"chat_id": chat_id},
            {"$inc": {"stars_balance": stars_to_add}},
            upsert=True
        )
        updated_user_data = get_user_data(chat_id)
        new_balance = updated_user_data.get("stars_balance", 0)

        bot.send_message(chat_id, 
                         f"✅ Hambalyo! Lacag bixintaada waa la aqbalay. Waxaad heshay {stars_to_add} ⭐.\n"
                         f"Haraagaaga cusub waa {new_balance} ⭐.\n"
                         "Waad ku mahadsan tahay taageeradaada! Hadda waxaad sii wadan kartaa adeegga bot-ka.")
        
        # Waxaad dib ugu celin kartaa user-ka bogga guusha, ama bixin kartaa fariin xaqiijin ah.
        return "Payment Successful! You can return to Telegram.", 200

    except Exception as e:
        logging.error(f"Error in payment success webhook for user {chat_id}: {e}")
        return "Internal Server Error", 500

@app.route("/payment-cancel/<int:chat_id>", methods=["GET"])
def handle_payment_cancel_webhook(chat_id):
    try:
        bot.send_message(chat_id, "Iibsashada Stars waa la joojiyay. Haddii aad beddesho maskaxdaada, waxaad mar kale iibsan kartaa.")
        return "Payment Cancelled. You can return to Telegram.", 200
    except Exception as e:
        logging.error(f"Error in payment cancel webhook for user {chat_id}: {e}")
        return "Internal Server Error", 500

# ======= Webhook Routes (Existing) =======
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

