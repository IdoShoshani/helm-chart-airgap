# Helm Airgap Packager: Project Requirements and Code Review

## Project Purpose

Helm Airgap Packager is a Python web application that packages Helm charts and their container images for installation in air-gapped environments. Users select a chart source (Helm repository or OCI registry), optionally provide values, and the application fetches the chart, resolves dependencies, discovers container images from rendered manifests, downloads images using crane, and produces a single tarball bundle ready for offline deployment.

---

## Part A: What the Project Needs to Do (Per Piece)

### 1. Settings / Configuration

**Location:** `backend/app/settings.py`

**Purpose:** Centralize all configuration from environment variables and define paths used by the application.

**Required behavior:**
- Define and create base paths: `DATA_DIR`, `JOBS_DIR`, `BUNDLES_DIR`, `CREDENTIALS_DIR`, `DOCKER_CONFIG_DIR` (for crane/Docker auth).
- Ensure `JOBS_DIR`, `BUNDLES_DIR`, `CREDENTIALS_DIR` exist on import.
- Flask: `FLASK_HOST`, `FLASK_PORT`, `FLASK_DEBUG` from env with sensible defaults.
- Helm: `HELM_TIMEOUT`, `HELM_RETRY_LIMIT` (seconds).
- Image pull: `IMAGE_PULL_TIMEOUT` (per image), `MAX_IMAGES`, `MAX_BUNDLE_SIZE_GB`, `IMAGE_PULL_PARALLEL`.
- Registry env vars: `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `REGISTRY_SERVER` (optional).
- Tool paths: `HELM_BIN`, `CRANE_BIN` (override for custom installs).
- Repo update: `REPO_UPDATE_INTERVAL_HOURS` (0 = no automatic refresh).

---

### 2. Flask App (main)

**Location:** `backend/app/main.py`

**Purpose:** Web server, routing, request validation, background job orchestration, and integration of packager, credential manager, and repo manager.

**Required behavior:**
- **Routes:**
  - `GET /`, `GET /jobs`: Index page with jobs list and package form.
  - `POST /package`: Validate form, create job dir, run packaging in background thread, redirect to progress page.
  - `GET /result/<job_id>`: Show result page (log, download link, retry, delete).
  - `GET /download/<job_id>`: Serve bundle file as attachment.
  - `GET /job/<job_id>/progress`: Progress page (polls status).
  - `GET /job/<job_id>/status`: JSON status for polling.
  - `GET /job/<job_id>/log`: Log content since offset (streaming).
  - `POST /job/<job_id>/cancel`: Create cancel flag; redirect to result.
  - `POST /job/<job_id>/retry`: Create new job with same params; redirect to progress.
  - `POST /job/<job_id>/delete`: Delete job dir and optionally bundle (only if not running).
  - Credentials: list, add, edit, delete, test, env-status.
  - Repos: list, add, update, delete; API for charts search, versions, updated timestamp.
- **Index context:** `repos`, `jobs` (sorted by mtime), `has_running_jobs`.
- **Validation:** source_type (repo/oci), repo_url (http/https), chart_name (valid chars), oci_chart (oci://), values YAML parseable, bundle_name valid chars.
- **Background:** Repo update loop when `REPO_UPDATE_INTERVAL_HOURS > 0`.
- **Startup:** Sync env credentials, sync Helm repos from storage.
- **Security:** Use `SECRET_KEY` for session; in production, a strong random key must be set.

---

### 3. Packager (Core Logic)

**Location:** `backend/app/packager.py`

**Purpose:** Orchestrate chart fetch, extraction, template rendering, image discovery, image download, and bundle creation.

**Required behavior:**
- **Initialization:** Verify Helm and Crane binaries exist; set up registry auth via credential manager.
- **Job lifecycle:** Create job dir (UUID), write progress.json, support cancel.flag for user cancellation.
- **Fetch chart:**
  - Repo: add temp repo, update, pull chart, remove repo; support chart_version.
  - OCI: use credential for registry if available; helm pull with version.
- **Extract:** Extract .tgz, add dependency repos from Chart.yaml (skip OCI deps), run `helm dependency build`.
- **Render:** `helm template` with optional values file; output to rendered/.
- **Discover images:** Parse YAML (recursive dict for `image`, `repository`+`tag`); regex fallback; also scan chart values files; normalize (add :latest if no tag); save images.json.
- **Download images:** Resolve digests with crane; deduplicate by digest; download in parallel (ThreadPoolExecutor); fail on first error; write per-image status for UI; enforce MAX_IMAGES.
- **Build bundle:** Create bundle_temp with chart/, images/, README.md, metadata.json; tarball to BUNDLES_DIR; enforce MAX_BUNDLE_SIZE_GB before/during creation.
- **Metadata:** Save job.json with status, params (for retry), bundle_filename, error_message on failure.
- **Error handling:** PackagingError with helpful messages; JobCancelledError on cancel; preserve params for retry.

---

### 4. Credential Manager

**Location:** `backend/app/credential_manager.py`

**Purpose:** Store and manage registry credentials; sync to Docker config for crane; support env-sourced credentials.

**Required behavior:**
- **Storage:** JSON file (registries.json) with id, server, username, password_encrypted, source (web/env), timestamps, login_status.
- **Encoding:** Base64 for password (obfuscation only; not encryption).
- **Sync:** Write Docker config.json from registries so crane uses same auth; handle docker.io alias.
- **CRUD:** add, update, delete; block update/delete for source=env.
- **Queries:** get_credential_for_server (with decoded password), get_all_credentials (masked or with password).
- **Login:** Helm registry login on add/update; sync_env_credentials on startup.
- **Test:** test_login uses crane catalog; fallback to Helm if registry doesn't support catalog; update login_status.
- **Usage tracking:** mark_credential_used(server).
- **Permissions:** Set credentials file to 600.

---

### 5. Repo Manager

**Location:** `backend/app/repo_manager.py`

**Purpose:** Manage Helm repositories (add, remove, update) and provide chart search/versions.

**Required behavior:**
- **Storage:** repos.json with list of {name, url} and updated_at.
- **Operations:** add_repo (validate name/URL, helm repo add, persist), remove_repo, update_repos (helm repo update for all).
- **Startup:** sync_helm_repos_from_storage re-adds all stored repos (idempotent).
- **Search:** search_charts(query) returns {repo, chart, version, app_version, repo_url}.
- **Versions:** get_chart_versions(repo, chart).
- **Lookup:** get_repo_url(repo_name).

---

### 6. Web UI (Templates)

**Location:** `backend/templates/`

**Purpose:** User interface for packaging, job management, credentials, and repos.

**Required behavior:**
- **base.html:** Header with nav (Home, Repositories, Registry Credentials); flash messages with correct Bootstrap classes (success, danger, info, warning); footer.
- **index.html:** Jobs table (ID, chart, status, created, actions: View, Cancel, Retry, Download, Delete); refresh controls; package form: source (repo/OCI), chart/version, values (textarea + file upload), bundle name, include_images; validation feedback.
- **progress.html:** Progress bar, step message, per-image status; poll /job/<id>/status; link to result when done.
- **result.html:** Log display, download button, retry, cancel (if running), delete.
- **credentials/list.html:** List credentials with server, username, source badge, login status, test, edit, delete.
- **credentials/form.html:** Add/edit form (server, username, password).
- **repos/list.html:** List repos, add form, update, delete.

---

### 7. Docker / Deployment

**Location:** `docker/`, `docker-compose.yml`, `.env.example`

**Purpose:** Containerized deployment with Helm and Crane pre-installed.

**Required behavior:**
- **Dockerfile:** Python 3.11-slim; install Helm (get-helm-3 script), Crane (go-containerregistry); copy app; create data dirs; non-root user (appuser); entrypoint for optional reload.
- **docker-compose.yml:** Build from Dockerfile; expose port; volume for data; env passthrough for all config vars.
- **.env.example:** Document all config vars with defaults and comments.

---

## Part B: Code Review (Remove / Change / Add)

### Remove

| Item | File | Rationale |
|------|------|-----------|
| Bare `except` in repo cleanup | `backend/app/packager.py` | In `_fetch_chart_from_repo` finally block, `except:` catches `KeyboardInterrupt` and `SystemExit`. Replace with `except Exception:` and optionally log. |

---

### Change

| Item | File | Rationale |
|------|------|-----------|
| Flash category for success | `backend/templates/base.html` | Currently maps only `error` → `danger`, else `info`. Success messages should use `alert-success`. Map `success` → `success`, `error` → `danger`, `warning` → `warning`, else `info`. |
| job_cancel uses HelmPackager without credential_manager | `backend/app/main.py` | `job_cancel` instantiates `HelmPackager()` without `credential_manager`. For consistency and future-proofing, pass `credential_manager` (or use a shared packager instance). |
| Credential storage documentation | `backend/app/credential_manager.py` | Add docstring/comment that password encoding is obfuscation only, not encryption. Recommend production: use env vars or external secret store. |

---

### Add

| Item | File | Rationale |
|------|------|-----------|
| Enforce MAX_BUNDLE_SIZE_GB | `backend/app/packager.py` | `MAX_BUNDLE_SIZE_GB` is in settings and README but never enforced. Check total size before/during `_build_bundle` and raise `PackagingError` if exceeded. |
| CSRF protection | `backend/app/main.py`, templates | Forms (package, cancel, retry, delete, credentials, repos) use POST without CSRF. Add Flask-WTF or similar for state-changing routes. |
| SECRET_KEY documentation | README, `.env.example` | Reinforce that a strong random `SECRET_KEY` must be set in production (already in .env.example; add to README security section). |

---

## Implementation Checklist

- [x] **Remove:** Replace bare `except` with `except Exception:` in packager.py
- [x] **Change:** Update base.html flash rendering for success/warning categories
- [x] **Change:** Pass credential_manager to HelmPackager in job_cancel
- [x] **Change:** Add credential storage security note in credential_manager.py
- [x] **Add:** Enforce MAX_BUNDLE_SIZE_GB in packager._build_bundle
- [x] **Add:** CSRF protection (Flask-WTF) for POST routes
- [x] **Add:** SECRET_KEY production note in README
