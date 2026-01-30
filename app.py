import os
import tempfile
import subprocess
import uuid
import datetime as dt
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

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "social-shorts-generator"})

@app.route("/process-social-video", methods=["POST"])
def process_social_video():
    """
    Processa video YouTube per Agente Social:
    1. Download con yt-dlp
    2. Taglia 4 clip verticali 9:16
    3. Upload su R2
    """
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
        print("📥 Step 1/3: Downloading video...", flush=True)
        video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_path = video_tmp.name
        video_tmp.close()
        
        subprocess.run([
            "yt-dlp",
            "-f", "best[height<=1080]",
            "-o", video_path,
            "--no-playlist",
            video_url
        ], check=True, timeout=300)
        
        print(f"✅ Video downloaded: {os.path.getsize(video_path)/1024/1024:.1f}MB", flush=True)
        
        # STEP 2: Momenti predefiniti (dopo implementeremo AI)
        clips_moments = [
            {"start": "00:00:10", "duration": 45, "caption": "Hook iniziale"},
            {"start": "00:05:30", "duration": 50, "caption": "Momento chiave"},
            {"start": "00:12:00", "duration": 40, "caption": "Valore centrale"},
            {"start": "00:18:30", "duration": 48, "caption": "CTA finale"}
        ]
        
        # STEP 3: Taglia clip verticali + upload R2
        print("✂️ Step 2/3: Cutting & uploading clips...", flush=True)
        s3_client = get_s3_client()
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        clips_data = []
        
        for i, moment in enumerate(clips_moments, 1):
            clip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            clip_path = clip_tmp.name
            clip_tmp.close()
            
            # Taglia clip verticale 1080x1920 (9:16)
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", moment["start"],
                "-i", video_path,
                "-t", str(moment["duration"]),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                clip_path
            ], check=True, timeout=120)
            
            # Upload R2
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
                "start_time": moment["start"]
            })
            
            clip_paths.append(clip_path)
            print(f"   ✅ Clip {i}/4: {moment['duration']}s → R2", flush=True)
        
        print(f"✅ AGENTE SOCIAL COMPLETED: 4 clips generated!", flush=True)
        print("=" * 80, flush=True)
        
        return jsonify({
            "success": True,
            "video_id": video_id,
            "canale_id": canale_id,
            "clips": clips_data,
            "clips_count": len(clips_data)
        })
    
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Processing timeout"}), 500
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500
    
    finally:
        # Cleanup
        for path in [video_path] + clip_paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except:
                    pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
