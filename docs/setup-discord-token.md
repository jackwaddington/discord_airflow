# Getting Your Discord Personal Token

DiscordChatExporter uses your **personal Discord token** — the same credential your browser uses when you're logged in to Discord. This lets it export the message history of any server you're a member of, without needing a bot invite.

> **Security warning:** Treat your token like a password. Anyone who has it can log into your Discord account. Never share it, never commit it to git, and store it only in `.env`.

---

## Step-by-Step: Extract Token from Browser

These instructions work in Chrome, Firefox, Edge, and Brave.

### 1. Open Discord in your browser

Go to [discord.com/app](https://discord.com/app) and log in if needed.

### 2. Open Developer Tools

- **Windows/Linux:** `F12` or `Ctrl+Shift+I`
- **macOS:** `Cmd+Option+I`

### 3. Go to the Network tab

Click the **Network** tab at the top of DevTools.

### 4. Filter for API requests

In the filter box, type `api/v` to show only Discord API requests. If nothing appears yet, click anywhere in Discord (switch channels, etc.) to trigger a request.

### 5. Click any request and find the token

Click on any request in the list, then look at the **Request Headers** panel on the right. Find the `Authorization` header — its value is your personal token.

```
Authorization: MTAxNDM4NDM4Nzc...  ← this is your token
```

Copy the full value (it starts with `MT` for most accounts, or `OD` for older ones).

### 6. Store it in .env

Open `.env` in your project root and set:

```env
DISCORD_TOKEN=MTAxNDM4NDM4Nzc...your token here...
```

Then in `layer1-collector/config.yaml`, the token is read automatically:

```yaml
discord:
  token: ${DISCORD_TOKEN}
```

---

## Alternative: Discord App (Desktop)

If you prefer not to use a browser:

1. Open the Discord desktop app
2. Press `Ctrl+Shift+I` (Windows/Linux) or `Cmd+Option+I` (macOS) to open DevTools
3. Go to **Console** tab
4. Paste this and press Enter:

```javascript
window.webpackChunkdiscord_app.push([
  [Math.random()],
  {},
  req => {
    for (const m of Object.keys(req.c).map(x => req.c[x].exports).filter(x => x)) {
      if (m.default && m.default.getToken !== undefined) {
        return copy(m.default.getToken());
      }
      if (m.getToken !== undefined) {
        return copy(m.getToken());
      }
    }
  }
]);
console.log('%cWorked!', 'font-size: 50px');
console.log(`Your token is now in your clipboard.`);
```

Your token is now in your clipboard. Paste it into `.env`.

> Note: Discord occasionally changes the desktop app's internals. If the script above doesn't work, use the browser method instead.

---

## Token Expiry and Rotation

Your personal token does **not** expire automatically. However:

- Changing your password invalidates it
- Logging out of all sessions invalidates it
- Discord may invalidate it if suspicious activity is detected

If your exports start failing with authentication errors, re-extract your token using the steps above and update `.env`.

---

## Security Checklist

- [x] Token is in `.env` (not in `config.yaml`, not in git)
- [x] `.env` is in `.gitignore`
- [x] Token is not shared with anyone
- [x] `config.yaml` uses `${DISCORD_TOKEN}` placeholder, not the actual token

If you accidentally committed your token:
1. Immediately change your Discord password (this invalidates the token)
2. Remove the token from git history: `git filter-repo --path .env --invert-paths`
3. Force-push (or contact GitHub support if it's on a public repo)
