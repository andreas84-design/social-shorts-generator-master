from flask import Flask, request, jsonify
import os
import json
import gspread
from google.cloud import texttospeech
from google.oauth2 import service_account
from openai import OpenAI
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
import base64
from threading import Thread

app = Flask(__name__)

# ==================== CONFIGURAZIONE ====================

# OpenAI
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# Google Credentials
GOOGLE_CREDENTIALS = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON', '{}'))

# R2 Cloudflare
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

# N8N Webhook (NUOVO!)
N8N_CALLBACK_WEBHOOK_URL = os.environ.get('N8N_CALLBACK_WEBHOOK_URL')

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

def send_n8n_webhook(payload):
    """
    Invia webhook a n8n quando video sono pronti (NUOVO!)
    """
    webhook_url = payload.get('webhook_callback_url') or N8N_CALLBACK_WEBHOOK_URL
    
    if not webhook_url:
        print("[WARNING] ⚠️ Nessun webhook URL configurato, skip notifica n8n")
        return
    
    try:
        print(f"[INFO] 🔔 Invio webhook a n8n: {webhook_url}")
        
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers={'Content-Type': 'application/json'}
        )
        
        print(f"[SUCCESS] ✅ Webhook inviato a n8n: {response.status_code}")
        return response.json() if response.ok else None
        
    except Exception as e:
        print(f"[ERROR] ❌ Errore invio webhook n8n: {e}")
        traceback.print_exc()


def text_to_speech_from_base64(audio_base64, output_path):
    """
    Decodifica audio base64 e salva su file (NUOVO!)
    """
    print(f"[INFO] Decodifica audio base64...")
    
    try:
        # Rimuovi header data URL se presente
        if ',' in audio_base64:
            audio_base64 = audio_base64.split(',')[1]
        
        audio_data = base64.b64decode(audio_base64)
        
        with open(output_path, 'wb') as f:
            f.write(audio_data)
        
        print(f"[SUCCESS] Audio decodificato: {output_path}")
        return output_path
        
    except Exception as e:
        print(f"[ERROR] Errore decodifica audio: {e}")
        traceback.print_exc()
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
        traceback.print_exc()
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
        
        # Crea sfondo colorato con gradiente
        from PIL import Image, ImageDraw
        import numpy as np
        
        img = Image.new('RGB', (width, height), color=(30, 30, 50))
        draw = ImageDraw.Draw(img)
        
        # Aggiungi gradiente verticale
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
        
        # Sanitizza nome canale per path
        channel_safe = channel_name.replace(" ", "_").replace("/", "_")
        platform_safe = platform.replace(" ", "_")
        
        s3_key = f"shorts/{channel_safe}/{platform_safe}_{timestamp}_{unique_id}.mp4"
        
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
        traceback.print_exc()
        raise


def process_video_generation_background(task_id, videos, channel_name, row_number, sheet_id, webhook_url):
    """
    Processa generazione video in background (NUOVO!)
    """
    print(f"\n[INFO] 🎬 Background processing started for task {task_id}")
    
    try:
        video_urls = {}
        
        for video_data in videos:
            platform = video_data.get('platform')
            script = video_data.get('script')
            audio_base64 = video_data.get('audio_base64')
            title = video_data.get('title')
            
            print(f"\n[INFO] === Generazione video per {platform} ===")
            
            try:
                # 1. Decodifica audio base64
                with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as audio_file:
                    audio_path = audio_file.name
                
                text_to_speech_from_base64(audio_base64, audio_path)
                
                # 2. Crea video
                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as video_file:
                    video_path = video_file.name
                
                create_short_video(script, audio_path, video_path, platform)
                
                # 3. Upload su R2
                video_url = upload_to_r2(video_path, channel_name, platform)
                
                # 4. Cleanup
                try:
                    os.unlink(audio_path)
                    os.unlink(video_path)
                except:
                    pass
                
                video_urls[platform] = video_url
                print(f"[SUCCESS] ✅ Video {platform} completato!")
                
            except Exception as e:
                print(f"[ERROR] ❌ Errore video {platform}: {e}")
                traceback.print_exc()
                continue
        
        # Invia webhook a n8n
        if webhook_url and video_urls:
            webhook_payload = {
                'status': 'completed',
                'task_id': task_id,
                'row_number': row_number,
                'sheet_id': sheet_id,
                'channel_name': channel_name,
                'videos': [
                    {'platform': 'youtube_shorts', 'video_url': video_urls.get('youtube_shorts', '')},
                    {'platform': 'tiktok', 'video_url': video_urls.get('tiktok', '')},
                    {'platform': 'instagram_reels', 'video_url': video_urls.get('instagram_reels', '')},
                    {'platform': 'facebook_reels', 'video_url': video_urls.get('facebook_reels', '')}
                ]
            }
            
            send_n8n_webhook(webhook_payload)
        
        print(f"\n[SUCCESS] 🎉 Task {task_id} completato! {len(video_urls)}/4 video generati")
        
    except Exception as e:
        print(f"\n[ERROR] ❌ Errore task {task_id}: {e}")
        traceback.print_exc()
        
        # Invia webhook di errore
        if webhook_url:
            error_payload = {
                'status': 'failed',
                'task_id': task_id,
                'row_number': row_number,
                'sheet_id': sheet_id,
                'error': str(e)
            }
            send_n8n_webhook(error_payload)


# ==================== ENDPOINT ====================

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok"}), 200


@app.route('/api/generate', methods=['POST'])
def generate_videos():
    """
    Endpoint per generare 4 video social da n8n (NUOVO!)
    """
    try:
        data = request.json
        
        # Estrai parametri
        videos = data.get('videos', [])
        channel_name = data.get('channel_name')
        row_number = data.get('row_number')
        sheet_id = data.get('sheet_id')
        webhook_callback_url = data.get('webhook_callback_url')
        
        # Validazione
        if not videos or len(videos) != 4:
            return jsonify({
                "error": "Servono esattamente 4 video (youtube_shorts, tiktok, instagram_reels, facebook_reels)",
                "success": False
            }), 400
        
        if not all([channel_name, row_number, sheet_id]):
            return jsonify({
                "error": "Parametri mancanti: channel_name, row_number, sheet_id",
                "success": False
            }), 400
        
        # Genera task ID
        task_id = str(uuid.uuid4())
        
        print(f"\n{'='*60}")
        print(f"[INFO] 📥 Nuova richiesta generazione video")
        print(f"[INFO] Task ID: {task_id}")
        print(f"[INFO] Channel: {channel_name}")
        print(f"[INFO] Row: {row_number}")
        print(f"[INFO] Videos: {len(videos)}")
        print(f"{'='*60}\n")
        
        # Avvia processing in background thread
        thread = Thread(
            target=process_video_generation_background,
            args=(task_id, videos, channel_name, row_number, sheet_id, webhook_callback_url)
        )
        thread.start()
        
        # Risposta immediata
        return jsonify({
            "task_id": task_id,
            "status": "processing",
            "message": f"{len(videos)} video in coda per generazione",
            "estimated_time": "30-60 secondi"
        }), 202
        
    except Exception as e:
        print(f"\n[ERROR] ❌ Errore /api/generate: {e}")
        traceback.print_exc()
        return jsonify({
            "error": str(e),
            "success": False
        }), 500


@app.route('/generate-shorts', methods=['POST'])
def generate_shorts():
    """
    Endpoint legacy per generare short da video YouTube (MANTIENI PER RETROCOMPATIBILITÀ)
    """
    try:
        data = request.json
        
        # ... (mantieni codice originale) ...
        
    except Exception as e:
        print(f"\n[ERROR] ❌ ERRORE GENERALE: {e}")
        traceback.print_exc()
        return jsonify({
            "error": str(e),
            "success": False
        }), 500


# ==================== RUN ====================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"\n{'='*60}")
    print(f"🚀 Starting Social Shorts Generator Backend")
    print(f"📍 Port: {port}")
    print(f"🔔 N8N Webhook: {N8N_CALLBACK_WEBHOOK_URL or 'Not configured'}")
    print(f"{'='*60}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
