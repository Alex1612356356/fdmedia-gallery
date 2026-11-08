import json
import io
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


DATA = Path(os.environ.get("DATA", "/data"))
CACHE = Path(os.environ.get("CACHE", "/cache"))
MEDIA = Path(os.environ.get("MEDIA", "/media"))
INTERVAL = max(10, int(os.environ.get("INTERVAL", "30")))
LOG = logging.getLogger("fdmedia-worker")
STATUS_LOCK = threading.Lock()
IMAGE_EXTENSIONS = re.compile(r"\.(jpe?g|png|webp|tiff?|heic|avif)$", re.IGNORECASE)
RAW_EXTENSIONS = re.compile(r"\.(cr2|cr3|nef|arw|dng|raf|orf|rw2)$", re.IGNORECASE)
VIDEO_EXTENSIONS = re.compile(r"\.(mp4|mov|mkv|m4v|avi)$", re.IGNORECASE)


class AlbumRemoved(Exception):
    pass


def read_json(path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}") from exc


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_worker_status(**updates):
    with STATUS_LOCK:
        status_path = DATA / "worker-status.json"
        status = read_json(status_path, {})
        if not isinstance(status, dict):
            status = {}
        status.update(updates)
        status["heartbeat"] = time.time()
        write_json_atomic(status_path, status)


def detect_kind(name):
    if IMAGE_EXTENSIONS.search(name):
        return "image"
    if RAW_EXTENSIONS.search(name):
        return "raw"
    if VIDEO_EXTENSIONS.search(name):
        return "video"
    return None


def album_is_registered(slug):
    albums = read_json(DATA / "albums.json", {})
    if not isinstance(albums, dict):
        raise ValueError("albums.json must contain a JSON object")
    return slug in albums


def extract_raw_preview(source):
    last_error = None
    for tag in ("PreviewImage", "JpgFromRaw"):
        try:
            result = subprocess.run(
                ["exiftool", "-b", f"-{tag}", str(source)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            if result.stdout:
                return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
    raise RuntimeError(f"No embedded JPEG preview found in {source.name}") from last_error


def run_ffmpeg(args, slug=None):
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    if slug is None:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3600,
        )
        return

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 3600
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, 3600, output=stdout, stderr=stderr)
        try:
            stdout, stderr = process.communicate(timeout=min(1, remaining))
            break
        except subprocess.TimeoutExpired:
            try:
                still_registered = album_is_registered(slug)
            except (OSError, ValueError):
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise
            if not still_registered:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise AlbumRemoved(slug)

    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, command, output=stdout, stderr=stderr
        )


def make_webp(image_source, target, width, quality):
    target.parent.mkdir(parents=True, exist_ok=True)
    source = io.BytesIO(image_source) if isinstance(image_source, bytes) else image_source
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((width, width), Image.Resampling.LANCZOS)
        dimensions = image.size
        image.save(target, "WEBP", quality=quality, method=5)
    return dimensions


def process_media(source, slug, name, kind):
    root = CACHE / slug
    relative_name = Path(*Path(name).parts)
    thumbnail = root / "t" / relative_name.with_name(f"{relative_name.name}.webp")
    large = root / "l" / relative_name.with_name(f"{relative_name.name}.webp")
    if kind == "video":
        poster = root / "tmp" / relative_name.with_name(f"{relative_name.name}.jpg")
        video = root / "v" / relative_name.with_name(f"{relative_name.name}.mp4")
        poster.parent.mkdir(parents=True, exist_ok=True)
        video.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_ffmpeg(["-ss", "1", "-i", str(source), "-frames:v", "1", str(poster)], slug)
        except (OSError, subprocess.SubprocessError):
            run_ffmpeg(["-i", str(source), "-frames:v", "1", str(poster)], slug)
        run_ffmpeg(
            [
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-sn",
                "-dn",
                "-vf",
                "scale=-2:720:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "26",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(video),
            ],
            slug,
        )
        image_source = poster
    elif kind == "raw":
        image_source = extract_raw_preview(source)
    else:
        image_source = source

    large_width, large_height = make_webp(image_source, large, 2560, 82)
    make_webp(image_source, thumbnail, 640, 72)
    return {"w": large_width, "h": large_height}


def cache_is_complete(root, name, kind):
    relative_name = Path(*Path(name).parts)
    expected = [
        root / "t" / relative_name.with_name(f"{relative_name.name}.webp"),
        root / "l" / relative_name.with_name(f"{relative_name.name}.webp"),
    ]
    if kind == "video":
        expected.append(root / "v" / relative_name.with_name(f"{relative_name.name}.mp4"))
    return all(file.is_file() and file.stat().st_size > 0 for file in expected)


def scan_album(slug, album):
    if not album_is_registered(slug):
        LOG.info("stopping scan for removed album %s", slug)
        return

    folder = album.get("folder")
    if not isinstance(folder, str) or not folder:
        raise ValueError(f"Album {slug} has no valid media folder")
    source_dir = (MEDIA / folder).resolve()
    if not source_dir.is_relative_to(MEDIA.resolve()) or not source_dir.is_dir():
        raise ValueError(f"Album folder is missing or outside MEDIA: {folder}")

    root = CACHE / slug
    index_file = root / "index.json"
    try:
        previous = read_json(index_file, {})
    except ValueError:
        LOG.exception("rebuilding corrupt index %s", index_file)
        previous = {}
    old_items = previous.get("items", []) if isinstance(previous, dict) else []
    old = {item.get("n"): item for item in old_items if isinstance(item, dict) and isinstance(item.get("n"), str)}
    items = []
    sources = sorted(
        (source for source in source_dir.rglob("*") if source.is_file()),
        key=lambda entry: entry.relative_to(source_dir).as_posix().casefold(),
    )
    media_sources = [source for source in sources if detect_kind(source.name)]
    update_worker_status(
        state="processing",
        current_album=slug,
        current_file=None,
        processed=0,
        total=len(media_sources),
    )

    for position, source in enumerate(media_sources, start=1):
        if not album_is_registered(slug):
            LOG.info("stopping scan for removed album %s", slug)
            return
        name = source.relative_to(source_dir).as_posix()
        update_worker_status(current_file=name, processed=position - 1, total=len(media_sources))
        kind = detect_kind(name)
        try:
            if not source.resolve().is_relative_to(source_dir):
                LOG.warning("skip %s: file resolves outside album directory", name)
                continue
            stat = source.stat()
            prior = old.get(name)
            same_source = prior and prior.get("m") == stat.st_mtime_ns and prior.get("size") == stat.st_size
            if same_source and cache_is_complete(root, name, kind):
                dimensions = {"w": prior.get("w"), "h": prior.get("h")}
            else:
                dimensions = process_media(source, slug, name, kind)
                LOG.info("processed %s %s", slug, name)
            if not album_is_registered(slug):
                LOG.info("stopping scan for removed album %s", slug)
                return
            if not dimensions["w"] or not dimensions["h"]:
                raise ValueError("Could not determine image dimensions")
            items.append(
                {
                    "n": name,
                    "type": "video" if kind == "video" else "image",
                    "raw": kind == "raw",
                    "w": dimensions["w"],
                    "h": dimensions["h"],
                    "m": stat.st_mtime_ns,
                    "size": stat.st_size,
                }
            )
            write_json_atomic(
                index_file,
                {"updated": int(time.time() * 1000), "items": items},
            )
            update_worker_status(
                current_file=None,
                processed=position,
                total=len(media_sources),
                errors=0,
            )
        except AlbumRemoved:
            LOG.info("stopping scan for removed album %s", slug)
            return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None)
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace").strip()
            if detail:
                LOG.error("skip %s: %s; ffmpeg: %s", name, exc, detail[-1200:])
            else:
                LOG.error("skip %s: %s", name, exc)
            current_status = read_json(DATA / "worker-status.json", {})
            error_count = current_status.get("errors", 0) if isinstance(current_status, dict) else 0
            update_worker_status(
                current_file=None,
                processed=position,
                total=len(media_sources),
                errors=error_count + 1,
                last_error=f"{slug}/{name}: {exc}",
            )

    write_json_atomic(index_file, {"updated": int(time.time() * 1000), "items": items})


def scan_once():
    albums = read_json(DATA / "albums.json", {})
    if not isinstance(albums, dict):
        raise ValueError("albums.json must contain a JSON object")
    update_worker_status(
        state="scanning",
        current_album=None,
        current_file=None,
        processed=0,
        total=0,
        errors=0,
        last_error=None,
        scan_started=int(time.time()),
    )
    for slug, album in albums.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
            LOG.error("skip invalid album slug %r", slug)
            continue
        if not isinstance(album, dict):
            LOG.error("skip album %s: album entry must be a JSON object", slug)
            continue
        try:
            scan_album(slug, album)
        except (OSError, ValueError) as exc:
            LOG.error("album %s: %s", slug, exc)
            current = read_json(DATA / "worker-status.json", {})
            current_errors = current.get("errors", 0) if isinstance(current, dict) else 0
            update_worker_status(
                state="error",
                current_album=slug,
                current_file=None,
                errors=current_errors + 1,
                last_error=f"{slug}: {exc}",
            )
    update_worker_status(
        state="idle",
        current_album=None,
        current_file=None,
        last_scan=int(time.time()),
    )


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    heartbeat_stop = threading.Event()

    def heartbeat():
        while not heartbeat_stop.wait(15):
            try:
                update_worker_status()
            except (OSError, ValueError):
                LOG.exception("could not update worker heartbeat")

    threading.Thread(target=heartbeat, name="worker-heartbeat", daemon=True).start()
    while True:
        try:
            scan_once()
        except (OSError, ValueError) as exc:
            LOG.error("scan failed: %s", exc)
            update_worker_status(state="error", last_error=str(exc))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
