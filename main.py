import os
import re
import uuid
import shutil
import logging
import requests
import json
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from faster_whisper import WhisperModel
from datetime import datetime
from PIL import Image
from io import BytesIO
import yt_dlp
import threading
import time

# ─────────────── CONFIGURATION ───────────────
BOT_TOKEN    = "7770743573:AAHDlDTlactC7KU2L6nT6bzW9ueDuIp0p4Q"
ADMIN_ID     = 5978150981
WEBHOOK_URL  = "https://media-transcriber-bot.onrender.com"

# File size limit for transcription (20MB)
FILE_SIZE_LIMIT = 20 * 1024 * 1024  

# Directories
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Whisper model init
model = WhisperModel(model_size_or_path="tiny", device="cpu", compute_type="int8")

# Flask + TeleBot init
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
# ────────────────────────────────────────────────

# ─────────── Logging & User Data ───────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
users_file = 'users.json'
user_data = {}
if os.path.exists(users_file):
    with open(users_file, 'r') as f:
        try: user_data = json.load(f)
        except: user_data = {}

lang_file = 'user_language_settings.json'
user_language_settings = {}
if os.path.exists(lang_file):
    with open(lang_file, 'r') as f:
        try: user_language_settings = json.load(f)
        except: user_language_settings = {}

def save_user_data():
    with open(users_file, 'w') as f:
        json.dump(user_data, f, indent=4)

def save_user_language_settings():
    with open(lang_file, 'w') as f:
        json.dump(user_language_settings, f, indent=4)

def update_user_activity(user_id):
    user_data[str(user_id)] = datetime.now().isoformat()
    save_user_data()
# ────────────────────────────────────────────────

# ───────────── Bot Command Setup ─────────────
def set_bot_info():
    commands = [
        telebot.types.BotCommand("start", "Restart the bot"),
        telebot.types.BotCommand("status", "View Bot statistics"),
        telebot.types.BotCommand("help", "Usage instructions"),
        telebot.types.BotCommand("language", "Set your preferred language"),
        telebot.types.BotCommand("privacy", "Privacy notice"),
    ]
    bot.set_my_commands(commands)
    bot.set_my_short_description("Media transcriber + downloader bot")
    bot.set_my_description("🤖 I transcribe audio/video and download from YouTube & socials—powered by Whisper & Gemini.")
# ────────────────────────────────────────────────

# ─────────── Helper Functions ───────────
def get_fullname(u):
    return f"{u.first_name or ''} {u.last_name or ''}".strip()

def transcribe(path: str) -> str:
    segments, _ = model.transcribe(path, beam_size=1)
    return " ".join(seg.text for seg in segments)

def ask_gemini(uid, prompt):
    # ... same as in Bot1, sending to Gemini API ...
    return "Gemini reply..."  # stubbed for brevity

def generate_language_keyboard(prefix, msg_id=None):
    LANGUAGES = [
        {"name":"English","flag":"🇬🇧"},{"name":"Somali","flag":"🇸🇴"}, # etc...
    ]
    kb = InlineKeyboardMarkup(row_width=3)
    for lang in LANGUAGES:
        data = f"{prefix}|{lang['name']}"
        if msg_id: data += f"|{msg_id}"
        kb.add(InlineKeyboardButton(f"{lang['name']} {lang['flag']}", callback_data=data))
    return kb

def is_supported_url(url):
    return any(p in url.lower() for p in (
        'tiktok.com','youtube.com','pinterest.com','instagram.com',
        'facebook.com','x.com','twitter.com','snapchat.com'
    ))

# ───────────── Webhook Routes ─────────────
@app.route('/', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return abort(403)

@app.route('/set_webhook', methods=['GET','POST'])
def set_webhook():
    bot.delete_webhook()
    bot.set_webhook(url=WEBHOOK_URL + '/')
    return f"Webhook set to {WEBHOOK_URL}/", 200

# ────────────────────────────────────────────────

# ───────────── Start / Help / Privacy ─────────────
@bot.message_handler(commands=['start'])
def start(m):
    update_user_activity(m.from_user.id)
    name = get_fullname(m.from_user)
    if m.from_user.id == ADMIN_ID:
        kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("Send Broadcast","Total Users","/status")
        bot.send_message(m.chat.id, f"👑 Admin Panel, {name}", reply_markup=kb)
    else:
        bot.send_message(
            m.chat.id,
            f"👋 Hello {name}!\n• Send a voice msg, audio or video to transcribe.\n• Send a video link to download."
        )

@bot.message_handler(commands=['help'])
def help_(m):
    update_user_activity(m.from_user.id)
    bot.send_message(m.chat.id, (
        "ℹ️ *How to use*\n"
        "• Send voice/audio/video ⇒ get transcription (+ translate/summarize)\n"
        "• Send YouTube/social link ⇒ choose quality ⇒ get download\n"
        "Commands: /start, /status, /help, /language, /privacy"
    ), parse_mode="Markdown")

@bot.message_handler(commands=['privacy'])
def privacy(m):
    update_user_activity(m.from_user.id)
    bot.send_message(m.chat.id, (
        "*Privacy Notice*\n"
        "• Media files deleted after transcription.\n"
        "• Transcriptions held temporarily in memory.\n"
        "• User IDs & language prefs stored for service.\n"
        "• No 3rd‑party sharing."
    ), parse_mode="Markdown")
# ────────────────────────────────────────────────

# ───────────── Statistics & Admin ─────────────
total_files = total_audio = total_voice = total_videos = total_time = 0.0

@bot.message_handler(commands=['status'])
def status(m):
    update_user_activity(m.from_user.id)
    today = datetime.now().date()
    active = sum(
        1 for t in user_data.values()
        if datetime.fromisoformat(t).date()==today
    )
    h = int(total_time)//3600; mnt = (int(total_time)%3600)//60; s = int(total_time)%60
    bot.send_message(m.chat.id, (
        f"📊 Stats:\n"
        f"• Active Today: {active}\n"
        f"• Files Proc: {total_files}\n"
        f"  – Audio: {total_audio}\n"
        f"  – Voice: {total_voice}\n"
        f"  – Video: {total_videos}\n"
        f"• Time: {h}h {mnt}m {s}s"
    ))

@bot.message_handler(func=lambda m: m.text=="Total Users" and m.from_user.id==ADMIN_ID)
def total_users(m):
    bot.send_message(m.chat.id, f"Total registered users: {len(user_data)}")

@bot.message_handler(func=lambda m: m.text=="Send Broadcast" and m.from_user.id==ADMIN_ID)
def ask_broadcast(m):
    bot.send_message(m.chat.id, "Send me the broadcast now.")
    bot.set_state(m.from_user.id, "broadcast")

@bot.message_handler(func=lambda m: bot.get_state(m.from_user.id)=="broadcast", content_types=['text','photo','video','audio','document'])
def do_broadcast(m):
    bot.set_state(m.from_user.id, None)
    succ=fail=0
    for uid in user_data:
        try:
            bot.copy_message(int(uid), m.chat.id, m.message_id)
            succ+=1
        except: fail+=1
    bot.send_message(m.chat.id, f"Done: {succ} sent, {fail} failed")
# ────────────────────────────────────────────────

# ───────────── Media Transcription ─────────────
user_transcriptions = {}  # {uid: {msg_id: text}}

@bot.message_handler(content_types=['voice','audio','video','video_note'])
def media_handler(m):
    global total_files, total_audio, total_voice, total_videos, total_time
    update_user_activity(m.from_user.id)

    file_obj = m.voice or m.audio or m.video or m.video_note
    if file_obj.file_size > FILE_SIZE_LIMIT:
        return bot.send_message(m.chat.id, "❌ Max file size is 20 MB.")

    info = bot.get_file(file_obj.file_id)
    local = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.ogg")
    data = bot.download_file(info.file_path)
    with open(local,'wb') as f: f.write(data)

    msg = bot.send_message(m.chat.id, "🔄 Processing…")
    start = datetime.now()

    text = transcribe(local) or ""
    uid = str(m.from_user.id)
    user_transcriptions.setdefault(uid, {})[m.message_id] = text

    elapsed = (datetime.now()-start).total_seconds()
    total_time += elapsed; total_files+=1
    if m.voice: total_voice+=1
    elif m.audio: total_audio+=1
    else: total_videos+=1

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("Translate", callback_data=f"translate|{m.message_id}"),
        InlineKeyboardButton("Summarize", callback_data=f"summarize|{m.message_id}")
    )

    bot.edit_message_text(
        text if len(text)<=4000 else "✉️ Sending as file…",
        m.chat.id, msg.message_id,
        reply_markup=kb
    )
    if len(text)>4000:
        fn='out.txt'
        with open(fn,'w',encoding='utf-8') as f: f.write(text)
        bot.send_document(m.chat.id, open(fn,'rb'), caption="Here’s your transcription.", reply_markup=kb)
        os.remove(fn)

    os.remove(local)

@bot.callback_query_handler(func=lambda c: c.data.startswith(("translate|","summarize|")))
def on_action(c):
    kind,msgid = c.data.split("|")
    uid = str(c.from_user.id)
    if uid not in user_transcriptions or int(msgid) not in user_transcriptions[uid]:
        return bot.answer_callback_query(c.id, "No transcription found.")
    lang = user_language_settings.get(uid)
    if not lang:
        prefix = "setlang_translate" if kind=="translate" else "setlang_summarize"
        bot.edit_message_text(
            "Select language:",
            c.message.chat.id, c.message.message_id,
            reply_markup=generate_language_keyboard(prefix, msgid)
        )
        return
    bot.answer_callback_query(c.id)
    prompt = (
        f"{'Translate' if kind=='translate' else 'Summarize'} to {lang}:\n\n"
        + user_transcriptions[uid][int(msgid)]
    )
    reply = ask_gemini(uid, prompt)
    bot.send_message(c.message.chat.id, reply, reply_to_message_id=int(msgid))

@bot.callback_query_handler(func=lambda c: c.data.startswith(("setlang_translate|","setlang_summarize|")))
def setlang(c):
    parts = c.data.split("|")
    prefix = parts[0]
    lang = parts[1]
    msgid = parts[2]
    uid = str(c.from_user.id)
    user_language_settings[uid] = lang
    save_user_language_settings()
    bot.edit_message_text(
        f"Language set to *{lang}*.\nNow processing...",
        c.message.chat.id, c.message.message_id,
        parse_mode="Markdown"
    )
    # re-trigger
    fake = telebot.types.CallbackQuery(
        id=c.id, from_user=c.from_user,
        message=c.message, data=f"{ 'translate' if prefix.endswith('translate') else 'summarize'}|{msgid}"
    )
    on_action(fake)
# ────────────────────────────────────────────────

# ───────────── Video Link Downloading ─────────────
@bot.message_handler(func=lambda m: isinstance(m.text,str) and is_supported_url(m.text))
def handle_url(m):
    url = m.text.strip()
    if 'youtube.com' in url:
        return handle_youtube(m, url)
    else:
        return handle_social(m, url)

def handle_youtube(m, url):
    try:
        bot.send_chat_action(m.chat.id,'typing')
        ydl_opts = {'quiet':True,'no_warnings':True,'extract_flat':True,'skip_download':True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get('title','Video')
        desc  = info.get('description','')
        thumb = info.get('thumbnail')
        formats = info.get('formats',[])
        resos = {f"{f['height']}p":f['format_id']
                 for f in formats if f.get('height') and f['acodec']!='none'}

        if not resos:
            return bot.send_message(m.chat.id, "❌ No downloadable formats.")
        # build keyboard
        kb = InlineKeyboardMarkup(row_width=3)
        for res, fid in sorted(resos.items(), key=lambda x:int(x[0][:-1])):
            vid = str(uuid.uuid4())[:8]
            bot.video_info = getattr(bot,'video_info',{})
            bot.video_info[vid] = {'url':url,'fmt':fid,'desc':desc}
            kb.add(InlineKeyboardButton(res, callback_data=f"dl_youtube|{vid}"))
        # send thumbnail + buttons
        if thumb:
            r = requests.get(thumb); img=Image.open(BytesIO(r.content))
            buf=BytesIO(); img.save(buf,'JPEG'); buf.seek(0)
            bot.send_photo(m.chat.id, buf,
                caption=f"{title}\n\nSelect quality:",
                reply_markup=kb
            )
        else:
            bot.send_message(m.chat.id, f"{title}\n\nSelect quality:", reply_markup=kb)
    except Exception as e:
        bot.send_message(m.chat.id, f"Error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("dl_youtube|"))
def dl_youtube(c):
    vid = c.data.split("|")[1]
    info = bot.video_info.get(vid)
    if not info:
        return bot.answer_callback_query(c.id, "Expired—try again.")
    bot.answer_callback_query(c.id, f"Downloading {info['fmt']}…")
    out = os.path.join(DOWNLOAD_DIR,f"{uuid.uuid4()}.mp4")
    ydl_opts={'format':info['fmt'],'outtmpl':out,'quiet':True,'no_warnings':True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([info['url']])
    # send video with description & branding
    caption = (info['desc'] or "No description") + "\n\nPowered by @transcriberbo"
    bot.send_video(c.message.chat.id, open(out,'rb'), caption=caption)
    os.remove(out)

def handle_social(m, url):
    try:
        bot.send_chat_action(m.chat.id,'record_video')
        out = download_any(url)
        # no metadata available, so only branding
        caption = "Powered by @transcriberbo"
        bot.send_video(m.chat.id, open(out,'rb'), caption=caption)
    except Exception as e:
        bot.send_message(m.chat.id, f"Error: {e}")
    finally:
        if out and os.path.exists(out): os.remove(out)

def download_any(url):
    out = os.path.join(DOWNLOAD_DIR,f"{uuid.uuid4()}.mp4")
    opts = {
        'format':'bestvideo[ext=mp4]+bestaudio/best',
        'merge_output_format':'mp4','outtmpl':out,
        'quiet':True,'no_warnings':True,'noplaylist':True
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return out
# ────────────────────────────────────────────────────

if __name__ == "__main__":
    # Clean downloads on start
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    set_bot_info()
    bot.delete_webhook()
    bot.set_webhook(url=WEBHOOK_URL + '/')
    logging.info(f"Webhook set to {WEBHOOK_URL}/")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
