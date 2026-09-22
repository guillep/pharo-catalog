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
        if path == "repos/example/demo/topics":
            return {"names": ["HTTP", "json", "rest"]}
        raise AssertionError(path)

    def download(self, url, destination):
        self.downloaded.append((url, destination))
        destination.write_text("<h1>Demo</h1>", encoding="utf-8")

    def repository_topics(self, full_name):
        return ["HTTP", "json", "rest"]


class GenerateTests(unittest.TestCase):
    def test_registry_normalizes_urls_and_validates_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packages.yml"
            path.write_text(
                "packages:\n  - name: Demo\n    description: Demo\n    tags: [demo]\n    repository: https://github.com/example/demo/\n",
                encoding="utf-8",
            )
            registry = generate.load_registry(path)
            self.assertEqual(registry[0]["repository"], "https://github.com/example/demo")

    def test_category_matching_is_exact_and_case_insensitive(self):
        rules = [{"name": "Web", "keywords": {"http", "rest"}}]
        self.assertEqual(generate.derive_categories(["HTTP", "http-client"], rules), ["Web"])

    def test_registry_entry_overrides_discovered_metadata(self):
        project = generate.merge_project_metadata(
            {"name": "demo", "description": "GitHub", "categories": [], "tags": []},
            {"name": "Demo package", "description": "Registry", "tags": ["demo"]},
        )
        self.assertEqual(project["description"], "Registry")
        self.assertEqual(project["tags"], ["demo"])

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
            self.assertEqual(project["tags"], ["HTTP", "json", "rest"])
            self.assertTrue((Path(directory) / "projects/demo/index.html").is_file())

    def test_rendered_data_is_static_json(self):
        site = generate.render_site([{
            "name": "Demo",
            "description": "A package",
            "project_url": "projects/demo/index.html",
            "repository_url": "https://github.com/example/demo",
            "status": "standard",
            "standard": True,
            "artifacts": [],
        }], [])
        self.assertIn("Project catalog", site)
        self.assertIn("search.addEventListener", site)
        self.assertIn("localStorage", site)
        self.assertIn("project.status === 'error'", site)
        self.assertIn("hide-non-standard", site)
        self.assertIn("Project without release", site)
        self.assertIn("Project without standard release", site)
        self.assertIn("<h2><a href=", site)
        self.assertIn("filter-menu-bar", site)
        self.assertIn("Copyright © 2026 Pharo contributors", site)
        self.assertIn("guillep/pharo-catalog", site)
        self.assertIn("mobile-expanded", site)
        self.assertIn("pagination", site)
        self.assertIn("sidebar-toggle", site)
        self.assertIn("filters-hidden", site)
        self.assertIn("Hide projects without releases", site)
        self.assertIn("catalog-hide-no-release", site)
        self.assertIn("catalog-filters-collapsed", site)
        self.assertIn("hideNonStandard.checked", site)
        self.assertIn("filter(([, count]) => count > 0)", site)
        self.assertNotIn('id="tag-list"', site)
        self.assertIn('type="application/json"', site)

    def test_documentation_page_contains_requested_guidance(self):
        documentation = generate.render_documentation()
        self.assertIn("How to get my project here", documentation)
        self.assertIn("How do I make my project not hidden", documentation)
        self.assertIn("How to make my project list correct information", documentation)

    def test_shared_logo_asset_is_available(self):
        self.assertTrue(generate.LOGO_PATH.is_file())
        self.assertIn("<svg", generate.LOGO_PATH.read_text(encoding="utf-8"))

    def test_project_index_has_catalog_wrapper(self):
        wrapper = generate.render_project_wrapper(
            "Demo", "https://github.com/example/demo", "index.html", ".index-source.html"
        )
        self.assertIn("Pharo Project catalog", wrapper)
        self.assertIn("pharo-beacon.svg", wrapper)
        self.assertIn("catalog-bar", wrapper)
        self.assertIn("https://github.com/example/demo", wrapper)
        self.assertIn(".index-source.html", wrapper)


if __name__ == "__main__":
    unittest.main()
