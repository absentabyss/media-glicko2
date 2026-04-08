import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


MODULE_PATH = Path(__file__).resolve().parents[1] / "media-glicko2.py"
spec = importlib.util.spec_from_file_location("media_glicko2", MODULE_PATH)
media_glicko2 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(media_glicko2)


class MediaHelpersTests(unittest.TestCase):
    def test_supported_extensions_include_videos(self):
        self.assertIn(".gif", media_glicko2.SUPPORTED_EXTS)
        self.assertIn(".mp4", media_glicko2.SUPPORTED_EXTS)
        self.assertIn(".webm", media_glicko2.SUPPORTED_EXTS)

    def test_load_media_frames_for_static_image_returns_one_frame(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "still.png"
            Image.new("RGB", (16, 16), color=(255, 0, 0)).save(image_path)

            frames = media_glicko2.load_media_frames(image_path, (200, 200))

            self.assertEqual(len(frames), 1)
            image, delay = frames[0]
            self.assertEqual(image.size, (200, 200))
            self.assertGreaterEqual(delay, 16)

    def test_load_media_frames_for_gif_returns_multiple_frames(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gif_path = Path(tmpdir) / "animated.gif"
            frame_one = Image.new("RGB", (20, 20), color=(255, 0, 0))
            frame_two = Image.new("RGB", (20, 20), color=(0, 255, 0))
            frame_one.save(
                gif_path,
                save_all=True,
                append_images=[frame_two],
                duration=[40, 80],
                loop=0,
                format="GIF",
            )

            frames = media_glicko2.load_media_frames(gif_path, (150, 150))

            self.assertGreaterEqual(len(frames), 2)
            for image, delay in frames:
                self.assertEqual(image.size, (150, 150))
                self.assertGreaterEqual(delay, 16)


if __name__ == "__main__":
    unittest.main()
