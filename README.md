<p align="center"><img src="assets/logo.png" width="112" alt="NIGHT MANAGER logo"></p>

<h1 align="center">NIGHT MANAGER</h1>

<p align="center">A Windows desktop app for managing, launching and keeping multiple Roblox accounts running.<br>
by NightHub &middot; <a href="https://discord.gg/nighthubqx">Discord</a></p>

> NIGHT MANAGER is a modified version of [Evanovar RAM](https://github.com/evanovar/RobloxAccountManager) by evanovar,
> redistributed under the GNU General Public License v3. The account, launch and automation features come from
> Evanovar RAM; NIGHT MANAGER adds its own interface, branding and release channel.

## Installation

1. Download `NightManager-v<version>.exe` from the [latest release](https://github.com/nighthub00/night-manager/releases/latest).
2. Put it in its own folder. Saved accounts live in `AccountManagerData/` next to the executable.
3. Run it.

Switching from Evanovar RAM: copy your old `AccountManagerData/` folder next to `NightManager.exe`. The data format is the same.

## Features

### Account management

| Feature | Description |
| :--- | :--- |
| Browser login | Add an account through a supported browser and save it to the local account list. |
| Cookie import | Import one or multiple `.ROBLOSECURITY` cookies. |
| User and password import | Import credentials manually or from a `User:Pass` text file. Login sessions run in batches of up to five browsers. |
| Account Creator | Create up to 100 accounts in one operation with up to five browser sessions, an optional custom prefix, and an optional shared password. |
| JavaScript login | Open multiple browser sessions and run custom JavaScript for advanced login workflows. |
| Groups and notes | Organize accounts into groups and assign notes to one or multiple selected accounts. |
| Account list controls | Use avatars, drag-and-drop ordering, multi-select actions, refresh, deletion, and controlled password or cookie copying. |
| Cookie status | Detect unauthorized cookies while keeping rate limits and temporary validation failures separate from invalid accounts. |
| Activity data | Display online status, Roblox memory usage, and CPU usage beside saved accounts. |

### Game launching

| Feature | Description |
| :--- | :--- |
| Place launch | Launch one or multiple selected accounts into a Place ID. |
| Private servers | Resolve current and legacy private server links. A Place ID inside the link is used when the Place ID field is empty. |
| Join User | Resolve a username or user ID and join the user's current game when permitted. |
| Job ID | Join a specific running server by Place ID and Job ID. |
| Small Server | Find and join a server with a low player count. |
| Game favorites | Save a Place ID with its optional private server link, select it from the Place ID list, or remove it from the context menu. |
| Recent games | Save recent public and private server launches. Private entries are marked with `[P]`. |
| Launch delay | Add a configurable delay between accounts during bulk launches. |
| Launcher selection | Use Automatic, Bloxstrap, Fishstrap, Froststrap, Roblox Client, or a custom executable. |

### Multi Roblox and window management

| Feature | Description |
| :--- | :--- |
| Default Multi Roblox | Pre-create the Roblox singleton mutex before clients launch. Existing Roblox clients must be closed before enabling this mode. |
| Handle64 mode | Detect validated Roblox game processes and close their singleton handles with retry handling. Administrator access is required. |
| Error 773 protection | Lock `RobloxCookies.dat` when possible while Multi Roblox is active. |
| Rename Roblox windows | Continuously map Roblox processes to accounts and rename windows to the account username or note. |
| Window Grid | Arrange visible Roblox windows into an equal grid with a customizable global keyboard shortcut. |
| Headless Manager | List running Roblox clients and hide or show selected windows. Hidden windows are restored when the application exits. |
| Kill all Roblox processes | Close every validated Roblox game client from General settings. |
| Roblox Installer Fix | Temporarily quarantine Roblox installer executables to prevent installer popups, then restore them on exit. |
| RAM optimization | Optionally trim the working set of detected Roblox clients to a configured target. |

### Auto-Rejoin and Anti-AFK

| Feature | Description |
| :--- | :--- |
| Per-account Auto-Rejoin | Monitor configured accounts and relaunch them after a client exits or disconnects. |
| Flexible destinations | Configure a Place ID, private server, or Job ID for each Auto-Rejoin entry. |
| Process cleanup | Track account processes and close stale disconnected clients before relaunching. |
| Network handling | Wait for connectivity and stagger relaunch attempts to reduce duplicate clients. |
| Anti-AFK actions | Record a keyboard or mouse action, press count, and maintenance interval. |
| Headless support | Temporarily restore hidden Roblox windows for Anti-AFK maintenance, then return them to their previous state. |

### Roblox tools and settings

| Feature | Description |
| :--- | :--- |
| Basic Roblox settings | Enable presets for Framerate Cap, Master Volume, and Start Quality. Enabled presets apply before Roblox launches. |
| Advanced settings editor | Search and edit values from `GlobalBasicSettings_13.xml` through a local settings profile. |
| Advanced Auto Apply | Apply the saved advanced profile on application startup and before Roblox launches, with basic presets taking priority. |
| Roblox Downloader | Download the latest LIVE Windows Roblox Player deployment or a specific version hash to a chosen folder. |
| Portable Chromium | Download or reinstall the latest supported Chromium build and its matching driver. |
| Browser selection | Choose Chrome, Firefox, Edge, or portable Chromium for automated browser flows. Brave and Opera GX users can use portable Chromium. |

### Application controls and integrations

| Feature | Description |
| :--- | :--- |
| System tray | Hide the main window to the system tray, restore it from the tray icon, or exit from the tray menu. |
| Windows startup | Optionally start NIGHT MANAGER with Windows and add a Start Menu shortcut. |
| Update manager | Check GitHub releases on startup or manually, then download updates from the application. |
| Discord webhooks | Send selected log levels, Auto-Rejoin events, optional mentions, and periodic screenshots to a configured webhook. |
| WebSocket server | Run an optional local command server with a configurable port and encrypted password storage. Password-protected commands use `AUTH <password> | <command>`. |
| Console | Review timestamped, color-coded application output and copy or clear the current console view. |
| Structured errors | Show specific error codes and technical details instead of generic failure messages. |
| Crash diagnostics | Save timestamped session and crash logs under `AccountManagerData/logs`. Error dialogs can copy the message or the full log. |

### Security and local data

| Feature | Description |
| :--- | :--- |
| Hardware encryption | Encrypt saved accounts with a key tied to the current Windows machine. No password is required at startup. |
| Password encryption | Encrypt saved accounts with a user-provided password. |
| No encryption | Store account data without encryption when explicitly selected. |
| Encryption switching | Re-encrypt saved accounts and secure settings when changing encryption methods. |
| Encryption status | Display the active hardware, password, or unencrypted state beside the account list. |
| Data removal | Wipe local application data from Settings > Misc. |

## Data and privacy

NIGHT MANAGER stores its persistent data in `AccountManagerData`. This includes saved accounts, settings, groups, recent games, local Roblox settings, avatar cache, and diagnostic logs.

The application does not include hidden telemetry, advertising SDKs, or analytics tracking. Network communication is limited to enabled or requested functionality:

- Roblox API requests for account, game, presence, authentication, and download features.
- GitHub requests for release and update checks.
- Discord webhook requests when Discord integration is configured.
- Connectivity checks used by Auto-Rejoin.

Account cookies and stored WebSocket passwords remain local unless the user explicitly enables a feature that sends related data elsewhere.

## Build from source

Install the locked runtime and build dependencies, then run the shared build script:

```powershell
uv sync --locked --group build
uv run --no-sync python scripts/build.py
```

The executable is written to `dist/NightManager.exe`. Build configuration lives in `packaging/NightManager.spec`, and version metadata is generated during the build. Release builds also create `dist/NightManager-v<version>.exe` for GitHub Releases.

`src/utils/version.py` is the single source of truth for the application version. Release tags must match `APP_VERSION`.

## System changes and uninstallation

Depending on enabled features, NIGHT MANAGER can:

- Create and update files under `AccountManagerData`.
- Register or remove Windows startup and Start Menu entries.
- Download portable Chromium, Handle64, or Roblox deployment files when requested.
- Temporarily mark Roblox settings as read-only when Advanced Auto Apply is enabled.
- Temporarily move Roblox installer files into the application quarantine folder.

To uninstall:

1. Exit the application from the window or system tray.
2. Delete the application folder or executable.
3. Delete `AccountManagerData` to remove saved accounts, settings, and logs.
4. Remove any startup or Start Menu entry that was enabled in the application.

## Releases and automatic updates

The in-app updater only installs NIGHT MANAGER builds:

- It checks releases in the repository named by `UPDATE_REPOSITORY` in `src/features/updater.py` (`nighthub00/night-manager`).
- It only downloads assets named `NightManager-v<version>.exe`.
- Before installing, it reads the downloaded file's Windows version information and refuses anything whose product name is not `NIGHT MANAGER`. An Evanovar RAM executable is rejected even if it is uploaded to the wrong release by mistake.

To publish a release, bump `APP_VERSION` in `src/utils/version.py`, commit, then push a matching tag:

```powershell
git tag v1.0.1
git push origin v1.0.1
```

The `Release` workflow builds `NightManager-v1.0.1.exe` and attaches it to a GitHub release. Installed copies older than that version will offer the update.

## Pulling fixes from Evanovar RAM

The upstream project is the `upstream` git remote. Its feature code under `src/classes/` and `src/features/` is mostly unchanged here, so upstream fixes usually merge cleanly:

```powershell
git fetch upstream
git merge upstream/main
```

Expect conflicts in `src/utils/ui.py` (the redesigned interface) and in the branding files. Keep the NIGHT MANAGER side for layout and names, and keep upstream's side for behaviour.

## Disclaimer

This project is not affiliated with Roblox Corporation. Using multiple accounts or automation may violate Roblox's Terms of Use. Use it at your own risk.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE). Original work copyright evanovar; modifications copyright NightHub.
