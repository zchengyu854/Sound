"""Independent training page routes; no real models or private datasets."""
from html.parser import HTMLParser
from pathlib import Path
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from test_voice_app import Engine, Transcriber
from voice_app import create_app, ROOT


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.assets = []
        self.inline_scripts = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script":
            self.inline_scripts |= "src" not in attrs
            if "src" in attrs:
                self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])


class TrainingPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / "data", Engine(),
                              lambda history: ("测试回复。", ""), Transcriber())
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_training_has_separate_route_and_assets(self):
        response = self.client.get("/training/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("声线研习室", response.text)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(self.client.get("/training").url.path, "/training/")
        parser = PageParser()
        parser.feed(response.text)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertFalse(parser.inline_scripts)
        self.assertEqual(set(parser.assets), {"/training/app.js", "/training/style.css"})
        for asset in parser.assets:
            result = self.client.get(asset)
            self.assertEqual(result.status_code, 200, asset)
            self.assertEqual(result.headers["x-content-type-options"], "nosniff")
        source = (ROOT / "training_web" / "app.js").read_text()
        referenced_ids = set(re.findall(r'\$\("([a-z-]+)"\)', source))
        self.assertFalse(referenced_ids - set(parser.ids))
        self.assertNotIn(".innerHTML", source)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])

    def test_realtime_page_has_separate_route_and_reachable_assets(self):
        response = self.client.get("/realtime/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("声线对话", response.text)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(self.client.get("/realtime").url.path, "/realtime/")
        parser = PageParser()
        parser.feed(response.text)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertFalse(parser.inline_scripts)
        self.assertEqual(set(parser.assets),
                         {"/realtime/style.css", "/realtime/app.js", "/audio.js", "/vad.js"})
        for asset in parser.assets:
            result = self.client.get(asset)
            self.assertEqual(result.status_code, 200, asset)
            self.assertEqual(result.headers["x-content-type-options"], "nosniff")
        source = (ROOT / "realtime_web" / "app.js").read_text()
        referenced_ids = set(re.findall(r'\$\("([a-z-]+)"\)', source))
        self.assertFalse(referenced_ids - set(parser.ids), referenced_ids - set(parser.ids))
        self.assertNotIn(".innerHTML", source)
        worklet = self.client.get("/realtime-worklet.js")
        self.assertEqual(worklet.status_code, 200)
        self.assertIn("realtime-capture", worklet.text)
        for path in ("/realtime/data/chat.sqlite3", "/realtime/voice_app.py", "/realtime/%2e%2e/voice_app.py"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_chat_home_is_unchanged_and_has_no_training_entry(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, (ROOT / "web" / "index.html").read_bytes())
        self.assertNotIn("/training", response.text)
        self.assertNotIn("声线研习室", response.text)
        self.assertEqual(self.client.get("/app.js").content, (ROOT / "web" / "app.js").read_bytes())

    def test_page_cannot_expose_training_files_or_bypass_api_origin_guard(self):
        for path in ("/training/data/chat.sqlite3", "/training/voice_app.py", "/training/%2e%2e/voice_app.py"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        rejected = self.client.post("/api/training/datasets", json={"name": "unauthorized"},
                                    headers={"Origin": "https://other.example"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.get("/api/training/datasets").json(), [])


if __name__ == "__main__":
    unittest.main()
