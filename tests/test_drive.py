import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

os.environ.setdefault("SECRET", "test-only-secret-used-for-module-initialization")

from app import create_app


ADMIN_PASSWORD = "admin-password-for-tests-at-least-24"
SECRET = "test-secret-that-is-at-least-32-bytes"


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.cache = self.root / "cache"
        self.media = self.root / "media"
        self.data.mkdir()
        self.app = create_app(
            data_dir=self.data,
            cache_dir=self.cache,
            media_dir=self.media,
            secret=SECRET,
            admin_password=ADMIN_PASSWORD,
            cookie_secure=False,
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self):
        return self.client.post("/api/admin/login", json={"password": ADMIN_PASSWORD})

    def test_drive_page_and_file_routes_require_admin_authentication(self):
        page = self.client.get("/drive")
        self.assertEqual(page.status_code, 200)
        page.close()
        self.assertEqual(self.client.get("/api/admin/drive").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/drive/folders", json={"name": "Private"}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/drive/download?path=private.txt").status_code, 401)
        self.assertEqual(self.client.delete("/api/admin/drive", json={"path": "private.txt"}).status_code, 401)

    def test_create_list_upload_download_and_delete_files(self):
        self.assertEqual(self.login().status_code, 200)
        created = self.client.post("/api/admin/drive/folders", json={"folder": "", "name": "Proiecte"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            self.client.post("/api/admin/drive/folders", json={"folder": "", "name": "Proiecte"}).status_code,
            409,
        )

        uploaded = self.client.post(
            "/api/admin/drive/upload",
            data={
                "folder": "Proiecte",
                "files": [
                    (BytesIO(b"continut imagine"), "Fotografie.png"),
                    (BytesIO(b"document"), "notite.txt"),
                ],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(
            {item["name"] for item in uploaded.json["uploaded"]},
            {"Fotografie.png", "notite.txt"},
        )
        self.assertEqual((self.data / "drive" / "Proiecte" / "Fotografie.png").read_bytes(), b"continut imagine")

        listing = self.client.get("/api/admin/drive?folder=Proiecte")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.headers["Cache-Control"], "private, no-store")
        self.assertEqual({item["name"] for item in listing.json["items"]}, {"Fotografie.png", "notite.txt"})

        download = self.client.get("/api/admin/drive/download?path=Proiecte%2FFotografie.png")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertEqual(download.data, b"continut imagine")
        download.close()
        self.assertEqual(
            self.client.delete("/api/admin/drive", json={"path": "Proiecte/Fotografie.png"}).status_code,
            200,
        )
        self.assertFalse((self.data / "drive" / "Proiecte" / "Fotografie.png").exists())

    def test_upload_rejects_invalid_paths_names_and_existing_files(self):
        self.login()
        for folder in ("../outside", "/absolute", "folder\\name"):
            response = self.client.get("/api/admin/drive", query_string={"folder": folder})
            self.assertEqual(response.status_code, 400)
        invalid = self.client.post(
            "/api/admin/drive/upload",
            data={"folder": "", "files": (BytesIO(b"no"), "../outside.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse((self.root / "outside.txt").exists())

        first = self.client.post(
            "/api/admin/drive/upload",
            data={"folder": "", "files": (BytesIO(b"one"), "same.txt")},
            content_type="multipart/form-data",
        )
        second = self.client.post(
            "/api/admin/drive/upload",
            data={"folder": "", "files": (BytesIO(b"two"), "same.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual((self.data / "drive" / "same.txt").read_bytes(), b"one")

    def test_upload_limit_is_enforced_and_staged_files_are_removed(self):
        limited_app = create_app(
            data_dir=self.root / "limited-data",
            cache_dir=self.cache,
            media_dir=self.media,
            secret=SECRET,
            admin_password=ADMIN_PASSWORD,
            cookie_secure=False,
            max_drive_upload_bytes=3,
        )
        limited_client = limited_app.test_client()
        limited_client.post("/api/admin/login", json={"password": ADMIN_PASSWORD})
        response = limited_client.post(
            "/api/admin/drive/upload",
            data={"folder": "", "files": (BytesIO(b"four"), "large.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list((self.root / "limited-data" / "drive").iterdir()), [])

    def test_symlinks_cannot_expose_files_outside_drive(self):
        self.login()
        outside = self.root / "private.txt"
        outside.write_text("private", encoding="utf-8")
        link = self.data / "drive" / "escape"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not available in this environment.")
        self.assertEqual(self.client.get("/api/admin/drive?folder=escape").status_code, 400)
        self.assertEqual(self.client.get("/api/admin/drive/download?path=escape").status_code, 400)
        self.assertEqual(self.client.get("/api/admin/drive").json["items"], [])
        self.assertEqual(outside.read_text(encoding="utf-8"), "private")


if __name__ == "__main__":
    unittest.main()
