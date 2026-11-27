import os, re, uuid, shutil, socket, ipaddress, subprocess
from pathlib import Path
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
UPLOADS, OUTPUTS = ROOT / "uploads", ROOT / "outputs"
UPLOADS.mkdir(exist_ok=True); OUTPUTS.mkdir(exist_ok=True)
MAX_BYTES = 750 * 1024 * 1024
ALLOWED_HOST_SUFFIXES = (".youtube.com", ".youtu.be", ".googlevideo.com", ".vimeo.com", ".dailymotion.com")

app = FastAPI(title="ClipForge AI")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

def validate_public_url(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Enter a valid public http/https video URL.")
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        raise HTTPException(400, "Private/local URLs are not allowed.")
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise HTTPException(400, "Private/local network URLs are not allowed.")
    except socket.gaierror:
        raise HTTPException(400, "The link host could not be resolved.")
    return p

def download_direct(url: str, dest: Path):
    validate_public_url(url)
    try:
        with requests.get(url, stream=True, timeout=(15, 60), allow_redirects=True,
                          headers={"User-Agent": "ClipForgeAI/1.0"}) as r:
            r.raise_for_status()
            # Validate redirect destination too.
            validate_public_url(r.url)
            ctype = r.headers.get("content-type", "").lower()
            if not (ctype.startswith("video/") or urlparse(r.url).path.lower().endswith(
                (".mp4", ".mov", ".mkv", ".webm", ".m4v"))):
                raise HTTPException(400, "This link does not appear to point directly to a video file. Try a direct MP4/MOV/WEBM link.")
            total = 0
            with dest.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if not chunk: continue
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, "Video is larger than the 750 MB limit.")
                    f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not access this video link: {e}")

def is_youtube(url: str):
    host = (urlparse(url).hostname or "").lower()
    return host == "youtu.be" or host.endswith("youtube.com")

def make_clips(src: Path, job: str, clip_count: int, clip_seconds: int):
    if shutil.which("ffmpeg") is None:
        raise HTTPException(500, "FFmpeg is not installed on the server.")
    try:
        import whisper
        model = whisper.load_model(os.getenv("WHISPER_MODEL", "base"))
        result = model.transcribe(str(src), fp16=False)
        segments = result.get("segments", [])
        if not segments:
            raise RuntimeError("No speech was detected. Try a video with clear speech.")
        duration = max(float(s["end"]) for s in segments)
        windows, step = [], max(10, clip_seconds // 2)
        for start in range(0, max(1, int(duration - clip_seconds + 1)), step):
            end = min(duration, start + clip_seconds)
            words = " ".join(s["text"] for s in segments if s["start"] < end and s["end"] > start).lower()
            if len(words.split()) < 12: continue
            score = len(words.split()) / max(1, clip_seconds)
            score += sum(words.count(k) for k in [" but ", "why", "how", "secret", "never", "best", "amazing", "important", "mistake", "imagine"])
            score += words.count("?") * 2 + words.count("!") * 1.5
            windows.append((score, start, end, words))
        windows.sort(reverse=True)
        chosen = []
        for item in windows:
            if all(abs(item[1] - old[1]) > clip_seconds * .75 for old in chosen):
                chosen.append(item)
            if len(chosen) >= min(max(1, clip_count), 10): break
        if not chosen: chosen = [(0, 0, min(duration, clip_seconds), "")]
        results = []
        for i, (_, start, end, transcript) in enumerate(sorted(chosen, key=lambda x: x[1]), 1):
            out = OUTPUTS / f"{job}_clip_{i}.mp4"
            cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(end-start),
                   "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            results.append({"name": f"Clip {i}", "start": round(start,1), "end": round(end,1),
                            "preview": f"/api/file/{out.name}", "download": f"/api/file/{out.name}",
                            "transcript": transcript[:220]})
        return results
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, f"Could not process video: {str(e)}")

@app.get("/")
def home(): return FileResponse(ROOT / "static" / "index.html")

@app.post("/api/create-from-link")
def create_from_link(url: str = Form(...), clip_count: int = Form(5), clip_seconds: int = Form(45),
                     rights_confirmed: bool = Form(False)):
    if not rights_confirmed:
        raise HTTPException(400, "Please confirm you own this video or have permission to edit it.")
    p = validate_public_url(url)
    job = uuid.uuid4().hex[:10]
    src = UPLOADS / f"{job}.video"
    if is_youtube(url):
        raise HTTPException(400, "YouTube link importing is not enabled in this hosted starter. YouTube does not provide unrestricted video-file access through a normal link. Upload a video you own, or use a direct MP4/MOV/WEBM link. For your own YouTube channel, an authorized YouTube OAuth import workflow can be added.")
    download_direct(url, src)
    clips = make_clips(src, job, clip_count, clip_seconds)
    return {"job_id": job, "clips": clips, "note": "Review clips before publishing."}

@app.post("/api/create-clips")
async def create_clips(video: UploadFile = File(...), clip_count: int = Form(5), clip_seconds: int = Form(45),
                       rights_confirmed: bool = Form(False)):
    if not rights_confirmed:
        raise HTTPException(400, "Please confirm you own this video or have permission to edit it.")
    if not video.filename or not video.filename.lower().endswith((".mp4",".mov",".mkv",".webm",".m4v")):
        raise HTTPException(400, "Please upload MP4, MOV, MKV, WEBM or M4V.")
    job = uuid.uuid4().hex[:10]
    src = UPLOADS / f"{job}_{Path(video.filename).name}"
    size = 0
    with src.open("wb") as f:
        while True:
            chunk = await video.read(1024 * 1024)
            if not chunk: break
            size += len(chunk)
            if size > MAX_BYTES:
                src.unlink(missing_ok=True)
                raise HTTPException(413, "Video is larger than the 750 MB limit.")
            f.write(chunk)
    return {"job_id": job, "clips": make_clips(src, job, clip_count, clip_seconds),
            "note": "Review clips before publishing."}

@app.get("/api/file/{filename}")
def get_file(filename: str):
    path = OUTPUTS / Path(filename).name
    if not path.exists(): raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","7860")))
