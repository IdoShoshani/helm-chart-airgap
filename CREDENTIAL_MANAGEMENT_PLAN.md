# Credential Management System - Design Plan

## Overview
Add a comprehensive credential management system that allows users to:
1. Enter registry credentials through the web UI
2. View saved credentials and their login status
3. Configure credentials via Docker Compose environment variables
4. Coordinate between both credential sources

## Architecture

### Data Storage
- **Location**: `data/credentials/registries.json`
- **Format**: JSON file with encrypted passwords (base64 encoding for basic obfuscation)
- **Structure**:
```json
{
  "registries": [
    {
      "id": "uuid",
      "server": "ghcr.io",
      "username": "user",
      "password_encrypted": "base64_encoded",
      "source": "web" | "env",
      "created_at": "ISO timestamp",
      "last_used": "ISO timestamp",
      "login_status": "success" | "failed" | "unknown",
      "login_status_checked_at": "ISO timestamp"
    }
  ]
}
```

### Components

#### 1. Credential Manager Module (`backend/app/credential_manager.py`)
- `CredentialManager` class:
  - `load_credentials()` - Load from JSON file
  - `save_credentials()` - Save to JSON file
  - `add_credential()` - Add new credential
  - `update_credential()` - Update existing
  - `delete_credential()` - Remove credential
  - `get_credential_for_server()` - Get credential for a registry
  - `test_login()` - Test if credentials work
  - `sync_env_credentials()` - Load credentials from env vars on startup
  - `get_all_credentials()` - List all (with masked passwords)

#### 2. Web UI Routes (`backend/app/main.py`)
- `GET /credentials` - List all credentials page
- `GET /credentials/add` - Add credential form
- `POST /credentials/add` - Save new credential
- `GET /credentials/<id>/edit` - Edit credential form
- `POST /credentials/<id>/edit` - Update credential
- `POST /credentials/<id>/delete` - Delete credential
- `POST /credentials/<id>/test` - Test login (AJAX)
- `GET /credentials/env-status` - Get env var credentials status (AJAX)

#### 3. Templates
- `credentials/list.html` - List all credentials with status badges
- `credentials/form.html` - Add/edit credential form
- Update `base.html` - Add navigation link to credentials

#### 4. Integration Points
- **Startup**: Load env var credentials and sync to storage
- **Packaging**: Use credentials from storage when pulling charts/images
- **Display**: Show credential source (env vs web) and status

## User Flows

### Flow 1: Add Credential via Web UI
1. User navigates to "Registry Credentials" page
2. Clicks "Add Credential"
3. Fills form: Server, Username, Password
4. Optionally clicks "Test Login" to verify
5. Saves credential
6. Credential appears in list with status

### Flow 2: View Credentials
1. User navigates to "Registry Credentials" page
2. Sees list of all credentials:
   - Server name
   - Username (masked)
   - Source badge (Environment Variable / Web UI)
   - Status badge (Logged In / Failed / Unknown)
   - Last checked timestamp
   - Actions (Edit, Delete, Test)

### Flow 3: Docker Compose Pre-configuration
1. Admin sets env vars in docker-compose.yml:
   ```yaml
   REGISTRY_USERNAME=user
   REGISTRY_PASSWORD=token
   REGISTRY_SERVER=ghcr.io
   ```
2. Container starts, automatically:
   - Detects env vars
   - Runs `helm registry login`
   - Saves to credentials storage (marked as "env" source)
   - Shows in web UI with "Environment Variable" badge

### Flow 4: Using Credentials During Packaging
1. User starts packaging job
2. System checks if chart/image registry needs auth
3. Looks up credential for that registry
4. Uses credential automatically
5. Updates "last_used" timestamp

## Security Considerations

1. **Password Storage**: Base64 encoding (basic obfuscation, not encryption)
   - For production, consider proper encryption
   - File permissions: 600 (owner read/write only)

2. **Password Display**: Never show full password, only masked version

3. **Environment Variables**: Marked as "env" source, read-only in UI

4. **File Permissions**: Ensure credentials file is not world-readable

## Implementation Steps

1. ✅ Create credential manager module
2. ✅ Add credential storage JSON structure
3. ✅ Create web routes for CRUD operations
4. ✅ Create UI templates
5. ✅ Integrate with packager (use credentials automatically)
6. ✅ Add env var sync on startup
7. ✅ Add login testing functionality
8. ✅ Update navigation and UI
9. ✅ Add status badges and indicators
10. ✅ Update documentation

## UI Design

### Credentials List Page
- Table with columns: Server | Username | Source | Status | Last Checked | Actions
- Color-coded status badges:
  - Green: Logged in successfully
  - Red: Login failed
  - Gray: Not tested
- Source badges:
  - Blue: Environment Variable
  - Purple: Web UI

### Credential Form
- Server input (with common registries as suggestions)
- Username input
- Password input (with show/hide toggle)
- "Test Login" button (AJAX, shows result)
- Save/Cancel buttons

### Navigation
- Add "Registry Credentials" link to main navigation
- Show count of configured registries
- Show warning if no credentials configured but trying to use private registry
