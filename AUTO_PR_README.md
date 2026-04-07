# Automated PR Creator for Pulse Incidents

This script automatically watches the Pulse database for resolved incidents and creates GitHub Pull Requests documenting each incident.

## Features

- 🔍 **Automatic Detection**: Monitors `pulse.db` every 10 seconds for newly resolved incidents
- 📝 **Documentation**: Creates detailed markdown reports for each incident
- 🌿 **Branch Management**: Automatically creates feature branches for each incident
- 🚀 **PR Creation**: Creates GitHub PRs automatically (requires GitHub CLI)
- 💾 **Tracking**: Keeps track of processed incidents to avoid duplicates

## Usage

### Terminal 5 - Auto PR Creator

```bash
python3 auto_pr_creator.py
```

The script will:
1. Watch for incidents with status `resolved` or `auto_resolved`
2. Create a new branch: `incident/INC-XXXXXXXX`
3. Generate a detailed incident report in `incidents/INC-XXXXXXXX.md`
4. Commit and push the changes
5. Create a Pull Request on GitHub (if `gh` CLI is installed)

## Setup

### Install GitHub CLI (Optional but Recommended)

**macOS:**
```bash
brew install gh
gh auth login
```

**Linux:**
```bash
# See: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
```

Without GitHub CLI, the script will still create branches and push them, but you'll need to create PRs manually via the provided URL.

## What Gets Created

Each incident PR includes:

- **Incident Overview**: Service, severity, timestamps
- **Problem Description**: What went wrong
- **Investigation Steps**: All steps BOB took during investigation
- **Root Cause Analysis**: Detailed RCA with confidence level
- **Resolution**: Actions taken to fix the issue
- **Recommended Actions**: Future prevention measures

## Example Workflow

```bash
# Terminal 1: Watcher
python3 pulse_watcher.py

# Terminal 2: Mock Services
python3 mock_services.py

# Terminal 3: API
python3 pulse_api.py

# Terminal 4: Trigger Incidents
python3 trigger_pagerduty.py incident1

# Terminal 5: Auto PR Creator (NEW!)
python3 auto_pr_creator.py
```

## Files Created

- `incidents/INC-XXXXXXXX.md` - Incident documentation
- `.processed_incidents.json` - Tracking file (gitignored)

## Stopping the Script

Press `Ctrl+C` to stop the auto-PR creator gracefully.