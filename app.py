import os
import tempfile
import subprocess
import uuid
import json
import traceback
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import boto3
from botocore.config import Config
from openai import OpenAI
from google.cloud import texttospeech
from google.oauth2 import service_account
import gspread

app = Flask(__name__)

# ==================== CONFIG ====================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "https://pub-yourdomain.r2.dev")

# OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Google TTS client
credentials = service_account.Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDENTIALS_JSON)
)
tts_client = texttospeech.TextToSpeechClient(credentials=credentials)

# Google Sheets client
gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDENTIALS_JSON))

# R2 client
r2_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)

# ==================== HELPER FUNCTIONS ====================

def generate_scripts(video_title, channel_name):
    """GPT-4 genera 4 script virali per short, ognuno ottimizzato per una piattaforma"""
    
    prompt = f"""Sei un esperto di contenuti virali per social media.

Titolo video YouTube: "{video_title}"
Canale: {channel_name}

Genera 4 SCRIPT DIVERSI per short verticali (max 45 secondi). Ogni script DEVE essere ottimizzato per una piattaforma specifica:

1. YOUTUBE SHORTS - Focus su retention e watch time
2. TIKTOK - Hook ultra forte, trend-friendly
3. INSTAGRAM REELS - Estetica curata, caption engaging
4. FACEBOOK REELS - Più descrittivo, audience più ampia

Ogni script deve avere questa struttura:
- HOOK (3 sec) - Frase d'impatto che cattura subito
- PROBLEMA (10 sec) - Identifica il pain point
- SOLUZIONE (25 sec) - Contenuto di valore, 2-3 punti chiave
- CTA (7 sec) - "Guarda il video completo su YouTube per saperne di più"

REGOLE:
- Script in ITALIANO
- Linguaggio diretto e conversazionale
- Ogni script UNICO (angolo diverso dello stesso tema)
- Usa numeri e liste (es: "3 segnali di...", "Il metodo in 2 step...")
- Max 120 parole per script
- Adatta tono e stile alla piattaforma target

Ritorna JSON:
{{
  "shorts": [
    {{
      "platform": "YouTube Shorts",
      "script": "testo completo script",
      "hook": "frase hook",
      "title": "titolo short (max 50 caratteri)",
      "caption": "descrizione ottimizzata per la piattaforma (max 150 caratteri)",
      "hashtags": ["hashtag1", "hashtag2", "hashtag3", "hashtag4", "hashtag5"]
    }},
    // ... altri 3 per TikTok, Instagram, Facebook
  ]
}}"""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.8
    )
    
    return json.loads(response.choices[0].message.content)


def text_to_speech(text, output_path):
    """Converte testo in audio MP3 con Google TTS Neural"""
    
    synthesis_input = texttospeech.SynthesisInput(text=text)
    
    voice = texttospeech.VoiceSelectionParams(
        language_code="it-IT",
        name="it-IT-Neural2-A",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
    )
    
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.05,
        pitch=0.0
    )
    
    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )
    
    with open(output_path, "wb") as out:
        out.write(response.audio_content)
    
    return output_path


def get_audio_duration(audio_path):
    """Ottiene durata audio in secondi"""
    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def generate_subtitles(script, audio_duration):
    """Genera file SRT con timing automatico"""
    
    words = script.split()
    total_words = len(words)
    time_per_word = audio_duration / total_words
    
    srt_content = []
    current_time = 0
    words_per_subtitle = 4
    
    for i in range(0, total_words, words_per_subtitle):
        subtitle_words = words[i:i+words_per_subtitle]
        subtitle_text = " ".join(subtitle_words)
        
        start_time = current_time
        end_time = current_time + (len(subtitle_words) * time_per_word)
        
        srt_content.append(f"{len(srt_content) + 1}")
        srt_content.append(f"{format_srt_time(start_time)} --> {format_srt_time(end_time)}")
        srt_content.append(subtitle_text.upper())
        srt_content.append("")
        
        current_time = end_time
    
    return "\n".join(srt_content)


def format_srt_time(seconds):
    """Formatta secondi in formato SRT"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def create_video(audio_path, srt_path, output_path, title, cta_text):
    """Genera video 9:16 con sottotitoli e CTA usando FFmpeg"""
    
    duration = get_audio_duration(audio_path)
    
    # Escape caratteri speciali per FFmpeg
    srt_path_escaped = srt_path.replace('\\', '/').replace(':', '\\:')
    cta_escaped = cta_text.replace("'", "'\\\\\\''")
    
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi', '-i', f'color=c=#1a1a2e:s=1080x1920:d={duration}',
        '-i', audio_path,
        '-vf',
        f"subtitles={srt_path_escaped}:force_style='FontName=Arial Bold,FontSize=48,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=3,Bold=1,Alignment=2,MarginV=120',"
        f"drawtext=text='{cta_escaped}':fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=10:x=(w-text_w)/2:y=h-150:enable='gte(t,{duration-5})'",
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest',
        output_path
    ]
    
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def upload_to_r2(file_path, object_name):
    """Upload file su Cloudflare R2"""
    
    r2_client.upload_file(
        file_path,
        R2_BUCKET_NAME,
        object_name,
        ExtraArgs={'ContentType': 'video/mp4'}
    )
    
    url = f"{R2_PUBLIC_URL}/{object_name}"
    return url


def write_to_sheets(video_id, channel_name, sheet_id, youtube_url, shorts_data):
    """Scrive 4 short nel Calendario_Social e aggiorna Video_Master"""
    
    sheet = gc.open_by_key(sheet_id)
    
    # Leggi SOCIAL_SCHEDULE_TEMPLATE
    schedule_ws = sheet.worksheet("SOCIAL_SCHEDULE_TEMPLATE")
    schedule_data = schedule_ws.get_all_records()
    
    # Mappa giorni → numero
    days_map = {
        "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
        "Friday": 4, "Saturday": 5, "Sunday": 6
    }
    
    # Trova quale slot usare (rotazione Day 1-4 basata su conteggio video)
    video_master_ws = sheet.worksheet("Video_Master")
    all_videos = video_master_ws.get_all_records()
    video_count = len([v for v in all_videos if v.get('Status_Social') in ['SOCIAL_PROCESSED', 'PROCESSING']])
    day_slot = (video_count % 4) + 1  # Rotazione 1,2,3,4
    
    day_col = f"Day {day_slot}"
    time_col = f"Time {day_slot}"
    
    # Scrivi nel Calendario_Social
    calendario_ws = sheet.worksheet("Calendario_Social")
    
    platform_map = {
        "YouTube Shorts": "YT_Shorts",
        "TikTok": "TikTok",
        "Instagram Reels": "IG_Reels",
        "Facebook Reels": "FB_Reels"
    }
    
    for i, short in enumerate(shorts_data):
        if i >= len(schedule_data):
            break
        
        platform_schedule = schedule_data[i]
        platform = platform_map.get(short["platform"], platform_schedule["Platform"])
        day_name = platform_schedule.get(day_col, "Monday")
        time_str = platform_schedule.get(time_col, "12:00")
        
        # Calcola prossima occorrenza
        publish_date = get_next_weekday(day_name, days_map)
        publish_datetime = f"{publish_date.strftime('%Y-%m-%d')} {time_str}"
        
        row = [
            video_id,
            channel_name,
            i + 1,
            platform,
            short["video_url"],
            short["title"],
            short["caption"],
            ", ".join(short["hashtags"]),
            youtube_url,
            publish_datetime,
            "Scheduled"
        ]
        calendario_ws.append_row(row)
    
    # Aggiorna Video_Master
    cell = video_master_ws.find(video_id)
    if cell:
        row_num = cell.row
        # Assume colonna F = Status_Social, G = Social_Processed_Date
        video_master_ws.update_cell(row_num, 6, "SOCIAL_PROCESSED")
        video_master_ws.update_cell(row_num, 7, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def get_next_weekday(day_name, days_map):
    """Calcola prossima occorrenza di un giorno della settimana"""
    today = datetime.now()
    target_day = days_map.get(day_name, 0)
    current_day = today.weekday()
    
    days_ahead = target_day - current_day
    if days_ahead <= 0:
        days_ahead += 7
    
    return today + timedelta(days=days_ahead)


# ==================== API ENDPOINT ====================

@app.route('/generate-shorts', methods=['POST'])
def generate_shorts():
    """
    Endpoint principale per generare 4 short da video YouTube
    
    Input JSON:
    {
        "video_id": "unique_id",
        "video_title": "Titolo video YouTube",
        "youtube_url": "https://youtube.com/watch?v=...",
        "channel_name": "Nome Canale",
        "sheet_id": "Google Sheet ID"
    }
    """
    
    temp_files = []
    
    try:
        data = request.json
        video_id = data.get("video_id")
        video_title = data.get("video_title")
        youtube_url = data.get("youtube_url")
        channel_name = data.get("channel_name")
        sheet_id = data.get("sheet_id")
        
        if not all([video_id, video_title, youtube_url, channel_name, sheet_id]):
            return jsonify({"success": False, "error": "Missing required fields"}), 400
        
        print(f"[INFO] Generazione short per: {video_title}")
        
        # STEP 1: Genera 4 script con GPT-4
        print("[INFO] Generazione script con GPT-4...")
        scripts_data = generate_scripts(video_title, channel_name)
        shorts_data = scripts_data["shorts"]
        
        results = []
        
        # STEP 2: Per ogni script genera short completo
        for idx, short in enumerate(shorts_data, 1):
            print(f"[INFO] Generazione short {idx}/4 per {short['platform']}...")
            
            script = short["script"]
            
            # TTS
            audio_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.mp3")
            temp_files.append(audio_path)
            text_to_speech(script, audio_path)
            
            # Duration
            audio_duration = get_audio_duration(audio_path)
            
            # SRT
            srt_content = generate_subtitles(script, audio_duration)
            srt_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.srt")
            temp_files.append(srt_path)
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
            
            # Video
            video_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.mp4")
            temp_files.append(video_path)
            cta_text = "Video completo su YouTube ▶"
            create_video(audio_path, srt_path, video_path, short["title"], cta_text)
            
            # Upload R2
            r2_object_name = f"shorts/{channel_name}/{video_id}_short{idx}.mp4"
            video_url = upload_to_r2(video_path, r2_object_name)
            
            results.append({
                "platform": short["platform"],
                "video_url": video_url,
                "title": short["title"],
                "caption": short["caption"],
                "hashtags": short["hashtags"]
            })
        
        # STEP 3: Scrivi su Google Sheets
        print("[INFO] Scrittura su Google Sheets...")
        write_to_sheets(video_id, channel_name, sheet_id, youtube_url, results)
        
        # Cleanup
        for path in temp_files:
            if os.path.exists(path):
                os.unlink(path)
        
        print(f"[SUCCESS] 4 short generati con successo!")
        
        return jsonify({
            "success": True,
            "shorts": results,
            "message": "4 short generati e scritti su Google Sheets"
        }), 200
        
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        traceback.print_exc()
        
        for path in temp_files:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except:
                    pass
        
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
