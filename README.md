# Intelligent FO & AT

A Windows desktop file organizer and activity tracker built with Python and Tkinter. It
monitors folders for file-system changes, displays activity in a live log, and can send
desktop notifications. MongoDB stores user accounts, preferences, trial usage, and audit
events.

## Features

- Monitor selected folders for file and folder creation, deletion, modification, moves,
	and renames.
- View, filter, search, bookmark, and export activity from the live log.
- Receive desktop notifications for monitored activity.
- Configure folders to monitor and organize, with checks that block protected system
	locations.
- Register and sign in, or use the limited free-trial mode.

## Requirements

- Windows 10 or later
- Python 3.10 or later with Tkinter
- A MongoDB database and connection URI

Runtime packages: `pymongo`, `plyer`, `watchdog`, and `Pillow`.

## Setup

Clone the repository, then create and activate a virtual environment in PowerShell:

```powershell
git clone https://github.com/Piyush30-sh/Intelligent-File-Organizer-and-Activity-Trackers.git
cd Intelligent-File-Organizer-and-Activity-Trackers
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install pymongo plyer watchdog Pillow
```

Configure MongoDB before launching. In PowerShell, set the URI for the current terminal
session (replace the placeholders with your own MongoDB credentials and cluster):

```powershell
$env:MONGO_URI = "mongodb+srv://<username>:<password>@<cluster>/"
# Optional; defaults to smart_file_organizer
$env:MONGO_DB_NAME = "smart_file_organizer"
python finalprojectcode.py
```

Create a MongoDB database user with only the permissions the app needs, and configure the
cluster network access list for your environment. Never put a real URI or password in
source control. The application exits with a configuration error if `MONGO_URI` is unset.

## Build a Windows executable

Install PyInstaller in the active virtual environment and build from the project root:

```powershell
python -m pip install pyinstaller
pyinstaller --clean build_exe.spec
```

The executable is written to `dist/`. It is generated output and is intentionally excluded
from Git; build a fresh executable after configuring credentials through the environment.

## Project files

| File | Purpose |
| --- | --- |
| `finalprojectcode.py` | Tkinter interface, folder monitoring, and notifications |
| `database.py` | MongoDB connection and persistence |
| `session.py` | Login, session, and free-trial state |
| `folder_guard.py` | Checks that prevent operations in protected system folders |
| `build_exe.spec` | PyInstaller configuration |

## Security notes

- Keep `MONGO_URI` private. If it has ever been published, rotate the database password and
	review the database user's access.
- The current account implementation hashes passwords with unsalted SHA-256. This is not
	appropriate password storage for a production authentication system; use a dedicated
	password-hashing algorithm such as Argon2id or bcrypt before deploying for real users.
- Folder monitoring and organization can expose or move user files. Select only folders
	you intend the application to manage.

Build output, logs, local environment files, and Python cache files are excluded by
`.gitignore`.
