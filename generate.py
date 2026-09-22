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

import yaml


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_INDEX_FILENAME = "index.html"
LOGO_PATH = Path(__file__).with_name("assets") / "pharo-beacon.svg"


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
    if not isinstance(organizations, list) or not organizations:
        raise ValueError("config must define a non-empty organizations list")
    return config


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


def archive_links(repository_url: str, tag: str) -> list[dict]:
    base = f"{repository_url}/archive/refs/tags/{urllib.parse.quote(tag, safe='') }"
    return [
        {"name": "Source code (zip)", "url": f"{base}.zip"},
        {"name": "Source code (tar.gz)", "url": f"{base}.tar.gz"},
    ]


def process_repository(
    client: GitHubClient,
    repository: dict,
    output: Path,
    index_filename: str,
) -> dict:
    full_name = repository.get("full_name", repository.get("name", "unknown"))
    project = {
        "name": repository.get("name", full_name),
        "description": repository.get("description") or "No description provided.",
        "repository_url": repository.get("html_url", ""),
        "project_url": repository.get("html_url", ""),
        "organization": full_name.split("/", 1)[0] if "/" in full_name else "",
        "version": None,
        "standard": False,
        "status": "no-release",
        "artifacts": [],
    }
    release = latest_release(client, full_name)
    if release is None:
        project["status"] = "no-release"
        return project

    tag = release.get("tag_name") or release.get("name") or ""
    project["version"] = tag or None
    project["artifacts"] = archive_links(repository.get("html_url", ""), tag) if tag else []
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    client.download(index_asset["browser_download_url"], destination)
    project["standard"] = True
    project["status"] = "standard"
    project["project_url"] = f"projects/{slug}/{index_filename}"
    project["artifacts"].append(
        {"name": index_filename, "url": index_asset.get("browser_download_url", "")}
    )
    return project


def render_site(projects: list[dict], errors: list[dict]) -> str:
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
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="A searchable catalog of Pharo projects.">
  <title>Pharo Project Catalog</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="catalog-shell">
    <header class="catalog-header">
    <img class="pharo-logo" src="assets/pharo-beacon.svg" alt="Pharo">
      <div>
        <p class="eyebrow">PHARO ECOSYSTEM</p>
        <h1>Project catalog</h1>
        <p class="lede">Discover Pharo packages, releases, and their project indexes.</p>
      </div>
    </header>
    <section class="catalog-controls" aria-label="Catalog controls">
      <label class="search-label" for="search">Search projects</label>
      <input id="search" type="search" placeholder="Search by name or description" autocomplete="off">
      <p id="summary" class="summary"></p>
    </section>
    <section id="projects" class="project-grid" aria-live="polite"></section>
    <p id="empty" class="empty-state" hidden>No projects match your search.</p>
    {error_report}
    <footer class="catalog-footer">Generated from GitHub metadata. Standard releases include a published project index.</footer>
  </main>
    <script id="catalog-data" type="application/json">{payload}</script>
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
    output = Path(index_config.get("output_directory", "site"))
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    assets = output / "assets"
    assets.mkdir()
    shutil.copyfile(LOGO_PATH, assets / "pharo-beacon.svg")

    projects: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for organization in config["organizations"]:
        try:
            repositories = paged_repositories(client, organization)
        except GitHubError as error:
            errors.append({"organization": organization, "error": str(error)})
            continue
        for repository in repositories:
            full_name = repository.get("full_name", "")
            if full_name in seen:
                continue
            seen.add(full_name)
            try:
                projects.append(process_repository(client, repository, output, index_filename))
            except GitHubError as error:
                errors.append({"repository": full_name, "error": str(error)})
                projects.append({
                    "name": repository.get("name", full_name),
                    "description": repository.get("description") or "Metadata unavailable.",
                    "repository_url": repository.get("html_url", ""),
                    "project_url": repository.get("html_url", ""),
                    "version": None,
                    "standard": False,
                    "status": "error",
                    "artifacts": [],
                })

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
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.catalog-shell { max-width: 1120px; margin: auto; padding: 2.5rem 1.25rem 4rem; }
.catalog-header { display: flex; align-items: center; gap: 1rem; border-bottom: 5px solid #f5f5f5; padding-bottom: 1.5rem; }
.pharo-logo { width: 72px; height: 72px; flex: 0 0 72px; object-fit: contain; }
.eyebrow { margin: 0 0 .2rem; color: var(--blue); font: bold .75rem/1.2 "Open Sans", sans-serif; letter-spacing: .14em; }
h1 { margin: 0; color: #111111; font-size: clamp(2.2rem, 6vw, 4rem); line-height: 1.05; font-weight: 600; }
.lede { margin: .7rem 0 0; color: var(--muted); max-width: 42rem; }
.catalog-controls { margin: 2rem 0 1.5rem; padding: 1.25rem 0; border-bottom: 1px solid var(--line); }
.search-label { display: block; color: var(--muted); font: bold .75rem "Open Sans", sans-serif; letter-spacing: .1em; text-transform: uppercase; }
input { width: 100%; margin-top: .5rem; padding: .85rem 1rem; border: 2px solid #dddddd; border-radius: 2px; color: var(--ink); background: var(--card); font: 1rem "Open Sans", sans-serif; }
input:focus { outline: 3px solid rgba(50,151,212,.2); border-color: var(--blue); }
.summary { margin: .7rem 0 0; color: var(--muted); font: .9rem "Open Sans", sans-serif; }
.project-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 1rem; }
.project-card { display: flex; flex-direction: column; min-height: 220px; padding: 1.2rem; background: var(--card); border: 1px solid var(--line); border-top: 3px solid var(--blue); border-radius: 2px; box-shadow: 0 2px 8px rgba(0,0,0,.04); }
.project-card h2 { margin: 0; font-size: 1.35rem; line-height: 1.15; }
.project-card p { color: var(--muted); margin: .7rem 0; }
.project-card .links { display: flex; flex-wrap: wrap; gap: .7rem; margin-top: auto; padding-top: .8rem; font: .9rem "Open Sans", sans-serif; }
a { color: var(--blue); }
.badge { display: inline-block; margin: .7rem 0 0; padding: .2rem .5rem; border-radius: 2px; background: #fff0eb; color: #c43f16; font: .72rem "Open Sans", sans-serif; }
.badge.standard { background: var(--blue-soft); color: #24719e; }
.catalog-footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font: .82rem "Open Sans", sans-serif; }
.empty-state { padding: 2rem; text-align: center; color: var(--muted); }
.error-report { margin-top: 1.5rem; padding: 1rem; border: 1px solid #f0c5a9; border-radius: 2px; background: #fff8f3; color: #7e3e1f; font: .9rem "Open Sans", sans-serif; }
.error-report summary { cursor: pointer; font-weight: bold; }
.error-report li { margin-top: .4rem; overflow-wrap: anywhere; }
@media (max-width: 600px) { .catalog-shell { padding-top: 1.5rem; } .catalog-header { align-items: flex-start; } .pharo-logo { width: 56px; height: 56px; flex-basis: 56px; } }
"""


JS = """
const data = JSON.parse(document.getElementById('catalog-data').textContent);
const grid = document.getElementById('projects');
const search = document.getElementById('search');
const summary = document.getElementById('summary');
const empty = document.getElementById('empty');
const escapeHtml = (value) => String(value ?? '').replace(/[&<>\"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
function render() {
  const query = search.value.trim().toLowerCase();
  const projects = data.projects.filter((project) => `${project.name} ${project.description}`.toLowerCase().includes(query));
  grid.innerHTML = projects.map((project) => {
    const standard = project.status === 'standard';
    const status = standard ? 'Standard release' : project.status === 'no-release' ? 'No release' : 'Non-standard release';
    const version = project.version ? `<span>Latest ${escapeHtml(project.version)}</span>` : '<span>No release version</span>';
    return `<article class="project-card"><h2>${escapeHtml(project.name)}</h2><p>${escapeHtml(project.description)}</p><span class="badge ${standard ? 'standard' : ''}" title="${escapeHtml(status)}">${standard ? 'Index available' : '⚠ ' + escapeHtml(status)}</span><div class="links"><a href="${escapeHtml(project.project_url)}">Project</a><a href="${escapeHtml(project.repository_url)}">Repository</a>${version}</div></article>`;
  }).join('');
  summary.textContent = `${projects.length} of ${data.projects.length} projects${data.errors.length ? ` · ${data.errors.length} discovery errors` : ''}`;
  empty.hidden = projects.length !== 0;
}
search.addEventListener('input', render);
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
