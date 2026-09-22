#!/usr/bin/env python3
"""Generate a static catalog of Pharo projects from GitHub organizations."""

from __future__ import annotations

import argparse
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
    organizations = config.get("organizations")
    if not isinstance(organizations, list):
        raise ValueError("config organizations must be a list")
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


def load_registry(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
    return registry


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
    return [rule["name"] for rule in rules if tag_set.intersection(rule["keywords"])]


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
    client.download(index_asset["browser_download_url"], destination)
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
        <h1>Project catalog</h1>
        <p class="lede">Discover Pharo packages, releases, and their project indexes.</p>
      </div>
            <div class="header-actions">
                <label class="theme-toggle"><input id="theme-toggle" type="checkbox"> Dark mode</label>
                <button id="sidebar-toggle" class="icon-button burger" type="button" aria-label="Open filters">☰</button>
            </div>
    </header>
        <div id="catalog-layout" class="catalog-layout">
            <aside id="filters" class="filters" aria-label="Project filters">
                <button id="sidebar-close" class="sidebar-close" type="button">Close filters</button>
                <label class="toggle-row"><input id="hide-no-release" type="checkbox"> Hide projects without releases</label>
                <h2>Categories</h2><div id="category-list" class="filter-list"></div>
                <h2>Tags</h2><div id="tag-list" class="filter-list"></div>
            </aside>
            <section class="catalog-content">
                <section class="catalog-controls" aria-label="Catalog controls">
                    <label class="search-label" for="search">Search projects</label>
                    <input id="search" type="search" placeholder="Search name, description, category, or tag" autocomplete="off">
                    <p id="summary" class="summary"></p>
                </section>
                <section id="projects" class="project-grid" aria-live="polite"></section>
                <nav id="pagination" class="pagination" aria-label="Project pages"></nav>
                <p id="empty" class="empty-state" hidden>No projects match your filters.</p>
                {error_report}
            </section>
        </div>
    <footer class="catalog-footer">Generated from GitHub metadata. Standard releases include a published project index.</footer>
  </main>
    <script id="catalog-data" type="application/json">{payload}</script>
    <script id="catalog-categories" type="application/json">{category_json}</script>
  <script>{JS}</script>
</body>
</html>
    """


def generate(config_path: Path) -> tuple[int, int]:
    config = load_config(config_path)
    github = config.get("github", {})
    index_config = config.get("index", {})
    token_env = github.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env)
    client = GitHubClient(github.get("api_url", DEFAULT_API_URL), token)
    index_filename = index_config.get("filename", DEFAULT_INDEX_FILENAME)
    category_rules = load_category_rules(config)
    registry_path = config_path.parent / config.get("registry", "packages.yml")
    registry = load_registry(registry_path)
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
    for organization in config["organizations"]:
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
    report = {"projects": projects, "errors": errors}
    (output / "catalog.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"Generated {len(projects)} projects with {len(errors)} errors in {output}")
    return len(projects), len(errors)


CSS = """
:root { --ink: #333333; --muted: #777777; --line: #dddddd; --paper: #f7f7f7; --card: #ffffff; --blue: #3297d4; --blue-soft: #dcedf7; --orange: #f15a24; }
[data-theme="dark"] { --ink: #f4f4f4; --muted: #b8b8b8; --line: #454b50; --paper: #20252a; --card: #2b3035; --blue: #62b1e3; --blue-soft: #294b61; --orange: #ff8a5c; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.catalog-shell { max-width: 1120px; margin: auto; padding: 2.5rem 1.25rem 4rem; }
.catalog-header { display: flex; align-items: center; gap: 1rem; border-bottom: 5px solid #f5f5f5; padding-bottom: 1.5rem; }
.header-copy { flex: 1; }
.header-actions { display: flex; gap: .5rem; align-self: flex-start; }
.icon-button, .sidebar-close { border: 1px solid var(--line); border-radius: 2px; background: var(--card); color: var(--ink); padding: .45rem .65rem; cursor: pointer; font: .9rem "Open Sans", sans-serif; }
.theme-toggle { display: flex; align-items: center; gap: .4rem; color: var(--muted); cursor: pointer; font: .82rem "Open Sans", sans-serif; }
.icon-button { width: 2.4rem; height: 2.4rem; font-size: 1.2rem; }
.icon-button:hover, .sidebar-close:hover { border-color: var(--blue); color: var(--blue); }
.pharo-logo { width: 72px; height: 72px; flex: 0 0 72px; object-fit: contain; }
.eyebrow { margin: 0 0 .2rem; color: var(--blue); font: bold .75rem/1.2 "Open Sans", sans-serif; letter-spacing: .14em; }
h1 { margin: 0; color: #111111; font-size: clamp(2.2rem, 6vw, 4rem); line-height: 1.05; font-weight: 600; }
.lede { margin: .7rem 0 0; color: var(--muted); max-width: 42rem; }
.catalog-controls { margin: 2rem 0 1.5rem; padding: 1.25rem 0; border-bottom: 1px solid var(--line); }
.search-label { display: block; color: var(--muted); font: bold .75rem "Open Sans", sans-serif; letter-spacing: .1em; text-transform: uppercase; }
input { width: 100%; margin-top: .5rem; padding: .85rem 1rem; border: 2px solid #dddddd; border-radius: 2px; color: var(--ink); background: var(--card); font: 1rem "Open Sans", sans-serif; }
input:focus { outline: 3px solid rgba(50,151,212,.2); border-color: var(--blue); }
.catalog-layout { display: grid; grid-template-columns: 230px 1fr; gap: 2rem; }
.catalog-layout.filters-hidden { display: block; }
.catalog-layout.filters-hidden .filters { display: none; }
.filters { padding-top: 2rem; }
.filters h2 { margin: 1.3rem 0 .45rem; color: var(--ink); font-size: .95rem; }
.sidebar-close { display: none; margin-bottom: 1rem; }
.toggle-row { display: flex; gap: .45rem; align-items: flex-start; color: var(--muted); font: .8rem "Open Sans", sans-serif; }
.filter-list { display: grid; gap: .2rem; }
.filter-option { display: flex; justify-content: space-between; gap: .5rem; width: 100%; padding: .3rem .4rem; border: 0; border-radius: 2px; background: transparent; color: var(--muted); text-align: left; cursor: pointer; font: .82rem "Open Sans", sans-serif; }
.filter-option:hover, .filter-option.active { background: var(--blue-soft); color: var(--blue); }
.filter-count { color: var(--muted); }
.pagination { display: flex; flex-wrap: wrap; justify-content: center; gap: .35rem; margin-top: 1.5rem; }
.page-button { min-width: 2.2rem; padding: .4rem .6rem; border: 1px solid var(--line); border-radius: 2px; background: var(--card); color: var(--blue); cursor: pointer; }
.page-button.active { background: var(--blue); color: #fff; }
.summary { margin: .7rem 0 0; color: var(--muted); font: .9rem "Open Sans", sans-serif; }
.project-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 1rem; }
.project-card { display: flex; flex-direction: column; min-height: 220px; padding: 1.2rem; background: var(--card); border: 1px solid var(--line); border-top: 3px solid var(--blue); border-radius: 2px; box-shadow: 0 2px 8px rgba(0,0,0,.04); }
.project-card h2 { margin: 0; font-size: 1.35rem; line-height: 1.15; }
.project-card p { color: var(--muted); margin: .7rem 0; }
.metadata { font-size: .86rem; color: var(--blue) !important; }
.tags { display: flex; flex-wrap: wrap; gap: .35rem; }
.tag { padding: .15rem .4rem; background: var(--paper); border: 1px solid var(--line); color: var(--muted); font: .75rem "Open Sans", sans-serif; }
.project-card .links { display: flex; flex-wrap: wrap; gap: .7rem; margin-top: auto; padding-top: .8rem; font: .9rem "Open Sans", sans-serif; }
a { color: var(--blue); }
.badge { display: inline-block; margin: .7rem 0 0; padding: .2rem .5rem; border-radius: 2px; background: #fff0eb; color: #c43f16; font: .72rem "Open Sans", sans-serif; }
.badge.standard { background: var(--blue-soft); color: #24719e; }
.catalog-footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font: .82rem "Open Sans", sans-serif; }
.empty-state { padding: 2rem; text-align: center; color: var(--muted); }
.error-report { margin-top: 1.5rem; padding: 1rem; border: 1px solid #f0c5a9; border-radius: 2px; background: #fff8f3; color: #7e3e1f; font: .9rem "Open Sans", sans-serif; }
.error-report summary { cursor: pointer; font-weight: bold; }
.error-report li { margin-top: .4rem; overflow-wrap: anywhere; }
@media (max-width: 760px) { .catalog-shell { padding-top: 1.5rem; } .catalog-header { align-items: flex-start; } .pharo-logo { width: 56px; height: 56px; flex-basis: 56px; } .catalog-layout { display: block; } .filters { position: fixed; z-index: 5; inset: 0 auto 0 0; width: min(290px, 84vw); padding: 1.5rem; overflow-y: auto; background: var(--card); box-shadow: 4px 0 18px rgba(0,0,0,.15); transform: translateX(-105%); transition: transform .2s ease; } .filters.open { transform: translateX(0); } .sidebar-close { display: block; } .burger { display: block; } }
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
const tagList = document.getElementById('tag-list');
const hideNoRelease = document.getElementById('hide-no-release');
const filters = document.getElementById('filters');
const layout = document.getElementById('catalog-layout');
const pageSize = 12;
let currentPage = 1;
let selectedCategory = localStorage.getItem('catalog-category') || '';
let selectedTag = localStorage.getItem('catalog-tag') || '';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>\"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
hideNoRelease.checked = localStorage.getItem('catalog-hide-no-release') === 'true';
document.documentElement.dataset.theme = localStorage.getItem('catalog-theme') || 'light';
document.getElementById('theme-toggle').checked = document.documentElement.dataset.theme === 'dark';
function baseProjects() {
  const query = search.value.trim().toLowerCase();
    return data.projects.filter((project) => {
        const searchable = `${project.name} ${project.description} ${(project.categories || []).join(' ')} ${(project.tags || []).join(' ')}`.toLowerCase();
        return searchable.includes(query) && (!hideNoRelease.checked || project.status !== 'no-release');
    });
}
function renderFilterList(container, values, selected, setter) {
    container.innerHTML = values.map(([value, count]) => `<button class="filter-option ${selected === value ? 'active' : ''}" data-value="${escapeHtml(value)}"><span>${escapeHtml(value || 'All')}</span><span class="filter-count">${count}</span></button>`).join('');
    container.querySelectorAll('.filter-option').forEach((button) => button.addEventListener('click', () => {
        setter(button.dataset.value);
        currentPage = 1;
        render();
    }));
}
function render() {
    const candidates = baseProjects();
    const projects = candidates.filter((project) => (!selectedCategory || (project.categories || []).includes(selectedCategory)) && (!selectedTag || (project.tags || []).includes(selectedTag)));
    const categoryCounts = [['', candidates.length], ...categoryNames.map((name) => [name, candidates.filter((project) => (project.categories || []).includes(name)).length])];
    const tagValues = [...new Set(data.projects.flatMap((project) => project.tags || []))].sort((a, b) => a.localeCompare(b));
    const tagCounts = [['', candidates.length], ...tagValues.map((name) => [name, candidates.filter((project) => (project.tags || []).includes(name)).length])];
    renderFilterList(categoryList, categoryCounts, selectedCategory, (value) => { selectedCategory = value; localStorage.setItem('catalog-category', value); });
    renderFilterList(tagList, tagCounts, selectedTag, (value) => { selectedTag = value; localStorage.setItem('catalog-tag', value); });
    const pages = Math.max(1, Math.ceil(projects.length / pageSize));
    currentPage = Math.min(currentPage, pages);
    const visible = projects.slice((currentPage - 1) * pageSize, currentPage * pageSize);
    grid.innerHTML = visible.map((project) => {
    const standard = project.status === 'standard';
    const status = standard ? 'Standard release' : project.status === 'no-release' ? 'No release' : 'Non-standard release';
    const categories = (project.categories || []).map(escapeHtml).join(' · ');
    const tags = (project.tags || []).map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join(' ');
    return `<article class="project-card"><h2>${escapeHtml(project.name)}</h2><p>${escapeHtml(project.description)}</p><p class="metadata">${categories}</p><div class="tags">${tags}</div><span class="badge ${standard ? 'standard' : ''}" title="${escapeHtml(status)}">${standard ? 'Index available' : '⚠ ' + escapeHtml(status)}</span><div class="links"><a href="${escapeHtml(project.project_url)}">Project</a><a href="${escapeHtml(project.repository_url)}">Repository</a></div></article>`;
  }).join('');
    pagination.innerHTML = Array.from({length: pages}, (_, index) => `<button class="page-button ${currentPage === index + 1 ? 'active' : ''}" data-page="${index + 1}">${index + 1}</button>`).join('');
    pagination.querySelectorAll('.page-button').forEach((button) => button.addEventListener('click', () => { currentPage = Number(button.dataset.page); render(); window.scrollTo({top: 0, behavior: 'smooth'}); }));
    summary.textContent = `${projects.length} of ${data.projects.length} projects${data.errors.length ? ` · ${data.errors.length} discovery errors` : ''}`;
  empty.hidden = projects.length !== 0;
}
search.addEventListener('input', render);
hideNoRelease.addEventListener('change', () => { localStorage.setItem('catalog-hide-no-release', hideNoRelease.checked); currentPage = 1; render(); });
document.getElementById('theme-toggle').addEventListener('change', (event) => { const theme = event.target.checked ? 'dark' : 'light'; document.documentElement.dataset.theme = theme; localStorage.setItem('catalog-theme', theme); });
document.getElementById('sidebar-toggle').addEventListener('click', () => {
    if (window.matchMedia('(max-width: 760px)').matches) {
        filters.classList.toggle('open');
    } else {
        layout.classList.toggle('filters-hidden');
    }
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
