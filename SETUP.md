# Suvy Agency Discord Bot — Setup Guide

## What This Bot Does
- Randomly pings chatters every **10–40 min** (unpredictable)
- If no response in **5 min** → DMs you + posts in #alerts
- Chatters reply with stats → auto-logged to #stats-log
- Full shift reports with PPV, fans, revenue totals

---

## STEP 1 — Create Your Discord Server

1. Open Discord → click **+** (Add a Server) → Create My Own → For me and my friends
2. Name it: **Suvy Agency** (or whatever you want)
3. Create these **text channels** (exact names required):
   - `night-shift`
   - `morning-shift`
   - `day-shift`
   - `stats-log`
   - `alerts`

---

## STEP 2 — Create the Bot

1. Go to **https://discord.com/developers/applications**
2. Click **New Application** → name it "Suvy Monitor Bot" → Create
3. Click **Bot** in the left sidebar
4. Click **Reset Token** → copy the token (save it, you only see it once)
5. Scroll down and enable ALL three **Privileged Gateway Intents**:
   - ✅ Presence Intent
   - ✅ Server Members Intent
   - ✅ Message Content Intent
6. Click **Save Changes**

---

## STEP 3 — Invite Bot to Your Server

1. In the Developer Portal → click **OAuth2** → **URL Generator**
2. Under SCOPES check: `bot`
3. Under BOT PERMISSIONS check:
   - ✅ Send Messages
   - ✅ Read Message History
   - ✅ Mention Everyone
   - ✅ Manage Messages
   - ✅ View Channels
4. Copy the generated URL → paste in browser → select your server → Authorize

---

## STEP 4 — Get Your Discord User ID

1. In Discord → Settings → Advanced → Enable **Developer Mode**
2. Right-click your own username anywhere → **Copy User ID**
3. Save this number — you'll need it in Step 6

---

## STEP 5 — Deploy to Railway (Free, Runs 24/7)

1. Go to **https://railway.app** → Sign up with GitHub (free)
2. Click **New Project** → **Deploy from GitHub repo**
3. Upload these files to a GitHub repo first:
   - `bot.py`
   - `requirements.txt`
   - `Procfile`
4. Select your repo → Railway auto-detects it

---

## STEP 6 — Add Environment Variables on Railway

In your Railway project → **Variables** tab → add:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | The token from Step 2 |
| `OWNER_ID` | Your Discord user ID from Step 4 |

Click **Deploy** → bot goes live.

---

## STEP 7 — Set Up Your Server Permissions

1. In Discord, right-click each shift channel → Edit Channel → Permissions
2. For **@everyone**: deny **Send Messages** (so only chatters in that shift can post)
3. Give each chatter a role (e.g. "Night Chatter") and allow them only in their shift channel
4. Make sure YOU have admin access to all channels

---

## DAILY USAGE

### Starting a shift
```
!startshift @ChatterName night
```
Options: `night` / `morning` / `day`

Bot confirms, pings the chatter in their channel, and starts random timers.

### Ending a shift
```
!endshift @ChatterName
```
Shows a full summary of their shift stats.

### Check who's active
```
!status
```

### See shift totals
```
!shiftreport night
```

### Reset stats for new day
```
!resetstats night
```

---

## CHATTER INSTRUCTIONS (send this to your chatters)

> "When I ping you in your shift channel, reply within 5 minutes with:
> **PPV: X | Fans: X | Rev: $X**
> Example: `PPV: 8 | Fans: 15 | Rev: $220`
> If you don't respond in 5 min, I get an automatic alert."

---

## Channel Structure

```
📁 SUVY AGENCY (Discord Server)
  ├── #night-shift      ← Night chatters work here (7 PM – 3 AM)
  ├── #morning-shift    ← Morning chatters (3 AM – 11 AM)
  ├── #day-shift        ← Day chatters (11 AM – 7 PM)
  ├── #stats-log        ← All check-ins auto-logged here
  └── #alerts           ← Bot posts missed check-in alerts here
```

---

## Troubleshooting

**Bot not responding to commands?**
→ Make sure Message Content Intent is enabled in Developer Portal

**Bot not pinging chatters?**
→ Make sure channels are named exactly: `night-shift`, `morning-shift`, `day-shift`

**Not receiving DM alerts?**
→ Check your OWNER_ID is correct in Railway variables. Must be your actual Discord user ID (18-digit number).
