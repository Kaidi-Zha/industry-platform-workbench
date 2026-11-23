# industplatform

## Quick start

Use Python 3.11 or later. If you already cloned the platform repository below, open its root directory and skip the clone step.

```bash
git clone https://github.com/Kaidi-Zha/industry-platform-workbench.git
cd industry-platform-workbench
```

**macOS & Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m industplatform start --port 18765
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m industplatform start --port 18765
```

If port `18765` is in use, pass a free port to `--port`. Keep the terminal running and open `localhost:<port>` in your browser using the same port. Follow the on-screen instructions.

To start the workbench again, run only the last command for your OS from the repository root. Press `Ctrl+C` to stop it.

## Optional team data sync

In the workbench, open **Data Sync** and enter your team's private GitHub repository as `owner/repository`. Sign in with GitHub CLI using `gh auth login --hostname github.com`, then test the connection. You can use the workbench locally with this setting left blank.

The data repository stores shared JSON records. Local databases, model keys, run logs, and source files stay on your machine. [Team sync guide](docs/github-monorepo.md)
