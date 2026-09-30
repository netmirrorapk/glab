# G-Labs Automation Studio — Mac Guide (Install & Update)

> Private repo: when Git asks you to sign in, use your **GitHub username** and a
> **Personal Access Token** (with `repo` scope) as the password.

---

## How to Install

**Step 1:** Open **Terminal** (press **Cmd + Space**, type "Terminal", hit Enter)

**Step 2:** Copy-paste these commands and hit Enter:

```bash
cd ~/Downloads
git clone https://github.com/netmirrorapk/glab.git Flow-Automation-App
cd Flow-Automation-App
chmod +x build_macos.sh
./build_macos.sh
```

**Step 3:** Wait for the download and build to finish (**10–15 minutes** the first time)

**Step 4:** When it's done, close the Terminal window

**Step 5:** Go to **Downloads > Flow-Automation-App > dist > G-Labs Automation Studio**

**Step 6:** Open the app → **Account Manager → log in to your Pro / Ultra account → enjoy!**

> **If macOS blocks the app on first launch:** right-click the app → **Open** → **Open** again to confirm. Or allow it in **System Settings > Privacy & Security**.

---

## How to Update (when a new version is available)

**Step 1:** Right-click the **Flow-Automation-App** folder → **"New Terminal at Folder"**
(or in Terminal: `cd ~/Downloads/Flow-Automation-App`)

**Step 2:** Run:

```bash
git pull
chmod +x build_macos.sh
./build_macos.sh
```

**Step 3:** Wait for the rebuild to finish — done!

> Your data (accounts, sessions, outputs) is preserved across updates.

---

## Run without building (developers)

```bash
cd ~/Downloads/Flow-Automation-App
python3 main.py
```

---

## Troubleshooting

**Check if Git is installed:**

```bash
git --version
```

If you see "not found", install the developer tools:

```bash
xcode-select --install
```

A popup appears — click **Install** and wait 5–10 minutes. Then re-run the install steps:

```bash
cd ~/Downloads
git clone https://github.com/netmirrorapk/glab.git Flow-Automation-App
cd Flow-Automation-App
chmod +x build_macos.sh
./build_macos.sh
```

**Asked for a GitHub login** — the repo is private. Enter your GitHub username and a Personal Access Token (GitHub → Settings → Developer settings → Personal access tokens, `repo` scope) as the password.

---

## Quick Reference

| What | Command |
|------|---------|
| First-time install | The 5 commands under **How to Install** |
| Update to latest | `cd ~/Downloads/Flow-Automation-App; git pull; ./build_macos.sh` |
| Run without building | `cd ~/Downloads/Flow-Automation-App; python3 main.py` |
| App location | `Downloads/Flow-Automation-App/dist/G-Labs Automation Studio/` |
