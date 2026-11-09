import os
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

os.environ.setdefault("SECRET", "test-only-secret-used-for-module-initialization")

from app import create_app
from cli import add_album


class GalleryAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.cache = self.root / "cache"
        self.media = self.root / "media"
        (self.media / "event").mkdir(parents=True)
        (self.cache / "event-one" / "t").mkdir(parents=True)
        (self.cache / "event-one" / "l").mkdir(parents=True)
        (self.data).mkdir()
        add_album(self.data, self.media, "event-one", "Event", "event", "12345678")
        (self.cache / "event-one" / "index.json").write_text(
            json.dumps({"items": [{"n": "photo one.jpg", "type": "image", "w": 10, "h": 8}]}),
            encoding="utf-8",
        )
        (self.cache / "event-one" / "t" / "photo one.jpg.webp").write_bytes(b"thumb")
        (self.cache / "event-one" / "l" / "photo one.jpg.webp").write_bytes(b"large")
        (self.media / "event" / "photo one.jpg").write_bytes(b"original")
        self.app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_login_items_favorites_and_media(self):
        response = self.client.post("/api/event-one/login", json={"pin": "12345678"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Set-Cookie"].startswith("fd_event-one="))

        response = self.client.get("/api/event-one/items")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["title"], "Event")
        self.assertEqual(response.json["items"][0]["n"], "photo one.jpg")

        response = self.client.post("/api/event-one/fav", json={"name": "photo one.jpg", "on": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, ["photo one.jpg"])

        response = self.client.get("/m/event-one/t/photo%20one.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"thumb")
        response.close()
        response = self.client.get("/m/event-one/o/photo%20one.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"original")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        response.close()

    def test_gallery_serves_full_size_images_and_fixed_video_rendition(self):
        index_path = self.cache / "event-one" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["items"].append({"n": "clip.mp4", "type": "video", "w": 640, "h": 360})
        index_path.write_text(json.dumps(index), encoding="utf-8")
        for directory in ("l", "v"):
            path = self.cache / "event-one" / directory / ("clip.mp4.mp4" if directory == "v" else "photo one.jpg.webp")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(directory.encode("ascii"))

        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        for kind, name, expected in (
            ("l", "photo one.jpg", b"l"),
            ("v", "clip.mp4", b"v"),
        ):
            with self.subTest(kind=kind):
                response = self.client.get(f"/m/event-one/{kind}/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, expected)
                response.close()

    def test_adaptive_rendition_routes_are_not_available(self):
        index_path = self.cache / "event-one" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["items"].append({"n": "clip.mp4", "type": "video", "w": 640, "h": 360})
        index_path.write_text(json.dumps(index), encoding="utf-8")

        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        for kind in ("m", "v360", "v1080"):
            with self.subTest(kind=kind):
                response = self.client.get(f"/m/event-one/{kind}/clip.mp4")
                self.assertEqual(response.status_code, 404)
                response.close()

    def test_admin_delete_removes_current_and_legacy_media_renditions(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin = admin_app.test_client()
        admin.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        for directory, suffix in (
            ("m", ".webp"),
            ("v360", ".mp4"),
            ("v", ".mp4"),
            ("v1080", ".mp4"),
        ):
            path = self.cache / "event-one" / directory / f"photo one.jpg{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"rendition")

        response = admin.delete(
            "/api/admin/albums/event-one/items",
            json={"name": "photo one.jpg"},
        )
        self.assertEqual(response.status_code, 200)
        for directory, suffix in (
            ("m", ".webp"),
            ("v360", ".mp4"),
            ("v", ".mp4"),
            ("v1080", ".mp4"),
        ):
            self.assertFalse(
                (self.cache / "event-one" / directory / f"photo one.jpg{suffix}").exists()
            )

    def test_malformed_or_wrong_length_cookie_is_unauthorized(self):
        for cookie in ("fd_event-one=1.x", "fd_event-one=1." + "a" * 10000, "fd_event-one=not-a-token"):
            response = self.client.get("/api/event-one/items", headers={"Cookie": cookie})
            self.assertEqual(response.status_code, 401)

    def test_unknown_album_and_unlisted_media_are_not_served(self):
        self.assertEqual(self.client.get("/g/not-real").status_code, 404)
        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        self.assertEqual(self.client.get("/m/event-one/o/unlisted.jpg").status_code, 404)

    def test_gallery_download_returns_all_originals_as_zip(self):
        (self.media / "event" / "second.jpg").write_bytes(b"second original")
        index_path = self.cache / "event-one" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["items"].append({"n": "second.jpg", "type": "image"})
        index_path.write_text(json.dumps(index), encoding="utf-8")

        self.assertEqual(self.client.get("/api/event-one/download").status_code, 401)
        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        response = self.client.get("/api/event-one/download")

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"photo one.jpg", "second.jpg"})
            self.assertEqual(archive.read("photo one.jpg"), b"original")
            self.assertEqual(archive.read("second.jpg"), b"second original")
        response.close()

    def test_gallery_downloads_only_shared_favorites(self):
        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        self.client.post("/api/event-one/fav", json={"name": "photo one.jpg", "on": True})
        self.assertEqual(self.client.get("/api/event-one/download-favorites").status_code, 200)
        response = self.client.get("/api/event-one/download-favorites")
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["photo one.jpg"])
            self.assertEqual(archive.read("photo one.jpg"), b"original")
        response.close()

        self.client.post("/api/event-one/fav", json={"name": "photo one.jpg", "on": False})
        self.assertEqual(self.client.get("/api/event-one/download-favorites").status_code, 404)

    def test_gallery_supports_nested_sections_cover_and_media(self):
        nested = self.media / "event" / "Ceremonie"
        nested.mkdir()
        (nested / "photo.jpg").write_bytes(b"nested original")
        (self.cache / "event-one" / "t" / "Ceremonie").mkdir()
        (self.cache / "event-one" / "l" / "Ceremonie").mkdir()
        (self.cache / "event-one" / "t" / "Ceremonie" / "photo.jpg.webp").write_bytes(b"nested thumb")
        (self.cache / "event-one" / "l" / "Ceremonie" / "photo.jpg.webp").write_bytes(b"nested large")
        index_path = self.cache / "event-one" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["items"].append(
            {"n": "Ceremonie/photo.jpg", "type": "image", "w": 10, "h": 8, "m": 123}
        )
        index_path.write_text(json.dumps(index), encoding="utf-8")
        albums_path = self.data / "albums.json"
        albums = json.loads(albums_path.read_text(encoding="utf-8"))
        albums["event-one"]["cover"] = "Ceremonie/photo.jpg"
        albums_path.write_text(json.dumps(albums), encoding="utf-8")

        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        response = self.client.get("/api/event-one/items")
        self.assertEqual(response.json["sections"], [{"id": "Ceremonie", "title": "Ceremonie"}, {"id": "__root__", "title": "General"}])
        nested_item = next(item for item in response.json["items"] if item["n"] == "Ceremonie/photo.jpg")
        self.assertEqual(nested_item["section"], "Ceremonie")
        self.assertEqual(response.json["cover"], "Ceremonie/photo.jpg")
        self.assertEqual(response.json["cover_m"], nested_item["m"])
        media_response = self.client.get("/m/event-one/t/Ceremonie%2Fphoto.jpg")
        self.assertEqual(media_response.status_code, 200)
        self.assertEqual(media_response.data, b"nested thumb")
        original_response = self.client.get("/m/event-one/o/Ceremonie%2Fphoto.jpg")
        self.assertEqual(original_response.status_code, 200)
        self.assertEqual(original_response.data, b"nested original")

    def test_expired_gallery_blocks_pin_and_authenticated_routes(self):
        albums_path = self.data / "albums.json"
        albums = json.loads(albums_path.read_text(encoding="utf-8"))
        albums["event-one"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        albums_path.write_text(json.dumps(albums), encoding="utf-8")

        self.assertEqual(self.client.get("/g/event-one").status_code, 200)
        self.assertEqual(self.client.post("/api/event-one/login", json={"pin": "12345678"}).status_code, 410)
        self.assertEqual(self.client.get("/api/event-one/items").status_code, 410)

    def test_admin_can_set_cover_and_expiration_and_status_is_private(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        client = admin_app.test_client()
        self.assertEqual(client.get("/api/admin/status").status_code, 401)
        client.post(
            "/api/admin/login",
            json={"password": "admin-password-for-tests-at-least-24"},
        )
        future = datetime.now(timezone.utc) + timedelta(days=10)
        response = client.put(
            "/api/admin/albums/event-one",
            json={
                "cover": "photo one.jpg",
                "expires_at": future.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        album = json.loads((self.data / "albums.json").read_text(encoding="utf-8"))["event-one"]
        self.assertEqual(album["cover"], "photo one.jpg")
        self.assertEqual(album["expires_at"], future.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
        status_path = self.data / "worker-status.json"
        status_path.write_text(json.dumps({"state": "processing", "heartbeat": datetime.now().timestamp(), "processed": 2, "total": 5}), encoding="utf-8")
        status_response = client.get("/api/admin/status")
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.json["alive"])
        self.assertEqual(status_response.json["total"], 5)

    def test_secret_is_required_and_long_enough(self):
        with self.assertRaises(RuntimeError):
            create_app(data_dir=self.data, cache_dir=self.cache, media_dir=self.media, secret="short")

    def test_login_rate_limit(self):
        for _ in range(8):
            response = self.client.post("/api/event-one/login", json={"pin": "wrong"})
            self.assertEqual(response.status_code, 401)
        response = self.client.post("/api/event-one/login", json={"pin": "12345678"})
        self.assertEqual(response.status_code, 429)

    def test_admin_page_requires_password_and_creates_album(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin_app.config["TESTING"] = True
        client = admin_app.test_client()

        admin_page = client.get("/admin")
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn(b"Administrare", admin_page.data)
        admin_page.close()
        self.assertEqual(client.get("/api/admin/albums").status_code, 401)
        self.assertEqual(client.get("/api/admin/albums/new-event/pin").status_code, 401)
        self.assertEqual(
            client.post("/api/admin/login", json={"password": "wrong"}).status_code,
            401,
        )
        login_response = client.post(
            "/api/admin/login",
            json={"password": "admin-password-for-tests-at-least-24"},
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("SameSite=Strict", login_response.headers["Set-Cookie"])

        response = client.post(
            "/api/admin/albums",
            json={
                "slug": "new-event",
                "title": "New Event",
                "folder": "event",
                "pin": "98765432",
                "theme": "wedding",
                "description": "O zi plină de bucurie.",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["url"], "/g/new-event")
        albums_response = client.get("/api/admin/albums")
        created_album = next(album for album in albums_response.json if album["slug"] == "new-event")
        self.assertTrue(created_album["pin_recoverable"])
        self.assertNotIn("pin", created_album)
        self.assertNotIn("pin_encrypted", created_album)
        revealed_pin = client.get("/api/admin/albums/new-event/pin")
        self.assertEqual(revealed_pin.status_code, 200)
        self.assertEqual(revealed_pin.json, {"pin": "98765432"})
        self.assertEqual(revealed_pin.headers["Cache-Control"], "private, no-store")

        visitor = admin_app.test_client()
        self.assertEqual(
            visitor.post("/api/new-event/login", json={"pin": "98765432"}).status_code,
            200,
        )
        gallery = visitor.get("/api/new-event/items")
        self.assertEqual(gallery.json["theme"], "wedding")
        self.assertEqual(gallery.json["description"], "O zi plină de bucurie.")

        duplicate = client.post(
            "/api/admin/albums",
            json={
                "slug": "new-event",
                "title": "Duplicate",
                "folder": "event",
                "pin": "11112222",
            },
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_existing_album_pin_remains_unchanged_but_cannot_be_revealed(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        client = admin_app.test_client()
        client.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        before = json.loads((self.data / "albums.json").read_text(encoding="utf-8"))["event-one"]

        album_list = client.get("/api/admin/albums")
        existing_album = next(album for album in album_list.json if album["slug"] == "event-one")
        self.assertFalse(existing_album["pin_recoverable"])
        reveal = client.get("/api/admin/albums/event-one/pin")
        self.assertEqual(reveal.status_code, 409)

        after = json.loads((self.data / "albums.json").read_text(encoding="utf-8"))["event-one"]
        self.assertEqual(after["salt"], before["salt"])
        self.assertEqual(after["hash"], before["hash"])
        visitor = admin_app.test_client()
        self.assertEqual(
            visitor.post("/api/event-one/login", json={"pin": "12345678"}).status_code,
            200,
        )

    def test_gallery_defaults_theme_and_description(self):
        self.client.post("/api/event-one/login", json={"pin": "12345678"})
        response = self.client.get("/api/event-one/items")

        self.assertEqual(response.json["theme"], "default")
        self.assertEqual(response.json["description"], "Am păstrat aici momentele voastre. Bucurați-vă de ele.")

    def test_admin_rejects_unknown_theme(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        client = admin_app.test_client()
        client.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        response = client.post(
            "/api/admin/albums",
            json={
                "slug": "bad-theme",
                "title": "Bad Theme",
                "folder": "event",
                "pin": "98765432",
                "theme": "unknown",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_can_change_theme_after_gallery_creation(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin_client = admin_app.test_client()
        update_url = "/api/admin/albums/event-one"

        self.assertEqual(admin_client.put(update_url, json={"theme": "wedding"}).status_code, 401)
        admin_client.post(
            "/api/admin/login",
            json={"password": "admin-password-for-tests-at-least-24"},
        )
        invalid = admin_client.put(update_url, json={"theme": "unknown"})
        self.assertEqual(invalid.status_code, 400)

        response = admin_client.put(update_url, json={"theme": "prom-girl"})
        self.assertEqual(response.status_code, 200)
        album = json.loads((self.data / "albums.json").read_text(encoding="utf-8"))["event-one"]
        self.assertEqual(album["theme"], "prom-girl")
        self.assertEqual(album["folder"], "event")
        self.assertEqual(album["description"], "")

        visitor = admin_app.test_client()
        visitor.post("/api/event-one/login", json={"pin": "12345678"})
        self.assertEqual(visitor.get("/api/event-one/items").json["theme"], "prom-girl")

    def test_admin_can_delete_album_without_deleting_original_photos(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin_app.config["TESTING"] = True
        client = admin_app.test_client()
        delete_url = "/api/admin/albums/event-one"

        self.assertEqual(client.delete(delete_url).status_code, 401)
        client.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        response = client.delete(delete_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True})
        self.assertEqual(client.get("/api/admin/albums").json, [])
        self.assertEqual(client.get("/g/event-one").status_code, 404)
        self.assertTrue((self.media / "event" / "photo one.jpg").is_file())

    def test_admin_can_permanently_delete_gallery_photo_only(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin_app.config["TESTING"] = True
        client = admin_app.test_client()
        delete_url = "/api/admin/albums/event-one/items"
        payload = {"name": "photo one.jpg"}

        self.assertEqual(client.delete(delete_url, json=payload).status_code, 401)
        client.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        response = client.delete(delete_url, json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.media / "event" / "photo one.jpg").exists())
        self.assertEqual(
            json.loads((self.cache / "event-one" / "index.json").read_text(encoding="utf-8"))["items"],
            [],
        )
        self.assertFalse((self.cache / "event-one" / "t" / "photo one.jpg.webp").exists())

    def test_admin_album_creation_rejects_folder_escape(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin_app.config["TESTING"] = True
        client = admin_app.test_client()
        client.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        response = client.post(
            "/api/admin/albums",
            json={
                "slug": "outside",
                "title": "Outside",
                "folder": "../data",
                "pin": "98765432",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_creates_gallery_folder_and_manages_pin_protected_upload_links(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin_app.config["TESTING"] = True
        admin = admin_app.test_client()
        self.assertEqual(
            admin.post(
                "/api/admin/albums/new-gallery/links",
                json={"allow_upload": True},
            ).status_code,
            401,
        )
        admin.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        created = admin.post(
            "/api/admin/albums",
            json={
                "slug": "new-gallery",
                "title": "New Gallery",
                "folder": "",
                "pin": "98765432",
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertTrue((self.media / "new-gallery").is_dir())

        upload_link = admin.post(
            "/api/admin/albums/new-gallery/links",
            json={"allow_upload": True},
        )
        view_link = admin.post(
            "/api/admin/albums/new-gallery/links",
            json={"allow_upload": False},
        )
        self.assertEqual(upload_link.status_code, 200)
        self.assertEqual(view_link.status_code, 200)
        upload_id = upload_link.json["link"]["id"]
        view_id = view_link.json["link"]["id"]
        self.assertNotEqual(upload_id, view_id)
        new_gallery = next(
            album for album in admin.get("/api/admin/albums").json if album["slug"] == "new-gallery"
        )
        self.assertEqual(
            {link["id"]: link["allow_upload"] for link in new_gallery["links"]},
            {upload_id: True, view_id: False},
        )

        visitor = admin_app.test_client()
        self.assertEqual(
            visitor.post(
                "/api/new-gallery/upload?link=" + upload_id,
                data={"files": (BytesIO(b"photo"), "photo.jpg")},
                content_type="multipart/form-data",
            ).status_code,
            401,
        )
        self.assertEqual(
            visitor.post("/api/new-gallery/login", json={"pin": "98765432"}).status_code,
            200,
        )
        self.assertTrue(visitor.get(f"/api/new-gallery/items?link={upload_id}").json["upload_enabled"])
        self.assertFalse(visitor.get(f"/api/new-gallery/items?link={view_id}").json["upload_enabled"])
        self.assertFalse(visitor.get("/api/new-gallery/items").json["upload_enabled"])
        self.assertEqual(
            visitor.post(
                f"/api/new-gallery/upload?link={view_id}",
                data={"files": (BytesIO(b"photo"), "view-only.jpg")},
                content_type="multipart/form-data",
            ).status_code,
            403,
        )
        self.assertEqual(
            visitor.post(
                f"/api/new-gallery/upload?link={upload_id}",
                data={"files": (BytesIO(b"photo"), "photo.jpg")},
                content_type="multipart/form-data",
            ).status_code,
            201,
        )
        self.assertEqual((self.media / "new-gallery" / "photo.jpg").read_bytes(), b"photo")
        self.assertEqual(
            visitor.post(
                "/api/new-gallery/upload",
                data={"files": (BytesIO(b"photo"), "canonical.jpg")},
                content_type="multipart/form-data",
            ).status_code,
            403,
        )

        changed = admin.put(
            f"/api/admin/albums/new-gallery/links/{view_id}",
            json={"allow_upload": True},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(visitor.get(f"/api/new-gallery/items?link={view_id}").json["upload_enabled"])
        revoked = admin.delete(f"/api/admin/albums/new-gallery/links/{upload_id}")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(
            visitor.post(
                f"/api/new-gallery/upload?link={upload_id}",
                data={"files": (BytesIO(b"photo"), "revoked.jpg")},
                content_type="multipart/form-data",
            ).status_code,
            403,
        )

        admin_upload = admin.post(
            "/api/admin/albums/new-gallery/upload",
            data={"files": (BytesIO(b"admin photo"), "admin.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(admin_upload.status_code, 201)
        self.assertEqual((self.media / "new-gallery" / "admin.jpg").read_bytes(), b"admin photo")
        self.assertEqual(
            visitor.post(
                "/api/admin/albums/new-gallery/upload",
                data={"files": (BytesIO(b"unauthorized"), "unauthorized.jpg")},
                content_type="multipart/form-data",
            ).status_code,
            401,
        )

    def test_gallery_upload_rejects_unsupported_and_duplicate_names(self):
        admin_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
        )
        admin = admin_app.test_client()
        admin.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        unsupported = admin.post(
            "/api/admin/albums/event-one/upload",
            data={"files": (BytesIO(b"text"), "notes.txt")},
            content_type="multipart/form-data",
        )
        duplicate = admin.post(
            "/api/admin/albums/event-one/upload",
            data={"files": [(BytesIO(b"one"), "same.jpg"), (BytesIO(b"two"), "same.jpg")]},
            content_type="multipart/form-data",
        )
        empty = admin.post(
            "/api/admin/albums/event-one/upload",
            data={"files": (BytesIO(b""), "empty.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        self.assertFalse((self.media / "event" / "notes.txt").exists())
        self.assertFalse((self.media / "event" / "same.jpg").exists())
        self.assertFalse((self.media / "event" / "empty.jpg").exists())

    def test_gallery_upload_enforces_limit_and_removes_staged_files(self):
        limited_app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret="test-secret-that-is-at-least-32-bytes",
            admin_password="admin-password-for-tests-at-least-24",
            cookie_secure=False,
            max_drive_upload_bytes=3,
        )
        admin = limited_app.test_client()
        admin.post("/api/admin/login", json={"password": "admin-password-for-tests-at-least-24"})
        response = admin.post(
            "/api/admin/albums/event-one/upload",
            data={"files": (BytesIO(b"four"), "large.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse((self.media / "event" / "large.jpg").exists())
        self.assertEqual(
            [path for path in (self.media / "event").iterdir() if path.name.startswith(".fdmedia-upload-")],
            [],
        )


if __name__ == "__main__":
    unittest.main()
