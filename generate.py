#!/usr/bin/env python3
"""Generate a static catalog of Pharo projects from GitHub organizations."""

from __future__ import annotations

import argparse
import csv
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
    "Education",
}
OTHER_CATEGORY = "Other"
HIDDEN_CATEGORY = "Hidden"


class GitHubError(RuntimeError):
    """An API or download failure with enough context for the report."""


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
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError(f"invalid GitHub repository URL: {value}")
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"invalid GitHub repository URL: {value}")
    return f"https://github.com/{parts[0]}/{parts[1]}"


def load_registry_data(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    organizations = raw.get("organizations", [])
    if not isinstance(organizations, list):
        raise ValueError("registry organizations must be a list")
    packages = raw.get("packages")
    if not isinstance(packages, list):
        raise ValueError("registry must define a packages list")
    registry = []
    identities = set()
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError("every registry package must be a mapping")
        missing = {"name", "description", "repository"} - entry.keys()
        if missing:
            raise ValueError(f"registry entry is missing: {', '.join(sorted(missing))}")
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"tags for {entry['name']} must be a list of strings")
        identity = normalize_repository_url(entry["repository"])
        if identity in identities:
            raise ValueError(f"duplicate repository in registry: {identity}")
        identities.add(identity)
        registry.append({**entry, "tags": tags, "repository": identity})
    return {"organizations": organizations, "packages": registry}


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
    <link rel="icon" href="../../assets/favicon.ico">
    <style>
        :root {{ color-scheme: light; --blue: #3297d4; --ink: #333; --muted: #777; --line: #ddd; --paper: #f7f7f7; }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; background: var(--paper); color: var(--ink); font: 15px/1.5 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        header {{ display: flex; align-items: center; gap: .85rem; padding: .75rem 1.25rem; border-bottom: 4px solid #f5f5f5; background: #fff; }}
        header img {{ width: 42px; height: 42px; object-fit: contain; }}
        h1 {{ margin: 0; font-size: 1.35rem; font-weight: 600; }}
        a {{ color: var(--blue); }}
        .catalog-bar {{ display: flex; justify-content: space-between; gap: 1rem; padding: .65rem 1.25rem; background: var(--blue); color: #fff; font-size: .85rem; }}
        .catalog-bar a {{ color: #fff; }}
        iframe {{ display: block; width: 100%; min-height: calc(100vh - 62px); border: 0; background: #fff; }}
    </style>
</head>
<body>
    <header><img src="../../assets/pharo-beacon.svg" alt="Pharo"><h1>Pharo Project catalog · {html.escape(project_name)}</h1></header>
    <nav class="catalog-bar"><a href="../../index.html">← Project catalog</a><a href="{html.escape(repository_url, quote=True)}">Repository ↗</a></nav>
    <iframe src="{html.escape(source_filename, quote=True)}" title="{html.escape(project_name)} project index"></iframe>
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
        "categories": derive_categories(tags, category_rules or []),
        "tags": tags,
        "standard": False,
        "status": "no-release",
        "artifacts": [],
    }
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
    destination = output / "projects" / slug / index_filename
    if destination.parent.exists():
        owner = full_name.split("/", 1)[0] if "/" in full_name else "repository"
        slug = f"{re.sub(r'[^a-zA-Z0-9._-]+', '-', owner).strip('-.').lower()}-{slug}"
    destination = output / "projects" / slug / index_filename
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
    project["project_url"] = f"projects/{slug}/{index_filename}"
    return project


def repository_from_registry(entry: dict) -> dict:
    repository_url = entry["repository"]
    parsed = urlparse(repository_url)
    owner, name = parsed.path.strip("/").split("/")
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "html_url": repository_url,
        "description": entry["description"],
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
        "name": entry["name"],
        "description": entry["description"],
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
    <details class="error-report">
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
  <title>Pharo Project Catalog</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="catalog-shell">
    <header class="catalog-header">
    <img class="pharo-logo" src="assets/pharo-beacon.svg" alt="Pharo">
    <div class="header-copy">
        <p class="eyebrow">PHARO ECOSYSTEM</p>
        <h1>Pharo Project catalog</h1>
        <p class="lede">Discover Pharo packages, releases, and their project indexes.</p>
      </div>
            <div class="header-actions">
                <nav class="data-nav" aria-label="Catalog resources">
                    <a href="documentation.html">Documentation</a>
                    <a href="catalog.json">JSON</a>
                    <a href="catalog.csv">CSV</a>
                </nav>
                <label class="theme-toggle"><input id="theme-toggle" type="checkbox"> Dark mode</label>
            </div>
    </header>
        <div id="catalog-layout" class="catalog-layout">
            <aside id="filters" class="filters" aria-label="Project filters">
                <div class="filter-menu-bar">
                    <button id="sidebar-toggle" class="icon-button burger" type="button" aria-label="Toggle filters">☰</button>
                    <span>Filters</span>
                    <button id="sidebar-close" class="sidebar-close" type="button" aria-label="Close filters">×</button>
                </div>
                <label class="toggle-row"><input id="hide-no-release" type="checkbox"> Hide projects without releases</label>
                <label class="toggle-row"><input id="hide-non-standard" type="checkbox"> Hide non-standard projects</label>
                <h2>Categories</h2><div id="category-list" class="filter-list"></div>
            </aside>
            <section class="catalog-content">
                <section class="catalog-controls" aria-label="Catalog controls">
                    <label class="search-label" for="search">Search projects</label>
                    <input id="search" type="search" placeholder="Search name, description, category, or tag" autocomplete="off">
                    <p id="summary" class="summary"></p>
                </section>
                <section id="projects" class="project-grid" aria-live="polite"></section>
                <p id="empty" class="empty-state" hidden>☹ No projects match your filters.</p>
                <nav id="pagination" class="pagination" aria-label="Project pages"></nav>
                {error_report}
            </section>
        </div>
    <footer class="catalog-footer">Copyright © 2026 Pharo contributors. Generated from GitHub metadata by <a href="https://github.com/guillep/pharo-catalog">guillep/pharo-catalog</a>.</footer>
  </main>
    <script id="catalog-data" type="application/json">{payload}</script>
    <script id="catalog-categories" type="application/json">{category_json}</script>
  <script>{JS}</script>
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
        <section><h2>How to get my project here</h2><p>Add your project to <code>packages.yml</code> through a pull request. Provide a display name, a useful description, optional tags, and a canonical GitHub repository URL.</p><pre><code>packages:
    - name: My Project
        description: A short description of what the project does.
        tags: [pharo, tools]
        repository: https://github.com/my-org/my-project</code></pre><p>The catalog discovers the latest GitHub release and imports its <code>index.html</code> when available.</p></section>
        <section><h2>How do I make my project not hidden</h2><p>The catalog hides projects without releases by default. Publish a GitHub release and attach an asset named exactly <code>index.html</code> to make the project standard and visible.</p><p>A project with a release but no index is marked as <strong>Project without standard release</strong>.</p></section>
        <section><h2>How to make my project list correct information</h2><p>Keep the registry name and description concise and accurate. Add useful free-form tags to the registry and maintain your repository topics. Topics and explicit tags are merged for search.</p><p>Categories are derived automatically from configured topic keywords, so categories should not be added to package entries. Use the canonical repository URL and keep your release asset named <code>index.html</code>.</p></section>
    </article>
    <footer class="catalog-footer">Copyright © 2026 Pharo contributors. <a href="https://github.com/guillep/pharo-catalog">guillep/pharo-catalog</a>.</footer>
</main></body></html>
"""


def generate(config_path: Path) -> tuple[int, int]:
    config = load_config(config_path)
    github = config.get("github", {})
    index_config = config.get("index", {})
    index_filename = index_config.get("filename", DEFAULT_INDEX_FILENAME)
    token_env = github.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env)
    client = GitHubClient(github.get("api_url", DEFAULT_API_URL), token)
    category_rules = load_category_rules(config)
    registry_path = config_path.parent / config.get("registry", "packages.yml")
    registry_data = load_registry_data(registry_path)
    registry = registry_data["packages"]
    organizations = registry_data["organizations"]
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
    repositories: dict[str, dict] = {
        entry["repository"]: repository_from_registry(entry) for entry in registry
    }
    for organization in organizations:
        try:
            organization_repositories = paged_repositories(client, organization)
        except GitHubError as error:
            errors.append({"organization": organization, "error": str(error)})
            continue
        for repository in organization_repositories:
            if repository.get("fork"):
                continue
            try:
                identity = normalize_repository_url(repository["html_url"])
                repositories.setdefault(identity, repository)
            except ValueError as error:
                errors.append({"repository": repository.get("full_name", "unknown"), "error": str(error)})

    for identity, repository in repositories.items():
        entry = registry_by_identity.get(identity)
        try:
            project = process_repository(
                client, repository, output, index_filename, category_rules,
                entry.get("tags", []) if entry else [],
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
        writer = csv.DictWriter(handle, fieldnames=["name", "description", "categories", "tags", "status", "project_url", "repository_url"])
        writer.writeheader()
        for project in projects:
            writer.writerow({
                "name": project.get("name", ""),
                "description": project.get("description", ""),
                "categories": ";".join(project.get("categories", [])),
                "tags": ";".join(project.get("tags", [])),
                "status": project.get("status", ""),
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
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.catalog-shell { max-width: 1120px; margin: auto; padding: 2.5rem 1.25rem 4rem; }
.catalog-header { display: flex; align-items: center; gap: 1rem; border-bottom: 5px solid #f5f5f5; padding-bottom: 1.5rem; }
.header-copy { flex: 1; }
.header-actions { display: flex; gap: .5rem; align-self: flex-start; }
.data-nav { display: flex; gap: .3rem; padding: .25rem; border: 1px solid var(--line); border-radius: 3px; background: var(--card); }
.data-nav a { padding: .35rem .55rem; border-radius: 2px; color: var(--blue); text-decoration: none; font: .78rem "Open Sans", sans-serif; }
.data-nav a:hover { background: var(--blue-soft); }
.icon-button, .sidebar-close { border: 1px solid var(--line); border-radius: 2px; background: var(--card); color: var(--ink); padding: .45rem .65rem; cursor: pointer; font: .9rem "Open Sans", sans-serif; }
.theme-toggle, .toggle-row { position: relative; display: flex; align-items: center; gap: .55rem; color: var(--muted); cursor: pointer; font: .82rem "Open Sans", sans-serif; }
.theme-toggle input, .toggle-row input { position: absolute; opacity: 0; pointer-events: none; }
.theme-toggle::before, .toggle-row::before { content: ""; width: 2.25rem; height: 1.25rem; flex: 0 0 2.25rem; border-radius: 999px; background: #c7c7c7; box-shadow: inset 0 0 0 1px rgba(0,0,0,.12); transition: background .2s ease; }
.theme-toggle::after, .toggle-row::after { content: ""; position: absolute; width: .95rem; height: .95rem; margin-left: .15rem; border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.25); transition: transform .2s ease; }
.theme-toggle:has(input:checked)::before, .toggle-row:has(input:checked)::before { background: var(--blue); }
.theme-toggle:has(input:checked)::after, .toggle-row:has(input:checked)::after { transform: translateX(1rem); }
.icon-button { width: 2.4rem; height: 2.4rem; font-size: 1.2rem; }
.icon-button:hover, .sidebar-close:hover { border-color: var(--blue); color: var(--blue); }
.pharo-logo { width: 72px; height: 72px; flex: 0 0 72px; object-fit: contain; }
.eyebrow { margin: 0 0 .2rem; color: var(--blue); font: bold .75rem/1.2 "Open Sans", sans-serif; letter-spacing: .14em; }
h1 { margin: 0; color: var(--ink); font-size: clamp(2.2rem, 6vw, 4rem); line-height: 1.05; font-weight: 600; }
.lede { margin: .7rem 0 0; color: var(--muted); max-width: 42rem; }
.catalog-controls { margin: 2rem 0 1.5rem; padding: 1rem 1.1rem 1.1rem; border: 1px solid var(--line); border-radius: 3px; background: var(--card); box-shadow: 0 2px 8px rgba(0,0,0,.04); }
.search-label { display: block; color: var(--muted); font: bold .75rem "Open Sans", sans-serif; letter-spacing: .1em; text-transform: uppercase; }
input { width: 100%; margin-top: .5rem; padding: .85rem 1rem; border: 2px solid #dddddd; border-radius: 2px; color: var(--ink); background: var(--card); font: 1rem "Open Sans", sans-serif; }
input:focus { outline: 3px solid rgba(50,151,212,.2); border-color: var(--blue); }
.catalog-layout { display: grid; grid-template-columns: 248px 1fr; gap: 2rem; }
.catalog-layout.filters-hidden { grid-template-columns: 3.5rem 1fr; }
.catalog-layout.filters-hidden .filters { padding: .5rem; background: transparent; border: 0; box-shadow: none; }
.catalog-layout.filters-hidden .filters > :not(.filter-menu-bar) { display: none; }
.filters { padding: 0 0 1rem; margin-top: 2rem; overflow: hidden; background: var(--filter-bg); border: 1px solid rgba(50,151,212,.32); border-radius: 4px; box-shadow: 0 3px 12px rgba(0,0,0,.07); }
.filter-menu-bar { display: flex; align-items: center; gap: .55rem; min-height: 3rem; padding: .7rem .8rem; background: var(--filter-header); color: #fff; font: bold .85rem "Open Sans", sans-serif; }
.catalog-layout.filters-hidden .filter-menu-bar { padding: .7rem; }
.catalog-layout.filters-hidden .filter-menu-bar > span { display: none; }
.catalog-layout.filters-hidden .filter-menu-bar { width: 3rem; min-height: 3rem; padding: 0; justify-content: center; border-radius: 3px; }
.catalog-layout.filters-hidden .filter-menu-bar .burger { width: 3rem; height: 3rem; border: 0; }
.filter-menu-bar .sidebar-close { margin-left: auto; }
.filter-menu-bar .burger { border-color: rgba(255,255,255,.65); background: transparent; color: #fff; }
.filters h2 { margin: 1.3rem 1rem .45rem; color: var(--ink); font-size: .95rem; }
.sidebar-close { display: none; }
.toggle-row { position: relative; justify-content: flex-start; margin: 1rem; text-align: left; }
.filter-list { display: grid; gap: .2rem; padding: 0 .65rem; }
.filter-option { display: flex; justify-content: space-between; gap: .5rem; width: 100%; padding: .3rem .4rem; border: 0; border-radius: 2px; background: transparent; color: var(--muted); text-align: left; cursor: pointer; font: .82rem "Open Sans", sans-serif; }
.filter-option:hover, .filter-option.active { background: var(--card); color: var(--blue); }
.filter-option.hidden-option { color: var(--orange); }
.filter-option.hidden-option:hover, .filter-option.hidden-option.active { color: var(--orange); background: var(--card); }
.filter-count { color: var(--muted); }
.pagination { display: flex; flex-wrap: wrap; justify-content: center; gap: .35rem; margin-top: 1.5rem; }
.page-button { min-width: 2.2rem; padding: .4rem .6rem; border: 1px solid var(--line); border-radius: 2px; background: var(--card); color: var(--blue); cursor: pointer; }
.page-button.active { background: var(--blue); color: #fff; }
.summary { margin: .7rem 0 0; color: var(--muted); font: .9rem "Open Sans", sans-serif; }
.project-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 1rem; }
.project-card { display: flex; flex-direction: column; min-height: 220px; padding: 1.2rem; background: var(--card); border: 1px solid var(--line); border-top: 3px solid var(--blue); border-radius: 2px; box-shadow: 0 2px 8px rgba(0,0,0,.04); }
.project-card h2 { margin: 0; font-size: 1.35rem; line-height: 1.15; }
.project-card h2 a { color: inherit; text-decoration: none; }
.project-card h2 a:hover { color: var(--blue); }
.project-card p { color: var(--muted); margin: .7rem 0; }
.metadata { font-size: .86rem; color: var(--blue) !important; }
.tags { display: flex; flex-wrap: wrap; gap: .35rem; }
.tag { padding: .15rem .4rem; background: var(--paper); border: 1px solid var(--line); color: var(--muted); font: .75rem "Open Sans", sans-serif; }
.project-card .links { display: flex; flex-wrap: wrap; gap: .7rem; margin-top: auto; padding-top: .8rem; font: .9rem "Open Sans", sans-serif; }
a { color: var(--blue); }
.badge { display: inline-block; margin: .7rem 0 0; padding: .2rem .5rem; border-radius: 2px; background: var(--warning-bg); color: var(--warning-text); font: .72rem "Open Sans", sans-serif; }
.badge.standard { background: var(--blue-soft); color: #24719e; }
.catalog-footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font: .82rem "Open Sans", sans-serif; }
.empty-state { padding: 2rem; text-align: center; color: var(--muted); }
.error-report { margin-top: 1.5rem; padding: 1rem; border: 1px solid #f0c5a9; border-radius: 2px; background: #fff8f3; color: #7e3e1f; font: .9rem "Open Sans", sans-serif; }
.error-report summary { cursor: pointer; font-weight: bold; }
.error-report li { margin-top: .4rem; overflow-wrap: anywhere; }
@media (max-width: 760px) { .catalog-shell { padding-top: 1.5rem; } .catalog-header { align-items: flex-start; } .pharo-logo { width: 56px; height: 56px; flex-basis: 56px; } .catalog-layout { display: block; } .filters { position: static; width: auto; min-height: 3.5rem; max-height: 3.5rem; margin: 1.25rem 0 0; transform: none; transition: max-height .2s ease; } .catalog-layout.filters-hidden .filters { padding: 0; min-height: 3.5rem; } .filters.mobile-expanded { max-height: 1000px; } .filters.open { transform: none; } .sidebar-close { display: none; } .burger { display: block; } }
"""


JS = """
const data = JSON.parse(document.getElementById('catalog-data').textContent);
const categoryNames = JSON.parse(document.getElementById('catalog-categories').textContent);
const grid = document.getElementById('projects');
const search = document.getElementById('search');
const summary = document.getElementById('summary');
const empty = document.getElementById('empty');
const pagination = document.getElementById('pagination');
const categoryList = document.getElementById('category-list');
const hideNoRelease = document.getElementById('hide-no-release');
const hideNonStandard = document.getElementById('hide-non-standard');
const filters = document.getElementById('filters');
const layout = document.getElementById('catalog-layout');
const pageSize = 12;
let currentPage = 1;
let selectedCategory = localStorage.getItem('catalog-category') || '';
let filtersCollapsed = localStorage.getItem('catalog-filters-collapsed') === 'true';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>\"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
hideNoRelease.checked = localStorage.getItem('catalog-hide-no-release') !== 'false';
hideNonStandard.checked = localStorage.getItem('catalog-hide-non-standard') !== 'false';
document.documentElement.dataset.theme = localStorage.getItem('catalog-theme') || 'light';
document.getElementById('theme-toggle').checked = document.documentElement.dataset.theme === 'dark';
if (filtersCollapsed) layout.classList.add('filters-hidden');
if (!filtersCollapsed && window.matchMedia('(max-width: 760px)').matches) filters.classList.add('mobile-expanded');
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
    container.innerHTML = values.map(([value, count]) => { const label = value === '' ? 'All' : value === 'Hidden' ? '👁 See hidden' : value; return `<button class="filter-option ${selected === value ? 'active' : ''} ${value === 'Hidden' ? 'hidden-option' : ''}" data-value="${escapeHtml(value)}"><span>${escapeHtml(label)}</span><span class="filter-count">${count}</span></button>`; }).join('');
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
    const categoryValues = [['', visibleProjects.length], ...categoryNames.map((name) => [name, visibleProjects.filter((project) => (project.categories || []).includes(name)).length]).filter(([, count]) => count > 0)];
    const otherCount = visibleProjects.filter((project) => (project.categories || []).includes('Other')).length;
    if (otherCount > 0) categoryValues.push(['Other', otherCount]);
    categoryValues.push(['Hidden', hiddenProjects.length]);
    const pool = selectedCategory === 'Hidden' ? hiddenProjects : visibleProjects;
    const projects = pool.filter((project) => !selectedCategory || (project.categories || []).includes(selectedCategory));
    const availableCategory = categoryValues.some(([name]) => name === selectedCategory);
    if (selectedCategory && !availableCategory) selectedCategory = '';
    renderFilterList(categoryList, categoryValues, selectedCategory, (value) => { selectedCategory = value; localStorage.setItem('catalog-category', value); });
    const pages = Math.max(1, Math.ceil(projects.length / pageSize));
    currentPage = Math.min(currentPage, pages);
    const visible = projects.slice((currentPage - 1) * pageSize, currentPage * pageSize);
    grid.innerHTML = visible.map((project) => {
    const standard = project.status === 'standard';
    const status = standard ? 'Standard release' : project.status === 'no-release' || project.status === 'error' ? 'Project without release' : 'Project without standard release';
    const categories = (project.categories || []).map(escapeHtml).join(' · ');
    const tags = (project.tags || []).map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join(' ');
    return `<article class="project-card"><h2><a href="${escapeHtml(project.project_url)}">${escapeHtml(project.name)}</a></h2><p>${escapeHtml(project.description)}</p><p class="metadata">${categories}</p><div class="tags">${tags}</div>${standard ? '' : `<span class="badge" title="${escapeHtml(status)}">⚠ ${escapeHtml(status)}</span>`}<div class="links"><a href="${escapeHtml(project.repository_url)}">Repository</a></div></article>`;
  }).join('');
    pagination.innerHTML = Array.from({length: pages}, (_, index) => `<button class="page-button ${currentPage === index + 1 ? 'active' : ''}" data-page="${index + 1}">${index + 1}</button>`).join('');
    pagination.querySelectorAll('.page-button').forEach((button) => button.addEventListener('click', () => { currentPage = Number(button.dataset.page); render(); window.scrollTo({top: 0, behavior: 'smooth'}); }));
    summary.textContent = `${projects.length} of ${data.projects.length} projects${data.errors.length ? ` · ${data.errors.length} discovery errors` : ''}`;
  empty.hidden = projects.length !== 0;
}
search.addEventListener('input', render);
hideNoRelease.addEventListener('change', () => { localStorage.setItem('catalog-hide-no-release', hideNoRelease.checked); currentPage = 1; render(); });
hideNonStandard.addEventListener('change', () => { localStorage.setItem('catalog-hide-non-standard', hideNonStandard.checked); currentPage = 1; render(); });
document.getElementById('theme-toggle').addEventListener('change', (event) => { const theme = event.target.checked ? 'dark' : 'light'; document.documentElement.dataset.theme = theme; localStorage.setItem('catalog-theme', theme); });
document.getElementById('sidebar-toggle').addEventListener('click', () => {
    if (window.matchMedia('(max-width: 760px)').matches) {
        filters.classList.toggle('mobile-expanded');
        filtersCollapsed = !filters.classList.contains('mobile-expanded');
    } else {
        layout.classList.toggle('filters-hidden');
        filtersCollapsed = layout.classList.contains('filters-hidden');
    }
    localStorage.setItem('catalog-filters-collapsed', filtersCollapsed);
});
document.getElementById('sidebar-close').addEventListener('click', () => filters.classList.remove('open'));
render();
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yml")
    args = parser.parse_args()
    try:
        generate(Path(args.config))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
