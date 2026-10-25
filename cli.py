import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ALBUM_THEMES = {"default", "wedding", "baptism-boy", "baptism-girl", "prom-boy", "prom-girl"}


def add_album(data_dir, media_dir, slug, title, folder, pin, theme="default", description=""):
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("Slug must be 1-63 lowercase letters, digits, or hyphens and start with a letter or digit.")
    if not title.strip():
        raise ValueError("Title cannot be empty.")
    if len(pin) < 4:
        raise ValueError("PIN must contain at least 4 characters.")
    if len(pin) > 128:
        raise ValueError("PIN must contain no more than 128 characters.")
    if not isinstance(theme, str) or theme not in ALBUM_THEMES:
        raise ValueError("Unknown album theme.")
    if len(description) > 500:
        raise ValueError("Description must contain no more than 500 characters.")
    media_root = media_dir.resolve()
    album_folder = (media_dir / folder).resolve()
    if not album_folder.is_relative_to(media_root) or not album_folder.is_dir():
        raise ValueError("Folder must be an existing directory inside MEDIA.")

    path = data_dir / "albums.json"
    try:
        with path.open(encoding="utf-8") as handle:
            albums = json.load(handle)
    except FileNotFoundError:
        albums = {}
    if not isinstance(albums, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    salt = secrets.token_hex(16)
    pin_hash = hashlib.scrypt(pin.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
    albums[slug] = {
        "title": title,
        "folder": str(album_folder.relative_to(media_root)),
        "theme": theme,
        "description": description.strip(),
        "salt": salt,
        "hash": pin_hash,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(albums, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return f"OK: https://<your-host>/g/{slug}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage client gallery albums.")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Create or replace an album PIN.")
    add.add_argument("slug")
    add.add_argument("title")
    add.add_argument("folder", help="Folder path relative to MEDIA.")
    add.add_argument("pin", nargs="?", help="PIN; omit to enter it without echoing")
    args = parser.parse_args(argv)
    data_dir = Path(os.environ.get("DATA", "/data"))
    media_dir = Path(os.environ.get("MEDIA", "/media"))
    try:
        pin = args.pin if args.pin is not None else getpass.getpass("Album PIN: ")
        print(add_album(data_dir, media_dir, args.slug, args.title, args.folder, pin))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
