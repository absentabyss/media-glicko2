import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ModuleNotFoundError:
    Image = None
    PIL_AVAILABLE = False


MODULE_PATH = Path(__file__).resolve().parents[1] / "media-glicko2.py"
MEDIA_MODULE_AVAILABLE = False
media_glicko2 = None
if PIL_AVAILABLE:
    spec = importlib.util.spec_from_file_location("media_glicko2", MODULE_PATH)
    media_glicko2 = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = media_glicko2
    try:
        spec.loader.exec_module(media_glicko2)
        MEDIA_MODULE_AVAILABLE = True
    except SystemExit:
        MEDIA_MODULE_AVAILABLE = False


@unittest.skipUnless(PIL_AVAILABLE and MEDIA_MODULE_AVAILABLE, "Pillow is required for media-glicko2 tests")
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

    def test_load_media_frames_for_video_returns_fallback_frame_when_decode_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "broken.webm"
            video_path.write_bytes(b"not-a-real-video")

            with patch.object(media_glicko2, "_load_video_frames", return_value=[]), patch.object(
                media_glicko2.Image, "open", side_effect=OSError("bad file")
            ):
                frames = media_glicko2.load_media_frames(video_path, (120, 90))

            self.assertEqual(len(frames), 1)
            image, delay = frames[0]
            self.assertEqual(image.size, (120, 90))
            self.assertGreaterEqual(delay, 16)

    def test_strip_existing_prefix_removes_stats_prefix(self):
        stem = "[G2_R1525.4_RD300.0_S0.0600] sunset_photo"
        stripped = media_glicko2.strip_existing_prefix(stem)
        self.assertEqual(stripped, "sunset_photo")

    def test_strip_existing_prefix_removes_stats_prefix_without_space(self):
        stem = "[G2_R1525.4_RD300.0_S0.0600]sunset_photo"
        stripped = media_glicko2.strip_existing_prefix(stem)
        self.assertEqual(stripped, "sunset_photo")

    def test_player_from_path_parses_existing_rating_metadata(self):
        path = Path("[G2_R1688.5_RD88.2_S0.0475] image.png")
        player = media_glicko2.player_from_path(path)
        self.assertAlmostEqual(player.rating, 1688.5)
        self.assertAlmostEqual(player.rd, 88.2)
        self.assertAlmostEqual(player.sigma, 0.0475)

    def test_player_from_path_parses_metadata_without_space_after_prefix(self):
        path = Path("[G2_R1688.5_RD88.2_S0.0475]image.png")
        player = media_glicko2.player_from_path(path)
        self.assertAlmostEqual(player.rating, 1688.5)
        self.assertAlmostEqual(player.rd, 88.2)
        self.assertAlmostEqual(player.sigma, 0.0475)

    def test_update_glicko2_player_with_no_matches_increases_rd_only(self):
        player = media_glicko2.Glicko2Player(rating=1600.0, rd=50.0, sigma=0.06)
        original_rating = player.rating
        original_sigma = player.sigma

        media_glicko2.update_glicko2_player(player)

        self.assertEqual(player.rating, original_rating)
        self.assertEqual(player.sigma, original_sigma)
        self.assertGreater(player.rd, 50.0)
        self.assertLessEqual(player.rd, 350.0)

    def test_update_glicko2_player_win_against_equal_opponent_increases_rating(self):
        player = media_glicko2.Glicko2Player(rating=1500.0, rd=200.0, sigma=0.06)
        player.add_result(opponent_rating=1500.0, opponent_rd=200.0, score=1.0)

        media_glicko2.update_glicko2_player(player)

        self.assertGreater(player.rating, 1500.0)
        self.assertLess(player.rd, 200.0)
        self.assertGreater(player.sigma, 0.0)

    def test_build_random_pairs_once_uses_each_path_at_most_once(self):
        paths = [Path(f"image_{i}.png") for i in range(7)]
        pairs = media_glicko2.build_random_pairs_once(paths)

        paired_items = [item for pair in pairs for item in pair]
        self.assertEqual(len(paired_items), len(set(paired_items)))
        self.assertEqual(len(paired_items), 6)

    def test_shorten_name_adds_ellipsis_for_long_names(self):
        long_name = "a" * 120
        shortened = media_glicko2.shorten_name(long_name, max_len=20)
        self.assertEqual(len(shortened), 20)
        self.assertIn("...", shortened)


if __name__ == "__main__":
    unittest.main()
