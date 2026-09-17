# Amazon Shift Finder

A local Python/Tkinter desktop monitor for authenticated Amazon Jobs and Telegram Web accounts. It watches for Dartford or Tilbury alerts and visible Amazon `Select Shift` availability at or above the configured weekly-hour minimum. It records detections in SQLite and always pauses for human review before final submission.

## Safety boundaries

- Authentication happens in a persistent local Playwright browser profile. The app does not store an Amazon password.
- CAPTCHA, 2FA, verification screens, missing page elements, and ambiguous submit controls stop the affected workflow and notify you.
- Final submission requires a confirmation dialog. The app never bypasses rate limits or access controls.
- Telegram is monitored through Telegram Web in a separate persistent local browser profile; no Telegram API credentials are required.

## Run

### Windows

Open Command Prompt in the folder that contains `app.py` and `requirements.txt`. For example, if the project was downloaded to `Downloads`:

```bat
cd /d "%USERPROFILE%\Downloads\AMZ"
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe app.py
```

You can also double-click `run_windows.bat` from that project folder. Do not run these commands from `C:\Users\aky11` unless that is where the project files were copied.

### Linux/macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

On first use, complete Amazon login in the Amazon browser window and Telegram login in the Telegram Web browser window. The two profiles are stored separately under `~/.amazon_shift_finder`, and both accounts remain under your control.

The SQLite history is stored at `~/.amazon_shift_finder/shifts.sqlite3`; the browser profile is stored at `~/.amazon_shift_finder/browser-profile`. Inspect page changes and selectors before relying on unattended monitoring because Amazon can change its UI.# AMZ
JOBMMM
