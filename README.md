# Intelligent FO&AT

This project appears to be a Windows desktop application with local project logic, session management, and a Python/GUI integration workflow.

## Overview
- Local project and session tracking
- Data persistence and configuration utilities
- A project explanation and build-related files are included

## Run
Set the MongoDB connection string in the environment before starting the app. In PowerShell:

```powershell
$env:MONGO_URI = "mongodb+srv://<username>:<password>@<cluster>/"
python finalprojectcode.py
```

Do not commit the real connection string. `MONGO_DB_NAME` is optional and defaults to
`smart_file_organizer`.

## Important files
- `finalprojectcode.py` — main application
- `database.py` — data access logic
- `session.py` — session state management
- `project_explanation.txt` — project notes
- `build_exe.spec` — packaging/build configuration

## Notes
This is a Windows desktop application. Install the required Python packages in a virtual
environment before running it; generated executables, logs, and build output are excluded
from Git.
