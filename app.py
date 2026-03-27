"""
VidDrop — Flask + yt-dlp
Railway-ready: serves frontend + API from one app.
"""

import os
import re
import uuid
import threading
import time
from flask import Flask, request, jsonify, send_file, after_this_request, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, origins=["*"])

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Track active downloads for cleanup
active_jobs = {}

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def clean_old_files():
    """Delete files older than 30 minutes."""
    while True:
        now = time.time()
        for fname in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            try:
                if now - os.path.getmtime(fpath) > 1800:
                    os.remove(fpath)
            except Exception:
                pass
        time.sleep(300)

# Start cleanup thread
threading.Thread(target=clean_old_files, daemon=True).start()


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80]


def format_size(bytes_val):
    if not bytes_val:
        return "غير معروف"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def get_ydl_opts_info(url: str) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }


# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_video_info():
    """
    POST /api/info
    Body: { "url": "https://..." }
    Returns video metadata + available formats.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "الرابط مطلوب"}), 400

    try:
        with yt_dlp.YoutubeDL(get_ydl_opts_info(url)) as ydl:
            info = ydl.extract_info(url, download=False)

        # Build clean format list
        formats_raw = info.get("formats", [])
        formats_out = []

        # Group: video+audio merged formats
        seen_res = set()
        for f in reversed(formats_raw):
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec == "none" or acodec == "none":
                continue
            height = f.get("height")
            ext = f.get("ext", "mp4")
            if not height or height in seen_res:
                continue
            seen_res.add(height)
            formats_out.append({
                "id": f["format_id"],
                "label": f"MP4 {height}p",
                "desc": f"{height}p • {format_size(f.get('filesize') or f.get('filesize_approx'))}",
                "quality": str(height),
                "type": "video",
                "ext": ext,
            })

        # Sort highest quality first
        formats_out.sort(key=lambda x: int(x["quality"]) if x["quality"].isdigit() else 0, reverse=True)

        # Add audio-only
        best_audio = next(
            (f for f in reversed(formats_raw)
             if f.get("vcodec") == "none" and f.get("acodec") != "none"),
            None
        )
        if best_audio:
            formats_out.append({
                "id": best_audio["format_id"],
                "label": "MP3 Audio",
                "desc": f"صوت فقط • {format_size(best_audio.get('filesize') or best_audio.get('filesize_approx'))}",
                "quality": "audio",
                "type": "audio",
                "ext": "mp3",
            })

        # If no merged formats found, add best video
        if not formats_out:
            formats_out.append({
                "id": "bestvideo+bestaudio/best",
                "label": "أفضل جودة متاحة",
                "desc": "أعلى جودة",
                "quality": "best",
                "type": "video",
                "ext": "mp4",
            })

        thumbnail = info.get("thumbnail") or ""
        if not thumbnail:
            thumbs = info.get("thumbnails") or []
            thumbnail = thumbs[-1]["url"] if thumbs else ""

        return jsonify({
            "title": info.get("title", "فيديو"),
            "duration": info.get("duration_string") or str(info.get("duration", "")),
            "uploader": info.get("uploader") or info.get("channel", ""),
            "thumbnail": thumbnail,
            "platform": info.get("extractor_key", ""),
            "formats": formats_out,
        })

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private" in msg or "private" in msg:
            return jsonify({"error": "الفيديو خاص أو محمي"}), 400
        if "not available" in msg.lower():
            return jsonify({"error": "الفيديو غير متاح في منطقتك"}), 400
        return jsonify({"error": f"تعذّر قراءة الرابط: {msg[:120]}"}), 400
    except Exception as e:
        return jsonify({"error": f"خطأ غير متوقع: {str(e)[:100]}"}), 500


@app.route("/api/download", methods=["POST"])
def download_video():
    """
    POST /api/download
    Body: { "url": "...", "format_id": "...", "type": "video|audio" }
    Returns the file directly as a download.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "bestvideo+bestaudio/best").strip()
    dl_type = data.get("type", "video")

    if not url:
        return jsonify({"error": "الرابط مطلوب"}), 400

    job_id = uuid.uuid4().hex[:12]
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title).60s.%(ext)s")

    if dl_type == "audio":
        ydl_opts = {
            "format": format_id,
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    else:
        ydl_opts = {
            "format": f"{format_id}+bestaudio/best" if "+" not in format_id else format_id,
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        # Find the actual output file (postprocessing may change ext)
        candidates = [
            filename,
            filename.rsplit(".", 1)[0] + ".mp4",
            filename.rsplit(".", 1)[0] + ".mp3",
        ]
        out_file = next((f for f in candidates if os.path.exists(f)), None)

        # Fallback: find by job_id prefix
        if not out_file:
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(job_id):
                    out_file = os.path.join(DOWNLOAD_DIR, f)
                    break

        if not out_file or not os.path.exists(out_file):
            return jsonify({"error": "فشل إنشاء الملف"}), 500

        safe_name = sanitize_filename(info.get("title", "video"))
        ext = out_file.rsplit(".", 1)[-1]
        dl_name = f"{safe_name}.{ext}"

        @after_this_request
        def cleanup(response):
            try:
                os.remove(out_file)
            except Exception:
                pass
            return response

        mimetype = "audio/mpeg" if ext == "mp3" else "video/mp4"
        return send_file(
            out_file,
            as_attachment=True,
            download_name=dl_name,
            mimetype=mimetype,
        )

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"خطأ في التحميل: {str(e)[:120]}"}), 400
    except Exception as e:
        return jsonify({"error": f"خطأ: {str(e)[:100]}"}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
