import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate


class FakeClient:
    def __init__(self):
        self.downloaded = []

    def request_json(self, path, query=None):
        if path.startswith("orgs/example/repos"):
            if query["page"] == "1":
                return [{
                    "name": "demo",
                    "full_name": "example/demo",
                    "html_url": "https://github.com/example/demo",
                    "description": "A demo package",
                }]
            return []
        if path == "repos/example/demo/releases/latest":
            return {
                "tag_name": "v1.2.0",
                "assets": [{
                    "name": "index.html",
                    "browser_download_url": "https://example.test/index.html",
                }],
            }
        raise AssertionError(path)

    def download(self, url, destination):
        self.downloaded.append((url, destination))
        destination.write_text("<h1>Demo</h1>", encoding="utf-8")


class GenerateTests(unittest.TestCase):
    def test_standard_release_imports_index_and_adds_archives(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            project = generate.process_repository(
                client,
                {
                    "name": "demo",
                    "full_name": "example/demo",
                    "html_url": "https://github.com/example/demo",
                    "description": "A demo package",
                },
                Path(directory),
                "index.html",
            )
            self.assertEqual(project["status"], "standard")
            self.assertEqual(project["project_url"], "projects/demo/index.html")
            self.assertEqual(len(project["artifacts"]), 3)
            self.assertTrue((Path(directory) / "projects/demo/index.html").is_file())

    def test_rendered_data_is_static_json(self):
        site = generate.render_site([{
            "name": "Demo",
            "description": "A package",
            "project_url": "projects/demo/index.html",
            "repository_url": "https://github.com/example/demo",
            "version": "v1.0.0",
            "status": "standard",
            "standard": True,
            "artifacts": [],
        }], [])
        self.assertIn("Project catalog", site)
        self.assertIn("search.addEventListener", site)
        self.assertIn('type="application/json"', site)


if __name__ == "__main__":
    unittest.main()
