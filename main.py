import os
import re
import uuid
import shutil
import logging
import requests
import telebot
import json
import yt_dlp
from flask import Flask, request, abort
from faster_whisper import WhisperModel
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup

# ---------- CONFIGURATION ----------
# Telegram token and admin
TOKEN = os.getenv("BOT_TOKEN", "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5978150981"))

# Directories & files
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
USERS_JSON = 'users.json'
USER_LANG_JSON = 'user_language_settings.json'

# Whisper & Gemini
WHISPER_MODEL = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_KEY")

# File size limit (20MB)
FILE_SIZE_LIMIT = 20 * 1024 * 1024

# Initialize
bot = telebot.TeleBot(TOKEN, threaded=True)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ---------- STORAGE ----------
# Load or init user data
if os.path.exists(USERS_JSON):
    with open(USERS_JSON, 'r') as f:
        try: user_data = json.load(f)
        except: user_data = {}
else:
    user_data = {}

if os.path.exists(USER_LANG_JSON):
    with open(USER_LANG_JSON, 'r') as f:
        try: user_language_settings = json.load(f)
        except: user_language_settings = {}
else:
    user_language_settings = {}

user_memory = {}            # For Gemini chat context
user_transcriptions = {}    # {user_id: {message_id: transcription}}
admin_state = {}            # For broadcast flow

# ---------- HELPERS ----------
def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_json(USERS_JSON, user_data)

def ask_gemini(user_id, prompt_text):
    user_memory.setdefault(user_id, []).append({"role":"user","text":prompt_text})
    history = user_memory[user_id][-10:]
    parts = [{"text":m["text"]} for m in history]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}")
    try:
        resp = requests.post(url, json={"contents":[{"parts":parts}]},
                             headers={"Content-Type":"application/json"})
        resp.raise_for_status()
        data = resp.json()
        reply = data["candidates"][0]["content"]["parts"][0]["text"]
        user_memory[user_id].append({"role":"model","text":reply})
        return reply
    except Exception as e:
        logging.error("Gemini error: %s", e)
        return f"Error: {e}"

def transcribe_file(path):
    try:
        segments, _ = WHISPER_MODEL.transcribe(path, beam_size=1)
        return " ".join(seg.text for seg in segments)
    except Exception as e:
        logging.error("Whisper error: %s", e)
        return ""

# Generate inline language keyboard
LANGUAGES = ["English","Somali","Arabic","French","Spanish","German","Chinese"]
def gen_lang_kb(prefix, msg_id=None):
    kb = InlineKeyboardMarkup(row_width=3)
    for lang in LANGUAGES:
        cb = f"{prefix}|{lang}"
        if msg_id: cb += f"|{msg_id}"
        kb.add(InlineKeyboardButton(lang, callback_data=cb))
    return kb

def is_supported_url(url):
    platforms = ['tiktok.com','youtube.com','youtu.be','instagram.com',
                 'pinterest.com','facebook.com','x.com','twitter.com']
    return any(p in url.lower() for p in platforms)

# ---------- BOT COMMANDS ----------
@bot.message_handler(commands=['start'])
def start_cmd(msg):
    uid = msg.from_user.id
    update_user_activity(uid)
    # Save new user
    if str(uid) not in user_data:
        user_data[str(uid)] = datetime.now().isoformat()
        save_json(USERS_JSON, user_data)
    name = msg.from_user.first_name
    text = (f"👋 Hi {name}! I can:\n"
            "- 🎙️ Transcribe voice/video/audio\n"
            "- 🌐 Translate & Summarize text\n"
            "- ▶️ Download media from links\n"
            "Send me a file or a supported link to get started.\n"
            "/help for details.")
    bot.send_message(uid, text)
    # Admin panel
    if uid == ADMIN_ID:
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📊 Stats","📢 Broadcast")
        bot.send_message(uid, "Admin Panel:", reply_markup=markup)

@bot.message_handler(commands=['help'])
def help_cmd(msg):
    uid = msg.chat.id
    help_text = (
        "ℹ️ *How to use this bot:*\n\n"
        "1️⃣ Send voice message, audio, or video → I transcribe it.\n"
        "2️⃣ After transcription, tap *Translate* or *Summarize*.\n"
        "3️⃣ Send a TikTok/YouTube/Instagram/etc link → I’ll download it.\n\n"
        "Commands:\n"
        "/start - Restart bot\n"
        "/status - View stats\n"
        "/language - Set default translate/summarize language\n"
        "/privacy - Privacy policy"
    )
    bot.send_message(uid, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy_cmd(msg):
    uid = msg.chat.id
    privacy = (
        "🔒 *Privacy Notice*\n"
        "- Media files are processed & deleted immediately.\n"
        "- Transcriptions kept only in memory.\n"
        "- User IDs & language prefs stored.\n"
        "- No third‑party sharing.\n"
        "Contact admin for data removal."
    )
    bot.send_message(uid, privacy, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status_cmd(msg):
    uid = msg.from_user.id
    update_user_activity(uid)
    total_users = len(user_data)
    text = f"📊 *Stats*\n• Total users: {total_users}"
    bot.send_message(uid, text, parse_mode="Markdown")

@bot.message_handler(commands=['language'])
def language_cmd(msg):
    uid = msg.from_user.id
    update_user_activity(uid)
    bot.send_message(uid, "Select your default language:", reply_markup=gen_lang_kb("setlang"))

# ---------- ADMIN TEXT HANDLERS ----------
@bot.message_handler(func=lambda m: m.text=="📢 Broadcast" and m.from_user.id==ADMIN_ID)
def ask_broadcast(msg):
    admin_state[ADMIN_ID] = 'awaiting_broadcast'
    bot.send_message(ADMIN_ID, "Send broadcast content now:")

@bot.message_handler(func=lambda m: m.from_user.id==ADMIN_ID and admin_state.get(ADMIN_ID)=='awaiting_broadcast',
                     content_types=['text','photo','video','audio','document'])
def do_broadcast(msg):
    admin_state[ADMIN_ID] = None
    sent = 0
    for uid in user_data:
        try:
            bot.copy_message(int(uid), msg.chat.id, msg.message_id)
            sent +=1
        except:
            pass
    bot.send_message(ADMIN_ID, f"Broadcast sent to {sent} users.")

# ---------- FILE HANDLER: TRANSCRIBE ----------
@bot.message_handler(content_types=['voice','audio','video','video_note'])
def handle_media(msg):
    uid = msg.from_user.id
    update_user_activity(uid)

    file_obj = msg.voice or msg.audio or msg.video or msg.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(msg.chat.id, "File too large (max 20MB).")

    info = bot.get_file(file_obj.file_id)
    path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
    data = bot.download_file(info.file_path)
    with open(path,'wb') as f: f.write(data)

    proc_msg = bot.send_message(msg.chat.id, "Transcribing... ⏳")
    text = transcribe_file(path)
    os.remove(path)

    # Save transcription
    user_transcriptions.setdefault(str(uid), {})[msg.message_id] = text

    # Reply with text or file
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Translate", callback_data=f"translate|{msg.message_id}"),
           InlineKeyboardButton("Summarize", callback_data=f"summarize|{msg.message_id}"))
    if len(text) > 4000:
        fname = "transcription.txt"
        with open(fname,'w',encoding='utf-8') as f: f.write(text)
        bot.send_document(msg.chat.id, open(fname,'rb'),
                          caption="Your transcription:", reply_markup=kb)
        os.remove(fname)
    else:
        bot.send_message(msg.chat.id, text, reply_markup=kb)
    bot.delete_message(msg.chat.id, proc_msg.message_id)

# ---------- CALLBACKS: TRANSLATE & SUMMARIZE ----------
@bot.callback_query_handler(func=lambda c: c.data.startswith(("translate","summarize")))
def cb_translate_summarize(call):
    uid = str(call.from_user.id)
    action, msg_id = call.data.split("|")
    msg_id = int(msg_id)
    original = user_transcriptions.get(uid, {}).get(msg_id)
    if not original:
        return bot.answer_callback_query(call.id, "No transcription found.")
    pref = user_language_settings.get(uid)
    if not pref:
        # ask language
        prefix = "translate_to" if action=="translate" else "summarize_in"
        bot.edit_message_text("Choose language:", call.message.chat.id,
                              call.message.message_id, reply_markup=gen_lang_kb(prefix, msg_id))
        return

    bot.answer_callback_query(call.id, f"{action.title()} in {pref}...")
    prompt = (f"Translate to {pref}:\n\n{original}"
              if action=="translate"
              else f"Summarize in {pref}:\n\n{original}")
    result = ask_gemini(call.from_user.id, prompt)

    # send result
    if len(result) > 4000:
        fn = f"{action}.txt"
        with open(fn,'w',encoding='utf-8') as f: f.write(result)
        bot.send_document(call.message.chat.id, open(fn,'rb'),
                          caption=f"{action.title()} result")
        os.remove(fn)
    else:
        bot.send_message(call.message.chat.id, result, reply_to_message_id=msg_id)

# Handle chosen language for translate/summarize
@bot.callback_query_handler(func=lambda c: c.data.startswith(("translate_to","summarize_in","setlang")))
def cb_set_lang(call):
    parts = call.data.split("|")
    prefix = parts[0]
    lang = parts[1]
    msg_id = int(parts[2]) if len(parts)==3 else None
    uid = str(call.from_user.id)
    user_language_settings[uid] = lang
    save_json(USER_LANG_JSON, user_language_settings)

    if prefix=="setlang":
        bot.edit_message_text(f"Default language set to *{lang}*", call.message.chat.id,
                              call.message.message_id, parse_mode="Markdown")
    else:
        act = "Translating" if prefix=="translate_to" else "Summarizing"
        bot.edit_message_text(f"{act} in *{lang}*...", call.message.chat.id,
                              call.message.message_id, parse_mode="Markdown")
        # trigger action
        fake_call = telebot.types.CallbackQuery(
            id=call.id, from_user=call.from_user,
            message=call.message,
            data=f"{'translate' if 'translate' in prefix else 'summarize'}|{msg_id}"
        )
        cb_translate_summarize(fake_call)
    bot.answer_callback_query(call.id)

# ---------- LINK HANDLER: DOWNLOAD MEDIA ----------
@bot.message_handler(func=lambda m: is_supported_url(m.text))
def handle_link(msg):
    uid = msg.chat.id
    update_user_activity(uid)

    icon = bot.send_message(uid, "👀")
    url = msg.text

    if 'youtu' in url.lower():
        _handle_youtube(msg, icon)
    else:
        _handle_generic_video(msg, icon)

def _handle_youtube(msg, icon_msg):
    url = msg.text
    try:
        ydl_opts = {'quiet':True,'no_warnings':True,'skip_download':True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = {f['height']:f['format_id'] for f in info['formats']
                   if f.get('vcodec')!='none' and f.get('height')<=720}
        kb = InlineKeyboardMarkup(row_width=3)
        for h,fid in sorted(formats.items()):
            vid = str(uuid.uuid4())[:8]
            bot.video_info = getattr(bot, 'video_info', {})
            bot.video_info[vid] = {'url':url,'format':fid}
            kb.add(InlineKeyboardButton(f"{h}p", callback_data=f"dlyt|{vid}"))
        bot.send_message(msg.chat.id, f"Choose quality for {info.get('title','video')}", reply_markup=kb)
    except Exception as e:
        bot.send_message(msg.chat.id, f"Error: {e}")
    finally:
        bot.delete_message(msg.chat.id, icon_msg.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dlyt"))
def cb_dlyt(call):
    vid = call.data.split("|")[1]
    data = getattr(bot, 'video_info', {}).get(vid)
    if not data:
        return bot.answer_callback_query(call.id, "Expired, send link again.")
    bot.answer_callback_query(call.id, "Downloading...")
    outpath = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")
    ydl_opts = {'format':data['format'],'outtmpl':outpath,'quiet':True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([data['url']])
    with open(outpath,'rb') as f:
        bot.send_video(call.message.chat.id, f)
    os.remove(outpath)

def _handle_generic_video(msg, icon_msg):
    url = msg.text
    path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp4")
    try:
        ydl_opts = {'format':'best','outtmpl':path,'quiet':True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        bot.send_video(msg.chat.id, open(path,'rb'))
    except Exception as e:
        bot.send_message(msg.chat.id, f"Error: {e}")
    finally:
        bot.delete_message(msg.chat.id, icon_msg.message_id)
        if os.path.exists(path): os.remove(path)

# ---------- FALLBACK ----------
@bot.message_handler(func=lambda m: True, content_types=['text','sticker','document','photo'])
def fallback(msg):
    update_user_activity(msg.from_user.id)
    bot.send_message(msg.chat.id, "Send only voice/audio/video or a supported link.")

# ---------- WEBHOOK ----------
@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type')=='application/json':
        update = telebot.types.Update.de_json(request.get_data().decode(), bot)
        bot.process_new_updates([update])
        return '',200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook():
    bot.delete_webhook()
    bot.set_webhook(url=os.getenv("WEBHOOK_URL"))
    return "Webhook set",200

@app.route('/delete_webhook', methods=['GET','POST'])
def del_webhook():
    bot.delete_webhook()
    return "Webhook deleted",200

if __name__=="__main__":
    # Clean downloads on startup
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    # Start Flask
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
