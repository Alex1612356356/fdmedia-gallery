import os
import json
import tempfile
import unittest
import zipfile
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


if __name__ == "__main__":
    unittest.main()
