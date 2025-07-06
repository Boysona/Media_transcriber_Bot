Here’s a suggested README.md for your Telegram Media Transcriber Bot repository:

# Media Transcriber Bot

A Telegram bot that can transcribe audio/video files, translate and summarize transcriptions, and convert text to speech with adjustable voice, pitch, and rate. Built with Python, Flask, PyTelegramBotAPI, AssemblyAI, Google Gemini API, Microsoft Cognitive Services, and MongoDB for persistent user settings.

---

## Features

- **Speech-to-Text**  
  • Transcribe voice messages, audio files, video notes, and video files (MP4, MP3, etc.) up to 20 MB  
  • Supports > 60 languages with automatic language detection or manual selection  

- **Translation**  
  • Translate your transcriptions into any of 60+ target languages  
  • Save your preferred translation language for one-tap translate  

- **Summarization**  
  • Summarize long transcriptions into concise text  
  • Save your preferred summary language; “Auto” uses the original transcription language  

- **Text-to-Speech (TTS)**  
  • Convert any text into audio using Microsoft’s Neural voices  
  • Choose from dozens of voices across 50+ languages  
  • Adjust **pitch** (–100 to +100) and **rate** (–100 to +100 / 0.5×–2×)  

- **Admin Panel**  
  • Live uptime display  
  • Total users & broadcast messaging  

- **Persistent Settings**  
  • User preferences for media language, translation language, summary language, TTS voice, pitch, and rate stored in MongoDB  
  • Activity logs and per-user usage stats  

---

## 📦 Requirements

- Python 3.8+  
- A public HTTPS endpoint (e.g. Render, Heroku) for Telegram webhooks  
- MongoDB Atlas or self-hosted MongoDB  
- API keys for:  
  - **Telegram Bot** (via [@BotFather](https://t.me/BotFather))  
  - **AssemblyAI** (for transcription)  
  - **Google Gemini** (for translate/summarize)  
  - **Microsoft Cognitive Services** (for TTS via `msspeech`)  

---

## 🚀 Installation

1. **Clone the repo**  
   ```bash
   git clone https://github.com/yourusername/media-transcriber-bot.git
   cd media-transcriber-bot

	2.	Create & activate a virtual environment

python3 -m venv venv
source venv/bin/activate


	3.	Install dependencies

pip install -r requirements.txt


	4.	Configure environment variables
Copy .env.example to .env and fill in your values:

BOT_TOKEN=7790991731:YOUR_TELEGRAM_TOKEN
ADMIN_ID=5978150981
WEBHOOK_URL=https://<your-domain>.com
REQUIRED_CHANNEL=@transcriber_bot_news_channel

ASSEMBLYAI_API_KEY=YOUR_ASSEMBLYAI_KEY
GEMINI_API_KEY=YOUR_GOOGLE_GEMINI_KEY
MSSPEECH_KEY=YOUR_MICROSOFT_SPEECH_KEY

MONGO_URI=mongodb+srv://<user>:<pass>@cluster0.mongodb.net/?retryWrites=true&w=majority


	5.	Set up Telegram webhook

python bot.py  # on startup it will set the webhook to your WEBHOOK_URL


	6.	Run the Flask app

# For development:
export FLASK_APP=bot.py
flask run --host=0.0.0.0 --port=8080



⸻

🎮 Usage

Once the bot is running and webhooks are set:
	1.	Start
Send /start in your bot’s chat.
• If a subscription channel is required, you’ll be prompted to join.
• Non-admin users will be asked to select their media language first.
	2.	Transcription
Send any voice message, audio file, or video (≤ 20 MB). The bot replies with the transcription and inline buttons for Translate / Summarize.
	3.	Translate & Summarize
• Tap Translate or use /translate
• Tap Summarize or use /summarize
• If you haven’t set a default language, you’ll choose one via inline menu.
	4.	Text-to-Speech
• Use /text_to_speech → select language & voice
• Send any text → bot replies with an audio clip
• Adjust pitch: /voice_pitch
• Adjust rate: /voice_rate
	5.	Language Settings
• /media_language – set language of incoming audio/video
• /translate_language – default for Translate
• /summary_language – default for Summarize
	6.	Admin Commands
(Only available to ADMIN_ID)
• Send Broadcast – broadcast any message to all users
• Total Users – view registered user count
• /status – bot & usage stats

⸻

🤝 Contributing
	1.	Fork the repo
	2.	Create a feature branch (git checkout -b feature/my-feature)
	3.	Commit your changes (git commit -m 'Add my feature')
	4.	Push to the branch (git push origin feature/my-feature)
	5.	Open a Pull Request

Please ensure any new dependencies are added to requirements.txt and that commands are updated in the set_bot_commands() function.
