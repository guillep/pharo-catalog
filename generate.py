#!/usr/bin/env python3
"""Generate a static catalog of Pharo projects from GitHub organizations."""

from __future__ import annotations

import argparse
import csv
import datetime
import html
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import yaml


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_INDEX_FILENAME = "index.html"
LOGO_PATH = Path(__file__).with_name("assets") / "pharo-beacon.svg"
CONTROLLED_CATEGORIES = {
    "Language & Compiler",
    "Development Environment",
    "Source Code & Version Control",
    "Testing",
    "Build & Deployment",
    "Web",
    "Networking",
    "UI",
    "Graphics & Visualization",
    "Data & Databases",
    "System & OS",
    "Tools",
    "Utilities",
    "Education",
    "AI",
}
OTHER_CATEGORY = "Uncategorized"
HIDDEN_CATEGORY = "Hidden"
UNTAGGED_CATEGORY = "Untagged"


class GitHubError(RuntimeError):
    """An API or download failure with enough context for the report."""


class MockGitHubClient:
    """Small deterministic client for local previews without GitHub requests."""

    def __init__(self, repositories: list[dict]):
        self.repositories = repositories

    def request_json(self, path: str, query: dict | None = None):
        if path.startswith("orgs/"):
            organization = path.split("/")[1]
            if query and str(query.get("page")) != "1":
                return []
            return [repo for repo in self.repositories if repo["full_name"].startswith(f"{organization}/")]
        if path.startswith("repos/") and path.endswith("/topics"):
            full_name = path.split("/")[1] + "/" + path.split("/")[2]
            return {"names": next((repo["topics"] for repo in self.repositories if repo["full_name"] == full_name), [])}
        if path.startswith("repos/") and path.endswith("/releases/latest"):
            full_name = path.split("/")[1] + "/" + path.split("/")[2]
            return next((repo["release"] for repo in self.repositories if repo["full_name"] == full_name), None)
        if path.startswith("repos/") and path.endswith("/commits"):
            full_name = path.split("/")[1] + "/" + path.split("/")[2]
            return [{"commit": {"author": {"date": next(repo["last_updated"] for repo in self.repositories if repo["full_name"] == full_name)}}}]
        raise GitHubError(f"mock path not found: {path}")

    def repository_topics(self, full_name: str) -> list[str]:
        return next((repo["topics"] for repo in self.repositories if repo["full_name"] == full_name), [])

    def latest_commit_date(self, full_name: str) -> str:
        return next((repo["last_updated"] for repo in self.repositories if repo["full_name"] == full_name), "")

    def download(self, url: str, destination: Path) -> None:
        destination.write_text("<h1>Mock Pharo project index</h1>", encoding="utf-8")


def mock_repositories() -> list[dict]:
    return [{
        "name": "mutalk", "full_name": "pharo-contributions/mutalk",
        "html_url": "https://github.com/pharo-contributions/mutalk",
        "description": "Mutation testing for Pharo.", "fork": False,
        "topics": ["pharo", "testing", "mutation-testing"],
        "release": {"tag_name": "v3.0.8", "assets": [{"name": "index.html", "browser_download_url": "mock://mutalk/index.html"}]}, "last_updated": "2026-09-20T00:00:00Z",
    }, {
        "name": "example-tool", "full_name": "pharo-project/example-tool",
        "html_url": "https://github.com/pharo-project/example-tool",
        "description": "A mock tool without a release index.", "fork": False,
        "topics": ["pharo", "tools"],
        "release": {"tag_name": "v1.0.0", "assets": []}, "last_updated": "2024-01-01T00:00:00Z",
    }]


class GitHubClient:
    def __init__(self, api_url: str, token: str | None):
        self.api_url = api_url.rstrip("/")
        self.token = token

    def request_json(self, path: str, query: dict[str, str] | None = None):
        url = f"{self.api_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers=self._headers("application/vnd.github+json"),
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise GitHubError(f"GET {url}: {error}") from error

    def download(self, url: str, destination: Path) -> None:
        request = urllib.request.Request(
            url,
            headers=self._headers("application/octet-stream"),
        )
        try:
            with urllib.request.urlopen(request) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as error:
            raise GitHubError(f"download {url}: {error}") from error

    def repository_topics(self, full_name: str) -> list[str]:
        result = self.request_json(f"repos/{full_name}/topics")
        topics = result.get("names", [])
        if not isinstance(topics, list) or not all(isinstance(topic, str) for topic in topics):
            raise GitHubError(f"GET repos/{full_name}/topics: invalid topic response")
        return topics

    def latest_commit_date(self, full_name: str) -> str:
        commits = self.request_json(f"repos/{full_name}/commits", {"per_page": "1"})
        return commits[0]["commit"]["author"]["date"] if commits else ""

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "pharo-catalog-generator",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return config


def normalize_repository_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        path = value.strip("/")
    else:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
            raise ValueError(f"invalid GitHub repository reference: {value}")
        path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"invalid GitHub repository reference: {value}")
    return f"https://github.com/{parts[0]}/{parts[1]}"


def load_registry_data(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    organizations = raw.get("organizations", [])
    if not isinstance(organizations, list):
        raise ValueError("registry organizations must be a list")
    normalized_organizations = []
    for organization in organizations:
        if isinstance(organization, str):
            normalized_organizations.append({"name": organization, "exclude": set(), "tags": []})
            continue
        if not isinstance(organization, dict) or not isinstance(organization.get("name"), str):
            raise ValueError("organizations must be names or mappings with a name")
        excluded = organization.get("exclude", [])
        tags = organization.get("tags", [])
        if not isinstance(excluded, list):
            raise ValueError(f"exclude for {organization['name']} must be a list")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"tags for {organization['name']} must be a list of strings")
        organization_name = organization["name"]
        normalized_organizations.append({
            "name": organization_name,
            "exclude": {
                normalize_repository_url(
                    url if "/" in url else f"{organization_name}/{url}"
                )
                for url in excluded
            },
            "tags": tags,
        })
    packages = raw.get("packages")
    if not isinstance(packages, list):
        raise ValueError("registry must define a packages list")
    registry = []
    identities = set()
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError("every registry package must be a mapping")
        missing = {"repository"} - entry.keys()
        if missing:
            raise ValueError(f"registry entry is missing: {', '.join(sorted(missing))}")
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"tags for {entry.get('repository', 'package')} must be a list of strings")
        identity = normalize_repository_url(entry["repository"])
        if identity in identities:
            raise ValueError(f"duplicate repository in registry: {identity}")
        identities.add(identity)
        registry.append({**entry, "tags": tags, "repository": identity})
    return {"organizations": normalized_organizations, "packages": registry}


def load_registry(path: Path) -> list[dict]:
    return load_registry_data(path)["packages"]


def load_category_rules(config: dict) -> list[dict]:
    rules = config.get("categories", [])
    if not isinstance(rules, list):
        raise ValueError("config categories must be a list")
    names = set()
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("name"), str):
            raise ValueError("every category must define a name and keywords")
        name = rule["name"]
        keywords = rule.get("keywords", [])
        if name not in CONTROLLED_CATEGORIES or name in names:
            raise ValueError(f"invalid or duplicate category: {name}")
        if not isinstance(keywords, list) or not all(isinstance(keyword, str) for keyword in keywords):
            raise ValueError(f"keywords for {name} must be a list of strings")
        names.add(name)
        normalized.append({"name": name, "keywords": {keyword.casefold() for keyword in keywords}})
    return normalized


def derive_categories(tags: list[str], rules: list[dict]) -> list[str]:
    tag_set = {tag.casefold() for tag in tags}
    categories = [rule["name"] for rule in rules if tag_set.intersection(rule["keywords"])]
    return categories or [OTHER_CATEGORY]


def merge_tags(topics: list[str], explicit_tags: list[str]) -> list[str]:
    result = []
    seen = set()
    for tag in [*topics, *explicit_tags]:
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def paged_repositories(client: GitHubClient, organization: str) -> list[dict]:
    repositories = []
    page = 1
    while True:
        batch = client.request_json(
            f"orgs/{urllib.parse.quote(organization)}/repos",
            {"per_page": "100", "page": str(page), "type": "all"},
        )
        if not batch:
            return repositories
        repositories.extend(batch)
        page += 1


def project_slug(repository: dict) -> str:
    name = repository.get("name") or repository.get("full_name", "project")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.").lower()
    return slug or "project"


def project_path(repository: dict) -> tuple[str, str]:
    owner = re.sub(r"[^a-zA-Z0-9._-]+", "-", repository.get("full_name", "organization/project").split("/", 1)[0]).strip("-.").lower() or "organization"
    return owner, project_slug(repository)


def is_special_repository(repository: dict) -> bool:
    name = repository.get("name", "").casefold()
    return name == ".github" or name == "github-pages" or name.endswith(".github.io")


def has_pharo_topic(client, repository: dict) -> bool:
    return any(topic.casefold() == "pharo" for topic in client.repository_topics(repository["full_name"]))


def update_age(date_text: str) -> str:
    if not date_text:
        return "Unknown"
    date = datetime.datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    age_days = (datetime.datetime.now(datetime.timezone.utc) - date).days
    if age_days < 30:
        return "Recent"
    if age_days < 90:
        return "3 months"
    if age_days < 365:
        return "This year"
    return "Over a year"


def latest_release(client: GitHubClient, full_name: str) -> dict | None:
    try:
        return client.request_json(f"repos/{full_name}/releases/latest")
    except GitHubError as error:
        if "HTTP Error 404" in str(error):
            return None
        raise


def render_project_wrapper(
        project_name: str,
        repository_url: str,
        index_filename: str,
        source_filename: str,
) -> str:
        return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(project_name)} · Pharo Project Catalog</title>
    <link rel="icon" href="../../../assets/favicon.ico">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <style>
        :root {{ color-scheme: light; --blue: #3297d4; --ink: #333; --muted: #777; --line: #ddd; --paper: #f7f7f7; --surface: #fff; }}
        [data-theme="dark"] {{ color-scheme: dark; --ink: #f4f4f4; --muted: #b8b8b8; --line: #454b50; --paper: #20252a; --surface: #2b3035; --blue: #62b1e3; }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; background: var(--paper); color: var(--ink); font: 15px/1.5 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        header {{ display: flex; align-items: center; gap: .85rem; padding: .75rem 1.25rem; border-bottom: 1px solid var(--line); background: var(--surface); }}
        header img {{ width: 42px; height: 42px; object-fit: contain; }}
        h1 {{ margin: 0; font-size: 1.35rem; font-weight: 600; }}
        a {{ color: var(--blue); }}
        .catalog-bar {{ display: flex; justify-content: space-between; gap: 1rem; padding: .65rem 1.25rem; background: var(--blue); color: #fff; font-size: .85rem; }}
        .catalog-bar a {{ color: #fff; }}
        iframe {{ display: block; width: 100%; min-height: calc(100vh - 62px); border: 0; background: var(--surface); }}
    </style>
</head>
<body>
    <header><img src="../../../assets/pharo-beacon.svg" alt="Pharo"><h1>Pharo Project Catalog · {html.escape(project_name)}</h1><div class="ml-auto"><a class="btn btn-secondary btn-sm" href="../../../documentation.html">Documentation</a><label class="form-check form-switch d-inline-block ml-2 mb-0"><input class="form-check-input" id="theme-toggle" type="checkbox"><span class="form-check-label">Dark mode</span></label></div></header>
    <nav class="catalog-bar"><a class="btn btn-link text-white" href="../../../index.html">← Project Catalog</a><a class="btn btn-link text-white" href="{html.escape(repository_url, quote=True)}">Repository ↗</a></nav>
    <iframe src="{html.escape(source_filename, quote=True)}" title="{html.escape(project_name)} project index"></iframe>
    <script>const theme = localStorage.getItem('catalog-theme') || 'light'; document.documentElement.dataset.theme = theme; document.getElementById('theme-toggle').checked = theme === 'dark'; document.getElementById('theme-toggle').addEventListener('change', (event) => {{ const next = event.target.checked ? 'dark' : 'light'; document.documentElement.dataset.theme = next; localStorage.setItem('catalog-theme', next); }});</script>
</body>
</html>
"""


def process_repository(
    client: GitHubClient,
    repository: dict,
    output: Path,
    index_filename: str,
    category_rules: list[dict] | None = None,
    explicit_tags: list[str] | None = None,
) -> dict:
    full_name = repository.get("full_name", repository.get("name", "unknown"))
    tags = merge_tags(client.repository_topics(full_name), explicit_tags or [])
    project = {
        "name": repository.get("name", full_name),
        "description": repository.get("description") or "No description provided.",
        "repository_url": repository.get("html_url", ""),
        "project_url": repository.get("html_url", ""),
        "organization": full_name.split("/", 1)[0] if "/" in full_name else "",
        "stars": repository.get("stargazers_count", 0),
        "categories": [UNTAGGED_CATEGORY] if repository.get("untagged") else derive_categories(tags, category_rules or []),
        "tags": tags,
        "standard": False,
        "status": "no-release",
        "artifacts": [],
        "last_updated": client.latest_commit_date(full_name) if hasattr(client, "latest_commit_date") else "",
    }
    project["update_age"] = update_age(project["last_updated"])
    release = latest_release(client, full_name)
    if release is None:
        project["status"] = "no-release"
        return project

    assets = release.get("assets", []) or []
    index_asset = next(
        (asset for asset in assets if asset.get("name") == index_filename),
        None,
    )
    if index_asset is None:
        project["status"] = "no-index"
        return project

    slug = project_slug(repository)
    owner, slug = project_path(repository)
    destination = output / "projects" / slug / index_filename
    destination = output / "projects" / owner / slug / index_filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    index_path = Path(index_filename)
    source_filename = f".{index_path.stem}-source{index_path.suffix or '.html'}"
    source_destination = destination.parent / source_filename
    client.download(index_asset["browser_download_url"], source_destination)
    destination.write_text(
        render_project_wrapper(
            project["name"], project["repository_url"], index_filename, source_filename
        ),
        encoding="utf-8",
    )
    project["standard"] = True
    project["status"] = "standard"
    project["project_url"] = f"projects/{owner}/{slug}/{index_filename}"
    return project


def repository_from_registry(entry: dict) -> dict:
    repository_url = entry["repository"]
    parsed = urlparse(repository_url)
    owner, name = parsed.path.strip("/").split("/")
    return {
        "name": entry.get("name") or name,
        "full_name": f"{owner}/{name}",
        "html_url": repository_url,
        "description": entry.get("description") or "",
        "fork": False,
    }


def merge_project_metadata(
    project: dict,
    entry: dict | None,
    category_rules: list[dict] | None = None,
) -> dict:
    if not entry:
        return project
    project.update({
        "name": entry.get("name") or project["name"],
        "description": entry.get("description") or project["description"],
        "tags": merge_tags(project.get("tags", []), entry.get("tags", [])),
    })
    project["categories"] = derive_categories(project["tags"], category_rules or [])
    return project


def render_site(
    projects: list[dict],
    errors: list[dict],
    category_names: list[str] | None = None,
) -> str:
    payload = json.dumps({"projects": projects, "errors": errors}, ensure_ascii=True)
    payload = payload.replace("</", "<\\/")
    error_report = ""
    if errors:
        error_items = "".join(
            f"<li><strong>{html.escape(error.get('organization', error.get('repository', 'unknown')))}</strong>: "
            f"{html.escape(error['error'])}</li>"
            for error in errors
        )
        error_report = f"""
    <details class="error-report alert alert-danger">
      <summary>{len(errors)} discovery error{'s' if len(errors) != 1 else ''}</summary>
      <ul>{error_items}</ul>
    </details>
"""
    category_names = category_names or sorted(CONTROLLED_CATEGORIES)
    category_json = json.dumps(category_names, ensure_ascii=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="A searchable catalog of Pharo projects.">
    <link rel="icon" href="assets/favicon.ico">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <title>Pharo Project Catalog</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="catalog-shell">
        <nav class="navbar navbar-expand-lg navbar-light bg-white border-bottom catalog-navbar" aria-label="Main navigation">
            <a class="navbar-brand d-flex align-items-center" href="index.html"><img class="pharo-logo mr-2" src="assets/pharo-beacon.svg" alt="Pharo"><span>Pharo Project Catalog</span></a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#catalog-nav" aria-controls="catalog-nav" aria-expanded="false" aria-label="Toggle navigation"><span class="navbar-toggler-icon"></span></button>
            <div class="collapse navbar-collapse" id="catalog-nav"><div class="navbar-nav align-items-lg-center ms-auto"><a class="btn btn-secondary btn-sm mx-1" href="documentation.html">Documentation</a><a class="btn btn-secondary btn-sm mx-1" href="catalog.json">JSON</a><a class="btn btn-secondary btn-sm mx-1" href="catalog.csv">CSV</a><div class="theme-toggle form-check form-switch mb-0 ms-lg-3"><input class="form-check-input" id="theme-toggle" type="checkbox"><label class="form-check-label text-secondary" for="theme-toggle">Dark mode</label></div></div></div>
        </nav>
                <div class="catalog-controls" aria-label="Catalog controls">
                    <div class="row align-items-end g-2">
                        <div class="col">
                            <label class="search-label" for="search">Search projects</label>
                            <input class="form-control" id="search" type="search" placeholder="Search name, description, category, or tag" autocomplete="off">
                        </div>
                        <div class="col-md-auto mt-3 mt-md-0">
                            <select class="form-select" id="sort" aria-label="Sort projects"><option value="name">Alphabetically</option><option value="stars">By stars</option><option value="updated">Last updated</option></select>
                        </div>
                    </div>
                    <div class="d-flex justify-content-between align-items-center mt-3">
                        <button id="sidebar-toggle" class="btn btn-primary text-nowrap" type="button" aria-label="Toggle categories" data-bs-toggle="collapse" data-bs-target="#filters" aria-controls="filters">Toggle Categories</button>
                        <p id="summary" class="text-end mb-0"></p>
                    </div>
                </div>
                <div id="catalog-layout" class="catalog-layout row">
                        <aside id="filters" class="filters collapse collapse-horizontal show bg-light col-md-3" aria-label="Project filters">
                <h2>Categories</h2><div id="category-list" class="filter-list list-group"></div>
                <div class="toggle-row form-check form-switch mt-3"><input class="form-check-input" id="hide-no-release" type="checkbox"><label class="form-check-label" for="hide-no-release">Hide projects without releases</label></div>
                <div class="toggle-row form-check form-switch"><input class="form-check-input" id="hide-non-standard" type="checkbox"><label class="form-check-label" for="hide-non-standard">Hide non-standard projects</label></div>
            </aside>
            <section id="catalog-content" class="catalog-content col-md-9">
                <section id="projects" class="project-grid row" aria-live="polite"></section>
                <p id="empty" class="empty-state alert alert-info" hidden>☹ No projects match your filters.</p>
                <nav id="pagination" class="pagination" aria-label="Project pages"></nav>
                {error_report}
            </section>
        </div>
    <footer class="catalog-footer">Copyright © 2026 Pharo contributors. Generated from GitHub metadata by <a href="https://github.com/guillep/pharo-catalog">guillep/pharo-catalog</a>.</footer>
  </main>
    <script id="catalog-data" type="application/json">{payload}</script>
    <script id="catalog-categories" type="application/json">{category_json}</script>
  <script>{JS}</script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
        """


DOCS_CSS = """
:root { --ink: #333; --muted: #777; --line: #ddd; --paper: #f7f7f7; --card: #fff; --blue: #3297d4; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.6 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.docs-shell { max-width: 900px; margin: auto; padding: 2rem 1.25rem 4rem; }
.docs-header { display: flex; align-items: center; gap: 1rem; border-bottom: 5px solid #f5f5f5; padding-bottom: 1.25rem; }
.docs-header img { width: 58px; height: 58px; object-fit: contain; }
.docs-header h1 { margin: 0; font-size: 2.2rem; font-weight: 600; }
.docs-header > a { margin-left: auto; color: var(--blue); }
.eyebrow { margin: 0 0 .15rem; color: var(--blue); font-size: .72rem; font-weight: bold; letter-spacing: .13em; }
.docs-content { margin-top: 2rem; }
.docs-content section { margin: 0 0 2rem; padding: 1.4rem; background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--blue); border-radius: 3px; }
.docs-content h2 { margin-top: 0; color: var(--blue); font-size: 1.45rem; }
code, pre { background: #f1f4f6; }
code { padding: .1rem .25rem; }
pre { padding: 1rem; overflow-x: auto; border: 1px solid var(--line); }
.catalog-footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .82rem; }
@media (max-width: 600px) { .docs-header { align-items: flex-start; flex-wrap: wrap; } .docs-header > a { margin-left: 0; width: 100%; } }
"""


def render_documentation() -> str:
        return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="assets/favicon.ico"><title>Catalog documentation</title><style>{DOCS_CSS}</style></head>
<body><main class="docs-shell">
    <header class="docs-header"><img src="assets/pharo-beacon.svg" alt="Pharo"><div><p class="eyebrow">PHARO ECOSYSTEM</p><h1>Catalog documentation</h1></div><a href="index.html">Project catalog</a></header>
    <article class="docs-content">
        <section><h2>How to get my project here</h2><p>Projects hosted in one of the managed GitHub organizations are discovered automatically. If you want your organization included, open a pull request against the <a href="https://github.com/guillep/pharo-catalog">guillep/pharo-catalog</a> repository and add it to <code>packages.yml</code>.</p><p>If your project is not hosted in a managed organization, you can publish it as a standalone package by adding an entry to the <code>packages</code> section in that same file through a pull request.</p><p>Provide a display name, a useful description, optional tags, and a concise <code>owner/repository</code> reference.</p><pre><code>packages:
    - name: My Project
        description: A short description of what the project does.
        tags: [pharo, tools]
        repository: https://github.com/my-org/my-project</code></pre><p>The catalog discovers the latest GitHub release and imports its <code>index.html</code> when available.</p></section>
        <section><h2>How do I make my project not hidden</h2><p>The catalog hides projects without releases by default. Use the reusable <a href="https://github.com/guillep/pharo-release">guillep/pharo-release</a> action to publish your package release and its standard <code>index.html</code> asset.</p><p>A project with a release but no index is marked as <strong>Project without standard release</strong>.</p></section>
        <section><h2>How to make my project list correct information</h2><p>Keep the registry name and description concise and accurate. Add useful free-form tags to the registry and maintain your repository topics. Topics and explicit tags are merged for search.</p><p>Categories are derived automatically from configured topic keywords, so categories should not be added to package entries. Use the canonical repository URL and keep your release asset named <code>index.html</code>.</p></section>
        <section><h2>How do GitHub topics and tags work?</h2><p>Topics configured on your GitHub repository become tags in the catalog. Registry tags from <code>registry.yml</code> are combined with those topics, and the result is used for search and category derivation.</p><p>The topic-to-category mapping is maintained in <a href="https://github.com/guillep/pharo-catalog/blob/main/config.yml#L18">config.yml</a>. If a topic should belong to a different category, propose the mapping change with a pull request to <a href="https://github.com/guillep/pharo-catalog">guillep/pharo-catalog</a>.</p></section>
        <section><h2>How can I consume this info programmatically?</h2><p>The generated catalog exposes machine-readable exports beside the website: <a href="catalog.json"><code>catalog.json</code></a> for structured consumers and <a href="catalog.csv"><code>catalog.csv</code></a> for spreadsheets and simple data pipelines.</p><p>Each project record includes its name, description, categories, tags, status, local project URL, and repository URL. The JSON export also includes generation errors. These files are static and can be fetched directly from the published catalog without authentication.</p></section>
    </article>
    <footer class="catalog-footer">Copyright © 2026 Pharo contributors. <a href="https://github.com/guillep/pharo-catalog">guillep/pharo-catalog</a>.</footer>
</main></body></html>
""".replace("packages.yml", "registry.yml").replace(
    "Projects hosted in one of the managed GitHub organizations are discovered automatically.",
    "Projects hosted in one of the managed GitHub organizations are discovered automatically when their repository has the exact <code>pharo</code> topic.",
)


def generate(config_path: Path, mock: bool = False) -> tuple[int, int]:
    config = load_config(config_path)
    github = config.get("github", {})
    index_config = config.get("index", {})
    index_filename = index_config.get("filename", DEFAULT_INDEX_FILENAME)
    token_env = github.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env)
    mock_data = mock_repositories() if mock else None
    client = MockGitHubClient(mock_data) if mock else GitHubClient(github.get("api_url", DEFAULT_API_URL), token)
    category_rules = load_category_rules(config)
    registry_path = config_path.parent / config.get("registry", "registry.yml")
    registry_data = load_registry_data(registry_path)
    registry = registry_data["packages"]
    organizations = (
        [{"name": owner, "exclude": set()} for owner in sorted({repo["full_name"].split("/", 1)[0] for repo in mock_data})]
        if mock else registry_data["organizations"]
    )
    output = Path(index_config.get("output_directory", "site"))
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    assets = output / "assets"
    assets.mkdir()
    shutil.copyfile(LOGO_PATH, assets / "pharo-beacon.svg")
    shutil.copyfile(config_path.parent / "assets" / "favicon.ico", assets / "favicon.ico")

    projects: list[dict] = []
    errors: list[dict] = []
    registry_by_identity = {entry["repository"]: entry for entry in registry}
    organization_tags: dict[str, list[str]] = {
        organization["name"]: organization.get("tags", []) for organization in organizations
    }
    repositories: dict[str, dict] = {
        entry["repository"]: repository_from_registry(entry) for entry in registry
    }
    for organization in organizations:
        organization_name = organization["name"]
        excluded = organization["exclude"]
        try:
            organization_repositories = paged_repositories(client, organization_name)
        except GitHubError as error:
            errors.append({"organization": organization_name, "error": str(error)})
            continue
        for repository in organization_repositories:
            if repository.get("fork") or is_special_repository(repository):
                continue
            try:
                identity = normalize_repository_url(repository["html_url"])
                repository["untagged"] = not has_pharo_topic(client, repository)
                if identity in excluded:
                    continue
                repositories.setdefault(identity, repository)
            except ValueError as error:
                errors.append({"repository": repository.get("full_name", "unknown"), "error": str(error)})

    for identity, repository in list(repositories.items()):
        entry = registry_by_identity.get(identity)
        try:
            project = process_repository(
                client, repository, output, index_filename, category_rules,
                [
                    *(entry.get("tags", []) if entry else []),
                    *organization_tags.get(repository["full_name"].split("/", 1)[0], []),
                ],
            )
            projects.append(merge_project_metadata(project, entry, category_rules))
        except GitHubError as error:
            errors.append({"repository": identity, "error": str(error)})
            projects.append(merge_project_metadata({
                "name": repository.get("name", identity),
                "description": repository.get("description") or "Metadata unavailable.",
                "repository_url": repository.get("html_url", identity),
                "project_url": repository.get("html_url", identity),
                "categories": [], "tags": [], "standard": False,
                "status": "error", "artifacts": [],
            }, entry, category_rules))

    projects.sort(key=lambda project: project["name"].lower())
    (output / "index.html").write_text(render_site(projects, errors), encoding="utf-8")
    (output / "documentation.html").write_text(render_documentation(), encoding="utf-8")
    report = {"projects": projects, "errors": errors}
    (output / "catalog.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (output / "catalog.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "description", "categories", "tags", "status", "last_updated", "update_age", "stars", "project_url", "repository_url"])
        writer.writeheader()
        for project in projects:
            writer.writerow({
                "name": project.get("name", ""),
                "description": project.get("description", ""),
                "categories": ";".join(project.get("categories", [])),
                "tags": ";".join(project.get("tags", [])),
                "status": project.get("status", ""),
                "last_updated": project.get("last_updated", ""),
                "update_age": project.get("update_age", "Unknown"),
                "stars": project.get("stars", 0),
                "project_url": project.get("project_url", ""),
                "repository_url": project.get("repository_url", ""),
            })
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"Generated {len(projects)} projects with {len(errors)} errors in {output}")
    return len(projects), len(errors)


CSS = """
:root { --ink: #333333; --muted: #777777; --line: #dddddd; --paper: #f7f7f7; --card: #ffffff; --blue: #3297d4; --blue-soft: #dcedf7; --filter-bg: #e7f3fa; --filter-header: #3297d4; --orange: #f15a24; --warning-bg: #fff0eb; --warning-text: #c43f16; }
[data-theme="dark"] { --ink: #f4f4f4; --muted: #b8b8b8; --line: #454b50; --paper: #20252a; --card: #2b3035; --blue: #62b1e3; --blue-soft: #294b61; --filter-bg: #263f50; --filter-header: #1f668f; --orange: #ff8a5c; --warning-bg: #5a3029; --warning-text: #ffc0a8; }
[data-theme="dark"] .catalog-navbar, [data-theme="dark"] .project-card, [data-theme="dark"] .filters { background-color: var(--card) !important; color: var(--ink); }
[data-theme="dark"] .filters { background-color: transparent !important; }
[data-theme="dark"] .navbar-light .navbar-brand, [data-theme="dark"] .navbar-light .nav-link { color: var(--muted); }
[data-theme="dark"] .navbar-toggler { border-color: var(--line); }
[data-theme="dark"] .btn-secondary { background-color: #4d5660; border-color: #626d78; color: #fff; }
[data-theme="dark"] .btn-primary { background-color: var(--blue); border-color: var(--blue); color: #17212b; }
[data-theme="dark"] #sidebar-toggle { color: #17212b; }
[data-theme="dark"] .form-control, [data-theme="dark"] .form-select, [data-theme="dark"] .list-group-item { background-color: var(--card); border-color: var(--line); color: var(--ink); }
[data-theme="dark"] .form-control::placeholder { color: var(--muted); opacity: 1; }
[data-theme="dark"] .list-group-item-action:hover, [data-theme="dark"] .list-group-item-action:focus { background-color: var(--blue-soft); color: var(--ink); }
[data-theme="dark"] .list-group-item.active { background-color: var(--blue); border-color: var(--blue); color: #17212b; }
[data-theme="dark"] .alert-info { background-color: #263f50; border-color: #3c718e; color: #c6e8f8; }
[data-theme="dark"] .alert-warning { background-color: #5a4530; border-color: #86673e; color: #ffe0a8; }
[data-theme="dark"] .alert-danger { background-color: #5a3029; border-color: #8e5148; color: #ffc0a8; }
* { box-sizing: border-box; }
html { overflow-y: scroll; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.catalog-shell { width: 100%; margin: 0; padding: 0 2rem 4rem; }
.catalog-navbar { margin: 0 -2rem; padding: .6rem 2rem; }
.icon-button, .sidebar-close { cursor: pointer; font: .9rem "Open Sans", sans-serif; }
.theme-toggle { margin-bottom: 0; }
.icon-button { width: 2.4rem; height: 2.4rem; font-size: 1.2rem; }
.icon-button:hover, .sidebar-close:hover { border-color: var(--blue); color: var(--blue); }
.pharo-logo { width: 42px; height: 42px; flex: 0 0 42px; object-fit: contain; }
.eyebrow { margin: 0 0 .2rem; color: var(--blue); font: bold .75rem/1.2 "Open Sans", sans-serif; letter-spacing: .14em; }
h1 { margin: 0; color: var(--ink); font-size: clamp(2.2rem, 6vw, 4rem); line-height: 1.05; font-weight: 600; }
.lede { margin: .7rem 0 0; color: var(--muted); max-width: 42rem; }
.catalog-controls { margin: 2rem 0 1.5rem; padding: 1rem 1.1rem 1.1rem; border: 1px solid var(--line); border-radius: 3px; background: var(--card); box-shadow: 0 2px 8px rgba(0,0,0,.04); }
.search-label { display: block; color: var(--muted); font: bold .75rem "Open Sans", sans-serif; letter-spacing: .1em; text-transform: uppercase; }
input { margin-top: .5rem; color: var(--ink); background: var(--card); font: 1rem "Open Sans", sans-serif; }
.catalog-layout { margin-left: -0.5rem; margin-right: -0.5rem; }
.filters { overflow: hidden; }
.filter-menu-bar { display: flex; align-items: center; gap: .55rem; min-height: 3rem; }
.filter-menu-bar .sidebar-close { margin-left: auto; }
.filter-menu-bar .navbar-toggler-icon { font-size: 1.1rem; line-height: 1; }
.filters h2 { margin: 1.3rem 1rem .45rem; color: var(--ink); font-size: .95rem; }
.sidebar-close { display: none; }
.filter-option.hidden-option { color: var(--orange); }
.pagination { display: flex; flex-wrap: wrap; justify-content: center; gap: .35rem; margin-top: 1.5rem; }
.page-button { min-width: 2.2rem; cursor: pointer; }
.summary { margin: .7rem 0 0; color: var(--muted); font: .9rem "Open Sans", sans-serif; }
.project-grid { row-gap: 1rem; }
.project-card h2 a { color: inherit; text-decoration: none; }
.project-card h2 a:hover { color: var(--blue); }
.metadata { font-size: .86rem; color: var(--blue) !important; }
.tags { display: flex; flex-wrap: wrap; gap: .35rem; }
.tag { padding: .15rem .4rem; background: var(--paper); border: 1px solid var(--line); color: var(--muted); font: .75rem "Open Sans", sans-serif; }
.project-card .links { display: flex; flex-wrap: wrap; gap: .7rem; margin-top: auto; padding-top: .8rem; font: .9rem "Open Sans", sans-serif; }
a { color: var(--blue); }
.catalog-footer { margin: 3rem -2rem 0; padding: 1rem 2rem 0; border-top: 1px solid var(--line); color: var(--muted); font: .82rem "Open Sans", sans-serif; }
.empty-state { text-align: center; }
.error-report summary { cursor: pointer; }
@media (max-width: 760px) { .catalog-shell { padding-top: 1.5rem; } .pharo-logo { width: 56px; height: 56px; flex-basis: 56px; } .filters { width: auto; margin: 1rem 0 0; } }
"""


JS = """
const data = JSON.parse(document.getElementById('catalog-data').textContent);
const categoryNames = JSON.parse(document.getElementById('catalog-categories').textContent);
const UNTAGGED_CATEGORY = 'Untagged';
const grid = document.getElementById('projects');
const search = document.getElementById('search');
const sort = document.getElementById('sort');
const summary = document.getElementById('summary');
const empty = document.getElementById('empty');
const pagination = document.getElementById('pagination');
const categoryList = document.getElementById('category-list');
const hideNoRelease = document.getElementById('hide-no-release');
const hideNonStandard = document.getElementById('hide-non-standard');
const filters = document.getElementById('filters');
const catalogContent = document.getElementById('catalog-content');
const layout = document.getElementById('catalog-layout');
const sidebarToggle = document.getElementById('sidebar-toggle');
const pageSize = 12;
let currentPage = 1;
let selectedCategory = localStorage.getItem('catalog-category') || '';
let selectedSort = localStorage.getItem('catalog-sort') || 'name';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>\"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
hideNoRelease.checked = localStorage.getItem('catalog-hide-no-release') === 'true';
hideNonStandard.checked = localStorage.getItem('catalog-hide-non-standard') === 'true';
document.documentElement.dataset.theme = localStorage.getItem('catalog-theme') || 'light';
document.getElementById('theme-toggle').checked = document.documentElement.dataset.theme === 'dark';
function baseProjects() {
  const query = search.value.trim().toLowerCase();
    return data.projects.filter((project) => {
        const searchable = `${project.name} ${project.description} ${(project.categories || []).join(' ')} ${(project.tags || []).join(' ')}`.toLowerCase();
                return searchable.includes(query);
    });
}
function isHidden(project) {
    const hasNoRelease = project.status === 'no-release' || project.status === 'error';
    const isNonStandard = project.status !== 'standard';
    return (hideNoRelease.checked && hasNoRelease) || (hideNonStandard.checked && isNonStandard);
}
function renderFilterList(container, values, selected, setter) {
    container.innerHTML = values.map(([value, count]) => { const label = value === '' ? 'All' : value === 'Hidden' ? '👁 See hidden' : value; return `<button type="button" class="filter-option list-group-item list-group-item-action d-flex justify-content-between align-items-center ${selected === value ? 'active' : ''} ${value === 'Hidden' ? 'hidden-option' : ''}" data-value="${escapeHtml(value)}"><span>${escapeHtml(label)}</span><span class="filter-count badge bg-secondary rounded-pill">${count}</span></button>`; }).join('');
    container.querySelectorAll('.filter-option').forEach((button) => button.addEventListener('click', () => {
        setter(button.dataset.value);
        currentPage = 1;
        render();
    }));
}
function render() {
    const candidates = baseProjects();
    const hiddenProjects = candidates.filter(isHidden);
    const visibleProjects = candidates.filter((project) => !isHidden(project));
    const catalogProjects = visibleProjects.filter((project) => !(project.categories || []).includes(UNTAGGED_CATEGORY));
    const untaggedProjects = visibleProjects.filter((project) => (project.categories || []).includes(UNTAGGED_CATEGORY));
    const categoryValues = [['', catalogProjects.length], ...categoryNames.map((name) => [name, catalogProjects.filter((project) => (project.categories || []).includes(name)).length]).filter(([, count]) => count > 0)];
    if (catalogProjects.some((project) => (project.categories || []).includes(UNTAGGED_CATEGORY))) categoryValues.push([UNTAGGED_CATEGORY, catalogProjects.filter((project) => (project.categories || []).includes(UNTAGGED_CATEGORY)).length]);
    const uncategorizedCount = catalogProjects.filter((project) => (project.categories || []).includes('Uncategorized')).length;
    if (uncategorizedCount > 0) categoryValues.push(['Uncategorized', uncategorizedCount]);
    categoryValues.push(['Hidden', hiddenProjects.length]);
    const pool = selectedCategory === 'Hidden' ? hiddenProjects : selectedCategory === UNTAGGED_CATEGORY ? untaggedProjects : selectedCategory ? visibleProjects : catalogProjects;
    const projects = selectedCategory === 'Hidden'
        ? hiddenProjects
        : pool.filter((project) => !selectedCategory || (project.categories || []).includes(selectedCategory));
    const availableCategory = categoryValues.some(([name]) => name === selectedCategory);
    if (selectedCategory && !availableCategory) selectedCategory = '';
    renderFilterList(categoryList, categoryValues, selectedCategory, (value) => { selectedCategory = value; localStorage.setItem('catalog-category', value); });
    const pages = Math.max(1, Math.ceil(projects.length / pageSize));
    currentPage = Math.min(currentPage, pages);
    const sorted = [...projects].sort((a, b) => selectedSort === 'stars' ? (b.stars || 0) - (a.stars || 0) : selectedSort === 'updated' ? String(b.last_updated || '').localeCompare(String(a.last_updated || '')) : a.name.localeCompare(b.name));
    const visible = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);
    grid.innerHTML = visible.map((project) => {
    const standard = project.status === 'standard';
    const status = standard ? 'Standard release' : project.status === 'no-release' || project.status === 'error' ? 'Project without release' : 'Project without standard release';
    const categories = (project.categories || []).map(escapeHtml).join(' · ');
    const tags = (project.tags || []).filter((value) => value.toLowerCase() !== 'pharo').map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join(' ');
    return `<div class="col-lg-4 p-0"><article class="project-card card m-2"><div class="card-body d-flex flex-column"><h2 class="card-title h5"><a href="${escapeHtml(project.project_url)}">${escapeHtml(project.name)}</a></h2><div class="d-flex flex-wrap align-items-center gap-2 mb-2"><span class="metadata">${categories}</span><div class="tags">${tags}</div></div><p class="card-text">${escapeHtml(project.description)}</p>${standard ? '' : `<div class="alert alert-warning py-2 mt-3" role="alert">⚠ ${escapeHtml(status)}</div>`}<div class="card-footer-row mt-auto pt-3 d-flex justify-content-between align-items-center"><span class="metadata">Updated: ${escapeHtml(project.update_age || 'Unknown')}</span><a class="btn btn-outline-secondary btn-sm" href="${escapeHtml(project.repository_url)}" aria-label="Repository"><i class="bi bi-github" aria-hidden="true"></i></a></div></div></article></div>`;
  }).join('');
    pagination.innerHTML = Array.from({length: pages}, (_, index) => `<button class="page-button btn ${currentPage === index + 1 ? 'btn-primary' : 'btn-outline-primary'}" data-page="${index + 1}">${index + 1}</button>`).join('');
    pagination.querySelectorAll('.page-button').forEach((button) => button.addEventListener('click', () => { currentPage = Number(button.dataset.page); render(); window.scrollTo({top: 0, behavior: 'smooth'}); }));
    summary.textContent = `${projects.length} of ${data.projects.length} projects${data.errors.length ? ` · ${data.errors.length} discovery errors` : ''}`;
  empty.hidden = projects.length !== 0;
}
search.addEventListener('input', render);
sort.value = selectedSort;
sort.addEventListener('change', () => { selectedSort = sort.value; localStorage.setItem('catalog-sort', selectedSort); currentPage = 1; render(); });
hideNoRelease.addEventListener('change', () => { localStorage.setItem('catalog-hide-no-release', hideNoRelease.checked); currentPage = 1; render(); });
hideNonStandard.addEventListener('change', () => { localStorage.setItem('catalog-hide-non-standard', hideNonStandard.checked); currentPage = 1; render(); });
document.getElementById('theme-toggle').addEventListener('change', (event) => { const theme = event.target.checked ? 'dark' : 'light'; document.documentElement.dataset.theme = theme; localStorage.setItem('catalog-theme', theme); });
filters.addEventListener('shown.bs.collapse', () => { sidebarToggle.textContent = 'Toggle Categories'; });
filters.addEventListener('hidden.bs.collapse', () => { sidebarToggle.textContent = 'Toggle Categories'; });
filters.addEventListener('shown.bs.collapse', () => { catalogContent.classList.remove('col-md-12'); catalogContent.classList.add('col-md-9'); });
filters.addEventListener('hidden.bs.collapse', () => { catalogContent.classList.remove('col-md-9'); catalogContent.classList.add('col-md-12'); });
render();
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--mock", action="store_true", help="generate locally without GitHub requests")
    args = parser.parse_args()
    try:
        generate(Path(args.config), mock=args.mock)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
