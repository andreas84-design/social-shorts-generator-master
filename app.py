from flask import Flask, request, jsonify
import os
import json
import gspread
from google.cloud import texttospeech
from google.oauth2 import service_account
import openai
from moviepy.editor import (
    VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip,
    concatenate_videoclips, ImageClip
)
from moviepy.video.fx.all import crop
import requests
import tempfile
from datetime import datetime, timedelta
import boto3
from botocore.client import Config
import uuid
import traceback

app = Flask(__name__)

# ==================== CONFIGURAZIONE ====================

# OpenAI
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
openai.api_key = OPENAI_API_KEY

# Google Credentials
GOOGLE_CREDENTIALS = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON', '{}'))

# R2 Cloudflare
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

# Configurazione R2
s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version='s3v4')
)

# Google TTS Client
tts_credentials = service_account.Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
tts_client = texttospeech.TextToSpeechClient(credentials=tts_credentials)


# ==================== FUNZIONI HELPER ====================

def generate_script_with_gpt4(video_title, platform):
    """
    Genera uno script per short usando GPT-4
    """
    print(f"[INFO] Generazione script per {platform}...")
    
    platform_specs = {
        "YouTube Shorts": "YouTube Shorts (max 60 secondi, tono diretto e engaging)",
        "TikTok": "TikTok (max 60 secondi, tono giovane e dinamico)",
        "Instagram Reels": "Instagram Reels (max 60 secondi, tono trendy e visivo)",
        "Facebook Reels": "Facebook Reels (max 90 secondi, tono familiare e coinvolgente)"
    }
    
    prompt = f"""Crea uno script per un video {platform_specs[platform]} basato su questo video YouTube:
Titolo: {video_title}

Lo script deve:
- Essere della durata di 30-45 secondi
- Iniziare con un hook forte (domanda o affermazione potente)
- Fornire 2-3 punti chiave o consigli pratici
- Concludere con una call-to-action per guardare il video completo
- Essere scritto in italiano colloquiale
- Non superare le 100 parole

Fornisci SOLO il testo dello script, senza titoli o etichette."""

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "Sei un esperto copywriter per social media."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300,
            temperature=0.8
        )
        
        script = response.choices[0].message.content.strip()
        print(f"[SUCCESS] Script generato per {platform}")
        return script
        
    except Exception as e:
        print(f"[ERROR] Errore generazione script GPT-4: {e}")
        raise


def text_to_speech(text, output_path):
    """
    Converte testo in audio usando Google TTS
    """
    print(f"[INFO] Generazione audio con Google TTS...")
    
    try:
        synthesis_input = texttospeech.SynthesisInput(text=text)
        
        voice = texttospeech.VoiceSelectionParams(
            language_code="it-IT",
            name="it-IT-Neural2-A",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
        )
        
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
            pitch=0.0
        )
        
        response = tts_client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config
        )
        
        with open(output_path, 'wb') as out:
            out.write(response.audio_content)
        
        print(f"[SUCCESS] Audio generato: {output_path}")
        return output_path
        
    except Exception as e:
        print(f"[ERROR] Errore Google TTS: {e}")
        raise


def create_short_video(script, audio_path, output_path, platform):
    """
    Crea video short 9:16 con sfondo, audio e sottotitoli
    """
    print(f"[INFO] Creazione video per {platform}...")
    
    try:
        # Parametri video 9:16
        width, height = 1080, 1920
        duration_audio = AudioFileClip(audio_path).duration
        
        # Crea sfondo colorato
        from PIL import Image, ImageDraw
        import numpy as np
        
        img = Image.new('RGB', (width, height), color=(30, 30, 50))
        draw = ImageDraw.Draw(img)
        
        # Aggiungi gradiente
        for y in range(height):
            r = int(30 + (y / height) * 40)
            g = int(30 + (y / height) * 50)
            b = int(50 + (y / height) * 80)
            draw.line([(0, y), (width, y)], fill=(r, g, b))
        
        img_array = np.array(img)
        
        # Crea clip immagine
        background_clip = ImageClip(img_array).set_duration(duration_audio)
        
        # Aggiungi testo (sottotitoli)
        txt_clip = TextClip(
            script,
            fontsize=60,
            color='white',
            font='Arial-Bold',
            size=(width - 100, None),
            method='caption',
            align='center'
        ).set_position('center').set_duration(duration_audio)
        
        # Aggiungi audio
        audio_clip = AudioFileClip(audio_path)
        
        # Componi video
        final_clip = CompositeVideoClip([background_clip, txt_clip])
        final_clip = final_clip.set_audio(audio_clip)
        
        # Esporta
        final_clip.write_videofile(
            output_path,
            fps=30,
            codec='libx264',
            audio_codec='aac',
            preset='ultrafast',
            threads=4
        )
        
        # Cleanup
        background_clip.close()
        txt_clip.close()
        audio_clip.close()
        final_clip.close()
        
        print(f"[SUCCESS] Video creato: {output_path}")
        return output_path
        
    except Exception as e:
        print(f"[ERROR] Errore creazione video: {e}")
        traceback.print_exc()
        raise


def upload_to_r2(file_path, channel_name, platform):
    """
    Carica video su R2 Cloudflare
    """
    print(f"[INFO] Upload su R2 per {platform}...")
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        
        s3_key = f"shorts/{channel_name}/{platform}_{timestamp}_{unique_id}.mp4"
        
        s3_client.upload_file(
            file_path,
            R2_BUCKET_NAME,
            s3_key,
            ExtraArgs={'ContentType': 'video/mp4'}
        )
        
        video_url = f"https://pub-{R2_ACCOUNT_ID}.r2.dev/{s3_key}"
        
        print(f"[SUCCESS] Video caricato: {video_url}")
        return video_url
        
    except Exception as e:
        print(f"[ERROR] Errore upload R2: {e}")
        raise


def write_to_sheets(video_id, channel_name, sheet_id, youtube_url, results):
    """
    Scrive i risultati su Google Sheets
    """
    print(f"[INFO] Scrittura su Google Sheets...")
    
    try:
        # FIX 1: Strip spazi da sheet_id
        sheet_id = sheet_id.strip()
        
        # FIX 2: Log per debug
        print(f"[DEBUG] Sheet ID ricevuto: '{sheet_id}'")
        print(f"[DEBUG] Lunghezza Sheet ID: {len(sheet_id)}")
        
        # Inizializza client gspread
        gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
        
        # FIX 3: Prova ad aprire lo sheet
        print(f"[DEBUG] Tentativo apertura Sheet...")
        spreadsheet = gc.open_by_key(sheet_id)
        print(f"[SUCCESS] Sheet aperto: {spreadsheet.title}")
        
        # Apri worksheet "Calendario_Social"
        try:
            worksheet = spreadsheet.worksheet("Calendario_Social")
        except gspread.exceptions.WorksheetNotFound:
            print("[WARNING] Worksheet 'Calendario_Social' non trovato, lo creo...")
            worksheet = spreadsheet.add_worksheet(title="Calendario_Social", rows=1000, cols=20)
            
            # Aggiungi header
            headers = [
                "Video_ID", "Canale", "Platform", "Video_URL_R2", "Script",
                "Caption", "Hashtags", "Publish_Date", "Publish_Time", "Status"
            ]
            worksheet.append_row(headers)
        
        # Calcola date di pubblicazione
        base_date = datetime.now()
        platforms_schedule = {
            "YouTube Shorts": (base_date + timedelta(days=1), "15:00"),
            "TikTok": (base_date + timedelta(days=1), "18:00"),
            "Instagram Reels": (base_date + timedelta(days=2), "12:00"),
            "Facebook Reels": (base_date + timedelta(days=2), "19:00")
        }
        
        # Scrivi righe per ogni piattaforma
        for result in results:
            platform = result['platform']
            pub_date, pub_time = platforms_schedule[platform]
            
            row_data = [
                video_id,
                channel_name,
                platform,
                result['video_url'],
                result['script'][:500],  # Limita lunghezza
                f"Guarda il video completo! {youtube_url}",
                "#shorts #viral #tutorial",
                pub_date.strftime("%Y-%m-%d"),
                pub_time,
                "Scheduled"
            ]
            
            worksheet.append_row(row_data)
            print(f"[INFO] Riga aggiunta per {platform}")
        
        print(f"[SUCCESS] {len(results)} righe scritte su Google Sheets")
        
    except gspread.exceptions.SpreadsheetNotFound as e:
        print(f"[ERROR] Sheet non trovato! ID: {sheet_id}")
        print(f"[ERROR] Dettaglio: {e}")
        raise
    except Exception as e:
        print(f"[ERROR] Errore scrittura Google Sheets: {e}")
        traceback.print_exc()
        raise


# ==================== ENDPOINT ====================

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok"}), 200


@app.route('/generate-shorts', methods=['POST'])
def generate_shorts():
    """
    Endpoint principale per generare short da video YouTube
    """
    try:
        data = request.json
        
        # Estrai parametri
        video_id = data.get('video_id')
        video_title = data.get('video_title')
        youtube_url = data.get('youtube_url')
        channel_name = data.get('channel_name')
        sheet_id = data.get('sheet_id')
        
        # Validazione
        if not all([video_id, video_title, youtube_url, channel_name, sheet_id]):
            return jsonify({
                "error": "Parametri mancanti",
                "success": False
            }), 400
        
        print(f"[INFO] Generazione short per: {video_title}")
        print(f"[INFO] Channel: {channel_name}")
        print(f"[INFO] Sheet ID: {sheet_id}")
        
        # Piattaforme target
        platforms = ["YouTube Shorts", "TikTok", "Instagram Reels", "Facebook Reels"]
        results = []
        
        # Genera short per ogni piattaforma
        for i, platform in enumerate(platforms, 1):
            print(f"[INFO] Generazione short {i}/4 per {platform}...")
            
            # 1. Genera script
            script = generate_script_with_gpt4(video_title, platform)
            
            # 2. Crea audio
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as audio_file:
                audio_path = audio_file.name
            
            text_to_speech(script, audio_path)
            
            # 3. Crea video
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as video_file:
                video_path = video_file.name
            
            create_short_video(script, audio_path, video_path, platform)
            
            # 4. Upload su R2
            video_url = upload_to_r2(video_path, channel_name, platform)
            
            # 5. Cleanup temp files
            os.unlink(audio_path)
            os.unlink(video_path)
            
            results.append({
                "platform": platform,
                "script": script,
                "video_url": video_url
            })
            
            print(f"[SUCCESS] Short {i}/4 completato per {platform}")
        
        # Scrivi su Google Sheets
        write_to_sheets(video_id, channel_name, sheet_id, youtube_url, results)
        
        return jsonify({
            "success": True,
            "message": f"{len(results)} short generati con successo",
            "results": results
        }), 200
        
    except Exception as e:
        print(f"[ERROR] Errore generale: {e}")
        traceback.print_exc()
        return jsonify({
            "error": str(e),
            "success": False
        }), 500


# ==================== RUN ====================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
