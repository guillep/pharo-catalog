# Pharo Catalog Technical Documentation

## Purpose

The catalog is a generated, static website for discovering Pharo packages. It treats one GitHub repository as one package and publishes no runtime backend. The generated site can be served directly by GitHub Pages or another static host.

The catalog deliberately answers a small question about each repository:

> Does its latest GitHub release provide the configured standard `index.html` asset?

The catalog does not interpret that index, parse package versions, or maintain a package version database.

## Implementation Strategy

The generator is [`generate.py`](../generate.py). A generation run follows this pipeline:

```text
registry.yml + config.yml
        |
        v
repository sources
        |
        v
normalized repository identities
        |
        v
GitHub metadata and topics
        |
        v
latest release and index.html asset
        |
        v
static site + imported project indexes + JSON/CSV exports
```

The generator:

1. Loads category rules and the central registry.
2. Discovers repositories from configured organizations.
3. Adds explicitly registered repositories from `registry.yml`.
4. Keeps automatically discovered repositories only when they have the exact GitHub topic `pharo`.
5. Normalizes repository identities and deduplicates the sources.
6. Excludes forks from organization discovery, while retaining explicitly registered forks with the `pharo` topic.
7. Retrieves repository topics and combines them with explicit registry tags.
8. Derives controlled categories using exact, case-insensitive keyword matches.
9. Retrieves the repository's latest GitHub release.
10. Looks for the configured release asset, normally `index.html`.
11. Imports that asset when present and creates a catalog wrapper around it.
12. Writes the catalog HTML, project files, `catalog.json`, `catalog.csv`, and documentation.

## Configuration And Registry

`config.yml` contains generator behavior:

- GitHub API endpoint and token environment variable
- output directory
- standard release index filename
- controlled category names and topic keywords

`registry.yml` contains catalog inputs:

- managed organizations and their repository exclusions
- standalone package entries
- package display names, descriptions, tags, and repository references

Keeping these files separate from Python code means catalog maintainers can change organizations, exclusions, package metadata, and category mappings through normal pull requests without changing the generator.

Repository references may use the concise `owner/repository` form. Full GitHub URLs remain accepted for compatibility. They are normalized to a canonical `https://github.com/owner/repository` identity before comparison.

## GitHub Boundary

GitHub is an input provider, not the catalog's application model. GitHub-specific response shapes are consumed in `GitHubClient` and converted into the generator's small internal project record.

The generator uses GitHub for:

- organization repository discovery with pagination
- fork metadata
- repository topics
- latest release lookup
- release asset download

The generated website contains no GitHub token. Absolute repository links and imported project files are safe static outputs; authentication exists only during generation.

If the GitHub API is unavailable, the generator records an error and continues processing other repositories. An API failure is kept distinct from a repository with no release or a release without `index.html`.

The `--mock` mode replaces GitHub with deterministic local fixtures. It exists for fast offline development, UI work, and local previews without API requests:

```sh
python3 generate.py --config config.yml --mock
```

## Package Decoupling

The catalog does not depend on a package's internal implementation or release format. A package only needs to provide:

- a GitHub repository
- the exact GitHub repository topic `pharo` when discovered through an organization
- optionally, a registry entry when it is outside managed organizations
- a GitHub release containing an asset named `index.html` to be standard

The imported `index.html` is treated as opaque content. The catalog does not parse its JSON, HTML, version, or package metadata. This lets package owners evolve their own index format independently and lets the catalog support packages written or released by different tools.

The catalog wrapper adds navigation and repository context without modifying the downloaded index. The original file is stored beside the wrapper under a predictable project directory.

## Generated Site

The generator writes the following outputs under `site/`:

- `index.html`: searchable catalog UI
- `documentation.html`: static contributor documentation
- `catalog.json`: machine-readable project and error data
- `catalog.csv`: tabular project export
- `projects/<slug>/index.html`: catalog wrapper for a standard project
- `projects/<slug>/.index-source.html`: downloaded opaque package index
- `assets/`: shared Beacon logo and favicon

The browser performs search, category selection, hide/show filtering, pagination, theme persistence, and filter-menu persistence entirely in JavaScript. No server-side request is needed after publication.

The category selector includes visible configured categories, `Uncategorized` for projects with no category match, and `See hidden` for projects excluded by the current hide settings. Tags remain project metadata and search content, but are intentionally not a navigation menu.

## Failure And Status Model

A project can have these relevant states:

- `standard`: latest release contains the configured index asset and it was imported
- `no-index`: a latest release exists, but it has no standard index asset
- `no-release`: GitHub has no latest release
- `error`: metadata, topic, release, or asset processing failed

Projects without a release or with an error can be hidden by the default UI settings. The dynamic hidden view makes those records inspectable without mixing them into the normal visible catalog.

## Extension Points

The design intentionally leaves several policies configurable:

- Add organizations or exclusions in `registry.yml`.
- Register standalone repositories in `registry.yml`.
- Add or revise exact topic-to-category keywords in `config.yml`.
- Change the release index asset filename in `config.yml`.
- Replace GitHub access with another provider by implementing the small client boundary and preserving the internal project record.
- Add alternate exporters beside the existing JSON and CSV writers.

Any new provider should preserve the same separation: discover metadata externally, normalize it into catalog records, and keep the generated site independent of authentication and network access.
