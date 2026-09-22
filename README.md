# Pharo Project Catalog

A static, searchable catalog of Pharo packages from a central registry and configured GitHub organizations. One GitHub repository is treated as one package/project.

## Run locally

Create a GitHub token with read access to the organizations you want to scan and export it without putting it in the repository:

```sh
export GITHUB_TOKEN=...
python3 -m pip install -r requirements.txt
python3 generate.py --config config.yml
open site/index.html
```

The generator handles registry validation, URL normalization, pagination, fork exclusion for automatic discovery, GitHub topics, config-driven category derivation, latest releases, index assets, deduplication, and per-package failures. It writes the static site to `site/` and a machine-readable report to `site/catalog.json`. A package is standard when its configured index filename is present as a release asset; otherwise the project remains linked to its repository and is visibly marked.

The generated site imports standard project indexes at `projects/<repository>/index.html`. It provides a responsive filter sidebar with dynamic category counts, `Other` for unmatched projects, `Hidden` for projects excluded by the current hide settings, client-side pagination, a remembered hide-no-release setting, and a remembered light/dark theme. Tags remain visible on cards and searchable but are not sidebar navigation controls. The favicon is copied from the Pharo Lighthouse asset, without uploading or embedding the authentication token.

## Configuration

Add packages to `packages.yml` to register them. The registry requires `name`, `description`, and `repository`; `tags` are optional explicit tags and categories are never stored there. Edit the `categories` section in `config.yml` to add or change exact topic keywords without changing application code. GitHub repository topics and explicit registry tags are merged, de-duplicated, and matched case-insensitively against those keywords. Edit `config.yml` to change organizations, the registry path, the GitHub API endpoint, the token environment variable, the index filename, or the output directory. The token is read only at generation time from `github.token_env`; it is never copied into generated files.

## GitHub Pages

The manual `Build and publish catalog` workflow in `.github/workflows/publish.yml` can be run from the Actions tab. It also refreshes nightly. Enable GitHub Pages for the repository using **GitHub Actions** as the source.

The workflow prefers the optional `CATALOG_GITHUB_TOKEN` secret and falls back to the workflow token. A dedicated token is useful when organization repositories require access beyond the repository's default token.
