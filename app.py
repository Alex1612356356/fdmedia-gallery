import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, make_response, redirect, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from cli import ALBUM_THEMES, add_album


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
log = logging.getLogger("fdmedia-gallery")
admin_write_lock = threading.Lock()
DEFAULT_GALLERY_DESCRIPTION = "Am păstrat aici momentele voastre. Bucurați-vă de ele."


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


def create_app(
    *,
    data_dir=None,
    cache_dir=None,
    media_dir=None,
    secret=None,
    admin_password=None,
    cookie_secure=None,
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
    if cookie_secure is None:
        cookie_secure = os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.config.update(
        DATA_DIR=data_dir,
        CACHE_DIR=cache_dir,
        MEDIA_DIR=media_dir,
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
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            abort(404)
        base = album_source_dir(album)
        file_path = (base / name).resolve()
        if not file_path.is_relative_to(base) or not file_path.is_file():
            abort(404)
        return file_path

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
            album_for(slug)
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
        if not all(isinstance(value, str) for value in (slug, title, folder, pin, theme, description)):
            return jsonify(error="Completați toate câmpurile."), 400
        if theme not in ALBUM_THEMES:
            return jsonify(error="Tema selectată nu este validă."), 400
        if len(description) > 500:
            return jsonify(error="Descrierea poate avea maximum 500 de caractere."), 400
        with admin_write_lock:
            albums = read_albums()
            if slug in albums:
                return jsonify(error="Există deja un album cu acest identificator."), 409
            try:
                result = add_album(data_dir, media_dir, slug, title, folder, pin, theme, description)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return jsonify(error=str(exc)), 400
        log.info("Created album %s through admin page", slug)
        return jsonify(ok=True, slug=slug, title=title, url=f"/g/{slug}", result=result), 201

    @app.put("/api/admin/albums/<slug>")
    @require_admin
    def admin_update_album(slug):
        if not _valid_slug(slug):
            abort(404)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Cerere invalidă."), 400
        theme = body.get("theme")
        if not isinstance(theme, str) or theme not in ALBUM_THEMES:
            return jsonify(error="Tema selectată nu este validă."), 400
        with admin_write_lock:
            albums = read_albums()
            album = albums.get(slug)
            if not isinstance(album, dict):
                abort(404)
            album["theme"] = theme
            _atomic_json(data_dir / "albums.json", albums)
        log.info("Updated theme for album %s to %s", slug, theme)
        return jsonify(ok=True, slug=slug, theme=theme)

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

    @app.get("/g/<slug>")
    def gallery(slug):
        album_for(slug)
        return send_from_directory(Path(__file__).parent / "public", "index.html")

    @app.get("/static/<path:filename>")
    def static_file(filename):
        return send_from_directory(Path(__file__).parent / "public", filename, max_age=3600)

    @app.get("/vendor/<path:filename>")
    def vendor_file(filename):
        return send_from_directory(Path(__file__).parent / "vendor", filename, max_age=604800)

    @app.post("/api/<slug>/login")
    def login(slug):
        album = album_for(slug)
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

        expires = str(int(time.time() * 1000) + 30 * 86400 * 1000)
        signature = hmac.new(secret.encode(), f"{slug}{expires}".encode(), hashlib.sha256).hexdigest()
        response = make_response(jsonify(ok=True, title=album.get("title", "")))
        response.set_cookie(
            f"fd_{slug}",
            f"{expires}.{signature}",
            max_age=30 * 86400,
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
        response = jsonify(
            title=album.get("title", ""),
            description=album.get("description") or DEFAULT_GALLERY_DESCRIPTION,
            theme=(
                album.get("theme")
                if isinstance(album.get("theme"), str) and album.get("theme") in ALBUM_THEMES
                else "default"
            ),
            items=index_for(slug)["items"],
            favs=favorites,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

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
                for relative_path in (
                    Path("t") / f"{name}.webp",
                    Path("l") / f"{name}.webp",
                    Path("v") / f"{name}.mp4",
                    Path("tmp") / f"{name}.jpg",
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
            subdir = {"t": "t", "l": "l", "p": "l", "v": "v"}[kind]
            suffix = ".mp4" if kind == "v" else ".webp"
            file_path = cache_dir / slug / subdir / f"{name}{suffix}"
        elif kind == "o":
            file_path = album_source_file(album, name)
        else:
            abort(404)
        if not file_path.is_file():
            abort(404)
        response = send_file(file_path, conditional=True, as_attachment=kind == "o", download_name=name if kind == "o" else None)
        response.headers["Cache-Control"] = (
            "private, no-store" if kind == "o" else "private, max-age=31536000, immutable"
        )
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify(error="Request too large"), 413

    return app


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
