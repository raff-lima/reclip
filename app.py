import os
import re
import uuid
import glob
import json
import subprocess
import threading
import logging
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
LOG_FILE = os.path.join(os.path.dirname(__file__), "reclip.log")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Logging setup — file + console
logger = logging.getLogger("reclip")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_ch)

# Check Node.js availability at startup (needed for yt-dlp JS challenge solving)
try:
    _node = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
    logger.info("Node.js found: %s", _node.stdout.strip())
except FileNotFoundError:
    logger.warning("Node.js NOT found — yt-dlp signature solving will fail")

# Check yt-dlp version
try:
    _ytdlp = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
    logger.info("yt-dlp version: %s", _ytdlp.stdout.strip())
except FileNotFoundError:
    logger.error("yt-dlp NOT found")

jobs = {}


def base_ytdlp_cmd(for_info=False):
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-check-certificates",
        "--js-runtimes", "node",
    ]
    # Always pass cookies when available (needed for bot detection bypass on VPS IPs)
    if os.path.isfile(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
        logger.debug("[cmd] cookies loaded")
    # tv_embedded: suporta cookies + retorna formatos DASH completos (sem SABR)
    # Aceita cookies e é projetado para players de alta qualidade (TV embed)
    cmd += ["--extractor-args", "youtube:player_client=tv_embedded"]
    logger.debug("[cmd] Using tv_embedded client")
    return cmd


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    job["progress"] = 0
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = base_ytdlp_cmd() + ["-o", out_template, "--progress", "--newline"]

    if format_choice == "audio":
        bitrate = format_id or "320"
        # Baixa melhor áudio disponível e re-encoda para o bitrate MP3 escolhido
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", f"{bitrate}k"]
    else:
        if format_id:
            fmt = f"bv*[height<={format_id}]+ba[ext=m4a]/bv*[height<={format_id}]+ba/b[height<={format_id}]"
        else:
            fmt = "bv*+ba[ext=m4a]/bv*+ba/b"
        cmd += ["-f", fmt, "--merge-output-format", "mp4"]

    cmd.append(url)
    logger.info("[download] job=%s url=%s cmd=%s", job_id, url, " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )

        # Rastreia progresso por fases (vídeo + áudio = 2 fases; only-audio = 1)
        phases_seen = 0
        last_pct = 0.0
        stderr_lines = []

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line_s = line.rstrip()
            if line_s:
                stderr_lines.append(line_s)
            # Detecta nova fase (início de download)
            if "[download] Destination:" in line_s:
                phases_seen += 1
                last_pct = 0.0
            # Parseia percentual: "[download]  45.3% of ..."
            if "[download]" in line_s and "%" in line_s:
                m = re.search(r'(\d+\.?\d*)%', line_s)
                if m:
                    last_pct = float(m.group(1))
                    # 2 fases: vídeo=0-50%, áudio=50-100%
                    # 1 fase:  áudio=0-100%
                    if format_choice == "audio":
                        job["progress"] = min(int(last_pct), 99)
                    else:
                        base = 50 * (phases_seen - 1)
                        job["progress"] = min(int(base + last_pct / 2), 99)

        proc.wait(timeout=300)
        stderr_out = "\n".join(stderr_lines)

        if proc.returncode != 0:
            job["status"] = "error"
            logger.error("[download] job=%s FAILED stderr:\n%s", job_id, stderr_out)
            error_lines = [l for l in stderr_lines if "ERROR" in l]
            job["error"] = error_lines[-1] if error_lines else (stderr_lines[-1] if stderr_lines else "Unknown error")
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        logger.info("[download] job=%s DONE file=%s", job_id, chosen)
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
        logger.error("[download] job=%s TIMEOUT", job_id)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        logger.exception("[download] job=%s EXCEPTION", job_id)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = base_ytdlp_cmd(for_info=True) + ["-j", url]
    logger.info("[info] url=%s cmd=%s", url, " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.error("[info] url=%s FAILED stderr:\n%s", url, stderr)
            error_lines = [l for l in stderr.splitlines() if "ERROR" in l]
            error_msg = error_lines[-1] if error_lines else (stderr.splitlines()[-1] if stderr else "Unknown error")
            return jsonify({"error": error_msg}), 400

        info = json.loads(result.stdout)

        # Formatos de vídeo — melhor stream por resolução
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f
        formats = sorted(
            [{"id": str(h), "label": f"{h}p", "height": h} for h in best_by_height],
            key=lambda x: x["height"], reverse=True
        )

        # Áudio: bitrates fixos de saída — ffmpeg re-encoda na qualidade escolhida
        audio_formats = [
            {"id": "320", "label": "320kbps", "abr": 320},
            {"id": "256", "label": "256kbps", "abr": 256},
            {"id": "192", "label": "192kbps", "abr": 192},
            {"id": "128", "label": "128kbps", "abr": 128},
        ]

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
            "audio_formats": audio_formats,
        })
    except subprocess.TimeoutExpired:
        logger.error("[info] url=%s TIMEOUT", url)
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        logger.exception("[info] url=%s EXCEPTION", url)
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job.get("progress", 0),
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/logs")
def view_logs():
    """Return the last 200 lines of the log file."""
    if not os.path.isfile(LOG_FILE):
        return "No logs yet.\n", 200, {"Content-Type": "text/plain; charset=utf-8"}
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    tail = lines[-200:]
    return "".join(tail), 200, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
