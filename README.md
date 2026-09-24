<div align="center">

# 🌲 CTC Agent

**An AI assistant for the staff of [CTC](https://www.cedartc.org/0/1/), an online Christian training platform**

*Takes routine work off the staff's plate so they have more time for students.*

![Platform](https://img.shields.io/badge/platform-macOS-lightgrey?logo=apple)
![Python](https://img.shields.io/badge/python-3-blue?logo=python&logoColor=white)
![Powered by Claude](https://img.shields.io/badge/powered%20by-Claude-D97757)
![Version](https://img.shields.io/badge/version-0.3.0-green)

</div>

---

## 📖 About

[CTC](https://www.cedartc.org/0/1/) is an online Christian training platform. **CTC Agent**
helps CTC staff with the everyday administrative tasks of running it.

The first feature is **automatic email replies**. CTC Agent is a Mac app that watches a Gmail
inbox. When someone writes in to ask about CTC, it sends them a short, friendly reply (in
English and Chinese) with a link to the website.

### 🗺️ Roadmap

| Feature | Status |
| --- | --- |
| ✉️ Automatic replies to inquiry emails | ✅ Available |
| 📝 Automatic homework grading | 🔭 Planned |
| 📊 Student activity tracking | 🔭 Planned |
| 💡 More staff tools | 💭 Ideas welcome |

## 📑 Contents

- [How it works](#-how-it-works)
- [Key features](#-key-features)
- [Installing (for staff)](#-installing-for-staff)
- [Building the installer (for distributors)](#️-building-the-installer-for-distributors)
- [Development](#-development)

---

## ⚙️ How it works

```
 📥 New email ──▶ 🛡️ Safety filters ──▶ 🤖 Claude: "Does this person want
                  (auto-replies, lists,      to know more about CTC?"
                   bulk mail, own mail,              │
                   already-replied threads)   ┌──────┴──────┐
                                              ▼             ▼
                                            Yes            No
                                              │             │
                                   📤 Reply in thread   📋 Logged as
                                   + label `ctc-acked`   "not answered"
```

While the app window is open, CTC Agent checks the inbox every 30 seconds. Each new email
goes to Claude (model `claude-haiku-4-5`), which decides whether the sender wants to know more
about CTC. If so, the app replies in the same thread with a short note pointing to
[www.cedartc.org](https://www.cedartc.org). The window lists every reply sent and every
email the agent decided not to answer.

> **To stop the agent**, close the window or press **⌘Q**.

## ✨ Key features

| | |
| --- | --- |
| 🆕 **Only new mail** | Emails received before the agent first started are ignored. Mail that arrives while the app is closed is handled the next time it's opened. |
| 1️⃣ **Each email is checked once** | Every email the agent reads gets a hidden Gmail label `ctc-checked`, and every email it answers also gets the visible label `ctc-acked`. Labeled emails are skipped afterwards, so no email is sent to Claude or answered twice. |
| 🔁 **No loops or intrusions** | Without asking Claude, the agent skips auto-replies, mailing lists, bulk mail, your own messages, and any thread you (or the agent) have already replied to. Its replies carry `Auto-Submitted: auto-replied`, so other auto-responders don't answer them. |
| 🔒 **Fixed reply text** | Claude only decides *whether* to reply. The reply text is the fixed `REPLY_BODY` template in [`ctc_agent.py`](ctc_agent.py), so an email can't make the agent write anything else. The classifier prompt (`CLASSIFIER_PROMPT`) is in the same file. |
| 💰 **Low cost** | One short Claude Haiku request per new email (typically about $0.001, at most about $0.01). Emails longer than 8,000 characters are truncated first. |
| 💪 **Resilient** | Polls every 30s and backs off exponentially (up to 10 min) on errors. If the Gmail sign-in expires or is revoked, the window says so and offers **Sign In** again. |

---

## 💻 Installing (for staff)

1. **Install.** Open `CTC-Agent-<version>-arm64.dmg` and drag **CTC Agent** into
   **Applications**.

2. **Open the app.** Open **CTC Agent** from Applications.
   > ⚠️ The first time, macOS may say it can't verify the developer (unless the build was
   > signed and notarized). If so, open **System Settings → Privacy & Security**, click
   > **Open Anyway** next to CTC Agent, and confirm.

3. **Add a Claude API key.** When asked, paste a Claude API key (create one at
   [console.anthropic.com](https://console.anthropic.com/)). You can change it later from
   **CTC Agent → Set Claude API Key…**. It's saved in
   `~/Library/Application Support/ctc-agent/anthropic_api_key`.

4. **Sign in to Gmail.** Click **Sign In**, then sign in to the Gmail account to watch and
   allow access in the browser.
   > ℹ️ If Google says the app is unverified, click **Advanced → Go to … (unsafe)**.

5. **You're all set! 🎉** The window shows **Watching you@gmail.com**. Every reply the agent
   sends, every email it decided not to answer, and any errors appear in the activity list.
   Keep the window open (it can be minimized) for the agent to keep working. Close it to stop.

**Good to know:**

- 🔑 Next time, just open CTC Agent: it remembers the sign-in. Use **Sign Out** to switch
  accounts.
- 📄 Activity is also logged to `~/Library/Logs/ctc-agent.log`.
- 🗑️ To uninstall, quit the app and move it to the Trash.

---

## 🛠️ Building the installer (for distributors)

### Step 1: Create the OAuth client (one time)

The app ships with your Google OAuth client, so end users only have to sign in.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable
   the **Gmail API**.
2. Configure the OAuth consent screen (user type **External**).
   - In **Testing** status, only the Google accounts you add as test users can sign in, and
     their sign-in expires after 7 days.
   - For long-running use, set it to **In production**. Without Google's verification,
     users see an "unverified app" warning, and at most 100 users can sign in.
3. Create credentials → **OAuth client ID** → **Desktop app**. Download the JSON and save it
   as `credentials.json` in this directory.

> 🔐 **Is it safe to bundle `credentials.json`?** Google doesn't treat a desktop app's client
> secret as confidential (anyone with the app can extract it), so bundling it is standard
> practice. It grants no access to anyone's mail, but someone could reuse it to pose as your
> app on the consent screen or use up your API quota. Only bundle a **Desktop app** client,
> never a web-app secret or a service-account key.

### Step 2: Build

```sh
./build.sh
```

This creates a build virtualenv, runs the tests, bundles Python and all dependencies with
PyInstaller into `dist/CTC Agent.app`, and packages it as
`dist/CTC-Agent-<version>-<arch>.dmg`. Send the DMG to users. **The user's Mac doesn't need
Python.**

- **Architecture**: the build targets the Mac it runs on (`arm64` on Apple Silicon). Build
  on an Intel Mac for an `x86_64` version.
- **Version**: bump `__version__` in [`ctc_agent.py`](ctc_agent.py).
- **Signing and notarization** (optional, removes the Gatekeeper warning; needs a paid Apple
  Developer account):
  ```sh
  xcrun notarytool store-credentials ctc-notary --apple-id you@example.com --team-id TEAMID
  CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" NOTARY_PROFILE=ctc-notary ./build.sh
  ```
  Without these, the app is ad-hoc signed and users go through the "Open Anyway" step above.

---

## 👩‍💻 Development

### Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest -v
.venv/bin/python ctc_agent.py set-api-key   # or export ANTHROPIC_API_KEY
.venv/bin/python ctc_agent.py app           # open the app window from source
```

### Project layout

| File | Purpose |
| --- | --- |
| [`ctc_agent.py`](ctc_agent.py) | Core agent: Gmail polling, Claude classifier, reply template, CLI |
| [`gui.py`](gui.py) | Native macOS window (AppKit via PyObjC) |
| [`test_ctc_agent.py`](test_ctc_agent.py) | Unit tests |
| [`build.sh`](build.sh), [`ctc_agent.spec`](ctc_agent.spec) | PyInstaller build and DMG packaging |

The agent runs on a worker thread and reports each action through `CtcAgent`'s `listener`
callback, which the UI uses to update the window.

### Commands

With no arguments, the packaged app opens the window. For debugging, the same commands work
from source or from `/Applications/CTC\ Agent.app/Contents/MacOS/ctc-agent`:

| Command | What it does |
| --- | --- |
| `app` | Open the app window (default for the packaged app) |
| `run` | Watch the inbox in the terminal, without a window |
| `auth` | Sign in to Gmail and save the token |

### Where files live

- 📁 **Data**: `~/Library/Application Support/ctc-agent/` (`token.json`, `state.json`, and
  optionally `credentials.json` to override the bundled OAuth client). Set `CTC_AGENT_HOME`
  to use a different directory.
- 📄 **Log**: `~/Library/Logs/ctc-agent.log`.

---

<div align="center">

Made with ❤️ for the staff and students of [CTC](https://www.cedartc.org/0/1/)

</div>
