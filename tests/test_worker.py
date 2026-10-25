import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.cache = self.root / "cache"
        self.data = self.root / "data"
        (self.media / "event").mkdir(parents=True)
        self.data.mkdir()
        Image.new("RGB", (1200, 800), "orange").save(self.media / "event" / "photo.jpg")
        (self.data / "albums.json").write_text(
            json.dumps({"summer": {"title": "Summer", "folder": "event"}}), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_generates_images_and_recovers_missing_cache_files(self):
        with patch.object(worker, "MEDIA", self.media), patch.object(worker, "CACHE", self.cache), patch.object(
            worker, "DATA", self.data
        ):
            worker.scan_once()
            root = self.cache / "summer"
            index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["n"] for item in index["items"]], ["photo.jpg"])
            self.assertEqual((root / "l" / "photo.jpg.webp").is_file(), True)
            self.assertEqual((root / "t" / "photo.jpg.webp").is_file(), True)

            Image.new("RGB", (640, 480), "blue").save(self.media / "event" / "new-photo.jpg")
            (root / "t" / "photo.jpg.webp").unlink()
            worker.scan_once()
            index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["n"] for item in index["items"]], ["new-photo.jpg", "photo.jpg"])
            self.assertTrue((root / "t" / "new-photo.jpg.webp").is_file())
            self.assertTrue((root / "t" / "photo.jpg.webp").is_file())

    def test_scan_publishes_processed_photos_before_later_media_finishes(self):
        Image.new("RGB", (640, 480), "blue").save(self.media / "event" / "second.jpg")

        def process_media(_source, _slug, name, _kind):
            if name == "second.jpg":
                index_path = self.cache / "summer" / "index.json"
                partial_index = json.loads(index_path.read_text(encoding="utf-8"))
                self.assertEqual([item["n"] for item in partial_index["items"]], ["photo.jpg"])
            return {"w": 640, "h": 480}

        with patch.object(worker, "MEDIA", self.media), patch.object(worker, "CACHE", self.cache), patch.object(
            worker, "DATA", self.data
        ), patch.object(worker, "process_media", side_effect=process_media):
            worker.scan_once()

        index = json.loads((self.cache / "summer" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([item["n"] for item in index["items"]], ["photo.jpg", "second.jpg"])

    def test_scan_stops_when_album_is_removed_during_processing(self):
        Image.new("RGB", (640, 480), "blue").save(self.media / "event" / "second.jpg")

        def process_media(_source, _slug, name, _kind):
            if name == "photo.jpg":
                (self.data / "albums.json").write_text("{}", encoding="utf-8")
            return {"w": 640, "h": 480}

        with patch.object(worker, "MEDIA", self.media), patch.object(worker, "CACHE", self.cache), patch.object(
            worker, "DATA", self.data
        ), patch.object(worker, "process_media", side_effect=process_media) as process:
            worker.scan_once()

        self.assertEqual(process.call_count, 1)
        self.assertFalse((self.cache / "summer" / "index.json").exists())

    def test_ffmpeg_is_terminated_when_album_is_removed(self):
        process = unittest.mock.Mock()
        process.communicate.side_effect = [
            worker.subprocess.TimeoutExpired("ffmpeg", 1),
            (b"", b""),
        ]

        with patch.object(worker.subprocess, "Popen", return_value=process), patch.object(
            worker, "album_is_registered", return_value=False
        ), self.assertRaises(worker.AlbumRemoved):
            worker.run_ffmpeg(["-i", "video.mp4"], "summer")

        process.terminate.assert_called_once()

    def test_video_transcode_maps_supported_streams_and_uses_compatible_pixel_format(self):
        source = self.media / "event" / "sample.mov"
        source.touch()
        with patch.object(worker, "CACHE", self.cache), patch.object(worker, "run_ffmpeg") as run_ffmpeg, patch.object(
            worker, "make_webp", return_value=(1280, 720)
        ):
            worker.process_media(source, "summer", source.name, "video")

        transcode_args = run_ffmpeg.call_args_list[1].args[0]
        self.assertIn("0:v:0", transcode_args)
        self.assertIn("0:a:0?", transcode_args)
        self.assertIn("yuv420p", transcode_args)
        self.assertIn("libx264", transcode_args)
        self.assertIn(
            "scale=-2:720:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            transcode_args,
        )


if __name__ == "__main__":
    unittest.main()
