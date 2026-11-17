import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

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

    def test_make_webp_converts_profiled_source_to_adobe_rgb_losslessly(self):
        source = self.media / "event" / "profiled.tif"
        target = self.cache / "summer" / "l" / "profiled.tif.webp"
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        lab = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
        reference = Image.new("RGB", (32, 24), (210, 80, 40))
        profiled = ImageCms.profileToProfile(reference, srgb, lab, outputMode="LAB")
        expected_adobe_rgb = ImageCms.profileToProfile(
            profiled, lab, worker.ADOBE_RGB_PROFILE, outputMode="RGB"
        )
        profiled.save(source, icc_profile=lab.tobytes())

        worker.make_webp(source, target, 64)

        with Image.open(target) as output:
            self.assertEqual(output.format, "WEBP")
            output_profile_data = output.info.get("icc_profile")
            self.assertTrue(output_profile_data)
            output_profile = ImageCms.ImageCmsProfile(io.BytesIO(output_profile_data))
            self.assertEqual(
                ImageCms.getProfileDescription(output_profile).strip(),
                "Adobe RGB (1998)",
            )
            self.assertEqual(
                output.convert("RGB").getpixel((0, 0)),
                expected_adobe_rgb.getpixel((0, 0)),
            )
            converted = ImageCms.profileToProfile(
                output, output_profile, srgb, outputMode="RGB"
            )
            for actual, expected in zip(
                converted.getpixel((0, 0)), reference.getpixel((0, 0))
            ):
                self.assertLessEqual(abs(actual - expected), 2)

    def test_make_webp_assumes_srgb_when_source_has_no_profile(self):
        source = self.media / "event" / "unprofiled.png"
        target = self.cache / "summer" / "l" / "unprofiled.png.webp"
        reference = Image.new("RGB", (8, 8), (210, 80, 40))
        reference.save(source)
        expected = ImageCms.profileToProfile(
            reference,
            ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")),
            worker.ADOBE_RGB_PROFILE,
            outputMode="RGB",
        )

        worker.make_webp(source, target, 64)

        with Image.open(target) as output:
            self.assertEqual(
                output.convert("RGB").getpixel((0, 0)),
                expected.getpixel((0, 0)),
            )

    def test_make_webp_preserves_adobe_rgb_source_profile_and_pixels(self):
        source = self.media / "event" / "adobe.png"
        target = self.cache / "summer" / "l" / "adobe.png.webp"
        reference = Image.new("RGB", (8, 8), (183, 81, 46))
        reference.save(source, icc_profile=worker.ADOBE_RGB_PROFILE_DATA)

        worker.make_webp(source, target, 64)

        with Image.open(target) as output:
            self.assertEqual(output.info.get("icc_profile"), worker.ADOBE_RGB_PROFILE_DATA)
            self.assertEqual(output.convert("RGB").getpixel((0, 0)), (183, 81, 46))

    def test_video_cache_requires_every_selectable_resolution(self):
        root = self.cache / "summer"
        name = "clip.mp4"
        relative_name = Path(name)
        for directory in ("t", "m", "l", "v360", "v", "v1080"):
            suffix = ".mp4" if directory.startswith("v") else ".webp"
            rendition = root / directory / relative_name.with_name(f"{name}{suffix}")
            rendition.parent.mkdir(parents=True, exist_ok=True)
            rendition.write_bytes(b"rendition")

        self.assertTrue(worker.cache_is_complete(root, name, "video"))
        (root / "v1080" / "clip.mp4.mp4").unlink()
        self.assertFalse(worker.cache_is_complete(root, name, "video"))

    def test_scan_rebuilds_video_cache_from_an_older_version(self):
        source = self.media / "event" / "clip.mp4"
        source.write_bytes(b"source")
        stat = source.stat()
        root = self.cache / "summer"
        for directory in ("t", "m", "l", "v360", "v", "v1080"):
            suffix = ".mp4" if directory.startswith("v") else ".webp"
            rendition = root / directory / f"clip.mp4{suffix}"
            rendition.parent.mkdir(parents=True, exist_ok=True)
            rendition.write_bytes(b"old rendition")
        (root / "index.json").parent.mkdir(parents=True, exist_ok=True)
        (root / "index.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "n": "clip.mp4",
                            "type": "video",
                            "w": 640,
                            "h": 360,
                            "m": stat.st_mtime_ns,
                            "size": stat.st_size,
                            "video_version": worker.VIDEO_CACHE_VERSION - 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with patch.object(worker, "MEDIA", self.media), patch.object(
            worker, "CACHE", self.cache
        ), patch.object(worker, "DATA", self.data), patch.object(
            worker, "process_media", return_value={"w": 640, "h": 360}
        ) as process_media:
            worker.scan_once()

        process_media.assert_any_call(source, "summer", "clip.mp4", "video")
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        video_item = next(item for item in index["items"] if item["n"] == "clip.mp4")
        self.assertEqual(video_item["video_version"], worker.VIDEO_CACHE_VERSION)

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
            self.assertTrue((root / "m" / "photo.jpg.webp").is_file())

            Image.new("RGB", (640, 480), "blue").save(self.media / "event" / "new-photo.jpg")
            (root / "t" / "photo.jpg.webp").unlink()
            worker.scan_once()
            index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["n"] for item in index["items"]], ["new-photo.jpg", "photo.jpg"])
            self.assertTrue((root / "m" / "new-photo.jpg.webp").is_file())
            self.assertTrue((root / "t" / "new-photo.jpg.webp").is_file())
            self.assertTrue((root / "t" / "photo.jpg.webp").is_file())

    def test_scan_processes_photos_before_videos(self):
        (self.media / "event" / "photo.jpg").unlink()
        (self.media / "event" / "a-video.mp4").touch()
        Image.new("RGB", (640, 480), "blue").save(
            self.media / "event" / "z-photo.jpg"
        )
        processed = []

        def process_media(_source, _slug, name, _kind):
            processed.append(name)
            return {"w": 640, "h": 480}

        with patch.object(worker, "MEDIA", self.media), patch.object(
            worker, "CACHE", self.cache
        ), patch.object(worker, "DATA", self.data), patch.object(
            worker, "process_media", side_effect=process_media
        ):
            worker.scan_once()

        self.assertEqual(processed, ["z-photo.jpg", "a-video.mp4"])

    def test_scan_rebuilds_photo_cache_from_an_older_version(self):
        source = self.media / "event" / "photo.jpg"
        stat = source.stat()
        root = self.cache / "summer"
        for directory in ("t", "m", "l"):
            rendition = root / directory / "photo.jpg.webp"
            rendition.parent.mkdir(parents=True, exist_ok=True)
            rendition.write_bytes(b"old rendition")
        legacy = root / "l" / "photo.jpg.png"
        legacy.write_bytes(b"old png rendition")
        (root / "index.json").parent.mkdir(parents=True, exist_ok=True)
        (root / "index.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "n": "photo.jpg",
                            "type": "image",
                            "w": 1200,
                            "h": 800,
                            "m": stat.st_mtime_ns,
                            "size": stat.st_size,
                            "photo_version": worker.PHOTO_CACHE_VERSION - 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with patch.object(worker, "MEDIA", self.media), patch.object(
            worker, "CACHE", self.cache
        ), patch.object(worker, "DATA", self.data):
            worker.scan_once()

        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["items"][0]["photo_version"], worker.PHOTO_CACHE_VERSION)
        with Image.open(root / "l" / "photo.jpg.webp") as rendition:
            self.assertEqual(rendition.format, "WEBP")
        self.assertFalse(legacy.exists())

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

    def test_scan_indexes_nested_sections_and_reports_worker_status(self):
        nested = self.media / "event" / "Ceremonie"
        nested.mkdir()
        Image.new("RGB", (800, 600), "purple").save(nested / "photo.jpg")

        with patch.object(worker, "MEDIA", self.media), patch.object(worker, "CACHE", self.cache), patch.object(
            worker, "DATA", self.data
        ):
            worker.scan_once()

        index = json.loads((self.cache / "summer" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["n"] for item in index["items"]],
            ["Ceremonie/photo.jpg", "photo.jpg"],
        )
        self.assertTrue((self.cache / "summer" / "t" / "Ceremonie" / "photo.jpg.webp").is_file())
        status = json.loads((self.data / "worker-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["last_scan"] > 0, True)
        self.assertEqual(status["total"], 2)

    def test_manual_scan_request_wakes_worker_wait_immediately(self):
        (self.data / worker.MANUAL_SCAN_REQUEST).touch()
        with patch.object(worker, "DATA", self.data), patch.object(
            worker, "INTERVAL", 30
        ), patch.object(worker.time, "sleep") as sleep:
            worker.wait_for_manual_scan_request()
        sleep.assert_not_called()


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

        def fake_ffmpeg(args, _slug):
            output = Path(args[-1])
            if output.suffix == ".mp4":
                output.write_bytes(b"encoded video")

        with patch.object(worker, "CACHE", self.cache), patch.object(
            worker, "run_ffmpeg", side_effect=fake_ffmpeg
        ) as run_ffmpeg, patch.object(worker, "make_webp", return_value=(1280, 720)):
            worker.process_media(source, "summer", source.name, "video")

        self.assertEqual(run_ffmpeg.call_count, 4)
        poster_args = run_ffmpeg.call_args_list[0].args[0]
        self.assertIn("0:V:0", poster_args)
        rendition_args = [call.args[0] for call in run_ffmpeg.call_args_list[1:]]
        for args, directory, height in zip(
            rendition_args,
            ("v360", "v", "v1080"),
            (360, 720, 1080),
        ):
            self.assertIn("0:V:0", args)
            self.assertIn("0:a:0?", args)
            self.assertIn("yuv420p", args)
            self.assertIn("libx264", args)
            self.assertIn(
                f"scale=-2:min(ih\\,{height}):force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
                args,
            )
            self.assertTrue(any(directory in arg for arg in args))
            self.assertTrue(Path(args[-1]).name.endswith(".tmp.mp4"))
            self.assertFalse(Path(args[-1]).exists())
            self.assertTrue((self.cache / "summer" / directory / "sample.mov.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
