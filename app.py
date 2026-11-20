import os
import json
import uuid
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
UPLOADS, OUTPUTS, TEMP = ROOT / "uploads", ROOT / "outputs", ROOT / "temp"
HISTORY = ROOT / "history.json"
for folder in (UPLOADS, OUTPUTS, TEMP):
    folder.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
ALLOWED = {"mp4", "mov", "mkv", "webm"}
MAX_FILES = 5
JOBS = {}
LOCK = threading.Lock()

FILTERS = {
    "natural": "eq=contrast=1:saturation=1",
    "cinematic": "eq=brightness=-0.02:contrast=1.12:saturation=0.88",
    "vivid": "eq=brightness=0.02:contrast=1.12:saturation=1.3",
    "vintage": "eq=contrast=0.92:saturation=0.72,colorbalance=rs=0.06:gs=0.01:bs=-0.05",
    "bw": "hue=s=0,eq=contrast=1.08",
    "warm": "colorbalance=rs=0.08:gs=0.02:bs=-0.08",
    "cool": "colorbalance=rs=-0.06:bs=0.08",
}

def read_history():
    try:
        data = json.loads(HISTORY.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []

def write_history(item):
    items = read_history()
    items.insert(0, item)
    HISTORY.write_text(json.dumps(items[:50], indent=2), encoding="utf-8")

def video_info(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,width,height",
           "-of", "json", str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if p.returncode:
        raise RuntimeError("FFprobe could not read: " + path.name)
    data = json.loads(p.stdout)
    duration = float(data.get("format", {}).get("duration") or 0)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video or duration <= 0:
        raise RuntimeError("Invalid video: " + path.name)
    return duration, any(s.get("codec_type") == "audio" for s in streams)

def number(value, default, low, high):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default

def dimensions(resolution, ratio):
    h = int(resolution)
    if ratio == "9:16":
        return max(2, int(h * 9 / 16) // 2 * 2), h
    if ratio == "1:1":
        return h, h
    return max(2, int(h * 16 / 9) // 2 * 2), h

def progress_update(job_id, percent, message):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id]["progress"] = min(98, max(0, int(percent)))
            JOBS[job_id]["message"] = message

def encode_clip(source, target, start, duration, has_audio, settings,
                watermark_path, job_id, clip_no, clip_count, elapsed, total):
    width, height = dimensions(settings["resolution"], settings["ratio"])
    vf = FILTERS[settings["filter"]]
    vf += f",scale={width}:{height}:force_original_aspect_ratio=decrease"
    vf += f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if watermark_path:
        # Drawtext reads user text from a file to avoid shell/text escaping problems.
        vf += (f",drawtext=textfile='{watermark_path}':fontcolor=white:"
               "fontsize=h/22:x=w-tw-24:y=h-th-24:"
               "box=1:boxcolor=black@0.45:boxborderw=10")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", str(start), "-i", str(source)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-t", str(duration), "-map", "0:v:0"]
    cmd += ["-map", "0:a:0" if has_audio else "1:a:0"]
    cmd += ["-vf", vf, "-af", f"volume={settings['volume']}",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", str(settings["crf"]), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart", "-progress", "pipe:1",
            "-nostats", str(target)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                    pct = ((elapsed + min(seconds, duration)) / total) * 97
                    progress_update(job_id, pct,
                                    f"Encoding clip {clip_no}/{clip_count}")
                except ValueError:
                    pass
        error = proc.stderr.read()
        code = proc.wait()
        if code:
            raise RuntimeError(error[-1000:] or "FFmpeg encoding failed.")
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("Output clip was not created.")

def process_job(job_id, paths, settings, output_name):
    job_dir = TEMP / job_id
    job_dir.mkdir(exist_ok=True)
    output = OUTPUTS / output_name
    created_files = []
    try:
        details = [video_info(p) for p in paths]
        ranges = []
        for path, (duration, has_audio) in zip(paths, details):
            start = settings["start"]
            # End=0 means full clip. A larger end is safely clamped to duration.
            end = duration if settings["end"] <= 0 else min(settings["end"], duration)
            if start >= duration or end <= start:
                raise RuntimeError(
                    f"Trim range is invalid for {path.name} "
                    f"(video length {duration:.1f}s; start {start:.1f}s; end {end:.1f}s). "
                    "Set Start to 0 and End to 0 to use the full video."
                )
            ranges.append((start, end - start, has_audio))

        total = sum(item[1] for item in ranges)
        watermark = None
        if settings["watermark"]:
            watermark = job_dir / "watermark.txt"
            watermark.write_text(settings["watermark"][:80], encoding="utf-8")

        elapsed = 0.0
        for index, (path, clip_range) in enumerate(zip(paths, ranges), 1):
            start, duration, has_audio = clip_range
            target = job_dir / f"clip_{index:02d}.mp4"
            encode_clip(path, target, start, duration, has_audio, settings,
                        str(watermark) if watermark else None,
                        job_id, index, len(paths), elapsed, total)
            created_files.append(target)
            elapsed += duration

        progress_update(job_id, 98, "Finalizing video")
        if len(created_files) == 1:
            os.replace(created_files[0], output)
        else:
            concat = job_dir / "concat.txt"
            concat.write_text(
                "".join("file '" + str(p).replace("'", "'\\''") + "'\n"
                        for p in created_files), encoding="utf-8")
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "concat", "-safe", "0", "-i", str(concat),
                 "-c", "copy", "-movflags", "+faststart", str(output)],
                capture_output=True, text=True, timeout=1800)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:] or "Video merge failed.")

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("Final MP4 was not created.")
        item = {
            "filename": output_name,
            "created": datetime.now(timezone.utc).isoformat(),
            "size": output.stat().st_size,
            "clips": len(paths),
            "resolution": settings["resolution"],
        }
        write_history(item)
        with LOCK:
            JOBS[job_id].update(status="done", progress=100,
                                message="Export complete", output=output_name)
    except Exception as exc:
        output.unlink(missing_ok=True)
        with LOCK:
            JOBS[job_id].update(status="error", message=str(exc)[:1400])
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        for path in job_dir.glob("*"):
            path.unlink(missing_ok=True)
        try:
            job_dir.rmdir()
        except OSError:
            pass

@app.get("/")
def home():
    return render_template("index.html")

@app.post("/api/transform")
def transform():
    files = [f for f in request.files.getlist("videos") if f and f.filename]
    if not files:
        return jsonify(error="Please select at least one video."), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"Maximum {MAX_FILES} videos can be merged."), 400

    job_id = uuid.uuid4().hex
    saved = []
    try:
        for i, file in enumerate(files, 1):
            name = secure_filename(file.filename or "")
            if not name or "." not in name or name.rsplit(".", 1)[1].lower() not in ALLOWED:
                raise ValueError("Allowed formats: MP4, MOV, MKV and WEBM.")
            path = UPLOADS / f"{job_id}_{i}.{name.rsplit('.', 1)[1].lower()}"
            file.save(path)
            saved.append(path)

        mode = request.form.get("mode", "auto")
        filter_name = request.form.get("filter", "natural")
        if mode == "auto":
            filter_name = request.form.get("preset", "cinematic")
        if filter_name not in FILTERS:
            filter_name = "natural"

        resolution = request.form.get("resolution", "720")
        if resolution not in {"480", "720", "1080", "2160"}:
            resolution = "720"
        ratio = request.form.get("ratio", "16:9")
        if ratio not in {"16:9", "9:16", "1:1"}:
            ratio = "16:9"

        settings = {
            "start": number(request.form.get("start"), 0, 0, 86400),
            "end": number(request.form.get("end"), 0, 0, 86400),
            "volume": number(request.form.get("volume"), 1, 0, 2),
            "filter": filter_name,
            "resolution": resolution,
            "ratio": ratio,
            "watermark": request.form.get("watermark", "").strip(),
            "crf": int(number(request.form.get("quality"), 23, 18, 32)),
        }
        output_name = f"pro_{job_id}.mp4"
        with LOCK:
            JOBS[job_id] = {"status": "queued", "progress": 0,
                            "message": "Queued", "output": None}
        threading.Thread(target=process_job,
                         args=(job_id, saved, settings, output_name),
                         daemon=True).start()
        return jsonify(job_id=job_id), 202
    except (ValueError, OSError) as exc:
        for path in saved:
            path.unlink(missing_ok=True)
        return jsonify(error=str(exc)), 400

@app.get("/api/progress/<job_id>")
def job_progress(job_id):
    with LOCK:
        item = JOBS.get(job_id)
        if not item:
            return jsonify(error="Job not found. It may have expired after a server restart."), 404
        return jsonify(item)

@app.get("/api/history")
def history():
    return jsonify(read_history())

@app.get("/download/<filename>")
def download(filename):
    if not filename.startswith("pro_") or not filename.endswith(".mp4") or Path(filename).name != filename:
        return "Invalid filename", 400
    if not (OUTPUTS / filename).is_file():
        return "File not found", 404
    return send_from_directory(OUTPUTS, filename, as_attachment=True)

@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="Upload is larger than the 500 MB limit."), 413

if __name__ == "__main__":
    # Local development only. Do not expose this server directly to the internet.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
