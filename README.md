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
   - Choose between Helm Repository or OCI Registry
   - Enter the repository URL and chart name (for repos) or OCI chart URL (for OCI)

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

- `FLASK_PORT`: Port for the web application (default: 8000)
- `FLASK_DEBUG`: Enable debug mode (default: false)
- `HELM_TIMEOUT`: Timeout for Helm operations in seconds (default: 300)
- `IMAGE_PULL_TIMEOUT`: Timeout for image pulls in seconds (default: 600)
- `MAX_IMAGES`: Maximum number of images allowed per bundle (default: 100)
- `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `REGISTRY_SERVER`: Optional registry credentials

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
- **Image pull fails**: Verify registry credentials if required, or check network connectivity
- **Bundle too large**: Adjust `MAX_BUNDLE_SIZE_GB` or `MAX_IMAGES` in `.env`
- **Timeout errors**: Increase `HELM_TIMEOUT` or `IMAGE_PULL_TIMEOUT` in `.env`

## License

This project is provided as-is for packaging Helm charts for air-gapped environments.
