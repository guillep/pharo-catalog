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
    def test_registry_normalizes_urls_and_validates_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packages.yml"
            path.write_text(
                "packages:\n  - name: Demo\n    description: Demo\n    categories: [Tools]\n    tags: [demo]\n    repository: https://github.com/example/demo/\n",
                encoding="utf-8",
            )
            registry = generate.load_registry(path)
            self.assertEqual(registry[0]["repository"], "https://github.com/example/demo")

    def test_registry_entry_overrides_discovered_metadata(self):
        project = generate.merge_project_metadata(
            {"name": "demo", "description": "GitHub", "categories": [], "tags": []},
            {"name": "Demo package", "description": "Registry", "categories": ["Tools"], "tags": ["demo"]},
        )
        self.assertEqual(project["description"], "Registry")
        self.assertEqual(project["categories"], ["Tools"])

    def test_standard_release_imports_index_without_version_metadata(self):
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
            self.assertNotIn("version", project)
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

    def test_shared_logo_asset_is_available(self):
        self.assertTrue(generate.LOGO_PATH.is_file())
        self.assertIn("<svg", generate.LOGO_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
