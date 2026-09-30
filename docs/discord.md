# Setting up Discord

Hallkeeper uses one Discord application for two things: the **bot** (slash commands, posts, buttons)
and **"Log in with Discord"** on the web hall.

1. Go to <https://discord.com/developers/applications> → **New Application**. Name it after your hall.
2. **Bot** tab:
   - **Reset Token**, copy it into `.env` as `MMGU_DISCORD_TOKEN`.
   - Turn on **Server Members Intent** (keeps hall ranks in sync with Discord roles).
3. **OAuth2** tab:
   - Copy **Client ID** → `MMGU_DISCORD_CLIENT_ID`, **Client Secret** → `MMGU_DISCORD_CLIENT_SECRET`.
   - Add a redirect: `<MMGU_BASE_URL>/auth/discord/callback`, e.g. `https://hallkeeper.example.ts.net/auth/discord/callback`.
4. Invite the bot: OAuth2 → URL Generator → scopes `bot` and `applications.commands`; bot permissions
   *View Channels, Send Messages, Embed Links, Attach Files, Read Message History*. Open the URL and pick
   your server.
5. In Discord, turn on Developer Mode (User Settings → Advanced), then right-click your server →
   **Copy Server ID** → `MMGU_DISCORD_GUILD_ID`. Slash commands register to this server instantly.
6. Make yourself leader: right-click your own name → **Copy User ID**, and set
   `MMGU_BOOTSTRAP_LEADERS=discord:<your id>`.
7. Optional role sync: `MMGU_DISCORD_ROLE_MAP=<officer role id>=officer,<member role id>=member,<bank role id>=banker`.
   Ranks: `leader officer member recruit guest`. Duties: `banker crafter streamer raidlead`.
8. Restart the hall. In the web hall, open **Steward's Office → Settings & channels** and paste channel
   ids for where discoveries, board posts, bank requests, events and spawn alerts should go.

## Commands members will use

| Command | What it does |
|---|---|
| `/item <name>` | Post an item card from the Archive |
| Right-click a screenshot → **Apps → Catalog item** | Read an item screenshot into the Archive |
| `/catalog <screenshot>` | Same, as a slash command |
| `/drops creature:<…> zone:<…>` | What drops there |
| `/char add`, `/char level`, `/roster` | Characters and who plays what |
| `/bank search`, `/bank request` | The guild bank |
| `/wts`, `/wtb`, `/request`, `/board` | The Notice Board |
| `/crafter set`, `/whocan` | Tradeskills |
| `/event create`, `/events`, `/loot award` | Events and loot |
| `/tod`, `/timers`, `/watch`, `/camp` | Spawn timers and camps |
| `/stream item`, `/stream say` | Push to the stream overlay |
| `/link <code>` | Link Discord to a web login made another way |
| `/hall`, `/whoami` | What the bot can do; your rank |
