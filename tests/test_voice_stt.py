"""STT backend dispatch + graceful fallback (no network / model download)."""
from __future__ import annotations

import unittest

from game import voice


class TranscribeDispatchTest(unittest.TestCase):
    def setUp(self):
        # Snapshot the module globals we poke, so each test is isolated.
        self._saved = (voice._STT_BACKEND, voice._mlx_failed,
                       voice._transcribe_mlx, voice._transcribe_gemini)

    def tearDown(self):
        (voice._STT_BACKEND, voice._mlx_failed,
         voice._transcribe_mlx, voice._transcribe_gemini) = self._saved

    def test_empty_audio_short_circuits(self):
        # No bytes → no backend call at all (would raise if either ran).
        voice._transcribe_mlx = lambda w: 1 / 0
        voice._transcribe_gemini = lambda w, m: 1 / 0
        self.assertEqual(voice.transcribe(b"", "model"), "")

    def test_mlx_result_used_without_calling_gemini(self):
        voice._STT_BACKEND = "mlx"
        voice._transcribe_mlx = lambda w: "from mlx"
        voice._transcribe_gemini = lambda w, m: self.fail("Gemini should not run")
        self.assertEqual(voice.transcribe(b"RIFF...", "model"), "from mlx")

    def test_falls_back_to_gemini_when_mlx_unavailable(self):
        voice._STT_BACKEND = "mlx"
        voice._transcribe_mlx = lambda w: None          # MLX missing/failed
        voice._transcribe_gemini = lambda w, m: "from gemini"
        self.assertEqual(voice.transcribe(b"RIFF...", "model"), "from gemini")

    def test_gemini_backend_skips_mlx(self):
        voice._STT_BACKEND = "gemini"
        voice._transcribe_mlx = lambda w: self.fail("MLX should not run")
        voice._transcribe_gemini = lambda w, m: "from gemini"
        self.assertEqual(voice.transcribe(b"RIFF...", "model"), "from gemini")

    def test_mlx_handles_bad_wav_gracefully(self):
        # Garbage bytes (not a WAV) → None + MLX disabled for the session, no raise.
        voice._mlx_failed = False
        self.assertIsNone(voice._transcribe_mlx(b"not a wav"))
        self.assertTrue(voice._mlx_failed)


if __name__ == "__main__":
    unittest.main()
