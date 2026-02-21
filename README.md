# Helm Airgap Packager

A simple Python web application that helps users package Helm charts and their container images for installation in air-gapped environments.

## Features

- **Web-based UI**: Simple browser interface for packaging Helm charts
- **Automatic Dependency Resolution**: Automatically discovers and downloads all container images referenced in Helm charts
- **Multiple Chart Sources**: Support for both Helm repositories and OCI registries
- **Offline Bundles**: Creates a single tarball containing charts and images ready for air-gap deployment
- **Docker Compose Ready**: Easy deployment with Docker Compose

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- Access to Helm repositories or OCI registries containing the charts you want to package

### Installation

1. Clone or download this repository

2. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

3. (Optional) Edit `.env` to configure settings like port, timeouts, or registry credentials

4. Start the application:
   ```bash
   docker-compose up -d
   ```

5. Open your browser and navigate to:
   ```
   http://localhost:8000
   ```

## Usage

1. **Select Chart Source**:
   - **Helm Repository**: Enter the repository URL and chart name
   - **OCI Registry**: Enter the full OCI chart URL (e.g., `oci://ghcr.io/nicklasfrahm/charts/argocd`)
     - For private registries, configure authentication (see Configuration section)

2. **Configure Chart**:
   - Optionally specify a chart version (defaults to latest)
   - Optionally provide values.yaml content or upload a values file

3. **Package**:
   - Click "Package Chart" to start the packaging process
   - The application will:
     - Download the Helm chart and dependencies
     - Render the chart templates
     - Discover all container images
     - Download all images
     - Create a bundle tarball

4. **Download**:
   - Once complete, download the bundle from the results page
   - Transfer the bundle to your air-gapped environment

## Bundle Contents

Each bundle contains:

- `chart/` - The Helm chart package (`.tgz` file)
- `images/` - All container images as tar archives
- `README.md` - Instructions for deploying in the air-gapped environment
- `metadata.json` - Bundle metadata including chart info and image list

## Configuration

Configuration is done via environment variables in `.env`:

- `SECRET_KEY`: **Required in production.** Set a strong random value (e.g. `openssl rand -hex 32`). The default is for development only and must not be used in production.
- `FLASK_PORT`: Port for the web application (default: 8000)
- `FLASK_DEBUG`: Enable debug mode (default: false)
- `HELM_TIMEOUT`: Timeout for Helm operations in seconds (default: 300)
- `IMAGE_PULL_TIMEOUT`: Timeout for image pulls in seconds (default: 600)
- `MAX_IMAGES`: Maximum number of images allowed per bundle (default: 100)
- `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `REGISTRY_SERVER`: Optional registry credentials for private OCI registries

### OCI Registry Authentication

The application includes a comprehensive credential management system accessible via the web UI.

#### Option 1: Web UI (Easiest)
1. Navigate to **"Registry Credentials"** in the web interface
2. Click **"Add Credential"**
3. Enter registry server (e.g., `ghcr.io`), username, and password/token
4. Optionally click **"Test Login"** to verify credentials
5. Credentials are automatically used when packaging charts

#### Option 2: Docker Compose Environment Variables (Recommended for Production)
```yaml
environment:
  - REGISTRY_USERNAME=your-username
  - REGISTRY_PASSWORD=your-token-or-password
  - REGISTRY_SERVER=ghcr.io
```
- Credentials are automatically synced on container startup
- Appear in web UI with "Environment Variable" badge
- Cannot be edited/deleted via UI (update docker-compose.yml instead)

#### Option 3: Helm Registry Login + Mount Config
```bash
# Login on your host machine
helm registry login ghcr.io
# Enter username and Personal Access Token when prompted

# Then mount Helm config in docker-compose.yml:
volumes:
  - ~/.config/helm:/home/appuser/.config/helm:ro
```

#### Credential Management Features
- **View All Credentials**: See all configured registries with login status
- **Test Credentials**: Verify credentials work before packaging
- **Source Tracking**: Distinguishes between env vars and web UI credentials
- **Status Indicators**: Shows which credentials are logged in successfully
- **Automatic Usage**: Credentials automatically used during packaging

**Note**: For GitHub Container Registry, use a Personal Access Token (PAT) with `read:packages` permission as the password.

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          # Flask application
│   │   ├── packager.py      # Core packaging logic
│   │   └── settings.py      # Configuration
│   └── templates/
│       ├── base.html        # Base template
│       ├── index.html       # Main form
│       └── result.html      # Results page
├── docker/
│   └── Dockerfile.web       # Docker image definition
├── data/                    # Generated data (jobs, bundles)
├── docker-compose.yml       # Docker Compose configuration
├── requirements.txt        # Python dependencies
└── README.md               # This file
```

## Development

### Running Locally (without Docker)

1. Install Python 3.11+ and Helm CLI

2. Install Crane:
   ```bash
   # Download from https://github.com/google/go-containerregistry/releases
   # Or use: go install github.com/google/go-containerregistry/cmd/crane@latest
   ```

3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the application:
   ```bash
   export FLASK_APP=backend.app.main
   flask run --host=0.0.0.0 --port=8000
   ```

## Troubleshooting

- **Chart download fails**: Check that the repository URL is correct and accessible
- **Image pull fails**: 
  - Verify registry credentials if required
  - For private registries (GHCR, Quay.io, ECR), you may need to authenticate:
    ```bash
    # For GitHub Container Registry
    echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
    
    # For Quay.io
    docker login quay.io
    
    # For AWS ECR Public
    aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
    ```
  - Then restart the container so it has access to Docker credentials
  - Check network connectivity
- **All images fail to download**: 
  - Ensure Docker is running and accessible from the container
  - Verify you have network access to the registries
  - Check if images exist and are publicly accessible (some may require authentication)
- **Bundle too large**: Adjust `MAX_BUNDLE_SIZE_GB` or `MAX_IMAGES` in `.env`
- **Timeout errors**: Increase `HELM_TIMEOUT` or `IMAGE_PULL_TIMEOUT` in `.env`

### Timeouts and progress

- **Helm operations** (fetch chart, add repos, dependency build): `HELM_TIMEOUT` seconds per command (default **300** = 5 minutes).
- **Image download**: `IMAGE_PULL_TIMEOUT` seconds **per image** (default **600** = 10 minutes). With many images, total time can be long (e.g. 10 images × 10 min = 100 min in the worst case).
- After you click "Package Chart", a **progress page** shows a live progress bar for: fetching chart → extracting → rendering templates → discovering images → downloading images (with per-image progress) → building bundle.

## License

This project is provided as-is for packaging Helm charts for air-gapped environments.
