import os
import tempfile
import subprocess
import uuid
import datetime as dt
import json
from flask import Flask, request, jsonify
import boto3
from botocore.config import Config

app = Flask(__name__)

# Config R2
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_REGION = os.environ.get("R2_REGION", "auto")

# Config rotazione
MAX_CLIPS_RETENTION = int(os.environ.get("MAX_CLIPS_RETENTION", "200"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))

def get_s3_client():
    """Client S3 per Cloudflare R2"""
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    session = boto3.session.Session()
    return session.client(
        service_name="s3",
        region_name=R2_REGION,
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "virtual"}),
    )

def cleanup_old_clips(s3_client):
    """Rotazione clip R2"""
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix="social-clips/")
        
        all_clips = []
        for page in pages:
            if "Contents" not in page:
                continue
            for obj in page["Contents"]:
                if obj["Key"].endswith(".mp4"):
                    all_clips.append({
                        "Key": obj["Key"],
                        "LastModified": obj["LastModified"]
                    })
        
        if not all_clips:
            print("✅ Nessuna clip da pulire", flush=True)
            return
        
        all_clips.sort(key=lambda x: x["LastModified"])
        
        now = dt.datetime.now(dt.timezone.utc)
        retention_cutoff = now - dt.timedelta(days=RETENTION_DAYS)
        deleted_count = 0
        
        for clip in all_clips:
            if clip["LastModified"] < retention_cutoff:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=clip["Key"])
                deleted_count += 1
                print(f"🗑️ Cancellato (>{RETENTION_DAYS}gg): {clip['Key']}", flush=True)
        
        remaining_clips = [c for c in all_clips if c["LastModified"] >= retention_cutoff]
        if len(remaining_clips) > MAX_CLIPS_RETENTION:
            to_delete = remaining_clips[:len(remaining_clips) - MAX_CLIPS_RETENTION]
            for clip in to_delete:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=clip["Key"])
                deleted_count += 1
                print(f"🗑️ Cancellato (max limit): {clip['Key']}", flush=True)
        
        if deleted_count > 0:
            print(f"✅ Rotazione completata: {deleted_count} clip cancellate", flush=True)
        else:
            print("✅ Nessuna clip da cancellare", flush=True)
            
    except Exception as e:
        print(f"⚠️ Errore rotazione R2: {str(e)}", flush=True)

def get_video_duration(video_path):
    """Ottiene durata video in secondi"""
    try:
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")
        
        file_size = os.path.getsize(video_path)
        if file_size < 1024:
            raise ValueError(f"Video file too small: {file_size} bytes")
        
        print(f"   Analyzing video file: {file_size/1024/1024:.1f}MB", flush=True)
        
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        
        data = json.loads(result.stdout)
        
        if 'format' in data and 'duration' in data['format']:
            duration = float(data['format']['duration'])
            if duration > 0:
                return duration
        
        if 'streams' in data:
            for stream in data['streams']:
                if stream.get('codec_type') == 'video' and 'duration' in stream:
                    duration = float(stream['duration'])
                    if duration > 0:
                        return duration
        
        raise ValueError("Duration not found in video metadata")
        
    except Exception as e:
        raise Exception(f"Failed to get video duration: {str(e)}")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "social-shorts-generator"})

@app.route("/debug", methods=["GET"])
def debug():
    """Endpoint debug per verificare tools installati"""
    debug_info = {}
    
    # Test yt-dlp
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        debug_info["yt-dlp_version"] = result.stdout.strip()
        debug_info["yt-dlp_installed"] = True
    except Exception as e:
        debug_info["yt-dlp_error"] = str(e)
        debug_info["yt-dlp_installed"] = False
    
    # Test ffmpeg
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        debug_info["ffmpeg_version"] = result.stdout.split('\n')[0]
        debug_info["ffmpeg_installed"] = True
    except Exception as e:
        debug_info["ffmpeg_error"] = str(e)
        debug_info["ffmpeg_installed"] = False
    
    # Test ffprobe
    try:
        result = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, timeout=5)
        debug_info["ffprobe_version"] = result.stdout.split('\n')[0]
        debug_info["ffprobe_installed"] = True
    except Exception as e:
        debug_info["ffprobe_error"] = str(e)
        debug_info["ffprobe_installed"] = False
    
    # Test Node.js
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        debug_info["nodejs_version"] = result.stdout.strip()
        debug_info["nodejs_installed"] = True
    except Exception as e:
        debug_info["nodejs_error"] = str(e)
        debug_info["nodejs_installed"] = False
    
    # Test download semplice
    try:
        result = subprocess.run([
            "yt-dlp",
            "--print", "title",
            "--quiet",
            "--no-warnings",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        ], capture_output=True, text=True, timeout=30)
        
        debug_info["test_download"] = {
            "success": result.returncode == 0,
            "title": result.stdout.strip(),
            "stderr": result.stderr[:200] if result.stderr else None
        }
    except Exception as e:
        debug_info["test_download"] = {
            "success": False,
            "error": str(e)
        }
    
    return jsonify(debug_info)

@app.route("/process-social-video", methods=["POST"])
def process_social_video():
    """Processa video YouTube per Agente Social"""
    video_path = None
    clip_paths = []
    
    try:
        data = request.get_json(force=True) or {}
        video_url = data.get("video_url")
        video_id = data.get("video_id", "unknown")
        canale_id = data.get("canale_id", "unknown")
        
        if not video_url:
            return jsonify({"success": False, "error": "video_url required"}), 400
        
        print("=" * 80, flush=True)
        print(f"🎬 AGENTE SOCIAL START", flush=True)
        print(f"📹 Video: {video_url}", flush=True)
        print(f"🆔 ID: {video_id} | Canale: {canale_id}", flush=True)
        
        # STEP 1: Download video
        print("📥 Step 1/5: Downloading video...", flush=True)
        video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_path = video_tmp.name
        video_tmp.close()
        
        print(f"   Temp file: {video_path}", flush=True)
        
        # yt-dlp con formato semplice (default client con fallback automatici)
        download_result = subprocess.run([
            "yt-dlp",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
            "--merge-output-format", "mp4",
            "-o", video_path,
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            video_url
        ], timeout=300, capture_output=True, text=True, check=False)
        
        print(f"   yt-dlp exit code: {download_result.returncode}", flush=True)
        
        if download_result.returncode != 0:
            print(f"   yt-dlp stderr: {download_result.stderr[:500]}", flush=True)
            raise Exception(f"yt-dlp download failed (code {download_result.returncode})")
        
        # Verifica file scaricato
        if not os.path.exists(video_path):
            raise Exception("Video file not created")
        
        video_size_mb = os.path.getsize(video_path) / 1024 / 1024
        print(f"   File size: {video_size_mb:.2f}MB", flush=True)
        
        if video_size_mb < 0.5:
            raise Exception(f"Downloaded video too small: {video_size_mb:.2f}MB")
        
        print(f"✅ Video downloaded: {video_size_mb:.1f}MB", flush=True)
        
        # STEP 2: Analizza durata video
        print("⏱️ Step 2/5: Analyzing duration...", flush=True)
        total_duration = get_video_duration(video_path)
        print(f"✅ Duration: {total_duration:.1f}s ({total_duration/60:.1f} min)", flush=True)
        
        # Verifica durata minima
        min_duration = 180
        if total_duration < min_duration:
            return jsonify({
                "success": False,
                "error": f"Video troppo corto ({total_duration:.0f}s). Serve almeno {min_duration}s (3 min)."
            }), 400
        
        # STEP 3: Calcola timestamp dinamici
        print("📍 Step 3/5: Calculating timestamps...", flush=True)
        clip_duration = 45
        safety_margin = 60
        usable_duration = total_duration - safety_margin - clip_duration
        
        clips_moments = []
        captions = ["Hook iniziale", "Momento chiave", "Valore centrale", "CTA finale"]
        
        for i in range(4):
            start_seconds = int((usable_duration / 5) * (i + 1))
            clips_moments.append({
                "start": start_seconds,
                "duration": clip_duration,
                "caption": captions[i]
            })
        
        timestamps_str = ', '.join([str(c['start']) + 's' for c in clips_moments])
        print(f"   Timestamps: {timestamps_str}", flush=True)
        
        # STEP 4: Taglia clip verticali + upload R2
        print("✂️ Step 4/5: Cutting & uploading clips...", flush=True)
        s3_client = get_s3_client()
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        clips_data = []
        
        for i, moment in enumerate(clips_moments, 1):
            clip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            clip_path = clip_tmp.name
            clip_tmp.close()
            
            # Taglia clip verticale 1080x1920 (9:16)
            print(f"   Cutting clip {i}/4...", flush=True)
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", str(moment["start"]),
                "-i", video_path,
                "-t", str(moment["duration"]),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                clip_path
            ], check=True, timeout=120)
            
            # Verifica clip creata
            if not os.path.exists(clip_path) or os.path.getsize(clip_path) < 1024:
                raise Exception(f"Failed to create clip {i}")
            
            # Upload R2
            print(f"   Uploading clip {i}/4 to R2...", flush=True)
            object_key = f"social-clips/{today}/{canale_id}/{video_id}_clip{i}_{uuid.uuid4().hex[:8]}.mp4"
            s3_client.upload_file(
                Filename=clip_path,
                Bucket=R2_BUCKET_NAME,
                Key=object_key,
                ExtraArgs={"ContentType": "video/mp4"}
            )
            
            public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
            clips_data.append({
                "clip_number": i,
                "storage_url": public_url,
                "duration": f"00:00:{moment['duration']}",
                "caption": moment["caption"],
                "start_time": f"00:{moment['start']//60:02d}:{moment['start']%60:02d}"
            })
            
            clip_paths.append(clip_path)
            clip_size = os.path.getsize(clip_path) / 1024 / 1024
            print(f"   ✅ Clip {i}/4: {clip_size:.1f}MB → R2", flush=True)
        
        # STEP 5: Rotazione clip vecchie
        print("🗑️ Step 5/5: Cleaning old clips...", flush=True)
        cleanup_old_clips(s3_client)
        
        print(f"✅ AGENTE SOCIAL COMPLETED: 4 clips generated!", flush=True)
        print("=" * 80, flush=True)
        
        return jsonify({
            "success": True,
            "video_id": video_id,
            "canale_id": canale_id,
            "video_duration": total_duration,
            "clips": clips_data,
            "clips_count": len(clips_data)
        })
    
    except subprocess.TimeoutExpired as e:
        print(f"❌ TIMEOUT ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": f"Processing timeout: {str(e)}"}), 500
    except subprocess.CalledProcessError as e:
        print(f"❌ COMMAND ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": f"Command failed: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ GENERAL ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    
    finally:
        # Cleanup file temporanei
        for path in [video_path] + clip_paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except Exception as e:
                    print(f"⚠️ Cleanup warning: {e}", flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
