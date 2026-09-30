# G-Labs Automation Studio — Windows Guide (Install & Update)

> Private repo: when Git asks you to sign in, use your **GitHub username** and a
> **Personal Access Token** (with `repo` scope) as the password.

---

## Step 1: Install Git (skip if already installed)

1. Open this link: https://git-scm.com/download/win
2. The download starts automatically
3. Run the installer — click **Next** on everything (keep all defaults)
4. Click **Install**, then **Finish**

## Step 2: Install Python (skip if already installed)

1. Open this link: https://www.python.org/downloads/
2. Click the big yellow **"Download Python"** button
3. Run the installer
4. **IMPORTANT:** check the box **"Add python.exe to PATH"** at the bottom
5. Click **"Install Now"**, then **Close**

## Step 3: Download & Build the App

1. Open **PowerShell** (press the Windows key, type "PowerShell", click it)
2. Copy-paste this entire command and press **Enter**:

```powershell
cd ~/Desktop; if(Test-Path Flow-Automation-App){cd Flow-Automation-App; git pull}else{git clone https://github.com/netmirrorapk/glab.git Flow-Automation-App; cd Flow-Automation-App}; .\build_windows.bat
```

3. Wait for the build to finish (**10–15 minutes** the first time)
4. When done, the app is at:
   `Desktop > Flow-Automation-App > dist > G-Labs Automation Studio > G-Labs Automation Studio.exe`

## Step 4: Run the App

Double-click:
`Desktop > Flow-Automation-App > dist > G-Labs Automation Studio > G-Labs Automation Studio.exe`

Then: **Account Manager → log in to your Pro / Ultra account → done.**

---

## How to Update (when a new version is available)

1. Open **PowerShell**
2. Copy-paste this command and press **Enter**:

```powershell
cd ~/Desktop/Flow-Automation-App; git pull; .\build_windows.bat
```

3. Wait for the build to finish
4. Run the same `.exe` again — it is now updated

> Your data (accounts, sessions, outputs) is preserved across updates.

---

## Run without building (developers)

```powershell
cd ~/Desktop/Flow-Automation-App; python main.py
```

---

## Quick Reference

| What | Command |
|------|---------|
| First-time install | Step 1 + Step 2 + Step 3 (above) |
| Update to latest | `cd ~/Desktop/Flow-Automation-App; git pull; .\build_windows.bat` |
| Run without building | `cd ~/Desktop/Flow-Automation-App; python main.py` |
| App location | `Desktop\Flow-Automation-App\dist\G-Labs Automation Studio\G-Labs Automation Studio.exe` |

---

## Troubleshooting

- **`git` is not recognized** — Git isn't installed or PATH didn't refresh. Redo Step 1, then close and reopen PowerShell.
- **`python` is not recognized** — you missed "Add python.exe to PATH" in Step 2. Reinstall Python with that box checked.
- **Asked for a GitHub login** — the repo is private. Enter your GitHub username and a Personal Access Token (Settings → Developer settings → Personal access tokens, `repo` scope) as the password.
- **Build fails partway** — re-run the same command; it resumes cleanly.
