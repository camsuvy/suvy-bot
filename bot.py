import discord
from discord.ext import commands, tasks
import asyncio
import random
import os
import json
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))          # YOUR Discord user ID
RESPONSE_TIMEOUT = 5 * 60                            # 5 min to respond before alert
MIN_INTERVAL = 10 * 60                               # 10 min minimum between pings
MAX_INTERVAL = 40 * 60                               # 40 min maximum between pings

SHIFTS = {
    "night":   {"name": "🌙 Night Shift",   "hours": "7 PM – 3 AM",  "start": 19, "end": 3},
    "morning": {"name": "🌅 Morning Shift", "hours": "3 AM – 11 AM", "start": 3,  "end": 11},
    "day":     {"name": "🌆 Day Shift",     "hours": "11 AM – 7 PM", "start": 11, "end": 19},
}

# Channel name → shift key mapping (bot will look for these channel names)
SHIFT_CHANNELS = {
    "night-shift":   "night",
    "morning-shift": "morning",
    "day-shift":     "day",
}

STATS_LOG_CHANNEL = "stats-log"
ALERTS_CHANNEL = "alerts"

# ─── BOT SETUP ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ─── STATE ─────────────────────────────────────────────────────────────────────
# { guild_id: { user_id: { "pending": bool, "ping_msg_id": int, "next_ping": float, "shift": str, "stats": {...} } } }
chatter_state = {}
shift_totals = {}   # { guild_id: { shift: { ppv, revenue, checkins } } }

def get_state(guild_id, user_id):
    if guild_id not in chatter_state:
        chatter_state[guild_id] = {}
    if user_id not in chatter_state[guild_id]:
        chatter_state[guild_id][user_id] = {
            "active": False,
            "pending": False,
            "ping_msg_id": None,
            "next_ping": None,
            "shift": None,
            "name": "",
            "last_checkin": None,
            "stats": {"ppv": 0, "fans": 0, "revenue": 0.0},
        }
    return chatter_state[guild_id][user_id]

def get_shift_totals(guild_id, shift):
    if guild_id not in shift_totals:
        shift_totals[guild_id] = {}
    if shift not in shift_totals[guild_id]:
        shift_totals[guild_id][shift] = {"ppv": 0, "revenue": 0.0, "checkins": 0}
    return shift_totals[guild_id][shift]

def random_interval():
    return random.randint(MIN_INTERVAL, MAX_INTERVAL)

def now_ts():
    return datetime.now(timezone.utc).timestamp()

def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%I:%M %p UTC")

async def get_channel(guild, name):
    return discord.utils.get(guild.text_channels, name=name)

async def get_log_channel(guild):
    return await get_channel(guild, STATS_LOG_CHANNEL)

async def get_alerts_channel(guild):
    return await get_channel(guild, ALERTS_CHANNEL)

# ─── EVENTS ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Suvy Agency Bot is online as {bot.user}")
    monitor_loop.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    guild = message.guild
    if not guild:
        await bot.process_commands(message)
        return

    user_id = message.author.id
    state = get_state(guild.id, user_id)

    # Check if this is a check-in response (message in shift channel while pending)
    channel_name = message.channel.name
    if channel_name in SHIFT_CHANNELS and state.get("pending"):
        content = message.content.strip()

        # Try to parse stats from message
        # Format: "PPV: 5 | Fans: 12 | Rev: $250" or just numbers "5 12 250"
        ppv, fans, rev = parse_stats(content)

        state["pending"] = False
        state["last_checkin"] = now_ts()
        state["stats"] = {"ppv": ppv, "fans": fans, "revenue": rev}
        next_in = random_interval()
        state["next_ping"] = now_ts() + next_in
        next_min = next_in // 60

        # Update shift totals
        shift_key = SHIFT_CHANNELS[channel_name]
        totals = get_shift_totals(guild.id, shift_key)
        totals["ppv"] += ppv
        totals["revenue"] += rev
        totals["checkins"] += 1

        await message.add_reaction("✅")

        # Log to stats channel
        log_ch = await get_log_channel(guild)
        if log_ch:
            embed = discord.Embed(color=0x00ff88)
            embed.set_author(name=f"✅ Check-in — {message.author.display_name}")
            embed.add_field(name="PPVs Sent", value=str(ppv) if ppv else "—", inline=True)
            embed.add_field(name="Fans Online", value=str(fans) if fans else "—", inline=True)
            embed.add_field(name="Revenue", value=f"${rev:.2f}" if rev else "—", inline=True)
            embed.add_field(name="Shift", value=SHIFTS.get(shift_key, {}).get("name", shift_key), inline=True)
            embed.set_footer(text=f"Next ping in ~{next_min} min (random) • {fmt_time(now_ts())}")
            await log_ch.send(embed=embed)

    await bot.process_commands(message)

def parse_stats(text):
    """Parse PPV/fans/revenue from chatter message. Returns (ppv, fans, revenue)."""
    import re
    ppv = fans = 0
    rev = 0.0

    # Try labeled format: ppv: 5, fans: 10, rev: $200
    ppv_match = re.search(r'ppv[:\s]+(\d+)', text, re.IGNORECASE)
    fans_match = re.search(r'fans?[:\s]+(\d+)', text, re.IGNORECASE)
    rev_match = re.search(r'rev(?:enue)?[:\s]*\$?([\d.]+)', text, re.IGNORECASE)

    if ppv_match: ppv = int(ppv_match.group(1))
    if fans_match: fans = int(fans_match.group(1))
    if rev_match: rev = float(rev_match.group(1))

    # Fallback: just 3 numbers in a row "5 12 250"
    if not ppv_match and not fans_match and not rev_match:
        nums = re.findall(r'[\d.]+', text)
        if len(nums) >= 3:
            ppv = int(float(nums[0]))
            fans = int(float(nums[1]))
            rev = float(nums[2])
        elif len(nums) == 1:
            rev = float(nums[0])

    return ppv, fans, rev

# ─── BACKGROUND MONITOR LOOP ───────────────────────────────────────────────────
@tasks.loop(seconds=30)
async def monitor_loop():
    for guild in bot.guilds:
        if guild.id not in chatter_state:
            continue
        for user_id, state in chatter_state[guild.id].items():
            if not state.get("active"):
                continue

            current = now_ts()

            # Time to send a ping?
            if not state["pending"] and state.get("next_ping") and current >= state["next_ping"]:
                shift_key = state.get("shift")
                ch_name = next((k for k, v in SHIFT_CHANNELS.items() if v == shift_key), None)
                if not ch_name:
                    continue
                channel = await get_channel(guild, ch_name)
                if not channel:
                    continue

                member = guild.get_member(user_id)
                if not member:
                    continue

                msg = await channel.send(
                    f"📋 {member.mention} **Check-in time!**\n"
                    f"Reply with your stats: `PPV: X | Fans: X | Rev: $X`\n"
                    f"*(You have 5 minutes to respond)*"
                )
                state["pending"] = True
                state["ping_msg_id"] = msg.id
                state["ping_sent_at"] = current

            # Check for overdue response (pending > 5 min)
            if state.get("pending") and state.get("ping_sent_at"):
                elapsed = current - state["ping_sent_at"]
                if elapsed >= RESPONSE_TIMEOUT and not state.get("alert_sent"):
                    state["alert_sent"] = True

                    # DM the owner
                    owner = guild.get_member(OWNER_ID)
                    if owner:
                        try:
                            await owner.send(
                                f"🚨 **ALERT — {guild.name}**\n"
                                f"**{guild.get_member(user_id).display_name}** hasn't responded to their check-in ping.\n"
                                f"Shift: {SHIFTS.get(state['shift'], {}).get('name', '')}\n"
                                f"Ping sent at: {fmt_time(state['ping_sent_at'])}"
                            )
                        except:
                            pass

                    # Also post in alerts channel
                    alerts_ch = await get_alerts_channel(guild)
                    if alerts_ch:
                        member = guild.get_member(user_id)
                        await alerts_ch.send(
                            f"🚨 **MISSED CHECK-IN**\n"
                            f"{member.mention if member else 'Unknown'} did not respond to their ping.\n"
                            f"Shift: {SHIFTS.get(state['shift'], {}).get('name', '')}"
                        )

# ─── COMMANDS ──────────────────────────────────────────────────────────────────

@bot.command(name="startshift")
@commands.has_permissions(manage_messages=True)
async def start_shift(ctx, member: discord.Member, shift_key: str = None):
    """Start monitoring a chatter. Usage: !startshift @username night"""
    if not shift_key or shift_key not in SHIFTS:
        await ctx.send(f"❌ Specify a shift: `!startshift @user night` (options: night, morning, day)")
        return

    state = get_state(ctx.guild.id, member.id)
    state["active"] = True
    state["shift"] = shift_key
    state["name"] = member.display_name
    state["pending"] = False
    state["alert_sent"] = False
    next_in = random_interval()
    state["next_ping"] = now_ts() + next_in

    embed = discord.Embed(
        title=f"✅ Shift Started — {member.display_name}",
        color=0x00ff88
    )
    embed.add_field(name="Shift", value=SHIFTS[shift_key]["name"], inline=True)
    embed.add_field(name="Hours", value=SHIFTS[shift_key]["hours"], inline=True)
    embed.add_field(name="First ping in", value=f"~{next_in // 60} min (random)", inline=True)
    embed.set_footer(text="Random check-in windows active — chatter cannot predict timing")
    await ctx.send(embed=embed)

    # Notify the chatter
    ch_name = next((k for k, v in SHIFT_CHANNELS.items() if v == shift_key), None)
    shift_ch = await get_channel(ctx.guild, ch_name) if ch_name else None
    if shift_ch:
        await shift_ch.send(
            f"👋 {member.mention} Your shift has started! Stay active — you'll receive random check-in pings.\n"
            f"When pinged, reply with: `PPV: X | Fans: X | Rev: $X`"
        )

@bot.command(name="endshift")
@commands.has_permissions(manage_messages=True)
async def end_shift(ctx, member: discord.Member):
    """End a chatter's shift. Usage: !endshift @username"""
    state = get_state(ctx.guild.id, member.id)
    shift_key = state.get("shift", "")
    totals = get_shift_totals(ctx.guild.id, shift_key)

    state["active"] = False
    state["pending"] = False

    embed = discord.Embed(title=f"⏹ Shift Ended — {member.display_name}", color=0xff6600)
    embed.add_field(name="Shift", value=SHIFTS.get(shift_key, {}).get("name", "—"), inline=True)
    embed.add_field(name="Total Check-ins", value=str(totals["checkins"]), inline=True)
    embed.add_field(name="Total PPVs", value=str(totals["ppv"]), inline=True)
    embed.add_field(name="Total Revenue", value=f"${totals['revenue']:.2f}", inline=True)
    await ctx.send(embed=embed)

    # Log to stats
    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(embed=embed)

@bot.command(name="status")
async def status(ctx):
    """Show current status of all active chatters."""
    if ctx.guild.id not in chatter_state:
        await ctx.send("No chatters being monitored.")
        return

    embed = discord.Embed(title="📊 Active Chatters", color=0x5865F2)
    found = False

    for user_id, state in chatter_state[ctx.guild.id].items():
        if not state.get("active"):
            continue
        found = True
        member = ctx.guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        shift_name = SHIFTS.get(state.get("shift", ""), {}).get("name", "—")
        pending = "⚠️ Waiting for response" if state.get("pending") else "✅ Active"
        next_ping = ""
        if state.get("next_ping") and not state.get("pending"):
            mins_left = max(0, int((state["next_ping"] - now_ts()) // 60))
            next_ping = f" | Next ping: ~{mins_left} min"

        embed.add_field(
            name=name,
            value=f"{shift_name} | {pending}{next_ping}",
            inline=False
        )

    if not found:
        embed.description = "No active chatters right now."

    await ctx.send(embed=embed)

@bot.command(name="shiftreport")
async def shift_report(ctx, shift_key: str = None):
    """Show stats for a shift. Usage: !shiftreport night"""
    if not shift_key or shift_key not in SHIFTS:
        await ctx.send("Usage: `!shiftreport night` (options: night, morning, day)")
        return

    totals = get_shift_totals(ctx.guild.id, shift_key)
    embed = discord.Embed(title=f"📈 Shift Report — {SHIFTS[shift_key]['name']}", color=0x00ff88)
    embed.add_field(name="Total Check-ins", value=str(totals["checkins"]), inline=True)
    embed.add_field(name="Total PPVs", value=str(totals["ppv"]), inline=True)
    embed.add_field(name="Total Revenue", value=f"${totals['revenue']:.2f}", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="resetstats")
@commands.has_permissions(administrator=True)
async def reset_stats(ctx, shift_key: str):
    """Reset stats for a shift. Usage: !resetstats night"""
    if shift_key not in SHIFTS:
        await ctx.send("Options: night, morning, day")
        return
    if ctx.guild.id in shift_totals and shift_key in shift_totals[ctx.guild.id]:
        shift_totals[ctx.guild.id][shift_key] = {"ppv": 0, "revenue": 0.0, "checkins": 0}
    await ctx.send(f"✅ Stats reset for {SHIFTS[shift_key]['name']}")

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="🤖 Suvy Agency Bot — Commands", color=0x5865F2)
    embed.add_field(name="!startshift @user [night/morning/day]", value="Start monitoring a chatter's shift", inline=False)
    embed.add_field(name="!endshift @user", value="End a chatter's shift + show summary", inline=False)
    embed.add_field(name="!status", value="See all active chatters and their status", inline=False)
    embed.add_field(name="!shiftreport [night/morning/day]", value="Show total stats for a shift", inline=False)
    embed.add_field(name="!resetstats [night/morning/day]", value="Reset stats for a shift (admin only)", inline=False)
    embed.add_field(name="─────────────────────────", value="**Chatter check-in format:**\n`PPV: 5 | Fans: 12 | Rev: $180`\nor just: `5 12 180`", inline=False)
    await ctx.send(embed=embed)

# ─── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
