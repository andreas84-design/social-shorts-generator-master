from flask import Flask, request, jsonify
import os
import json
import base64
import tempfile
from datetime import datetime, timedelta
import boto3
from botocore.client import Config
import uuid
import traceback
import requests
import subprocess
import math
import random
from threading import Thread
from collections import Counter

app = Flask(__name__)

# ==================== CONFIGURAZIONE ====================

R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

PEXELS_API_KEY = os.environ.get('PEXELS_API_KEY')
PIXABAY_API_KEY = os.environ.get('PIXABAY_API_KEY')

N8N_CALLBACK_WEBHOOK_URL = os.environ.get('N8N_CALLBACK_WEBHOOK_URL')

MAX_DURATION = int(os.environ.get('MAX_DURATION', '3600'))
MAX_CLIPS = int(os.environ.get('MAX_CLIPS', '5'))

s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version='s3v4')
)


# ==================== FUNZIONI HELPER GENERICHE ====================

def extract_keywords_from_text(text, max_keywords=10):
    """Estrae parole chiave significative da un testo (GENERICO)"""
    if not text:
        return []
    
    stopwords = {'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'una', 'di', 'da', 'a', 'in', 'per', 'con', 'su',
                 'come', 'che', 'si', 'non', 'del', 'della', 'dei', 'delle', 'sono', 'è', 'the', 'a', 'an',
                 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'as', 'is', 'was'}
    
    words = text.lower().split()
    keywords = [w for w in words if len(w) > 3 and w not in stopwords and w.isalpha()]
    
    word_freq = Counter(keywords)
    
    return [word for word, _ in word_freq.most_common(max_keywords)]


def build_dynamic_query(video_title, keywords, description, script, scene_context=""):
    """Costruisce query dinamica per Pexels/Pixabay"""
    all_text = f"{video_title} {keywords} {description} {script} {scene_context}"
    main_keywords = extract_keywords_from_text(all_text, max_keywords=5)
    
    if keywords and keywords.strip():
        sheet_keywords = [k.strip() for k in keywords.split(',')][:3]
        main_keywords = sheet_keywords + main_keywords
    
    query_parts = main_keywords[:5]
    query = " ".join(query_parts) if query_parts else "people activity lifestyle"
    
    print(f"[INFO] 📝 Query dinamica: '{query}'", flush=True)
    return query


def is_video_relevant(video_data, source, banned_topics=None):
    """Filtro GENERICO"""
    if banned_topics is None:
        banned_topics = []
    
    if source == "pexels":
        text = (video_data.get("description", "") + " " + " ".join(video_data.get("tags", []))).lower()
    else:
        text = " ".join(video_data.get("tags", [])).lower()
    
    has_banned = any(topic in text for topic in banned_topics)
    
    if has_banned:
        print(f"[WARNING] ⚠️ Video bannato: '{text[:60]}'", flush=True)
        return False
    
    return True


def download_file(url: str) -> str:
    """Download video"""
    tmp_clip = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    clip_resp = requests.get(url, stream=True, timeout=30)
    clip_resp.raise_for_status()
    for chunk in clip_resp.iter_content(chunk_size=1024 * 1024):
        if chunk:
            tmp_clip.write(chunk)
    tmp_clip.close()
    return tmp_clip.name


def fetch_clip_for_scene(scene_number: int, query: str, avg_scene_duration: float):
    """Cerca e scarica clip da Pexels o Pixabay"""
    target_duration = min(4.0, avg_scene_duration)
    
    def try_pexels():
        if not PEXELS_API_KEY:
            return None
        headers = {"Authorization": PEXELS_API_KEY}
        params = {
            "query": query,
            "orientation": "landscape",
            "per_page": 25,
            "page": random.randint(1, 3),
        }
        resp = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            return None
        videos = resp.json().get("videos", [])
        relevant_videos = [v for v in videos if is_video_relevant(v, "pexels")]
        print(f"[INFO] 🎯 Pexels scena {scene_number}: {len(videos)} totali → {len(relevant_videos)} rilevanti", flush=True)
        if relevant_videos:
            video = random.choice(relevant_videos)
            for vf in video.get("video_files", []):
                if vf.get("width", 0) >= 1280:
                    return download_file(vf["link"])
        return None
    
    def try_pixabay():
        if not PIXABAY_API_KEY:
            return None
        params = {
            "key": PIXABAY_API_KEY,
            "q": query,
            "per_page": 25,
            "safesearch": "true",
            "min_width": 1280,
        }
        resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=20)
        if resp.status_code != 200:
            return None
        hits = resp.json().get("hits", [])
        for hit in hits:
            if is_video_relevant(hit, "pixabay"):
                videos = hit.get("videos", {})
                for quality in ["large", "medium", "small"]:
                    if quality in videos and "url" in videos[quality]:
                        return download_file(videos[quality]["url"])
        return None
    
    for source_name, func in [("Pexels", try_pexels), ("Pixabay", try_pixabay)]:
        try:
            path = func()
            if path:
                print(f"[INFO] 🎥 Scena {scene_number}: {source_name} ✓", flush=True)
                return path, target_duration
        except Exception as e:
            print(f"[WARNING] ⚠️ {source_name} scena {scene_number}: {e}", flush=True)
    
    print(f"[WARNING] ⚠️ NO CLIP per scena {scene_number}", flush=True)
    return None, None


def download_audio_from_url(audio_url, output_path):
    """Scarica audio da URL o decodifica da base64"""
    if not audio_url:
        raise ValueError("audio_url è None o vuoto")
    
    print(f"[INFO] Processing audio...", flush=True)
    
    try:
        if audio_url.startswith('data:audio'):
            print("[INFO] Decodifica audio base64...", flush=True)
            base64_data = audio_url.split(',')[1] if ',' in audio_url else audio_url
            audio_bytes = base64.b64decode(base64_data)
            
            with open(output_path, 'wb') as f:
                f.write(audio_bytes)
            
            print(f"[SUCCESS] Audio decodificato ({len(audio_bytes)} bytes)", flush=True)
            return output_path
        
        print(f"[INFO] Download audio da URL", flush=True)
        response = requests.get(audio_url, timeout=30)
        response.raise_for_status()
        
        with open(output_path, 'wb') as f:
            f.write(response.content)
        
        print(f"[SUCCESS] Audio scaricato", flush=True)
        return output_path
        
    except Exception as e:
        print(f"[ERROR] Errore audio: {e}", flush=True)
        raise


def get_video_duration(path):
    """Ottieni durata video"""
    out = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ], stdout=subprocess.PIPE, text=True, timeout=10).stdout.strip()
    return float(out or 4.0)


def create_short_video_with_clips(video_data, audio_path, output_path, platform):
    """Crea video short 9:16 con 5 clip"""
    print(f"\n[INFO] 🎬 Creazione video {platform} con {MAX_CLIPS} clip...", flush=True)
    
    try:
        script = video_data.get('script', '')
        video_title = video_data.get('video_title', '')
        keywords = video_data.get('keywords', '')
        description = video_data.get('description', '')
        
        audio_duration = get_video_duration(audio_path)
        print(f"[INFO] ⏱️ Durata audio: {audio_duration:.1f}s", flush=True)
        print(f"[INFO] 📺 Video: '{video_title[:50]}'", flush=True)
        print(f"[INFO] 🔑 Keywords: '{keywords[:50]}'", flush=True)
        
        script_words = script.lower().split()
        words_per_second = len(script_words) / audio_duration if audio_duration > 0 else 2.5
        num_scenes = MAX_CLIPS
        avg_scene_duration = audio_duration / num_scenes
        
        print(f"[INFO] 🎞️ Cercando {num_scenes} clip...", flush=True)
        
        scene_clips = []
        for i in range(num_scenes):
            word_index = int((i * audio_duration / num_scenes) * words_per_second)
            scene_context = " ".join(script_words[word_index: word_index + 7]) if word_index < len(script_words) else ""
            
            scene_query = build_dynamic_query(video_title, keywords, description, script, scene_context)
            
            clip_path, _ = fetch_clip_for_scene(i + 1, scene_query, avg_scene_duration)
            if clip_path:
                scene_clips.append(clip_path)
        
        if len(scene_clips) < 3:
            raise RuntimeError(f"Troppe poche clip: {len(scene_clips)}/{num_scenes}")
        
        print(f"[SUCCESS] ✅ {len(scene_clips)}/{num_scenes} clip scaricate!", flush=True)
        
        print(f"[INFO] 🔧 Normalizzazione clip a 9:16...", flush=True)
        normalized_clips = []
        for i, clip_path in enumerate(scene_clips):
            try:
                normalized_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                normalized_path = normalized_tmp.name
                normalized_tmp.close()
                
                subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error", "-i", clip_path,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", normalized_path
                ], timeout=MAX_DURATION, check=True)
                
                if os.path.exists(normalized_path) and os.path.getsize(normalized_path) > 1000:
                    normalized_clips.append(normalized_path)
                    if (i + 1) % 2 == 0:
                        print(f"[INFO] 🔧 Normalizzate {i + 1}/{len(scene_clips)} clip", flush=True)
                    
                try:
                    os.unlink(clip_path)
                except:
                    pass
                    
            except Exception as e:
                print(f"[WARNING] Skip clip {i + 1}: {e}", flush=True)
        
        if not normalized_clips:
            raise RuntimeError("Nessuna clip normalizzata")
        
        print(f"[SUCCESS] ✅ {len(normalized_clips)} clip normalizzate!", flush=True)
        
        total_clips_duration = sum(get_video_duration(p) for p in normalized_clips)
        
        concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
        
        if total_clips_duration < audio_duration and len(normalized_clips) > 1:
            loops_needed = math.ceil(audio_duration / total_clips_duration)
            print(f"[INFO] 🔁 Loop {loops_needed}x per coprire {audio_duration:.1f}s", flush=True)
            for _ in range(loops_needed):
                for norm_path in normalized_clips:
                    concat_list_tmp.write(f"file '{norm_path}'\n")
        else:
            for norm_path in normalized_clips:
                concat_list_tmp.write(f"file '{norm_path}'\n")
        
        concat_list_tmp.close()
        
        print(f"[INFO] 🎞️ Concatenazione clip...", flush=True)
        video_looped_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_looped_path = video_looped_tmp.name
        video_looped_tmp.close()
        
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list_tmp.name,
            "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-t", str(audio_duration), video_looped_path
        ], timeout=MAX_DURATION, check=True)
        
        os.unlink(concat_list_tmp.name)
        
        print(f"[INFO] 🎵 Merge video + audio...", flush=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_looped_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", output_path
        ], timeout=MAX_DURATION, check=True)
        
        try:
            os.unlink(video_looped_path)
            for norm_path in normalized_clips:
                os.unlink(norm_path)
        except:
            pass
        
        print(f"[SUCCESS] ✅ Video {platform} creato!", flush=True)
        return output_path
        
    except Exception as e:
        print(f"[ERROR] ❌ Errore creazione video {platform}: {e}", flush=True)
        traceback.print_exc()
        raise


def upload_to_r2(file_path, channel_name, platform):
    """Carica video su R2"""
    print(f"[INFO] ☁️ Upload R2 per {platform}...", flush=True)
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
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
        print(f"[SUCCESS] ✅ Upload completato!", flush=True)
        print(f"[INFO] 🔗 URL: {video_url}", flush=True)
        return video_url
    except Exception as e:
        print(f"[ERROR] ❌ Upload R2: {e}", flush=True)
        raise


def process_video_generation_background(task_id, videos, channel_name, row_number, sheet_id, webhook_url):
    """Processa 4 video in background"""
    print(f"\n{'='*80}", flush=True)
    print(f"[INFO] 🎬 TASK {task_id} START", flush=True)
    print(f"[INFO] Channel: {channel_name}", flush=True)
    print(f"[INFO] Row: {row_number}", flush=True)
    print(f"[INFO] Sheet ID: {sheet_id}", flush=True)
    print(f"[INFO] Videos: {len(videos)}", flush=True)
    print(f"{'='*80}\n", flush=True)
    
    try:
        video_urls = {}
        video_descriptions = {}  # ✅ NUOVO: Salva descrizioni
        
        for idx, video_data in enumerate(videos, 1):
            platform = video_data.get('platform')
            script = video_data.get('script')
            audio_url = video_data.get('audio_url')
            description = video_data.get('description', '')  # ✅ ESTRAI DESCRIZIONE
            
            print(f"\n{'='*80}", flush=True)
            print(f"[INFO] 🎥 VIDEO {idx}/4: {platform.upper()}", flush=True)
            print(f"{'='*80}", flush=True)
            
            try:
                with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as audio_file:
                    audio_path = audio_file.name
                
                download_audio_from_url(audio_url, audio_path)
                
                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as video_file:
                    video_path = video_file.name
                
                create_short_video_with_clips(video_data, audio_path, video_path, platform)
                
                video_url = upload_to_r2(video_path, channel_name, platform)
                
                try:
                    os.unlink(audio_path)
                    os.unlink(video_path)
                except:
                    pass
                
                video_urls[platform] = video_url
                video_descriptions[platform] = description  # ✅ SALVA DESCRIZIONE
                print(f"\n[SUCCESS] ✅✅✅ {platform.upper()} COMPLETATO! ✅✅✅\n", flush=True)
                
            except Exception as e:
                print(f"\n[ERROR] ❌❌❌ {platform.upper()} FALLITO: {e} ❌❌❌\n", flush=True)
                traceback.print_exc()
                continue
        
        # ✅ WEBHOOK N8N CON DESCRIZIONI
        if video_urls:
            final_webhook_url = webhook_url if webhook_url else N8N_CALLBACK_WEBHOOK_URL
            
            if final_webhook_url:
                print(f"\n[INFO] 🔔 Invio webhook a n8n: {final_webhook_url[:80]}...", flush=True)
                
                webhook_payload = {
                    'status': 'completed',
                    'task_id': task_id,
                    'row_number': row_number,
                    'sheet_id': sheet_id,
                    'channel_name': channel_name,
                    
                    # YouTube Shorts
                    'youtube_shorts_url': video_urls.get('youtube_shorts', ''),
                    'youtube_shorts': {
                        'description': video_descriptions.get('youtube_shorts', '')  # ✅ AGGIUNGI!
                    },
                    
                    # TikTok
                    'tiktok_url': video_urls.get('tiktok', ''),
                    'tiktok': {
                        'description': video_descriptions.get('tiktok', '')  # ✅ AGGIUNGI!
                    },
                    
                    # Instagram Reels
                    'instagram_reels_url': video_urls.get('instagram_reels', ''),
                    'instagram_reels': {
                        'description': video_descriptions.get('instagram_reels', '')  # ✅ AGGIUNGI!
                    },
                    
                    # Facebook Reels
                    'facebook_reels_url': video_urls.get('facebook_reels', ''),
                    'facebook_reels': {
                        'description': video_descriptions.get('facebook_reels', '')  # ✅ AGGIUNGI!
                    },
                    
                    'total_videos': len(video_urls)
                }
                
                try:
                    response = requests.post(
                        final_webhook_url,
                        json=webhook_payload,
                        headers={'Content-Type': 'application/json'},
                        timeout=30
                    )
                    response.raise_for_status()
                    print(f"[SUCCESS] ✅ Webhook n8n inviato: {response.status_code}", flush=True)
                    print(f"[INFO] Response: {response.text[:200]}", flush=True)
                    print(f"[INFO] 📝 Descrizioni inviate per {len(video_descriptions)} piattaforme", flush=True)
                except requests.exceptions.RequestException as e:
                    print(f"[ERROR] ❌ Errore webhook n8n: {e}", flush=True)
                    traceback.print_exc()
            else:
                print(f"[WARNING] ⚠️ Nessun webhook URL configurato", flush=True)
        else:
            print(f"[WARNING] ⚠️ Nessun video generato, webhook non inviato", flush=True)
        
        print(f"\n{'='*80}", flush=True)
        print(f"[SUCCESS] 🎉🎉🎉 TASK {task_id} COMPLETATO! 🎉🎉🎉", flush=True)
        print(f"[INFO] Videos generati: {len(video_urls)}/4", flush=True)
        for platform, url in video_urls.items():
            print(f"[INFO] 🔗 {platform}: {url}", flush=True)
        print(f"{'='*80}\n", flush=True)
        
    except Exception as e:
        print(f"\n{'='*80}", flush=True)
        print(f"[ERROR] ❌❌❌ TASK {task_id} FALLITO: {e} ❌❌❌", flush=True)
        print(f"{'='*80}\n", flush=True)
        traceback.print_exc()
        
        # Webhook errore
        final_webhook_url = webhook_url if webhook_url else N8N_CALLBACK_WEBHOOK_URL
        if final_webhook_url:
            try:
                error_payload = {
                    'status': 'failed',
                    'task_id': task_id,
                    'row_number': row_number,
                    'sheet_id': sheet_id,
                    'channel_name': channel_name,
                    'error': str(e),
                    'traceback': traceback.format_exc()
                }
                requests.post(
                    final_webhook_url,
                    json=error_payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=30
                )
                print(f"[INFO] Webhook errore inviato a n8n", flush=True)
            except Exception as webhook_err:
                print(f"[ERROR] Impossibile inviare webhook errore: {webhook_err}", flush=True)


# ==================== ENDPOINT ====================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200


@app.route('/api/generate', methods=['POST'])
def generate_videos():
    """Endpoint generico per tutti i canali"""
    try:
        print("\n" + "="*80, flush=True)
        print("=== NUOVA RICHIESTA ===", flush=True)
        print("="*80, flush=True)
        
        body_data = request.get_json(force=True)
        
        videos_list = []
        channel_name = None
        row_number = None
        sheet_id = None
        webhook_callback_url = None
        
        # Formato Object
        if 'youtube_shorts' in body_data:
            first_video = body_data.get('youtube_shorts', {})
            channel_name = first_video.get('channel_name')
            row_number = first_video.get('row_number')
            sheet_id = first_video.get('sheet_id')
            webhook_callback_url = first_video.get('webhook_callback_url')
            
            for key, platform in [('youtube_shorts', 'youtube_shorts'), ('tiktok', 'tiktok'), 
                                  ('instagram_reels', 'instagram_reels'), ('facebook_reels', 'facebook_reels')]:
                if key in body_data:
                    video_data = body_data[key]
                    video_data['platform'] = platform
                    videos_list.append(video_data)
        
        # Formato Array
        elif 'videos' in body_data:
            videos_list = body_data.get('videos', [])
            channel_name = body_data.get('channel_name')
            row_number = body_data.get('row_number')
            sheet_id = body_data.get('sheet_id')
            webhook_callback_url = body_data.get('webhook_callback_url')
        
        else:
            return jsonify({"error": "Formato non valido", "success": False}), 400
        
        if len(videos_list) != 4:
            return jsonify({"error": f"Servono 4 video, ricevuti {len(videos_list)}", "success": False}), 400
        
        channel_name = channel_name or "Unknown"
        row_number = row_number or 0
        sheet_id = sheet_id or "unknown"
        
        # Fallback webhook
        if not webhook_callback_url:
            webhook_callback_url = N8N_CALLBACK_WEBHOOK_URL
        
        task_id = str(uuid.uuid4())
        
        print(f"\n[INFO] 🚀 Task ID: {task_id}", flush=True)
        print(f"[INFO] 📺 Channel: {channel_name}", flush=True)
        print(f"[INFO] 📊 Row: {row_number}", flush=True)
        print(f"[INFO] 📋 Sheet ID: {sheet_id}", flush=True)
        print(f"[INFO] 🎥 Videos: {len(videos_list)}", flush=True)
        print(f"[INFO] 🔔 Webhook: {webhook_callback_url[:60] if webhook_callback_url else 'NONE'}", flush=True)
        
        thread = Thread(
            target=process_video_generation_background,
            args=(task_id, videos_list, channel_name, row_number, sheet_id, webhook_callback_url),
            daemon=True
        )
        thread.start()
        
        print(f"[SUCCESS] ✅ Task {task_id} avviato in background!", flush=True)
        
        return jsonify({
            "task_id": task_id,
            "status": "processing",
            "message": f"{len(videos_list)} video con {MAX_CLIPS} clip dinamiche ciascuno",
            "estimated_time": "3-5 minuti",
            "success": True
        }), 202
        
    except Exception as e:
        print(f"\n[ERROR] ❌ Errore endpoint: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e), "success": False}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"\n{'='*80}", flush=True)
    print(f"🚀 Social Shorts Generator (GENERICO)", flush=True)
    print(f"📍 Port: {port}", flush=True)
    print(f"🎬 Clips per video: {MAX_CLIPS}", flush=True)
    print(f"🔑 Pexels: {'✅' if PEXELS_API_KEY else '❌'}", flush=True)
    print(f"🔑 Pixabay: {'✅' if PIXABAY_API_KEY else '❌'}", flush=True)
    print(f"{'='*80}\n", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False)
