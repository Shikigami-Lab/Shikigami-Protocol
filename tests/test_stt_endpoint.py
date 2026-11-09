"""Regression tests for POST /stt request-shape errors.

The transcribe path itself needs a SenseVoice model; these tests stop before it
(disabled -> 503, bad request shape -> guided 400) so no model is required.
"""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.stt import router


class _Cfg:
    def __init__(self, enabled):
        self._enabled = enabled

    def get_stt_config(self):
        return {"enabled": self._enabled}


def _client(enabled=True):
    app = FastAPI()
    app.include_router(router)
    app.state.config = _Cfg(enabled)
    return TestClient(app, raise_server_exceptions=False)


class SttEndpointShapeTests(unittest.TestCase):
    def test_wrong_multipart_field_returns_guided_400_not_500(self):
        r = _client().post("/stt", files={"file": ("a.wav", b"RIFFfake", "audio/wav")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("audio", r.json()["error"])

    def test_empty_octet_stream_returns_400(self):
        r = _client().post("/stt", content=b"", headers={"content-type": "application/octet-stream"})
        self.assertEqual(r.status_code, 400)

    def test_disabled_returns_503(self):
        r = _client(False).post("/stt", content=b"RIFFfake",
                                headers={"content-type": "application/octet-stream"})
        self.assertEqual(r.status_code, 503)

    def test_octet_stream_reaches_recognizer_gate_503(self):
        # Body is fine; without a SenseVoice model the guided 503 applies, not a 500.
        with patch("src.api.stt._get_sherpa_recognizer", return_value=None):
            r = _client().post("/stt", content=b"RIFFfake",
                               headers={"content-type": "application/octet-stream"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("SenseVoice", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
