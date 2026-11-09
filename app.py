import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from pathlib import PurePosixPath

from flask import Flask, abort, jsonify, make_response, redirect, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from cli import ALBUM_THEMES, add_album, decrypt_album_pin


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
log = logging.getLogger("fdmedia-gallery")
admin_write_lock = threading.Lock()
DEFAULT_GALLERY_DESCRIPTION = "Am păstrat aici momentele voastre. Bucurați-vă de ele."
GALLERY_UPLOAD_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".avif",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2",
    ".mp4", ".mov", ".mkv", ".m4v", ".avi",
}


def _read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read JSON file %s", path)
        raise RuntimeError(f"Could not read required JSON file: {path}") from None


def _atomic_json(path: Path, value):
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


def _valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug))


def _parse_expiration(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Data expirării nu este validă.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Data expirării nu este validă.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Data expirării trebuie să includă fusul orar.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _album_expired(album):
    expires_at = album.get("expires_at")
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("expiration must include a timezone")
    except ValueError:
        log.error("Album has invalid expires_at value: %r", expires_at)
        return True
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)


def _media_relative_path(name):
    if not isinstance(name, str) or not name or "\\" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path


def _drive_relative_path(name, *, allow_root=False):
    if allow_root and name == "":
        return PurePosixPath()
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path


def _valid_drive_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and name == name.strip()
        and not name.endswith(".")
        and not any(char in name for char in ("/", "\\", "\x00"))
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
        and len(name.encode("utf-8")) <= 255
    )


def _section_for_name(name):
    path = _media_relative_path(name)
    if path is None:
        return None
    if len(path.parts) == 1:
        return {"id": "__root__", "title": "General"}
    return {"id": path.parts[0], "title": path.parts[0]}


def _album_links(album):
    links = album.get("links", [])
    if not isinstance(links, list):
        log.error("Album links must be a list")
        return []
    return [
        link
        for link in links
        if isinstance(link, dict)
        and isinstance(link.get("id"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", link["id"])
        and isinstance(link.get("allow_upload"), bool)
    ]


def create_app(
    *,
    data_dir=None,
    cache_dir=None,
    media_dir=None,
    secret=None,
    admin_password=None,
    cookie_secure=None,
    max_drive_upload_bytes=None,
):
    data_dir = Path(data_dir or os.environ.get("DATA", "/data")).resolve()
    cache_dir = Path(cache_dir or os.environ.get("CACHE", "/cache")).resolve()
    media_dir = Path(media_dir or os.environ.get("MEDIA", "/media")).resolve()
    secret = secret if secret is not None else os.environ.get("SECRET")
    admin_password = admin_password if admin_password is not None else os.environ.get("ADMIN_PASSWORD")
    if not secret or len(secret.encode("utf-8")) < 32:
        raise RuntimeError("Set SECRET to a random value of at least 32 bytes.")
    if admin_password is not None and len(admin_password) < 24:
        raise RuntimeError("ADMIN_PASSWORD must be at least 24 characters.")
    try:
        drive_max_upload_bytes = int(
            max_drive_upload_bytes
            if max_drive_upload_bytes is not None
            else os.environ.get("MAX_DRIVE_UPLOAD_BYTES", str(10 * 1024**3))
        )
    except ValueError as exc:
        raise RuntimeError("MAX_DRIVE_UPLOAD_BYTES must be a positive integer.") from exc
    if drive_max_upload_bytes < 1:
        raise RuntimeError("MAX_DRIVE_UPLOAD_BYTES must be a positive integer.")
    if cookie_secure is None:
        cookie_secure = os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")

    drive_dir = data_dir / "drive"
    drive_dir.mkdir(parents=True, exist_ok=True)
    drive_dir = drive_dir.resolve()
    if not drive_dir.is_relative_to(data_dir):
        raise RuntimeError("The Drive storage folder must be inside DATA.")

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.config.update(
        DATA_DIR=data_dir,
        CACHE_DIR=cache_dir,
        MEDIA_DIR=media_dir,
        DRIVE_DIR=drive_dir,
        DRIVE_MAX_UPLOAD_BYTES=drive_max_upload_bytes,
        SECRET_KEY=secret,
        COOKIE_SECURE=cookie_secure,
    )

    proxy_hops = int(os.environ.get("PROXY_HOPS", "0"))
    if proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops, x_host=proxy_hops)

    attempts = {}
    attempts_lock = threading.Lock()

    def admin_signing_key():
        return hmac.new(secret.encode(), f"admin:{admin_password}".encode(), hashlib.sha256).digest()

    def valid_admin_cookie():
        value = request.cookies.get("fd_admin", "")
        try:
            expires, signature = value.split(".", 1)
        except ValueError:
            return False
        if not expires.isdecimal() or not HEX_64_RE.fullmatch(signature):
            return False
        if int(expires) <= int(time.time() * 1000):
            return False
        expected = hmac.new(admin_signing_key(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def require_admin(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if not admin_password:
                return jsonify(error="Admin access is not configured. Set ADMIN_PASSWORD and restart the app."), 503
            if not valid_admin_cookie():
                return jsonify(error="Admin login required."), 401
            return handler(*args, **kwargs)

        return wrapped

    def read_albums():
        value = _read_json(data_dir / "albums.json", {})
        return value if isinstance(value, dict) else {}

    def album_for(slug):
        if not _valid_slug(slug):
            abort(404)
        album = read_albums().get(slug)
        if not isinstance(album, dict):
            abort(404)
        return album

    def index_for(slug):
        value = _read_json(cache_dir / slug / "index.json", {"items": []})
        return value if isinstance(value, dict) and isinstance(value.get("items"), list) else {"items": []}

    def album_source_dir(album):
        folder = album.get("folder")
        if not isinstance(folder, str) or not folder:
            abort(404)
        base = (media_dir / folder).resolve()
        if not base.is_relative_to(media_dir) or not base.is_dir():
            abort(404)
        return base

    def album_source_file(album, name):
        relative = _media_relative_path(name)
        if relative is None:
            abort(404)
        base = album_source_dir(album)
        file_path = (base / Path(*relative.parts)).resolve()
        if not file_path.is_relative_to(base) or not file_path.is_file():
            abort(404)
        return file_path

    def drive_path(relative, *, allow_root=False):
        path = _drive_relative_path(relative, allow_root=allow_root)
        if path is None:
            return None
        candidate = drive_dir
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve()
        if not resolved.is_relative_to(drive_dir):
            return None
        return resolved

    def album_cover_options(slug):
        index = index_for(slug)
        return sorted(
            item["n"]
            for item in index["items"]
            if isinstance(item, dict)
            and isinstance(item.get("n"), str)
            and item.get("type") == "image"
            and _media_relative_path(item["n"]) is not None
        )

    def request_link_allows_upload(album):
        link_id = request.args.get("link", "")
        return any(
            link["id"] == link_id and link["allow_upload"]
            for link in _album_links(album)
        )

    def save_gallery_uploads(folder):
        request.max_content_length = drive_max_upload_bytes + 1024 * 1024
        uploads = request.files.getlist("files")
        if not uploads or len(uploads) > 100:
            return jsonify(error="Selectați între 1 și 100 de fotografii sau videoclipuri."), 400
        names = [upload.filename for upload in uploads]
        if (
            any(
                not _valid_drive_name(name)
                or Path(name).suffix.lower() not in GALLERY_UPLOAD_EXTENSIONS
                for name in names
            )
            or len(set(names)) != len(names)
        ):
            return jsonify(error="Unul sau mai multe nume sau formate de fișier nu sunt acceptate."), 400

        staged = []
        uploaded = []
        committed = []
        total_size = 0
        try:
            with admin_write_lock:
                for name in names:
                    if (folder / name).exists():
                        return jsonify(error=f"Există deja un fișier cu numele „{name}”."), 409
                for upload, name in zip(uploads, names):
                    with tempfile.NamedTemporaryFile(
                        dir=folder, prefix=".fdmedia-upload-", delete=False
                    ) as temporary:
                        staged_path = Path(temporary.name)
                        staged.append((staged_path, folder / name))
                        file_size = 0
                        while True:
                            chunk = upload.stream.read(1024 * 1024)
                            if not chunk:
                                break
                            file_size += len(chunk)
                            total_size += len(chunk)
                            if total_size > drive_max_upload_bytes:
                                return jsonify(error="Încărcarea depășește limita configurată pentru transfer."), 413
                            temporary.write(chunk)
                        if file_size == 0:
                            return jsonify(error=f"Fișierul „{name}” este gol."), 400
                for staged_path, target in staged:
                    if target.exists():
                        return jsonify(error=f"Există deja un fișier cu numele „{target.name}”."), 409
                    os.replace(staged_path, target)
                    committed.append(target)
                    uploaded.append({"name": target.name, "size": target.stat().st_size})
        except OSError:
            for target in committed:
                target.unlink(missing_ok=True)
            log.exception("Could not save gallery uploads in %s", folder)
            return jsonify(error="Nu am putut salva fișierele încărcate."), 500
        finally:
            for staged_path, _target in staged:
                staged_path.unlink(missing_ok=True)

        return jsonify(ok=True, uploaded=uploaded), 201

    def customer_access_error(slug):
        if _album_expired(album_for(slug)):
            return jsonify(error="Galeria a expirat. Contactați administratorul pentru acces."), 410
        return None

    def has_valid_cookie(slug):
        value = request.cookies.get(f"fd_{slug}", "")
        try:
            exp, signature = value.split(".", 1)
        except ValueError:
            return False
        if not exp.isdecimal() or not HEX_64_RE.fullmatch(signature):
            return False
        if int(exp) <= int(time.time() * 1000):
            return False
        expected = hmac.new(secret.encode(), f"{slug}{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def require_auth(handler):
        @wraps(handler)
        def wrapped(slug, *args, **kwargs):
            expired_response = customer_access_error(slug)
            if expired_response:
                return expired_response
            if not has_valid_cookie(slug):
                return jsonify(error="PIN necesar"), 401
            return handler(slug, *args, **kwargs)

        return wrapped

    @app.get("/")
    def home():
        return redirect("https://fdmedia.studio")

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.get("/admin")
    def admin_page():
        if not admin_password:
            abort(404)
        response = make_response(send_from_directory(Path(__file__).parent / "public", "admin.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/drive")
    def drive_page():
        if not admin_password:
            abort(404)
        response = make_response(send_from_directory(Path(__file__).parent / "public", "drive.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/admin/session")
    def admin_session():
        if not admin_password:
            return jsonify(error="Admin access is not configured."), 503
        response = jsonify(authenticated=valid_admin_cookie())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/admin/login")
    def admin_login():
        if not admin_password:
            return jsonify(error="Admin access is not configured. Set ADMIN_PASSWORD and restart the app."), 503
        now = time.time()
        key = (request.remote_addr or "unknown", "admin")
        with attempts_lock:
            recent = [timestamp for timestamp in attempts.get(key, []) if now - timestamp < 900]
            if len(recent) >= 8:
                attempts[key] = recent
                return jsonify(error="Prea multe încercări. Reîncercați peste 15 minute."), 429
            attempts[key] = recent

        body = request.get_json(silent=True)
        password = body.get("password", "") if isinstance(body, dict) else ""
        if not hmac.compare_digest(str(password).encode(), admin_password.encode()):
            with attempts_lock:
                attempts[key].append(now)
            return jsonify(error="Parola de administrare este incorectă."), 401

        expires = str(int(time.time() * 1000) + 12 * 60 * 60 * 1000)
        signature = hmac.new(admin_signing_key(), expires.encode(), hashlib.sha256).hexdigest()
        response = make_response(jsonify(ok=True))
        response.set_cookie(
            "fd_admin",
            f"{expires}.{signature}",
            max_age=12 * 60 * 60,
            httponly=True,
            secure=cookie_secure,
            samesite="Strict",
            path="/",
        )
        return response

    @app.post("/api/admin/logout")
    def admin_logout():
        response = make_response(jsonify(ok=True))
        response.delete_cookie("fd_admin", path="/", httponly=True, secure=cookie_secure, samesite="Strict")
        return response

    @app.get("/api/admin/albums")
    @require_admin
    def admin_albums():
        result = [
            {
                "slug": slug,
                "title": album.get("title", ""),
                "folder": album.get("folder", ""),
                "theme": album.get("theme", "default"),
                "description": album.get("description", ""),
                "cover": album.get("cover"),
                "expires_at": album.get("expires_at"),
                "expired": _album_expired(album),
                "pin_recoverable": isinstance(album.get("pin_encrypted"), str),
                "cover_options": album_cover_options(slug),
                "links": [
                    {"id": link["id"], "allow_upload": link["allow_upload"]}
                    for link in _album_links(album)
                ],
            }
            for slug, album in read_albums().items()
            if isinstance(album, dict)
        ]
        return jsonify(sorted(result, key=lambda album: album["title"].casefold()))

    @app.post("/api/admin/albums")
    @require_admin
    def admin_create_album():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Cerere invalidă."), 400
        slug = body.get("slug")
        title = body.get("title")
        folder = body.get("folder")
        pin = body.get("pin")
        theme = body.get("theme", "default")
        description = body.get("description", "")
        expires_at = body.get("expires_at")
        if not all(isinstance(value, str) for value in (slug, title, pin, theme, description)):
            return jsonify(error="Completați toate câmpurile."), 400
        if folder is None:
            folder = ""
        if not isinstance(folder, str) or "\\" in folder or "\x00" in folder:
            return jsonify(error="Calea folderului foto nu este validă."), 400
        if not SLUG_RE.fullmatch(slug):
            return jsonify(error="Identificatorul galeriei nu este valid."), 400
        if not title.strip() or len(pin) < 4 or len(pin) > 128:
            return jsonify(error="Numele galeriei și PIN-ul trebuie să fie valide."), 400
        if not folder:
            folder = slug
        folder_path = (media_dir / folder).resolve()
        if not folder_path.is_relative_to(media_dir) or folder_path == media_dir:
            return jsonify(error="Calea folderului foto nu este validă."), 400
        folder = folder_path.relative_to(media_dir).as_posix()
        try:
            expires_at = _parse_expiration(expires_at)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if theme not in ALBUM_THEMES:
            return jsonify(error="Tema selectată nu este validă."), 400
        if len(description) > 500:
            return jsonify(error="Descrierea poate avea maximum 500 de caractere."), 400
        with admin_write_lock:
            albums = read_albums()
            if slug in albums:
                return jsonify(error="Există deja un album cu acest identificator."), 409
            try:
                folder_path.mkdir(parents=True, exist_ok=True)
                result = add_album(
                    data_dir,
                    media_dir,
                    slug,
                    title,
                    folder,
                    pin,
                    theme,
                    description,
                    secret=secret,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return jsonify(error=str(exc)), 400
            if expires_at:
                albums = read_albums()
                albums[slug]["expires_at"] = expires_at
                _atomic_json(data_dir / "albums.json", albums)
        log.info("Created album %s through admin page", slug)
        return jsonify(ok=True, slug=slug, title=title, url=f"/g/{slug}", result=result), 201

    @app.get("/api/admin/albums/<slug>/pin")
    @require_admin
    def admin_reveal_album_pin(slug):
        album = album_for(slug)
        encrypted_pin = album.get("pin_encrypted")
        if not isinstance(encrypted_pin, str):
            return jsonify(error="PIN-ul existent nu poate fi recuperat fără schimbare."), 409
        try:
            pin = decrypt_album_pin(encrypted_pin, secret)
        except ValueError:
            log.exception("Could not decrypt stored PIN for album %s", slug)
            return jsonify(error="Nu am putut decripta PIN-ul galeriei. Verifică SECRET-ul aplicației."), 500
        response = jsonify(pin=pin)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/api/admin/albums/<slug>/links")
    @require_admin
    def admin_create_album_link(slug):
        album_for(slug)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("allow_upload"), bool):
            return jsonify(error="Selectați dacă acest link permite încărcarea."), 400
        with admin_write_lock:
            albums = read_albums()
            album = albums.get(slug)
            if not isinstance(album, dict):
                abort(404)
            links = album.setdefault("links", [])
            if not isinstance(links, list):
                return jsonify(error="Lista linkurilor galeriei nu este validă."), 500
            link = {"id": secrets.token_urlsafe(24), "allow_upload": body["allow_upload"]}
            links.append(link)
            _atomic_json(data_dir / "albums.json", albums)
        return jsonify(ok=True, link=link)

    @app.put("/api/admin/albums/<slug>/links/<link_id>")
    @require_admin
    def admin_update_album_link(slug, link_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("allow_upload"), bool):
            return jsonify(error="Selectați dacă acest link permite încărcarea."), 400
        with admin_write_lock:
            albums = read_albums()
            album = albums.get(slug)
            if not _valid_slug(slug) or not isinstance(album, dict):
                abort(404)
            links = album.get("links", [])
            if not isinstance(links, list):
                return jsonify(error="Lista linkurilor galeriei nu este validă."), 500
            link = next(
                (entry for entry in links if isinstance(entry, dict) and entry.get("id") == link_id),
                None,
            )
            if link is None:
                abort(404)
            link["allow_upload"] = body["allow_upload"]
            _atomic_json(data_dir / "albums.json", albums)
        return jsonify(ok=True)

    @app.delete("/api/admin/albums/<slug>/links/<link_id>")
    @require_admin
    def admin_delete_album_link(slug, link_id):
        with admin_write_lock:
            albums = read_albums()
            album = albums.get(slug)
            if not _valid_slug(slug) or not isinstance(album, dict):
                abort(404)
            links = album.get("links", [])
            if not isinstance(links, list):
                return jsonify(error="Lista linkurilor galeriei nu este validă."), 500
            remaining = [
                link
                for link in links
                if not (isinstance(link, dict) and link.get("id") == link_id)
            ]
            if len(remaining) == len(links):
                abort(404)
            album["links"] = remaining
            _atomic_json(data_dir / "albums.json", albums)
        return jsonify(ok=True)

    @app.put("/api/admin/albums/<slug>")
    @require_admin
    def admin_update_album(slug):
        if not _valid_slug(slug):
            abort(404)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Cerere invalidă."), 400
        if not body or not set(body).issubset({"theme", "cover", "expires_at"}):
            return jsonify(error="Cerere invalidă."), 400
        with admin_write_lock:
            albums = read_albums()
            album = albums.get(slug)
            if not isinstance(album, dict):
                abort(404)
            if "theme" in body:
                theme = body["theme"]
                if not isinstance(theme, str) or theme not in ALBUM_THEMES:
                    return jsonify(error="Tema selectată nu este validă."), 400
                album["theme"] = theme
            if "cover" in body:
                cover = body["cover"]
                if cover is not None and (not isinstance(cover, str) or cover not in album_cover_options(slug)):
                    return jsonify(error="Fotografia aleasă nu este disponibilă în galerie."), 400
                if cover:
                    album["cover"] = cover
                else:
                    album.pop("cover", None)
            if "expires_at" in body:
                try:
                    expires_at = _parse_expiration(body["expires_at"])
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
                if expires_at:
                    album["expires_at"] = expires_at
                else:
                    album.pop("expires_at", None)
            _atomic_json(data_dir / "albums.json", albums)
        log.info("Updated settings for album %s", slug)
        return jsonify(ok=True, slug=slug)

    @app.get("/api/admin/status")
    @require_admin
    def admin_status():
        status = _read_json(data_dir / "worker-status.json", {})
        if not isinstance(status, dict):
            status = {}
        heartbeat = status.get("heartbeat")
        try:
            last_heartbeat = float(heartbeat)
            alive = time.time() - last_heartbeat <= max(90, int(os.environ.get("INTERVAL", "30")) * 3)
        except (TypeError, ValueError):
            alive = False
        status["alive"] = alive
        return jsonify(status)

    @app.get("/api/admin/drive")
    @require_admin
    def admin_drive_list():
        relative = request.args.get("folder", "")
        folder = drive_path(relative, allow_root=True)
        if folder is None:
            return jsonify(error="Calea folderului nu este validă."), 400
        if not folder.is_dir():
            return jsonify(error="Folderul nu a fost găsit."), 404
        entries = []
        try:
            for entry in folder.iterdir():
                if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                    continue
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "type": "folder" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                })
        except OSError:
            log.exception("Could not list Drive folder %s", relative)
            return jsonify(error="Nu am putut citi conținutul folderului."), 500
        entries.sort(key=lambda item: (item["type"] != "folder", item["name"].casefold()))
        response = jsonify(folder=relative, items=entries)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/api/admin/drive/folders")
    @require_admin
    def admin_drive_create_folder():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Cerere invalidă."), 400
        parent = body.get("folder", "")
        name = body.get("name")
        if not isinstance(parent, str) or not _valid_drive_name(name):
            return jsonify(error="Numele folderului nu este valid."), 400
        parent_path = drive_path(parent, allow_root=True)
        if parent_path is None:
            return jsonify(error="Calea folderului nu este validă."), 400
        with admin_write_lock:
            if not parent_path.is_dir():
                return jsonify(error="Folderul curent nu a fost găsit."), 404
            try:
                (parent_path / name).mkdir()
            except FileExistsError:
                return jsonify(error="Există deja un fișier sau folder cu acest nume."), 409
            except OSError:
                log.exception("Could not create Drive folder %s in %s", name, parent)
                return jsonify(error="Nu am putut crea folderul."), 500
        return jsonify(ok=True), 201

    @app.post("/api/admin/drive/upload")
    @require_admin
    def admin_drive_upload():
        request.max_content_length = drive_max_upload_bytes + 1024 * 1024
        folder_name = request.form.get("folder", "")
        folder = drive_path(folder_name, allow_root=True)
        if folder is None:
            return jsonify(error="Calea folderului nu este validă."), 400
        if not folder.is_dir():
            return jsonify(error="Folderul nu a fost găsit."), 404
        uploads = request.files.getlist("files")
        if not uploads or len(uploads) > 100:
            return jsonify(error="Selectați între 1 și 100 de fișiere."), 400

        names = [upload.filename for upload in uploads]
        if any(not _valid_drive_name(name) for name in names) or len(set(names)) != len(names):
            return jsonify(error="Unul sau mai multe nume de fișier nu sunt valide sau se repetă."), 400

        staged = []
        uploaded = []
        total_size = 0
        try:
            with admin_write_lock:
                for upload, name in zip(uploads, names):
                    target = folder / name
                    if target.exists():
                        return jsonify(error=f"Există deja un fișier cu numele „{name}”."), 409
                for upload, name in zip(uploads, names):
                    with tempfile.NamedTemporaryFile(dir=folder, prefix=".fdmedia-upload-", delete=False) as temporary:
                        staged_path = Path(temporary.name)
                        staged.append((staged_path, folder / name))
                        while True:
                            chunk = upload.stream.read(1024 * 1024)
                            if not chunk:
                                break
                            total_size += len(chunk)
                            if total_size > drive_max_upload_bytes:
                                return jsonify(error="Încărcarea depășește limita configurată pentru transfer."), 413
                            temporary.write(chunk)
                for staged_path, target in staged:
                    if target.exists():
                        return jsonify(error=f"Există deja un fișier cu numele „{target.name}”."), 409
                    os.replace(staged_path, target)
                    uploaded.append({"name": target.name, "size": target.stat().st_size})
        except OSError:
            log.exception("Could not upload files to Drive folder %s", folder_name)
            return jsonify(error="Nu am putut salva fișierele încărcate."), 500
        finally:
            for staged_path, _target in staged:
                staged_path.unlink(missing_ok=True)

        return jsonify(ok=True, uploaded=uploaded), 201

    @app.get("/api/admin/drive/download")
    @require_admin
    def admin_drive_download():
        relative = request.args.get("path", "")
        file_path = drive_path(relative)
        if file_path is None:
            return jsonify(error="Calea fișierului nu este validă."), 400
        if not file_path.is_file():
            return jsonify(error="Fișierul nu a fost găsit."), 404
        response = send_file(file_path, as_attachment=True, download_name=file_path.name, conditional=True)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.delete("/api/admin/drive")
    @require_admin
    def admin_drive_delete():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            return jsonify(error="Cerere invalidă."), 400
        file_path = drive_path(body["path"])
        if file_path is None:
            return jsonify(error="Calea fișierului nu este validă."), 400
        with admin_write_lock:
            if not file_path.is_file():
                return jsonify(error="Fișierul nu a fost găsit."), 404
            try:
                file_path.unlink()
            except OSError:
                log.exception("Could not delete Drive file %s", body["path"])
                return jsonify(error="Nu am putut șterge fișierul."), 500
        log.info("Deleted Drive file %s", body["path"])
        return jsonify(ok=True)

    @app.delete("/api/admin/albums/<slug>")
    @require_admin
    def admin_delete_album(slug):
        if not _valid_slug(slug):
            abort(404)
        with admin_write_lock:
            albums = read_albums()
            if slug not in albums:
                abort(404)
            del albums[slug]
            _atomic_json(data_dir / "albums.json", albums)
        log.info("Deleted album %s through admin page", slug)
        return jsonify(ok=True)

    @app.post("/api/admin/albums/<slug>/upload")
    @require_admin
    def admin_upload_to_album(slug):
        album = album_for(slug)
        return save_gallery_uploads(album_source_dir(album))

    @app.get("/g/<slug>")
    def gallery(slug):
        album_for(slug)
        response = make_response(send_from_directory(Path(__file__).parent / "public", "index.html"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/static/<path:filename>")
    def static_file(filename):
        return send_from_directory(Path(__file__).parent / "public", filename, max_age=3600)

    @app.get("/vendor/<path:filename>")
    def vendor_file(filename):
        return send_from_directory(Path(__file__).parent / "vendor", filename, max_age=604800)

    @app.post("/api/<slug>/login")
    def login(slug):
        album = album_for(slug)
        if _album_expired(album):
            return jsonify(error="Galeria a expirat. Contactați administratorul pentru acces."), 410
        now = time.time()
        key = (request.remote_addr or "unknown", slug)
        with attempts_lock:
            for previous_key, timestamps in list(attempts.items()):
                active = [timestamp for timestamp in timestamps if now - timestamp < 900]
                if active:
                    attempts[previous_key] = active
                else:
                    del attempts[previous_key]
            recent = attempts.get(key, [])
            if len(recent) >= 8:
                return jsonify(error="Prea multe încercări. Reîncercați peste 15 minute."), 429
            attempts[key] = recent

        body = request.get_json(silent=True)
        pin = body.get("pin", "") if isinstance(body, dict) else ""
        salt, stored_hash = album.get("salt"), album.get("hash")
        if not isinstance(salt, str) or not isinstance(stored_hash, str) or not HEX_64_RE.fullmatch(stored_hash):
            log.error("Album %s has invalid PIN credentials", slug)
            abort(500)
        try:
            derived = hashlib.scrypt(str(pin).encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
        except (ValueError, MemoryError):
            log.exception("Could not validate PIN for album %s", slug)
            abort(500)
        if not hmac.compare_digest(derived, stored_hash):
            with attempts_lock:
                attempts[key].append(now)
            return jsonify(error="PIN incorect"), 401

        cookie_expiration = time.time() + 30 * 86400
        if album.get("expires_at"):
            expiration = datetime.fromisoformat(str(album["expires_at"]).replace("Z", "+00:00"))
            cookie_expiration = min(cookie_expiration, expiration.timestamp())
        expires = str(int(cookie_expiration * 1000))
        signature = hmac.new(secret.encode(), f"{slug}{expires}".encode(), hashlib.sha256).hexdigest()
        response = make_response(jsonify(ok=True, title=album.get("title", "")))
        response.set_cookie(
            f"fd_{slug}",
            f"{expires}.{signature}",
            max_age=max(1, int(cookie_expiration - time.time())),
            httponly=True,
            secure=cookie_secure,
            samesite="Lax",
            path="/",
        )
        return response

    @app.get("/api/<slug>/items")
    @require_auth
    def items(slug):
        album = album_for(slug)
        favorites = _read_json(data_dir / "favs" / f"{slug}.json", [])
        if not isinstance(favorites, list):
            favorites = []
        gallery_items = index_for(slug)["items"]
        for item in gallery_items:
            if isinstance(item, dict) and isinstance(item.get("n"), str):
                section = _section_for_name(item["n"])
                if section:
                    item["section"] = section["id"]
        section_map = {}
        for item in gallery_items:
            if isinstance(item, dict) and isinstance(item.get("section"), str):
                section = _section_for_name(item.get("n"))
                if section:
                    section_map[section["id"]] = section["title"]
        cover = album.get("cover")
        if cover not in album_cover_options(slug):
            cover = None
        cover_item = next(
            (item for item in gallery_items if isinstance(item, dict) and item.get("n") == cover),
            None,
        )
        response = jsonify(
            title=album.get("title", ""),
            description=album.get("description") or DEFAULT_GALLERY_DESCRIPTION,
            theme=(
                album.get("theme")
                if isinstance(album.get("theme"), str) and album.get("theme") in ALBUM_THEMES
                else "default"
            ),
            items=gallery_items,
            favs=favorites,
            sections=[{"id": key, "title": value} for key, value in sorted(section_map.items(), key=lambda pair: pair[1].casefold())],
            cover=cover,
            cover_m=cover_item.get("m") if cover_item else None,
            upload_enabled=request_link_allows_upload(album),
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/<slug>/upload")
    @require_auth
    def upload_to_gallery(slug):
        album = album_for(slug)
        if not request_link_allows_upload(album):
            return jsonify(error="Acest link nu permite încărcarea fișierelor."), 403
        return save_gallery_uploads(album_source_dir(album))

    @app.post("/api/<slug>/fav")
    @require_auth
    def favorite(slug):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not isinstance(body.get("on"), bool):
            return jsonify(error="Invalid favorite request"), 400
        name = body["name"]
        if not any(isinstance(item, dict) and item.get("n") == name for item in index_for(slug)["items"]):
            return "", 400
        path = data_dir / "favs" / f"{slug}.json"
        old = _read_json(path, [])
        favorites = set(value for value in old if isinstance(value, str)) if isinstance(old, list) else set()
        if body["on"]:
            favorites.add(name)
        else:
            favorites.discard(name)
        result = sorted(favorites)
        _atomic_json(path, result)
        return jsonify(result)

    @app.get("/api/<slug>/download")
    @require_auth
    def download_gallery(slug):
        album = album_for(slug)
        items = index_for(slug)["items"]
        if not items:
            return jsonify(error="Galeria nu conține fișiere de descărcat."), 404

        archive_file = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
        try:
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("n"), str):
                        continue
                    source = album_source_file(album, item["n"])
                    archive.write(source, arcname=item["n"])
            archive_file.seek(0)
        except OSError:
            archive_file.close()
            log.exception("Could not create download archive for album %s", slug)
            return jsonify(error="Nu am putut crea arhiva galeriei."), 500

        response = send_file(
            archive_file,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{slug}.zip",
            conditional=False,
        )
        response.call_on_close(archive_file.close)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/api/<slug>/download-favorites")
    @require_auth
    def download_favorites(slug):
        album = album_for(slug)
        favorites = _read_json(data_dir / "favs" / f"{slug}.json", [])
        if not isinstance(favorites, list):
            favorites = []
        selected = set(value for value in favorites if isinstance(value, str))
        items = [
            item
            for item in index_for(slug)["items"]
            if isinstance(item, dict)
            and isinstance(item.get("n"), str)
            and item["n"] in selected
        ]
        if not items:
            return jsonify(error="Nu există fotografii favorite pentru descărcat."), 404

        archive_file = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
        try:
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for item in items:
                    source = album_source_file(album, item["n"])
                    archive.write(source, arcname=item["n"])
            archive_file.seek(0)
        except OSError:
            archive_file.close()
            log.exception("Could not create favorites archive for album %s", slug)
            return jsonify(error="Nu am putut crea arhiva favoritelor."), 500

        response = send_file(
            archive_file,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{slug}-favorite.zip",
            conditional=False,
        )
        response.call_on_close(archive_file.close)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.delete("/api/admin/albums/<slug>/items")
    @require_admin
    def admin_delete_item(slug):
        album = album_for(slug)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            return jsonify(error="Cerere invalidă."), 400
        name = body["name"]
        with admin_write_lock:
            index = index_for(slug)
            if not any(isinstance(item, dict) and item.get("n") == name for item in index["items"]):
                abort(404)
            source = album_source_file(album, name)
            try:
                source.unlink()
                index["items"] = [
                    item for item in index["items"]
                    if not isinstance(item, dict) or item.get("n") != name
                ]
                index["updated"] = int(time.time() * 1000)
                _atomic_json(cache_dir / slug / "index.json", index)

                favorites_path = data_dir / "favs" / f"{slug}.json"
                favorites = _read_json(favorites_path, [])
                if isinstance(favorites, list) and name in favorites:
                    _atomic_json(
                        favorites_path,
                        [favorite for favorite in favorites if favorite != name],
                    )

                cache_root = cache_dir / slug
                relative_name = _media_relative_path(name)
                if relative_name is None:
                    abort(404)
                for relative_path in (
                    Path("t", *relative_name.parts[:-1]) / f"{relative_name.name}.webp",
                    Path("m", *relative_name.parts[:-1]) / f"{relative_name.name}.webp",
                    Path("l", *relative_name.parts[:-1]) / f"{relative_name.name}.webp",
                    Path("v", *relative_name.parts[:-1]) / f"{relative_name.name}.mp4",
                    Path("v360", *relative_name.parts[:-1]) / f"{relative_name.name}.mp4",
                    Path("v1080", *relative_name.parts[:-1]) / f"{relative_name.name}.mp4",
                    Path("tmp", *relative_name.parts[:-1]) / f"{relative_name.name}.jpg",
                ):
                    (cache_root / relative_path).unlink(missing_ok=True)
            except OSError:
                log.exception("Could not complete deletion of %s from album %s", name, slug)
                return jsonify(error="Fotografia a fost ștearsă, dar galeria nu a putut fi actualizată complet."), 500

        log.info("Deleted original %s from album %s through admin page", name, slug)
        return jsonify(ok=True)

    @app.get("/m/<slug>/<kind>/<path:name>")
    @require_auth
    def media(slug, kind, name):
        album = album_for(slug)
        item = next((i for i in index_for(slug)["items"] if isinstance(i, dict) and i.get("n") == name), None)
        if item is None:
            abort(404)
        if kind in ("t", "l", "p", "v"):
            relative_name = _media_relative_path(name)
            if relative_name is None:
                abort(404)
            subdir = {"t": "t", "l": "l", "p": "l", "v": "v"}[kind]
            suffix = ".mp4" if kind.startswith("v") else ".webp"
            file_path = cache_dir / slug / subdir / Path(*relative_name.parts[:-1]) / f"{relative_name.name}{suffix}"
        elif kind == "o":
            file_path = album_source_file(album, name)
        else:
            abort(404)
        if not file_path.is_file():
            abort(404)
        response = send_file(file_path, conditional=True, as_attachment=kind == "o", download_name=name if kind == "o" else None)
        response.headers["Cache-Control"] = "private, no-store" if kind == "o" else "private, max-age=31536000, immutable"
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        if request.endpoint not in ("admin_drive_upload", "admin_upload_to_album", "upload_to_gallery"):
            return jsonify(error="Cererea este prea mare."), 413
        limit_gb = drive_max_upload_bytes / (1024**3)
        return jsonify(error=f"Încărcarea depășește limita configurată de {limit_gb:g} GB."), 413

    return app


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
