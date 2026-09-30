# Ledger Sync companion

A small script you run on your own Windows PC. It reads your Monsters & Memories **Ledger** files (the
game's own record of what you looted and killed) and sends that to your guild's Hallkeeper site:

- **Loot** of items the guild's Archive already knows becomes a drop report ("dropped by X in Y").
  Items it doesn't know become suggestions an officer reviews.
- **Kills** of named creatures the guild tracks update their spawn timers.
- Vendor sales and harvesting are sent too, but the hall only counts them for now.

## Read this first

The Monsters & Memories user agreement forbids software that "intercepts, collects, reads, or mines"
information the game produces, and reading the Ledger files is very likely covered by that. **Running
this script probably breaks the game's rules as they stand today and could put your account at risk.**
The developers may loosen this; check the current rules yourself. Running it is your own decision.

What it does and doesn't do:

- It **only reads** the Ledger `.json` files. It never writes to, moves or changes any game file.
- It never reads game memory or network traffic, and it doesn't automate anything in the game.
- It sends only four kinds of events: loot, kills, vendor sales and harvests, with the item, creature,
  zone and time. Other events in the files (chat, party members, quests) are skipped and never leave your PC.
- Its own files are `ledger_sync_config.json` (hall address, token, your answer to the notice) and
  `ledger_sync_state.json` (which events it already sent). Both sit next to the script. Your token is in
  the config file, so don't share that file.

Your guild also has to switch the **Ledger Sync** add-on on in the hall. If it's off, the script
tells you.

## Install

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>. On the first installer
   screen, tick **"Add python.exe to PATH"**.
2. Make a folder, for example `Documents\Hallkeeper`, and put `ledger_sync.py` in it.
   Don't put it inside the game's folder.
3. In the hall, open your profile page (click your name, top right) → **API tokens** → create one
   named "Ledger Sync". Copy it; it's shown only once.

## Set up (once)

1. Open the folder in File Explorer, click the address bar, type `cmd` and press Enter. A black
   Command Prompt window opens in that folder.
2. Type this and press Enter:

   ```
   python ledger_sync.py --setup
   ```

3. Paste your hall's address (the one in your browser, like `https://hall.example.org`), then your token
   (right-click pastes; nothing shows while you type it, that's normal). Press Enter to accept the
   usual game folder.

## Use

See what it would send, without sending anything:

```
python ledger_sync.py --dry-run
```

Send it (the first time you must add `--i-understand` to confirm you read the notice; it remembers):

```
python ledger_sync.py --once --i-understand
```

Keep it running while you play; it checks every 30 seconds. Close the window or press Ctrl+C to stop:

```
python ledger_sync.py --watch
```

Other options:

| Option | What it does |
|---|---|
| `--days 2` | Only send events from the last 2 days (default 7; `--days 0` sends everything) |
| `--interval 60` | With `--watch`, check every 60 seconds |
| `--game-dir "D:\Games\Monsters and Memories"` | Use a different game folder |
| `--hall https://...` | Use a different hall address this time |
| `--forget` | Forget what was already sent. Safe: the hall ignores events it has seen |

You can also set `HALLKEEPER_URL`, `HALLKEEPER_TOKEN` and `HALLKEEPER_GAME_DIR` as environment
variables instead of using `--setup`.

## Where the files are

The script looks in
`%USERPROFILE%\AppData\LocalLow\Niche Worlds Cult\Monsters and Memories\`
for files named `<Character>_Character_<date>.json` and `<Character>_Social_<date>.json`, usually in
`<server>\<Character>\Ledger\`. It works out the character from the file name and the server from the
folder above the character's folder.

## What isn't certain

The Ledger format isn't documented by the developers. This script follows what the community tool
[mnm-tools](https://github.com/Boisteroux/mnm-tools) learned (its `DATA-COLLECTION.md` and
`tracker/ledger-parser.js`), and has not been checked against real files by Hallkeeper's authors:

- Events are read from a top-level `c01` list; each has `f01` (event code), `f02`, `f03` (a JSON string
  with fields `d01`…`d13`), `f04` (time) and `f05` (`zone_` + base64 zone name).
- Loot (`act_13`): item in `d04` (sometimes `"<id>|<name>"`), creature in `d02` (base64).
  Kill (`act_14`): creature in `d13` (base64), coin in `d12` (base64 `"plat,gold,silver,copper"`).
  Vendor sale (`act_24`): item `d04`, quantity `d01`, price `d03` (base64). Harvest (`act_27`): `d04`.
- **Time format of `f04` is unknown.** The script accepts ISO 8601 (with or without fractions and a
  time zone), epoch seconds/milliseconds and US-style dates. Times without a zone are taken as your PC's
  local time. Events whose time can't be read are skipped. If spawn timers come out hours off, this is why;
  please report it.
- Only internal zone names that mnm-tools mapped (e.g. `evergrove` → Evershade Weald) get a proper name;
  others are shown as the internal name, capitalised.
- The game writes kill events for only some creatures, so not every kill reaches the hall.
- If the same event appears in both the `_Character_` and `_Social_` file it may be sent twice.
- Anything that doesn't match these shapes is skipped rather than guessed at.
