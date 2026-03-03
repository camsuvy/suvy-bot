import discord
from discord.ext import commands, tasks
import asyncio
import random
import os
import json
import aiohttp
from datetime import datetime, timezone
from dotenv import load_dotenv
import pytz

AGENCY_TZ = pytz.timezone("America/New_York")  # Eastern Time

def now_eastern():
    """Get current time in Eastern timezone."""
    return datetime.now(AGENCY_TZ)

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))          # YOUR Discord user ID
RESPONSE_TIMEOUT = 5 * 60                            # 5 min to respond before alert

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

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─── STATE ─────────────────────────────────────────────────────────────────────
# { guild_id: { user_id: { "pending": bool, "ping_msg_id": int, "next_ping": float, "shift": str, "stats": {...} } } }
chatter_state = {}
shift_totals = {}   # { guild_id: { shift: { ppv, revenue, checkins } } }
weekly_stats = {}  # { guild_id: { user_id: { name, ppv, revenue, checkins } } }
chatter_daily = {}  # { guild_id: { user_id: { revenue, date } } }
last_stats = {}  # { guild_id: { user_id: { ppv, fans, revenue, count } } } — for anti-cheat
roster = {}  # { guild_id: { shift_key: [ user_id, ... ] } }  — expected chatters per shift
end_shift_warned = {}  # { "guild_id_user_id": True } — prevents end shift warning spam
recap_sent_date = {}   # { guild_id: "YYYY-MM-DD" } — prevents double daily recap
weekly_sent_date = {}  # { guild_id: "YYYY-WW" } — prevents double weekly review
strikes = {}  # { guild_id: { user_id: { count, reasons: [] } } }
daily_goal = {}  # { guild_id: { goal: float, current: float, date: str } }
model_weekly_goal = 5000.0  # Default $5k per model per week (Mon-Sat)
milestones_hit = {}  # { guild_id: { model_name: [milestones already celebrated] } }
REVENUE_MILESTONES = [500, 1000, 2500, 5000, 7500, 10000, 15000, 20000]
models = {}  # { guild_id: { model_name: { chatters: [user_id], revenue: float, ppv: int } } }
chatter_model = {}  # { guild_id: { user_id: model_name } }

DATA_FILE = "agency_data.json"

def save_data():
    """Save all persistent data to disk."""
    def int_keys(d):
        """Convert string keys back to ints for guild/user IDs."""
        return d

    data = {
        "weekly_stats":   {str(g): {str(u): v for u, v in ud.items()} for g, ud in weekly_stats.items()},
        "strikes":        {str(g): {str(u): v for u, v in ud.items()} for g, ud in strikes.items()},
        "roster":         {str(g): {sk: [str(u) for u in ul] for sk, ul in sd.items()} for g, sd in roster.items()},
        "models":         {str(g): md for g, md in models.items()},
        "chatter_model":  {str(g): {str(u): m for u, m in ud.items()} for g, ud in chatter_model.items()},
        "milestones_hit": {str(g): mh for g, mh in milestones_hit.items()},
        "daily_goal":     {str(g): dg for g, dg in daily_goal.items()},
    }
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"⚠️ Failed to save data: {e}")

def load_data():
    """Load persistent data from disk on startup."""
    global weekly_stats, strikes, roster, models, chatter_model, milestones_hit, daily_goal
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)

        weekly_stats  = {int(g): {int(u): v for u, v in ud.items()} for g, ud in data.get("weekly_stats", {}).items()}
        strikes       = {int(g): {int(u): v for u, v in ud.items()} for g, ud in data.get("strikes", {}).items()}
        roster        = {int(g): {sk: [int(u) for u in ul] for sk, ul in sd.items()} for g, sd in data.get("roster", {}).items()}
        raw_models = data.get("models", {})
        models = {}
        for g, model_dict in raw_models.items():
            models[int(g)] = {}
            for model_name, model_data in model_dict.items():
                models[int(g)][model_name] = model_data
                # Convert chatter IDs from strings back to ints
                if "chatters" in model_data:
                    models[int(g)][model_name]["chatters"] = [int(u) for u in model_data["chatters"]]
        chatter_model = {int(g): {int(u): m for u, m in ud.items()} for g, ud in data.get("chatter_model", {}).items()}
        milestones_hit = {int(g): mh for g, mh in data.get("milestones_hit", {}).items()}
        daily_goal    = {int(g): dg for g, dg in data.get("daily_goal", {}).items()}
        print("✅ Data loaded from disk.")
    except FileNotFoundError:
        print("ℹ️ No save file found — starting fresh.")
    except Exception as e:
        print(f"⚠️ Failed to load data: {e}")

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

def get_weekly_stats(guild_id, user_id, name=""):
    if guild_id not in weekly_stats:
        weekly_stats[guild_id] = {}
    if user_id not in weekly_stats[guild_id]:
        weekly_stats[guild_id][user_id] = {"name": name, "ppv": 0, "revenue": 0.0, "checkins": 0}
    if name:
        weekly_stats[guild_id][user_id]["name"] = name
    return weekly_stats[guild_id][user_id]

def get_strikes(guild_id, user_id):
    if guild_id not in strikes:
        strikes[guild_id] = {}
    if user_id not in strikes[guild_id]:
        strikes[guild_id][user_id] = {"count": 0, "reasons": []}
    return strikes[guild_id][user_id]

def get_daily_goal(guild_id):
    today = now_eastern().strftime("%Y-%m-%d")
    if guild_id not in daily_goal:
        daily_goal[guild_id] = {"goal": 0.0, "current": 0.0, "date": today}
    # Reset if new day
    if daily_goal[guild_id]["date"] != today:
        daily_goal[guild_id]["current"] = 0.0
        daily_goal[guild_id]["date"] = today
    return daily_goal[guild_id]

def get_chatter_daily(guild_id, user_id):
    today = now_eastern().strftime("%Y-%m-%d")
    if guild_id not in chatter_daily:
        chatter_daily[guild_id] = {}
    if user_id not in chatter_daily[guild_id] or chatter_daily[guild_id][user_id]["date"] != today:
        chatter_daily[guild_id][user_id] = {"revenue": 0.0, "date": today}
    return chatter_daily[guild_id][user_id]

def get_model(guild_id, model_name):
    if guild_id not in models:
        models[guild_id] = {}
    if model_name not in models[guild_id]:
        models[guild_id][model_name] = {"chatters": [], "revenue": 0.0, "ppv": 0}
    return models[guild_id][model_name]

def get_chatter_model(guild_id, user_id):
    return chatter_model.get(guild_id, {}).get(user_id, None)

def get_model_daily_goal(guild_id, model_name):
    """$5k/week ÷ 6 days = daily goal per model."""
    data = models.get(guild_id, {}).get(model_name, {})
    weekly = data.get("weekly_goal", model_weekly_goal)
    return weekly / 6

def get_chatter_daily_goal(guild_id, user_id):
    """Daily goal per chatter = model daily goal ÷ number of chatters on that model."""
    model_name = get_chatter_model(guild_id, user_id)
    if not model_name:
        return 0
    data = models.get(guild_id, {}).get(model_name, {})
    num_chatters = max(1, len(data.get("chatters", [1])))
    return get_model_daily_goal(guild_id, model_name) / num_chatters

def get_overall_daily_goal(guild_id):
    """Sum of all model daily goals."""
    if guild_id not in models or not models[guild_id]:
        return 0
    return sum(get_model_daily_goal(guild_id, m) for m in models[guild_id])

def random_interval():
    """Truly unpredictable ping interval — no detectable pattern."""
    # Pick a random strategy each time so there's zero pattern
    roll = random.random()
    if roll < 0.2:
        return random.randint(5 * 60, 15 * 60)    # 20% chance: very soon (5-15 min)
    elif roll < 0.5:
        return random.randint(15 * 60, 30 * 60)   # 30% chance: normal (15-30 min)
    elif roll < 0.8:
        return random.randint(30 * 60, 50 * 60)   # 30% chance: longer wait (30-50 min)
    else:
        return random.randint(50 * 60, 70 * 60)   # 20% chance: long wait (50-70 min)

def now_ts():
    return now_eastern().timestamp()

def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=AGENCY_TZ).strftime("%I:%M %p ET")

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
    load_data()
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
        ppv, fans, rev, msgs, convos = parse_stats(content)

        state["pending"] = False
        state["last_checkin"] = now_ts()
        state["stats"] = {"ppv": ppv, "fans": fans, "revenue": rev}
        state["alert_sent"] = False
        state["warning_sent"] = False
        next_in = random_interval()
        state["next_ping"] = now_ts() + next_in
        next_min = next_in // 60

        # Update shift totals (shared)
        shift_key = SHIFT_CHANNELS[channel_name]
        totals = get_shift_totals(guild.id, shift_key)
        totals["ppv"] += ppv
        totals["revenue"] += rev
        totals["checkins"] += 1

        # Update per-chatter shift totals
        state["shift_ppv"] = state.get("shift_ppv", 0) + ppv
        state["shift_revenue"] = state.get("shift_revenue", 0.0) + rev
        state["shift_checkins"] = state.get("shift_checkins", 0) + 1

        # Update weekly leaderboard
        w = get_weekly_stats(guild.id, user_id, message.author.display_name)
        w["ppv"]      += ppv
        w["revenue"]  += rev
        w["checkins"] += 1
        w["msgs"]      = w.get("msgs", 0) + msgs
        w["convos"]    = w.get("convos", 0) + convos
        # Track total response time for avg calculation
        if state.get("ping_sent_at"):
            resp_time = now_ts() - state["ping_sent_at"]
            w["total_response_time"] = w.get("total_response_time", 0) + resp_time
            w["response_count"]      = w.get("response_count", 0) + 1

        # Update model stats
        assigned_model = get_chatter_model(guild.id, user_id)
        if assigned_model:
            m = get_model(guild.id, assigned_model)
            m["revenue"] += rev
            m["ppv"] += ppv

            # Check revenue milestones
            if guild.id not in milestones_hit:
                milestones_hit[guild.id] = {}
            if assigned_model not in milestones_hit[guild.id]:
                milestones_hit[guild.id][assigned_model] = []

            total_model_rev = m["revenue"]
            for milestone in REVENUE_MILESTONES:
                if total_model_rev >= milestone and milestone not in milestones_hit[guild.id][assigned_model]:
                    milestones_hit[guild.id][assigned_model].append(milestone)

                    # Pick celebration message
                    if milestone >= 10000:
                        emoji = "🏆💰🎉"
                        msg = f"INCREDIBLE"
                    elif milestone >= 5000:
                        emoji = "🔥💵🔥"
                        msg = "AMAZING"
                    else:
                        emoji = "🎯💰"
                        msg = "LET'S GO"

                    celebration = (
                        f"{emoji} **{msg}! {assigned_model} just hit ${milestone:,}!** {emoji}\n"
                        f"Total revenue: **${total_model_rev:,.2f}**\n"
                        f"Keep pushing! 💪"
                    )

                    # Post in all shift channels
                    for ch_name in SHIFT_CHANNELS:
                        ch = await get_channel(guild, ch_name)
                        if ch:
                            await ch.send(celebration)

                    # Post in stats-log
                    log_ch = await get_log_channel(guild)
                    if log_ch:
                        await log_ch.send(celebration)

                    # DM owner
                    owner = guild.get_member(OWNER_ID)
                    if owner:
                        try:
                            await owner.send(f"🏆 **{assigned_model}** just hit **${milestone:,}** in total revenue!")
                        except:
                            pass

        # Update chatter daily revenue
        cd = get_chatter_daily(guild.id, user_id)
        cd["revenue"] += rev

        # Check chatter daily goal progress
        chatter_goal = get_chatter_daily_goal(guild.id, user_id)
        if chatter_goal > 0:
            pct = (cd["revenue"] / chatter_goal) * 100
            prev_pct = ((cd["revenue"] - rev) / chatter_goal) * 100
            log_ch = await get_log_channel(guild)
            for milestone in [50, 75, 100]:
                if prev_pct < milestone <= pct:
                    if log_ch:
                        if milestone == 100:
                            await log_ch.send(
                                f"🎯 **{message.author.display_name}** hit their daily goal! "
                                f"${cd['revenue']:.2f} / ${chatter_goal:.2f}"
                                + (f" (Model: {assigned_model})" if assigned_model else "")
                            )
                        else:
                            await log_ch.send(
                                f"📈 **{message.author.display_name}** is at {milestone}% of daily goal "
                                f"${cd['revenue']:.2f} / ${chatter_goal:.2f}"
                                + (f" (Model: {assigned_model})" if assigned_model else "")
                            )

        # Update daily goal
        goal_data = get_daily_goal(guild.id)
        goal_data["current"] += rev
        overall_goal = get_overall_daily_goal(guild.id)
        if overall_goal > 0:
            pct = (goal_data["current"] / overall_goal) * 100
            prev_pct = ((goal_data["current"] - rev) / overall_goal) * 100
            for milestone in [50, 75, 100]:
                if prev_pct < milestone <= pct:
                    log_ch = await get_log_channel(guild)
                    if log_ch:
                        if milestone == 100:
                            await log_ch.send(f"🎯 **DAILY GOAL REACHED!** ${goal_data['current']:.2f} / ${overall_goal:.2f} 🎉")
                        else:
                            await log_ch.send(f"📈 **{milestone}% of daily goal reached!** ${goal_data['current']:.2f} / ${overall_goal:.2f}")

        await message.add_reaction("✅")

        # ── ANTI-CHEAT CHECK ─────────────────────────────────────────
        if guild.id not in last_stats:
            last_stats[guild.id] = {}
        prev = last_stats[guild.id].get(user_id, {})
        is_suspicious = False

        if prev and ppv > 0 and fans > 0 and rev > 0:
            if prev.get("ppv") == ppv and prev.get("fans") == fans and prev.get("revenue") == rev:
                new_count = prev.get("count", 1) + 1
                if new_count >= 2:
                    is_suspicious = True
            else:
                new_count = 1
        else:
            new_count = 1

        last_stats[guild.id][user_id] = {"ppv": ppv, "fans": fans, "revenue": rev, "count": new_count}

        if is_suspicious:
            await message.add_reaction("🚨")
            owner = guild.get_member(OWNER_ID)
            if owner:
                try:
                    await owner.send(
                        f"🚨 **Anti-Cheat Alert — {message.author.display_name}**\n"
                        f"Reported identical stats {new_count} times in a row:\n"
                        f"PPV: {ppv} | Fans: {fans} | Rev: ${rev:.2f}\n"
                        f"Shift: {SHIFTS.get(SHIFT_CHANNELS.get(channel_name,''), {}).get('name', '')}"
                    )
                except:
                    pass
            alerts_ch = await get_alerts_channel(guild)
            if alerts_ch:
                await alerts_ch.send(
                    f"⚠️ **POSSIBLE STAT FARMING** — {message.author.mention} reported the exact same stats "
                    f"**{new_count}x in a row** (PPV: {ppv} | Fans: {fans} | Rev: ${rev:.2f})"
                )


        log_ch = await get_log_channel(guild)
        if log_ch:
            # Calculate rev/hr based on shift elapsed time
            shift_start_ts = state.get("shift_start_ts", now_ts())
            elapsed_hrs = max(0.1, (now_ts() - shift_start_ts) / 3600)
            rev_per_hr = get_chatter_daily(guild.id, user_id)["revenue"] / elapsed_hrs

            embed = discord.Embed(color=0x00ff88 if not is_suspicious else 0xff4444)
            embed.set_author(name=f"{'🚨' if is_suspicious else '✅'} Check-in — {message.author.display_name}")
            embed.add_field(name="PPVs Sent",    value=str(ppv)  if ppv  else "—", inline=True)
            embed.add_field(name="Fans Online",  value=str(fans) if fans else "—", inline=True)
            embed.add_field(name="Revenue",      value=f"${rev:.2f}" if rev else "—", inline=True)
            embed.add_field(name="Msgs Sent",    value=str(msgs)   if msgs   else "—", inline=True)
            embed.add_field(name="Active Convos",value=str(convos) if convos else "—", inline=True)
            embed.add_field(name="Rev/hr",       value=f"${rev_per_hr:.2f}", inline=True)
            embed.add_field(name="Shift",        value=SHIFTS.get(shift_key, {}).get("name", shift_key), inline=True)
            assigned_model = get_chatter_model(guild.id, user_id)
            if assigned_model:
                embed.add_field(name="Model", value=assigned_model, inline=True)
            if state.get("ping_sent_at"):
                resp_secs = now_ts() - state["ping_sent_at"]
                embed.add_field(name="Response Time", value=f"{resp_secs//60}m {resp_secs%60}s", inline=True)
            embed.set_footer(text=f"Logged • {fmt_time(now_ts())}")
            await log_ch.send(embed=embed)

        # Simple confirmation in shift channel — no timing info
        await message.reply("✅ Stats logged. Stay active.", mention_author=False)

    await bot.process_commands(message)

def parse_stats(text):
    """Parse PPV/fans/revenue/msgs/convos from chatter message."""
    import re
    ppv = fans = msgs = convos = 0
    rev = 0.0

    ppv_match   = re.search(r'ppv[:\s]+(\d+)', text, re.IGNORECASE)
    fans_match  = re.search(r'fans?[:\s]+(\d+)', text, re.IGNORECASE)
    rev_match   = re.search(r'rev(?:enue)?[:\s]*\$?([\d.]+)', text, re.IGNORECASE)
    msgs_match  = re.search(r'msgs?[:\s]+(\d+)', text, re.IGNORECASE)
    convos_match = re.search(r'convos?[:\s]+(\d+)', text, re.IGNORECASE)

    if ppv_match:   ppv   = int(ppv_match.group(1))
    if fans_match:  fans  = int(fans_match.group(1))
    if rev_match:   rev   = float(rev_match.group(1))
    if msgs_match:  msgs  = int(msgs_match.group(1))
    if convos_match: convos = int(convos_match.group(1))

    # Fallback: 3 numbers in a row
    if not any([ppv_match, fans_match, rev_match]):
        nums = re.findall(r'[\d.]+', text)
        if len(nums) >= 3:
            ppv  = int(float(nums[0]))
            fans = int(float(nums[1]))
            rev  = float(nums[2])
        elif len(nums) == 1:
            rev = float(nums[0])

    return ppv, fans, rev, msgs, convos

# ─── BACKGROUND MONITOR LOOP ───────────────────────────────────────────────────
@tasks.loop(seconds=30)
async def monitor_loop():
    # Auto-save data every 2 loop cycles (~60 seconds)
    if not hasattr(monitor_loop, "_save_counter"):
        monitor_loop._save_counter = 0
    monitor_loop._save_counter += 1
    if monitor_loop._save_counter >= 2:
        monitor_loop._save_counter = 0
        save_data()

    for guild in bot.guilds:
        # Check roster for no-shows (15 min after shift start) — Monday to Saturday only
        now = now_eastern()
        if now.weekday() < 6:
            for shift_key, shift_info in SHIFTS.items():
                start_hour = shift_info["start"]
                if now.hour == start_hour and now.minute == 15:
                    expected = roster.get(guild.id, {}).get(shift_key, [])
                    for user_id in expected:
                        state = chatter_state.get(guild.id, {}).get(user_id, {})
                        active = state.get("active", False)
                        noshow_key = f"noshow_{guild.id}_{user_id}_{shift_key}_{now.strftime('%Y-%m-%d')}"
                        if not active and not end_shift_warned.get(noshow_key):
                            end_shift_warned[noshow_key] = True
                            member = guild.get_member(user_id)
                            owner = guild.get_member(OWNER_ID)
                            if owner and member:
                                try:
                                    await owner.send(
                                        f"🚨 **No-show alert!**\n"
                                        f"**{member.display_name}** was expected for {shift_info['name']} "
                                        f"and hasn't started yet (15 min past shift start)."
                                    )
                                except:
                                    pass
                            alerts_ch = await get_alerts_channel(guild)
                            if alerts_ch and member:
                                await alerts_ch.send(
                                    f"🚨 **NO-SHOW** — {member.mention} hasn't started their "
                                    f"{shift_info['name']} shift (15 min overdue)"
                                )

        # Saturday night shift runs into Sunday 3AM — keep monitoring active chatters on Sunday until 3AM
        is_sunday_before_3am = now.weekday() == 6 and now.hour < 3

        # ── DAILY RECAP DM at 3AM every day ─────────────────────────
        if now.hour == 3 and now.minute == 0 and now.weekday() < 6:
            today_str = now.strftime("%Y-%m-%d")
            if recap_sent_date.get(guild.id) != today_str:
                recap_sent_date[guild.id] = today_str
                owner = guild.get_member(OWNER_ID)
                if owner:
                    overall_goal = get_overall_daily_goal(guild.id)
                    goal_data = get_daily_goal(guild.id)
                    current_rev = goal_data.get("current", 0)
                    pct = min(100, (current_rev / overall_goal * 100)) if overall_goal > 0 else 0

                    recap_lines = [f"📊 **Daily Recap — {now.strftime('%A %b %d')}**\n"]
                    recap_lines.append(f"💰 Total Revenue: ${current_rev:.2f} / ${overall_goal:.2f} ({pct:.0f}%)\n")

                    if guild.id in models and models[guild.id]:
                        for model_name, data in models[guild.id].items():
                            model_rev = sum(get_chatter_daily(guild.id, uid)["revenue"] for uid in data["chatters"])
                            model_goal = get_model_daily_goal(guild.id, model_name)
                            recap_lines.append(f"\n📌 **{model_name}** — ${model_rev:.2f} / ${model_goal:.2f}")
                            for uid in data["chatters"]:
                                m = guild.get_member(uid)
                                if m:
                                    cd = get_chatter_daily(guild.id, uid)
                                    cg = get_chatter_daily_goal(guild.id, uid)
                                    missed = chatter_state.get(guild.id, {}).get(uid, {}).get("missed_checkins", 0)
                                    recap_lines.append(f"  • {m.display_name}: ${cd['revenue']:.2f} / ${cg:.2f} | Missed: {missed}")

                    try:
                        await owner.send("\n".join(recap_lines))
                    except:
                        pass

        # ── WEEKLY PERFORMANCE RATING every Sunday at 3AM ────────────
        if now.weekday() == 6 and now.hour == 3 and now.minute == 0:
            week_str = now.strftime("%Y-%W")
            if weekly_sent_date.get(guild.id) != week_str:
                weekly_sent_date[guild.id] = week_str
                owner = guild.get_member(OWNER_ID)
                if guild.id in weekly_stats and weekly_stats[guild.id]:
                    rating_lines = ["🏆 **Weekly Performance Report**\n"]
                    low_performers = []

                for uid, stats in weekly_stats[guild.id].items():
                    member = guild.get_member(uid)
                    if not member:
                        continue
                    rev      = stats.get("revenue", 0)
                    checkins = stats.get("checkins", 0)
                    msgs     = stats.get("msgs", 0)
                    convos   = stats.get("convos", 0)
                    goal     = get_chatter_daily_goal(guild.id, uid) * 6

                    # Avg response time
                    total_rt = stats.get("total_response_time", 0)
                    rt_count = stats.get("response_count", 0)
                    avg_rt   = total_rt / rt_count if rt_count > 0 else 0
                    avg_rt_str = f"{int(avg_rt//60)}m {int(avg_rt%60)}s" if avg_rt > 0 else "—"

                    # Rev per hour (across full week = 6 days * 8hr shifts)
                    rev_per_hr = rev / 48 if rev > 0 else 0

                    # Msgs per hour
                    msgs_per_hr = msgs / 48 if msgs > 0 else 0

                    pct = (rev / goal * 100) if goal > 0 else 0

                    if pct >= 90:   grade = "🟢 A"
                    elif pct >= 70: grade = "🔵 B"
                    elif pct >= 50: grade = "🟡 C"
                    else:
                        grade = "🔴 D"
                        low_performers.append(member)

                    rating_lines.append(
                        f"{grade} **{stats.get('name', member.display_name)}**\n"
                        f"  💰 Revenue: ${rev:.2f} / ${goal:.2f} ({pct:.0f}%)\n"
                        f"  📨 PPVs: {stats.get('ppv',0)} | 💬 Msgs/hr: {msgs_per_hr:.1f} | 🗣 Convos: {convos}\n"
                        f"  ⚡ Rev/hr: ${rev_per_hr:.2f} | ⏱ Avg Response: {avg_rt_str} | ✅ Check-ins: {checkins}"
                    )
                    # Add OF dashboard stats if manually entered
                    of_line = ""
                    if stats.get("fan_cvr"):
                        of_line += f"  📈 Fan CVR: {stats['fan_cvr']}%"
                    if stats.get("of_response_time"):
                        of_line += f" | 📱 OF Response: {stats['of_response_time']}"
                    if of_line:
                        rating_lines.append(of_line)
                    rating_lines.append("")  # spacer

                if owner:
                    try:
                        # Add pay summary
                        pay_lines = ["\n💸 **Weekly Payouts**"]
                        total_payout = 0.0
                        for uid2, stats2 in weekly_stats[guild.id].items():
                            m2 = guild.get_member(uid2)
                            name2  = stats2.get("name", m2.display_name if m2 else str(uid2))
                            hours2 = stats2.get("hours_worked", 0)
                            rev2   = stats2.get("revenue", 0.0)
                            hourly2 = hours2 * 3.0
                            comm2   = rev2 * 0.025
                            total2  = hourly2 + comm2
                            total_payout += total2
                            pay_lines.append(f"  • {name2}: ${hourly2:.2f} (hrs) + ${comm2:.2f} (comm) = **${total2:.2f}**")
                        pay_lines.append(f"\n💵 **Total to pay out: ${total_payout:.2f}**")
                        await owner.send("\n".join(rating_lines + pay_lines))
                    except:
                        pass

                # Auto-warn low performers
                for member in low_performers:
                    try:
                        await member.send(
                            f"⚠️ **Performance Warning — Suvy Agency**\n"
                            f"Your revenue this week was below 50% of your goal.\n"
                            f"Please improve your performance next week or your position may be reviewed.\n"
                            f"If you have any issues, contact your manager."
                        )
                    except:
                        pass

                # Post in stats-log
                log_ch = await get_log_channel(guild)
                if log_ch:
                    await log_ch.send("\n".join(rating_lines))

        # ── AUTO RESET WEEKLY STATS every Monday at midnight ─────────
        if now.weekday() == 0 and now.hour == 0 and now.minute == 0:
            reset_key = f"reset_{guild.id}_{now.strftime('%Y-%W')}"
            if not end_shift_warned.get(reset_key):
                end_shift_warned[reset_key] = True
                if guild.id in weekly_stats:
                    weekly_stats[guild.id] = {}
                save_data()
                print(f"✅ Weekly stats auto-reset for guild {guild.id}")

        # ── PAY REMINDER every Monday at 9AM ────────────────────────
        if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
            pay_key = f"payreminder_{guild.id}_{now.strftime('%Y-%W')}"
            if not end_shift_warned.get(pay_key):
                end_shift_warned[pay_key] = True
                owner = guild.get_member(OWNER_ID)
                if owner and guild.id in weekly_stats and weekly_stats[guild.id]:
                    pay_lines = ["💸 **Monday Pay Reminder — Suvy Agency**\n"]
                    total_payout = 0.0
                    for uid, stats in weekly_stats[guild.id].items():
                        m = guild.get_member(uid)
                        name = stats.get("name", m.display_name if m else str(uid))
                        hours = stats.get("hours_worked", 0)
                        rev   = stats.get("revenue", 0.0)
                        hourly = hours * 3.0
                        comm   = rev * 0.025
                        total  = hourly + comm
                        total_payout += total
                        pay_lines.append(f"  • {name}: ${hourly:.2f} (hrs) + ${comm:.2f} (comm) = **${total:.2f}**")
                    pay_lines.append(f"\n💵 **Total to pay out: ${total_payout:.2f}**")
                    try:
                        await owner.send("\n".join(pay_lines))
                    except:
                        pass

        # ── MORNING BRIEFING every day at 11AM ──────────────────────
        if now.hour == 11 and now.minute == 0:
            briefing_key = f"briefing_{guild.id}_{now.strftime('%Y-%m-%d')}"
            if not end_shift_warned.get(briefing_key):
                end_shift_warned[briefing_key] = True
                owner = guild.get_member(OWNER_ID)
                if owner:
                    lines = [f"☀️ **Morning Briefing — {now.strftime('%A %b %d')}**\n"]
                    # Overnight shift summary
                    night_total = 0.0
                    night_checkins = 0
                    night_missed = 0
                    if guild.id in chatter_state:
                        for uid, s in chatter_state[guild.id].items():
                            if s.get("shift") == "night":
                                night_total += s.get("shift_revenue", 0)
                                night_checkins += s.get("shift_checkins", 0)
                                night_missed += s.get("missed_checkins", 0)
                    lines.append(f"🌙 Night Shift: ${night_total:.2f} revenue | {night_checkins} check-ins | {night_missed} missed")
                    # Current active chatters
                    active = [s.get("name") for s in chatter_state.get(guild.id, {}).values() if s.get("active")]
                    lines.append(f"🟢 Currently active: {', '.join(active) if active else 'Nobody'}")
                    # Daily goal progress
                    goal_data = get_daily_goal(guild.id)
                    overall_goal = get_overall_daily_goal(guild.id)
                    pct = (goal_data["current"] / overall_goal * 100) if overall_goal > 0 else 0
                    lines.append(f"🎯 Daily goal: ${goal_data['current']:.2f} / ${overall_goal:.2f} ({pct:.0f}%)")
                    try:
                        await owner.send("\n".join(lines))
                    except:
                        pass

        # ── COVERAGE ALERT — no chatter on shift at start time ───────
        if now.weekday() < 6:
            for shift_key, shift_info in SHIFTS.items():
                if now.hour == shift_info["start"] and now.minute == 0:
                    coverage_key = f"coverage_{guild.id}_{shift_key}_{now.strftime('%Y-%m-%d')}"
                    if not end_shift_warned.get(coverage_key):
                        active_on_shift = any(
                            s.get("active") and s.get("shift") == shift_key
                            for s in chatter_state.get(guild.id, {}).values()
                        )
                        if not active_on_shift:
                            end_shift_warned[coverage_key] = True
                            owner = guild.get_member(OWNER_ID)
                            if owner:
                                try:
                                    await owner.send(
                                        f"🚨 **Coverage Alert — Suvy Agency**\n"
                                        f"**{shift_info['name']}** just started and nobody is online.\n"
                                        f"The account is unattended right now."
                                    )
                                except:
                                    pass
                            alerts_ch = await get_alerts_channel(guild)
                            if alerts_ch:
                                await alerts_ch.send(
                                    f"🚨 **{shift_info['name']} started with no chatter online!**"
                                )

        if guild.id not in chatter_state:
            continue
        for user_id, state in chatter_state[guild.id].items():
            if not state.get("active"):
                continue

            # On Sunday after 3AM, stop all monitoring
            if now.weekday() == 6 and now.hour >= 3:
                continue

            current = now_ts()

            # Auto-strike if chatter hasn't ended shift 5 min after shift end time
            shift_key_active = state.get("shift")
            if shift_key_active and shift_key_active in SHIFTS:
                end_hour = SHIFTS[shift_key_active]["end"]
                # Calculate minutes past shift end (handle overnight shifts)
                mins_past_end = 0
                if shift_key_active == "night":
                    # Night shift ends at 3AM next day
                    if now.hour >= 3 and now.hour < 19:
                        mins_past_end = (now.hour - 3) * 60 + now.minute
                elif now.hour > end_hour or (now.hour == end_hour and now.minute > 0):
                    mins_past_end = (now.hour - end_hour) * 60 + now.minute

                warn_key = f"{guild.id}_{user_id}"
                if mins_past_end >= 15 and not end_shift_warned.get(warn_key) and not state.get("active_sale"):
                    end_shift_warned[warn_key] = True  # Set FIRST to prevent spam
                    member = guild.get_member(user_id)
                    if member:
                        next_shift_map = {"night": "morning", "morning": "day", "day": "night"}
                        next_shift = SHIFTS[next_shift_map[shift_key_active]]["name"]
                        try:
                            await member.send(
                                f"⏰ **Your shift has ended — please wrap up!**\n"
                                f"Your {SHIFTS[shift_key_active]['name']} ended 15 minutes ago.\n"
                                f"**{next_shift}** chatter may be waiting to start.\n"
                                f"Type `!endshift @{member.display_name}` in your shift channel now."
                            )
                        except:
                            pass
                        ch_name = next((k for k, v in SHIFT_CHANNELS.items() if v == shift_key_active), None)
                        shift_ch = await get_channel(guild, ch_name) if ch_name else None
                        if shift_ch:
                            await shift_ch.send(
                                f"⏰ {member.mention} Your shift ended 15 minutes ago. "
                                f"Please type `!endshift @{member.display_name}` so the next chatter can start."
                            )

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
                    f"Reply with your stats: `PPV: X | Fans: X | Rev: $X | Msgs: X | Convos: X`"
                )
                state["pending"] = True
                state["ping_msg_id"] = msg.id
                state["ping_sent_at"] = current
                state["warning_sent"] = False
                state["alert_sent"] = False

            # Check for overdue response (pending > 5 min)
            if state.get("pending") and state.get("ping_sent_at"):
                elapsed = current - state["ping_sent_at"]

                # 3 min warning — DM the chatter directly
                if elapsed >= 180 and not state.get("warning_sent"):
                    state["warning_sent"] = True
                    member = guild.get_member(user_id)
                    if member:
                        try:
                            await member.send(
                                f"⚠️ **Check-in reminder** — You have **2 minutes** to respond to your ping in "
                                f"**{guild.name}** or your manager will be alerted."
                            )
                        except:
                            pass

                # 5 min — alert owner
                if elapsed >= RESPONSE_TIMEOUT and not state.get("alert_sent"):
                    state["alert_sent"] = True
                    state["missed_checkins"] = state.get("missed_checkins", 0) + 1

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
async def start_shift(ctx, member: discord.Member = None, shift_key: str = None):
    """Start your shift. Usage: !startshift @yourname night"""
    if member is None or shift_key is None:
        await ctx.send("❌ Usage: `!startshift @YourName night` (options: night, morning, day)")
        return

    # Chatters can only start their OWN shift
    if not ctx.author.guild_permissions.manage_messages and ctx.author.id != member.id:
        await ctx.send("❌ You can only start your own shift.")
        return

    if not shift_key or shift_key not in SHIFTS:
        await ctx.send("❌ Specify a shift: `!startshift @yourname night` (options: night, morning, day)")
        return

    # Check if shift slot is already taken for this model
    assigned_model = get_chatter_model(ctx.guild.id, member.id)
    if assigned_model and ctx.guild.id in models and assigned_model in models[ctx.guild.id]:
        slot_key = f"{assigned_model}_{shift_key}"
        current_slot = models[ctx.guild.id][assigned_model].get("active_slot")
        if current_slot == slot_key:
            for uid, s in chatter_state.get(ctx.guild.id, {}).items():
                if s.get("active") and s.get("shift") == shift_key and get_chatter_model(ctx.guild.id, uid) == assigned_model and uid != member.id:
                    existing = ctx.guild.get_member(uid)
                    name = existing.display_name if existing else "Someone"
                    # Check if previous chatter is past their shift end time
                    end_hour = SHIFTS[shift_key]["end"]
                    now_check = now_eastern()
                    past_end = now_check.hour > end_hour or (now_check.hour == end_hour and now_check.minute >= 0)
                    if not past_end:
                        await ctx.send(f"❌ **{name}** is already working the {SHIFTS[shift_key]['name']} for **{assigned_model}**. Only one chatter per shift per model.")
                        return
                    else:
                        # Auto-end previous chatter's shift silently
                        s["active"] = False
                        s["pending"] = False
                        log_ch = await get_log_channel(ctx.guild)
                        if log_ch:
                            await log_ch.send(f"🔄 **{name}**'s shift auto-ended — **{member.display_name}** has taken over the {SHIFTS[shift_key]['name']} for **{assigned_model}**.")
        models[ctx.guild.id][assigned_model]["active_slot"] = slot_key

    state = get_state(ctx.guild.id, member.id)
    state["active"] = True
    state["shift"] = shift_key
    state["name"] = member.display_name
    state["pending"] = False
    state["alert_sent"] = False
    state["warning_sent"] = False
    state["end_strike_sent"] = False
    state["active_sale"] = False
    state["shift_start_ts"] = now_ts()
    state["shift_ppv"] = 0
    state["shift_revenue"] = 0.0
    state["shift_checkins"] = 0
    state["missed_checkins"] = 0
    end_shift_warned[f"{ctx.guild.id}_{member.id}"] = False
    next_in = random_interval()
    state["next_ping"] = now_ts() + next_in

    embed = discord.Embed(
        title=f"✅ Shift Started — {member.display_name}",
        color=0x00ff88
    )
    embed.add_field(name="Shift", value=SHIFTS[shift_key]["name"], inline=True)
    embed.add_field(name="Hours", value=SHIFTS[shift_key]["hours"], inline=True)

    # Auto-detect if they're late (3 min grace period from shift start)
    now = now_eastern()
    shift_start_hour = SHIFTS[shift_key]["start"]
    current_hour = now.hour
    current_minute = now.minute
    minutes_late = 0

    if shift_key == "night":
        if current_hour == shift_start_hour:
            minutes_late = current_minute  # minutes past 7PM
        elif current_hour > shift_start_hour or current_hour < 3:
            if current_hour < 3:
                minutes_late = (current_hour + 24 - shift_start_hour) * 60 + current_minute
            else:
                minutes_late = (current_hour - shift_start_hour) * 60 + current_minute
    elif current_hour == shift_start_hour:
        minutes_late = current_minute
    elif current_hour > shift_start_hour:
        minutes_late = (current_hour - shift_start_hour) * 60 + current_minute

    if minutes_late > 3:
        embed.add_field(name="⚠️ Late", value=f"{minutes_late} minutes past shift start", inline=False)
        embed.color = 0xffaa00

        # Auto strike
        s = get_strikes(ctx.guild.id, member.id)
        s["count"] += 1
        s["reasons"].append(f"Strike {s['count']}: Late shift start by {minutes_late} min ({SHIFTS[shift_key]['name']})")

        # DM chatter
        try:
            await member.send(
                f"⚠️ **Strike {s['count']}/3 — Suvy Agency**\n"
                f"You started your {SHIFTS[shift_key]['name']} **{minutes_late} minutes late.**\n"
                f"You have a 3 minute grace period. Please be on time next shift."
            )
        except:
            pass

        log_ch = await get_log_channel(ctx.guild)
        if log_ch:
            await log_ch.send(
                f"⏰ **{member.display_name}** started {minutes_late} min late ({SHIFTS[shift_key]['name']}) "
                f"— Strike {s['count']}/3 issued automatically."
            )
        owner = ctx.guild.get_member(OWNER_ID)
        if owner and owner.id != ctx.author.id:
            try:
                await owner.send(
                    f"⏰ **{member.display_name}** started their shift {minutes_late} minutes late.\n"
                    f"Shift: {SHIFTS[shift_key]['name']} | Strike {s['count']}/3 issued automatically."
                )
            except:
                pass
    else:
        embed.set_footer(text="Random check-in windows active — chatter cannot predict timing")
    await ctx.send(embed=embed)

    # Notify the chatter
    ch_name = next((k for k, v in SHIFT_CHANNELS.items() if v == shift_key), None)
    shift_ch = await get_channel(ctx.guild, ch_name) if ch_name else None
    if shift_ch:
        await shift_ch.send(
            f"👋 {member.mention} Your shift has started! Stay active — you'll receive random check-in pings.\n"
            f"When pinged, reply with: `PPV: X | Fans: X | Rev: $X | Msgs: X | Convos: X`\n"
            f"📺 **Join the {SHIFTS[shift_key]['name']} voice channel and share your screen now.**"
        )

@bot.command(name="endshift")
async def end_shift(ctx, member: discord.Member = None):
    """End a chatter's shift. Managers: !endshift @user | Chatters: !endshift @themselves"""
    # If no member specified, assume they mean themselves
    if member is None:
        member = ctx.author

    # Chatters can only end their OWN shift
    if not ctx.author.guild_permissions.manage_messages and ctx.author.id != member.id:
        await ctx.send("❌ You can only end your own shift.")
        return

    state = get_state(ctx.guild.id, member.id)
    if not state.get("active"):
        await ctx.send(f"❌ {member.display_name} doesn't have an active shift.")
        return

    shift_key = state.get("shift", "")

    # Use per-chatter totals for accurate summary
    chatter_ppv = state.get("shift_ppv", 0)
    chatter_rev = state.get("shift_revenue", 0.0)
    chatter_checkins = state.get("shift_checkins", 0)
    chatter_missed = state.get("missed_checkins", 0)
    hours_worked = (now_ts() - state.get("shift_start_ts", now_ts())) / 3600

    # Free up the shift slot for this model
    assigned_model = get_chatter_model(ctx.guild.id, member.id)
    if assigned_model and ctx.guild.id in models and assigned_model in models[ctx.guild.id]:
        slot_key = f"{assigned_model}_{shift_key}"
        if models[ctx.guild.id][assigned_model].get("active_slot") == slot_key:
            models[ctx.guild.id][assigned_model].pop("active_slot", None)

    # Clear end shift warning flag
    warn_key = f"{ctx.guild.id}_{member.id}"
    end_shift_warned.pop(warn_key, None)

    # Track hours worked this week
    shift_start_ts = state.get("shift_start_ts", now_ts())
    hours_worked = (now_ts() - shift_start_ts) / 3600
    w = get_weekly_stats(ctx.guild.id, member.id, member.display_name)
    w["hours_worked"] = w.get("hours_worked", 0) + hours_worked

    state["active"] = False
    state["pending"] = False
    end_shift_warned[f"{ctx.guild.id}_{member.id}"] = False  # Reset warn flag on endshift

    embed = discord.Embed(title=f"⏹ Shift Ended — {member.display_name}", color=0xff6600)
    embed.add_field(name="Shift", value=SHIFTS.get(shift_key, {}).get("name", "—"), inline=True)
    embed.add_field(name="Check-ins", value=str(chatter_checkins), inline=True)
    embed.add_field(name="Missed", value=str(chatter_missed), inline=True)
    embed.add_field(name="PPVs Sent", value=str(chatter_ppv), inline=True)
    embed.add_field(name="Revenue", value=f"${chatter_rev:.2f}", inline=True)
    embed.add_field(name="Hours Worked", value=f"{hours_worked:.1f}hr", inline=True)
    await ctx.send(embed=embed)

    # Post summary to stats-log
    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        summary = discord.Embed(
            title=f"📋 Shift Summary — {member.display_name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        summary.add_field(name="Shift", value=SHIFTS.get(shift_key, {}).get("name", "—"), inline=True)
        summary.add_field(name="Check-ins", value=str(chatter_checkins), inline=True)
        summary.add_field(name="Missed", value=str(chatter_missed), inline=True)
        summary.add_field(name="PPVs Sent", value=str(chatter_ppv), inline=True)
        summary.add_field(name="Revenue", value=f"${chatter_rev:.2f}", inline=True)
        summary.add_field(name="Hours Worked", value=f"{hours_worked:.1f}hr", inline=True)
        avg_rev = chatter_rev / chatter_checkins if chatter_checkins > 0 else 0
        summary.add_field(name="Avg Rev/Check-in", value=f"${avg_rev:.2f}", inline=True)
        if assigned_model:
            summary.add_field(name="Model", value=assigned_model, inline=True)
        summary.set_footer(text="Shift complete")
        await log_ch.send(embed=summary)

@bot.command(name="status")
@commands.has_permissions(manage_messages=True)
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

@bot.command(name="addmodel")
@commands.has_permissions(manage_messages=True)
async def add_model(ctx, *, model_name: str):
    """Add a new model to the system. Usage: !addmodel Mia"""
    get_model(ctx.guild.id, model_name)
    embed = discord.Embed(title=f"✅ Model Added — {model_name}", color=0x00ff88)
    embed.add_field(name="Assigned Chatters", value="None yet — use `!assignchatter @user " + model_name + "`", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="removemodel")
@commands.has_permissions(administrator=True)
async def remove_model(ctx, *, model_name: str):
    """Remove a model from the system. Usage: !removemodel Mia"""
    if ctx.guild.id in models and model_name in models[ctx.guild.id]:
        del models[ctx.guild.id][model_name]
        # Unassign any chatters from this model
        if ctx.guild.id in chatter_model:
            for uid in list(chatter_model[ctx.guild.id].keys()):
                if chatter_model[ctx.guild.id][uid] == model_name:
                    del chatter_model[ctx.guild.id][uid]
        await ctx.send(f"✅ Model **{model_name}** removed and all chatters unassigned.")
    else:
        await ctx.send(f"❌ Model **{model_name}** not found.")

@bot.command(name="models")
@commands.has_permissions(manage_messages=True)
async def list_models(ctx):
    """List all models and their assigned chatters."""
    if ctx.guild.id not in models or not models[ctx.guild.id]:
        await ctx.send("No models added yet. Use `!addmodel Mia` to add one.")
        return

    embed = discord.Embed(title="👥 Models & Chatters", color=0x5865F2)
    for model_name, data in models[ctx.guild.id].items():
        chatter_names = []
        for uid in data["chatters"]:
            m = ctx.guild.get_member(uid)
            if m:
                active = chatter_state.get(ctx.guild.id, {}).get(uid, {}).get("active", False)
                status = "🟢" if active else "⚫"
                chatter_names.append(f"{status} {m.display_name}")
        chatter_list = "\n".join(chatter_names) if chatter_names else "No chatters assigned"
        embed.add_field(
            name=f"📌 {model_name}",
            value=f"{chatter_list}\n💰 ${data['revenue']:.2f} | 📨 {data['ppv']} PPVs",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name="assignchatter")
@commands.has_permissions(manage_messages=True)
async def assign_chatter(ctx, member: discord.Member, *, model_name: str):
    """Assign a chatter to a model. Usage: !assignchatter @user Mia"""
    if ctx.guild.id not in models or model_name not in models[ctx.guild.id]:
        await ctx.send(f"❌ Model **{model_name}** doesn't exist. Add it first with `!addmodel {model_name}`")
        return

    # Remove from previous model if assigned
    if ctx.guild.id in chatter_model and member.id in chatter_model[ctx.guild.id]:
        old_model = chatter_model[ctx.guild.id][member.id]
        if member.id in models[ctx.guild.id].get(old_model, {}).get("chatters", []):
            models[ctx.guild.id][old_model]["chatters"].remove(member.id)

    # Assign to new model
    if ctx.guild.id not in chatter_model:
        chatter_model[ctx.guild.id] = {}
    chatter_model[ctx.guild.id][member.id] = model_name
    if member.id not in models[ctx.guild.id][model_name]["chatters"]:
        models[ctx.guild.id][model_name]["chatters"].append(member.id)

    embed = discord.Embed(title=f"✅ Chatter Assigned", color=0x00ff88)
    embed.add_field(name="Chatter", value=member.display_name, inline=True)
    embed.add_field(name="Model", value=model_name, inline=True)
    embed.set_footer(text="Their check-in stats will now be tracked under this model")
    await ctx.send(embed=embed)

@bot.command(name="unassignchatter")
@commands.has_permissions(manage_messages=True)
async def unassign_chatter(ctx, member: discord.Member):
    """Remove a chatter's model assignment. Usage: !unassignchatter @user"""
    if ctx.guild.id in chatter_model and member.id in chatter_model[ctx.guild.id]:
        old_model = chatter_model[ctx.guild.id][member.id]
        if ctx.guild.id in models and old_model in models[ctx.guild.id]:
            if member.id in models[ctx.guild.id][old_model]["chatters"]:
                models[ctx.guild.id][old_model]["chatters"].remove(member.id)
        del chatter_model[ctx.guild.id][member.id]
        await ctx.send(f"✅ {member.display_name} unassigned from **{old_model}**.")
    else:
        await ctx.send(f"{member.display_name} has no model assignment.")

@bot.command(name="modelstats")
@commands.has_permissions(manage_messages=True)
async def model_stats(ctx, *, model_name: str = None):
    """View revenue stats for a model. Usage: !modelstats Mia"""
    if not model_name:
        await ctx.send("Usage: `!modelstats Mia`")
        return
    if ctx.guild.id not in models or model_name not in models[ctx.guild.id]:
        await ctx.send(f"❌ Model **{model_name}** not found.")
        return

    data = models[ctx.guild.id][model_name]
    embed = discord.Embed(title=f"📊 Model Stats — {model_name}", color=0x5865F2)
    embed.add_field(name="Total Revenue", value=f"${data['revenue']:.2f}", inline=True)
    embed.add_field(name="Total PPVs", value=str(data["ppv"]), inline=True)
    embed.add_field(name="Chatters", value=str(len(data["chatters"])), inline=True)

    # Show each chatter's contribution
    for uid in data["chatters"]:
        m = ctx.guild.get_member(uid)
        if m:
            w = weekly_stats.get(ctx.guild.id, {}).get(uid, {})
            embed.add_field(
                name=m.display_name,
                value=f"💰 ${w.get('revenue', 0):.2f} | 📨 {w.get('ppv', 0)} PPVs this week",
                inline=False
            )
    await ctx.send(embed=embed)

@bot.command(name="addchatter")
@commands.has_permissions(manage_messages=True)
async def add_chatter(ctx, member: discord.Member, shift_key: str, *, model_name: str = None):
    """Add a new chatter to the system. Usage: !addchatter @user night Mia"""
    if shift_key not in SHIFTS:
        await ctx.send("Shift options: night, morning, day")
        return

    # Add to roster
    if ctx.guild.id not in roster:
        roster[ctx.guild.id] = {}
    if shift_key not in roster[ctx.guild.id]:
        roster[ctx.guild.id][shift_key] = []
    if member.id not in roster[ctx.guild.id][shift_key]:
        roster[ctx.guild.id][shift_key].append(member.id)

    # Assign to model if provided
    model_text = "None"
    if model_name:
        if ctx.guild.id not in models or model_name not in models[ctx.guild.id]:
            get_model(ctx.guild.id, model_name)
        if ctx.guild.id not in chatter_model:
            chatter_model[ctx.guild.id] = {}
        chatter_model[ctx.guild.id][member.id] = model_name
        if member.id not in models[ctx.guild.id][model_name]["chatters"]:
            models[ctx.guild.id][model_name]["chatters"].append(member.id)
        model_text = model_name

    embed = discord.Embed(title=f"✅ Chatter Added — {member.display_name}", color=0x00ff88)
    embed.add_field(name="Shift", value=SHIFTS[shift_key]["name"], inline=True)
    embed.add_field(name="Model", value=model_text, inline=True)
    embed.add_field(name="Next Step", value=f"Use `!onboard @{member.display_name} {shift_key}` to send them the welcome message", inline=False)
    await ctx.send(embed=embed)
    save_data()

@bot.command(name="strike")
@commands.has_permissions(manage_messages=True)
async def give_strike(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Give a chatter a strike. Usage: !strike @user [reason]"""
    s = get_strikes(ctx.guild.id, member.id)
    s["count"] += 1
    s["reasons"].append(f"Strike {s['count']}: {reason}")

    color = 0xffaa00 if s["count"] < 3 else 0xff0000
    embed = discord.Embed(title=f"⚠️ Strike {s['count']} — {member.display_name}", color=color)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Strikes", value=f"{s['count']}/3", inline=True)

    if s["count"] >= 3:
        embed.add_field(name="🚨 Status", value="3 strikes — review termination", inline=False)

    await ctx.send(embed=embed)

    # DM the chatter
    try:
        dm_embed = discord.Embed(title=f"⚠️ You have received a strike", color=color)
        dm_embed.add_field(name="Reason", value=reason, inline=False)
        dm_embed.add_field(name="Your Strikes", value=f"{s['count']}/3", inline=True)
        if s["count"] >= 3:
            dm_embed.add_field(name="Warning", value="You have reached 3 strikes. Your position is under review.", inline=False)
        await member.send(embed=dm_embed)
    except:
        pass

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(embed=embed)
    save_data()

@bot.command(name="strikes")
@commands.has_permissions(manage_messages=True)
async def view_strikes(ctx, member: discord.Member = None):
    """View strikes for a chatter. Usage: !strikes @user"""
    if not member:
        # Show all chatters with strikes
        embed = discord.Embed(title="⚠️ Strike Records", color=0xff6600)
        if ctx.guild.id not in strikes or not strikes[ctx.guild.id]:
            embed.description = "No strikes on record."
        else:
            for user_id, data in strikes[ctx.guild.id].items():
                if data["count"] > 0:
                    m = ctx.guild.get_member(user_id)
                    name = m.display_name if m else f"User {user_id}"
                    embed.add_field(name=name, value=f"{data['count']}/3 strikes", inline=True)
        await ctx.send(embed=embed)
        return

    s = get_strikes(ctx.guild.id, member.id)
    embed = discord.Embed(title=f"⚠️ Strikes — {member.display_name}", color=0xff6600)
    embed.add_field(name="Total", value=f"{s['count']}/3", inline=True)
    if s["reasons"]:
        embed.add_field(name="History", value="\n".join(s["reasons"]), inline=False)
    else:
        embed.add_field(name="History", value="No strikes on record.", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="clearstrikes")
@commands.has_permissions(administrator=True)
async def clear_strikes(ctx, member: discord.Member):
    """Clear all strikes for a chatter. Usage: !clearstrikes @user"""
    if ctx.guild.id in strikes and member.id in strikes[ctx.guild.id]:
        strikes[ctx.guild.id][member.id] = {"count": 0, "reasons": []}
    await ctx.send(f"✅ Strikes cleared for {member.display_name}.")
    save_data()

@bot.command(name="setgoal")
@commands.has_permissions(manage_messages=True)
async def set_goal(ctx, amount: float):
    """Set the daily revenue goal. Usage: !setgoal 500"""
    goal_data = get_daily_goal(ctx.guild.id)
    goal_data["goal"] = amount
    embed = discord.Embed(title="🎯 Daily Goal Set", color=0x00ff88)
    embed.add_field(name="Goal", value=f"${amount:.2f}", inline=True)
    embed.add_field(name="Current", value=f"${goal_data['current']:.2f}", inline=True)
    embed.add_field(name="Remaining", value=f"${max(0, amount - goal_data['current']):.2f}", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="goal")
@commands.has_permissions(manage_messages=True)
async def check_goal(ctx):
    """Check progress toward today's revenue goal."""
    overall_goal = get_overall_daily_goal(ctx.guild.id)

    if overall_goal == 0:
        await ctx.send("No models set up yet. Use `!addmodel Mia` to get started.")
        return

    goal_data = get_daily_goal(ctx.guild.id)
    current = goal_data["current"]
    pct = min(100, (current / overall_goal) * 100)
    filled = int(pct / 10)
    bar = "🟩" * filled + "⬛" * (10 - filled)

    embed = discord.Embed(title="🎯 Daily Revenue Goals", color=0x00ff88,
                          timestamp=datetime.now(timezone.utc))

    # Overall
    embed.add_field(
        name="📊 Overall",
        value=f"{bar} {pct:.1f}%\n${current:.2f} / ${overall_goal:.2f} | Remaining: ${max(0, overall_goal - current):.2f}",
        inline=False
    )

    # Per model breakdown
    if ctx.guild.id in models and models[ctx.guild.id]:
        for model_name, data in models[ctx.guild.id].items():
            model_daily = get_model_daily_goal(ctx.guild.id, model_name)
            model_current = sum(
                get_chatter_daily(ctx.guild.id, uid)["revenue"]
                for uid in data["chatters"]
            )
            model_pct = min(100, (model_current / model_daily * 100)) if model_daily > 0 else 0
            model_filled = int(model_pct / 10)
            model_bar = "🟩" * model_filled + "⬛" * (10 - model_filled)

            # Per chatter breakdown under model
            chatter_lines = []
            for uid in data["chatters"]:
                m = ctx.guild.get_member(uid)
                if m:
                    cd = get_chatter_daily(ctx.guild.id, uid)
                    chatter_goal = get_chatter_daily_goal(ctx.guild.id, uid)
                    c_pct = min(100, (cd["revenue"] / chatter_goal * 100)) if chatter_goal > 0 else 0
                    chatter_lines.append(
                        f"{'🟢' if chatter_state.get(ctx.guild.id,{}).get(uid,{}).get('active') else '⚫'} "
                        f"**{m.display_name}** — ${cd['revenue']:.2f} / ${chatter_goal:.2f} ({c_pct:.0f}%)"
                    )

            embed.add_field(
                name=f"📌 {model_name} — {model_bar} {model_pct:.0f}%",
                value=f"${model_current:.2f} / ${model_daily:.2f}\n" + ("\n".join(chatter_lines) if chatter_lines else "No chatters"),
                inline=False
            )

    await ctx.send(embed=embed)

@bot.command(name="setweeklygoal")
@commands.has_permissions(manage_messages=True)
async def set_weekly_goal(ctx, model_name: str, amount: float):
    """Set the weekly revenue goal for a model. Usage: !setweeklygoal Mia 5000"""
    if ctx.guild.id not in models or model_name not in models[ctx.guild.id]:
        await ctx.send(f"❌ Model **{model_name}** not found.")
        return
    models[ctx.guild.id][model_name]["weekly_goal"] = amount
    daily = amount / 6
    num_chatters = max(1, len(models[ctx.guild.id][model_name]["chatters"]))
    per_chatter = daily / num_chatters

    embed = discord.Embed(title=f"🎯 Weekly Goal Set — {model_name}", color=0x00ff88)
    embed.add_field(name="Weekly Goal", value=f"${amount:,.2f}", inline=True)
    embed.add_field(name="Daily Goal", value=f"${daily:,.2f}", inline=True)
    embed.add_field(name="Per Chatter/Day", value=f"${per_chatter:,.2f}", inline=True)
    embed.set_footer(text="Goals auto-split across all assigned chatters")
    await ctx.send(embed=embed)

@bot.command(name="ofstats")
@commands.has_permissions(manage_messages=True)
async def of_stats(ctx, member: discord.Member, cvr: float, response_time: str, *, notes: str = ""):
    """Log OF dashboard stats for a chatter. Usage: !ofstats @user 8.23 5m1s"""
    w = get_weekly_stats(ctx.guild.id, member.id, member.display_name)
    w["fan_cvr"] = cvr
    w["of_response_time"] = response_time
    w["of_notes"] = notes

    embed = discord.Embed(title=f"📊 OF Stats Logged — {member.display_name}", color=0x5865F2)
    embed.add_field(name="Fan CVR", value=f"{cvr}%", inline=True)
    embed.add_field(name="Response Time", value=response_time, inline=True)
    if notes:
        embed.add_field(name="Notes", value=notes, inline=False)
    embed.set_footer(text="Will appear in Sunday weekly review")
    await ctx.send(embed=embed)

@bot.command(name="pay")
@commands.has_permissions(manage_messages=True)
@commands.has_permissions(manage_messages=True)
async def pay(ctx):
    """Show weekly payout for all chatters. Usage: !pay"""
    HOURLY_RATE = 3.00
    COMMISSION_RATE = 0.025  # 2.5%

    if ctx.guild.id not in weekly_stats or not weekly_stats[ctx.guild.id]:
        await ctx.send("No weekly stats yet.")
        return

    embed = discord.Embed(
        title="💸 Weekly Payout Sheet",
        color=0xFFD700,
        timestamp=datetime.now(timezone.utc)
    )

    total_payout = 0.0

    # Group by model
    model_groups = {}
    for uid, stats in weekly_stats[ctx.guild.id].items():
        model_name = get_chatter_model(ctx.guild.id, uid) or "Unassigned"
        if model_name not in model_groups:
            model_groups[model_name] = []
        model_groups[model_name].append((uid, stats))

    for model_name, chatters in model_groups.items():
        model_lines = []
        for uid, stats in chatters:
            member = ctx.guild.get_member(uid)
            name = stats.get("name", member.display_name if member else str(uid))

            hours  = stats.get("hours_worked", 0)
            rev    = stats.get("revenue", 0.0)

            hourly_pay    = hours * HOURLY_RATE
            commission    = rev * COMMISSION_RATE
            total         = hourly_pay + commission
            total_payout += total

            model_lines.append(
                f"**{name}**\n"
                f"  ⏱ {hours:.1f}hrs × $3 = ${hourly_pay:.2f}\n"
                f"  💰 ${rev:.2f} × 2.5% = ${commission:.2f}\n"
                f"  **Total: ${total:.2f}**"
            )

        embed.add_field(
            name=f"📌 {model_name}",
            value="\n\n".join(model_lines) if model_lines else "No data",
            inline=False
        )

    embed.add_field(
        name="─────────────────",
        value=f"💵 **Total to pay out this week: ${total_payout:.2f}**",
        inline=False
    )
    embed.set_footer(text="$3/hr base + 2.5% commission on all revenue")
    await ctx.send(embed=embed)

@bot.command(name="performance")
@commands.has_permissions(manage_messages=True)
async def performance(ctx):
    """Show current weekly performance ratings for all chatters."""
    if ctx.guild.id not in weekly_stats or not weekly_stats[ctx.guild.id]:
        await ctx.send("No stats yet this week.")
        return

    embed = discord.Embed(title="📊 Weekly Performance Ratings", color=0x5865F2,
                          timestamp=datetime.now(timezone.utc))

    for uid, stats in weekly_stats[ctx.guild.id].items():
        member = ctx.guild.get_member(uid)
        if not member:
            continue
        rev      = stats.get("revenue", 0)
        checkins = stats.get("checkins", 0)
        msgs     = stats.get("msgs", 0)
        goal     = get_chatter_daily_goal(ctx.guild.id, uid) * 6
        pct      = (rev / goal * 100) if goal > 0 else 0

        total_rt = stats.get("total_response_time", 0)
        rt_count = stats.get("response_count", 0)
        avg_rt   = total_rt / rt_count if rt_count > 0 else 0
        avg_rt_str = f"{int(avg_rt//60)}m {int(avg_rt%60)}s" if avg_rt > 0 else "—"
        rev_per_hr   = rev / 48 if rev > 0 else 0
        msgs_per_hr  = msgs / 48 if msgs > 0 else 0

        if pct >= 90:   grade = "🟢 A"
        elif pct >= 70: grade = "🔵 B"
        elif pct >= 50: grade = "🟡 C"
        else:           grade = "🔴 D"

        embed.add_field(
            name=f"{grade} {stats.get('name', member.display_name)}",
            value=(
                f"💰 ${rev:.2f} / ${goal:.2f} ({pct:.0f}%)\n"
                f"📨 PPVs: {stats.get('ppv',0)} | 💬 Msgs/hr: {msgs_per_hr:.1f}\n"
                f"⚡ Rev/hr: ${rev_per_hr:.2f} | ⏱ Avg Response: {avg_rt_str}\n"
                f"✅ Check-ins: {checkins}"
            ),
            inline=False
        )

    embed.set_footer(text="A=90%+ | B=70%+ | C=50%+ | D=Below 50% of weekly goal")
    await ctx.send(embed=embed)

@bot.command(name="milestones")
@commands.has_permissions(manage_messages=True)
async def show_milestones(ctx):
    """Show revenue milestone progress for all models."""
    if ctx.guild.id not in models or not models[ctx.guild.id]:
        await ctx.send("No models set up yet.")
        return

    embed = discord.Embed(title="🏆 Revenue Milestones", color=0xFFD700,
                          timestamp=datetime.now(timezone.utc))

    for model_name, data in models[ctx.guild.id].items():
        total = data.get("revenue", 0)
        hit = milestones_hit.get(ctx.guild.id, {}).get(model_name, [])

        # Find next milestone
        next_milestone = next((m for m in REVENUE_MILESTONES if m > total), None)
        remaining = f"${next_milestone - total:,.2f} until ${next_milestone:,}" if next_milestone else "All milestones hit! 🏆"

        milestone_line = ""
        for m in REVENUE_MILESTONES:
            if m in hit:
                milestone_line += f"✅ ${m:,}  "
            elif m == next_milestone:
                pct = min(100, (total / m) * 100)
                milestone_line += f"🔜 ${m:,} ({pct:.0f}%)  "
            else:
                milestone_line += f"⬜ ${m:,}  "

        embed.add_field(
            name=f"📌 {model_name} — ${total:,.2f} total",
            value=f"{milestone_line}\n📍 {remaining}",
            inline=False
        )

    await ctx.send(embed=embed)

@bot.command(name="resetmilestones")
@commands.has_permissions(administrator=True)
async def reset_milestones(ctx, *, model_name: str):
    """Reset milestones for a model. Usage: !resetmilestones Mia"""
    if ctx.guild.id in milestones_hit and model_name in milestones_hit[ctx.guild.id]:
        milestones_hit[ctx.guild.id][model_name] = []
    await ctx.send(f"✅ Milestones reset for **{model_name}**.")

@bot.command(name="swapshift")
@commands.has_permissions(manage_messages=True)
async def swap_shift(ctx, chatter1: discord.Member, chatter2: discord.Member):
    """Swap two chatters' assigned shifts. Usage: !swapshift @chatter1 @chatter2"""
    if ctx.guild.id not in roster:
        await ctx.send("❌ No roster set up yet.")
        return

    # Find each chatter's current shift in roster
    shift1 = next((sk for sk, uids in roster[ctx.guild.id].items() if chatter1.id in uids), None)
    shift2 = next((sk for sk, uids in roster[ctx.guild.id].items() if chatter2.id in uids), None)

    if not shift1 or not shift2:
        await ctx.send("❌ Both chatters must be on the roster to swap. Use `!addtoroster` first.")
        return

    # Swap in roster
    roster[ctx.guild.id][shift1].remove(chatter1.id)
    roster[ctx.guild.id][shift2].remove(chatter2.id)
    roster[ctx.guild.id][shift1].append(chatter2.id)
    roster[ctx.guild.id][shift2].append(chatter1.id)

    embed = discord.Embed(title="🔄 Shift Swap Confirmed", color=0x00ff88)
    embed.add_field(name=chatter1.display_name, value=f"{SHIFTS[shift1]['name']} → {SHIFTS[shift2]['name']}", inline=True)
    embed.add_field(name=chatter2.display_name, value=f"{SHIFTS[shift2]['name']} → {SHIFTS[shift1]['name']}", inline=True)
    await ctx.send(embed=embed)

    for member, old, new in [(chatter1, shift1, shift2), (chatter2, shift2, shift1)]:
        try:
            await member.send(
                f"🔄 **Shift Swap — Suvy Agency**\n"
                f"Old: {SHIFTS[old]['name']} ({SHIFTS[old]['hours']})\n"
                f"New: {SHIFTS[new]['name']} ({SHIFTS[new]['hours']})\n"
                f"Be on time for your new shift."
            )
        except:
            pass

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(embed=embed)

@bot.command(name="doubleshift")
@commands.has_permissions(manage_messages=True)
async def double_shift(ctx, member: discord.Member, shift1: str, shift2: str):
    """Approve a chatter to cover two shifts. Usage: !doubleshift @user night morning"""
    if shift1 not in SHIFTS or shift2 not in SHIFTS:
        await ctx.send("❌ Options: night, morning, day")
        return

    if ctx.guild.id not in roster:
        roster[ctx.guild.id] = {}
    for sk in [shift1, shift2]:
        if sk not in roster[ctx.guild.id]:
            roster[ctx.guild.id][sk] = []
        if member.id not in roster[ctx.guild.id][sk]:
            roster[ctx.guild.id][sk].append(member.id)

    embed = discord.Embed(title=f"⚡ Double Shift — {member.display_name}", color=0xFFD700)
    embed.add_field(name="Shift 1", value=f"{SHIFTS[shift1]['name']} ({SHIFTS[shift1]['hours']})", inline=True)
    embed.add_field(name="Shift 2", value=f"{SHIFTS[shift2]['name']} ({SHIFTS[shift2]['hours']})", inline=True)
    embed.add_field(name="Pay", value="All hours tracked automatically", inline=False)
    await ctx.send(embed=embed)

    try:
        await member.send(
            f"⚡ **Double Shift Approved — Suvy Agency**\n"
            f"1. {SHIFTS[shift1]['name']} ({SHIFTS[shift1]['hours']})\n"
            f"2. {SHIFTS[shift2]['name']} ({SHIFTS[shift2]['hours']})\n"
            f"Start each shift normally with `!startshift @{member.display_name} [shift]`"
        )
    except:
        pass

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(embed=embed)

@bot.command(name="activesale")
async def active_sale(ctx):
    """Tell the bot you're closing a sale past shift end. Usage: !activesale"""
    state = get_state(ctx.guild.id, ctx.author.id)
    if not state.get("active"):
        await ctx.send("❌ You don't have an active shift.")
        return

    state["active_sale"] = True
    state["end_strike_sent"] = False

    await ctx.send(
        f"✅ {ctx.author.mention} Active sale noted — no warnings will be sent.\n"
        f"Type `!endshift @{ctx.author.display_name}` when you're done."
    )

    owner = ctx.guild.get_member(OWNER_ID)
    if owner:
        try:
            await owner.send(
                f"💰 **{ctx.author.display_name}** is staying past their shift to close a sale.\n"
                f"Shift: {SHIFTS.get(state.get('shift',''), {}).get('name', '')}"
            )
        except:
            pass

@bot.command(name="excuselate")
@commands.has_permissions(manage_messages=True)
async def excuse_late(ctx, member: discord.Member, *, reason: str = "Excused by manager"):
    """Remove a late strike from a chatter and set their shift start to on time. Usage: !excuselate @user reason"""
    s = get_strikes(ctx.guild.id, member.id)

    # Remove the most recent late strike
    late_strike = next((r for r in reversed(s["reasons"]) if "Late shift start" in r), None)
    if late_strike:
        s["reasons"].remove(late_strike)
        s["count"] = max(0, s["count"] - 1)
        removed = True
    else:
        removed = False

    embed = discord.Embed(title=f"✅ Late Excused — {member.display_name}", color=0x00ff88)
    if removed:
        embed.add_field(name="Strike Removed", value=late_strike, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Current Strikes", value=f"{s['count']}/3", inline=True)
    else:
        embed.add_field(name="Note", value="No late strike found to remove.", inline=False)
    await ctx.send(embed=embed)

    # DM the chatter
    try:
        await member.send(
            f"✅ **Late Excused — Suvy Agency**\n"
            f"Your late start has been excused by your manager.\n"
            f"Reason: {reason}\n"
            f"Current strikes: {s['count']}/3"
        )
    except:
        pass

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(
            f"✅ **{member.display_name}**'s late start excused by {ctx.author.display_name}. "
            f"Reason: {reason} | Strikes: {s['count']}/3"
        )

@bot.command(name="addtoroster")
@commands.has_permissions(manage_messages=True)
async def add_to_roster(ctx, member: discord.Member, shift_key: str):
    """Add a chatter to the expected roster for a shift. Usage: !addtoroster @user night"""
    if shift_key not in SHIFTS:
        await ctx.send("Options: night, morning, day")
        return
    if ctx.guild.id not in roster:
        roster[ctx.guild.id] = {}
    if shift_key not in roster[ctx.guild.id]:
        roster[ctx.guild.id][shift_key] = []
    if member.id not in roster[ctx.guild.id][shift_key]:
        roster[ctx.guild.id][shift_key].append(member.id)
    await ctx.send(f"✅ {member.display_name} added to {SHIFTS[shift_key]['name']} roster.")
    save_data()

@bot.command(name="removefromroster")
@commands.has_permissions(manage_messages=True)
async def remove_from_roster(ctx, member: discord.Member, shift_key: str):
    """Remove a chatter from a shift roster. Usage: !removefromroster @user night"""
    if shift_key not in SHIFTS:
        await ctx.send("Options: night, morning, day")
        return
    if ctx.guild.id in roster and shift_key in roster.get(ctx.guild.id, {}):
        roster[ctx.guild.id][shift_key] = [
            uid for uid in roster[ctx.guild.id][shift_key] if uid != member.id
        ]
    await ctx.send(f"✅ {member.display_name} removed from {SHIFTS[shift_key]['name']} roster.")
    save_data()

@bot.command(name="roster")
@commands.has_permissions(manage_messages=True)
async def show_roster(ctx):
    """Show the current shift roster."""
    embed = discord.Embed(title="📋 Shift Roster", color=0x5865F2)
    if ctx.guild.id not in roster or not roster[ctx.guild.id]:
        embed.description = "No roster set up yet. Use `!addtoroster @user shift`"
    else:
        for shift_key, user_ids in roster[ctx.guild.id].items():
            if user_ids:
                names = []
                for uid in user_ids:
                    m = ctx.guild.get_member(uid)
                    active = chatter_state.get(ctx.guild.id, {}).get(uid, {}).get("active", False)
                    status = "🟢" if active else "🔴"
                    names.append(f"{status} {m.display_name if m else uid}")
                embed.add_field(
                    name=SHIFTS[shift_key]["name"],
                    value="\n".join(names),
                    inline=False
                )
    await ctx.send(embed=embed)

@bot.command(name="sick")
@commands.has_permissions(manage_messages=True)
async def sick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Mark a chatter as sick/absent. Usage: !sick @user [reason]"""
    state = get_state(ctx.guild.id, member.id)
    state["active"] = False
    state["pending"] = False

    embed = discord.Embed(title=f"🤒 Absent — {member.display_name}", color=0xff4444)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Shift", value=SHIFTS.get(state.get("shift",""), {}).get("name", "—"), inline=True)
    embed.set_footer(text=f"Logged at {fmt_time(now_ts())}")
    await ctx.send(embed=embed)

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(embed=embed)

    # DM owner
    owner = ctx.guild.get_member(OWNER_ID)
    if owner and owner.id != ctx.author.id:
        try:
            await owner.send(f"🤒 **{member.display_name}** has called in absent.\nReason: {reason}")
        except:
            pass

@bot.command(name="late")
@commands.has_permissions(manage_messages=True)
async def late(ctx, member: discord.Member, minutes: int = 15):
    """Mark a chatter as late and delay their shift start. Usage: !late @user [minutes]"""
    state = get_state(ctx.guild.id, member.id)

    embed = discord.Embed(title=f"⏰ Late — {member.display_name}", color=0xffaa00)
    embed.add_field(name="Grace Period", value=f"{minutes} minutes", inline=True)
    embed.add_field(name="Shift", value=SHIFTS.get(state.get("shift",""), {}).get("name", "—"), inline=True)
    embed.set_footer(text="First check-in ping delayed")
    await ctx.send(embed=embed)

    # Push back their next ping
    if state.get("active"):
        state["next_ping"] = now_ts() + (minutes * 60)

    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        await log_ch.send(embed=embed)

    # DM owner
    owner = ctx.guild.get_member(OWNER_ID)
    if owner and owner.id != ctx.author.id:
        try:
            await owner.send(f"⏰ **{member.display_name}** is running {minutes} minutes late.")
        except:
            pass

@bot.command(name="onboard")
@commands.has_permissions(manage_messages=True)
async def onboard(ctx, member: discord.Member, shift_key: str = None):
    """Send onboarding message to a new chatter. Usage: !onboard @user night"""
    if not shift_key or shift_key not in SHIFTS:
        await ctx.send("Usage: `!onboard @user night` (options: night, morning, day)")
        return

    shift_info = SHIFTS[shift_key]

    embed = discord.Embed(
        title=f"👋 Welcome to Suvy Agency, {member.display_name}!",
        color=0x5865F2
    )
    embed.add_field(name="Your Shift", value=f"{shift_info['name']} ({shift_info['hours']})", inline=False)
    embed.add_field(
        name="How Check-ins Work",
        value="The bot will ping you randomly during your shift.\nReply promptly with your stats.\nNo response = manager gets alerted immediately.",
        inline=False
    )
    embed.add_field(
        name="Check-in Format",
        value="`PPV: X | Fans: X | Rev: $X | Msgs: X | Convos: X`\nExample: `PPV: 8 | Fans: 22 | Rev: $180`",
        inline=False
    )
    embed.add_field(
        name="Screen Share",
        value=f"Join the **{shift_info['name']} voice channel** and share your screen at the start of every shift.",
        inline=False
    )
    embed.add_field(
        name="Rules",
        value="✅ Always be online during your shift hours\n✅ Respond to pings immediately\n✅ Keep your stats accurate\n❌ Going AFK without notice = strike",
        inline=False
    )
    embed.set_footer(text="Welcome to the team 🚀")

    # Send in their shift channel
    ch_name = next((k for k, v in SHIFT_CHANNELS.items() if v == shift_key), None)
    shift_ch = await get_channel(ctx.guild, ch_name) if ch_name else None
    if shift_ch:
        await shift_ch.send(f"{member.mention}", embed=embed)
        await ctx.send(f"✅ Onboarding message sent to {member.display_name} in {shift_ch.mention}")
    else:
        await ctx.send(embed=embed)


async def leaderboard(ctx):
    """Show weekly leaderboard. Usage: !leaderboard"""
    if ctx.guild.id not in weekly_stats or not weekly_stats[ctx.guild.id]:
        await ctx.send("No weekly stats yet. Stats build up as chatters complete check-ins.")
        return

    sorted_chatters = sorted(
        weekly_stats[ctx.guild.id].items(),
        key=lambda x: x[1]["revenue"],
        reverse=True
    )

    embed = discord.Embed(title="🏆 Weekly Leaderboard", color=0xFFD700,
                          timestamp=datetime.now(timezone.utc))

    medals = ["🥇", "🥈", "🥉"]
    for i, (user_id, stats) in enumerate(sorted_chatters[:10]):
        medal = medals[i] if i < 3 else f"#{i+1}"
        embed.add_field(
            name=f"{medal} {stats['name']}",
            value=f"💰 ${stats['revenue']:.2f} | 📨 {stats['ppv']} PPVs | ✅ {stats['checkins']} check-ins",
            inline=False
        )

    embed.set_footer(text="Resets with !resetweekly")
    await ctx.send(embed=embed)

@bot.command(name="resetweekly")
@commands.has_permissions(administrator=True)
async def reset_weekly(ctx):
    """Reset weekly leaderboard stats."""
    if ctx.guild.id in weekly_stats:
        weekly_stats[ctx.guild.id] = {}
    await ctx.send("✅ Weekly leaderboard has been reset.")
    save_data()

@bot.command(name="shiftreport")
@commands.has_permissions(manage_messages=True)
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
    embed.add_field(name="── MODELS ──────────────────", value="\u200b", inline=False)
    embed.add_field(name="!addmodel [name]", value="Add a new model to the system", inline=False)
    embed.add_field(name="!removemodel [name]", value="Remove a model (admin only)", inline=False)
    embed.add_field(name="!models", value="List all models and their assigned chatters", inline=False)
    embed.add_field(name="!modelstats [name]", value="View revenue stats for a model", inline=False)
    embed.add_field(name="── CHATTERS ────────────────", value="\u200b", inline=False)
    embed.add_field(name="!addchatter @user [shift] [model]", value="Add a new chatter to roster + assign to model", inline=False)
    embed.add_field(name="!assignchatter @user [model]", value="Assign a chatter to a model", inline=False)
    embed.add_field(name="!unassignchatter @user", value="Remove a chatter's model assignment", inline=False)
    embed.add_field(name="── SHIFTS ──────────────────", value="\u200b", inline=False)
    embed.add_field(name="!startshift @user [night/morning/day]", value="Start monitoring a chatter's shift", inline=False)
    embed.add_field(name="!endshift @user", value="End a chatter's shift + show summary", inline=False)
    embed.add_field(name="!status", value="See all active chatters and their status", inline=False)
    embed.add_field(name="!shiftreport [night/morning/day]", value="Show total stats for a shift", inline=False)
    embed.add_field(name="!addtoroster @user [shift]", value="Add chatter to expected roster — bot alerts if they no-show", inline=False)
    embed.add_field(name="!removefromroster @user [shift]", value="Remove chatter from roster", inline=False)
    embed.add_field(name="!roster", value="Show all expected chatters per shift (🟢 active / 🔴 not started)", inline=False)
    embed.add_field(name="!strike @user [reason]", value="Give a chatter a strike — DMs them + logs it", inline=False)
    embed.add_field(name="!strikes [@user]", value="View strikes for one chatter or everyone", inline=False)
    embed.add_field(name="!clearstrikes @user", value="Clear all strikes for a chatter (admin only)", inline=False)
    embed.add_field(name="!setgoal [amount]", value="Set today's revenue goal (e.g. !setgoal 500)", inline=False)
    embed.add_field(name="!goal", value="Check progress toward today's revenue goal", inline=False)
    embed.add_field(name="!sick @user [reason]", value="Mark chatter as absent, logs it and alerts owner", inline=False)
    embed.add_field(name="!late @user [minutes]", value="Mark chatter as late, delays their first ping", inline=False)
    embed.add_field(name="!onboard @user [shift]", value="Send welcome/rules message to a new chatter", inline=False)
    embed.add_field(name="!leaderboard", value="Show weekly top chatters by revenue", inline=False)
    embed.add_field(name="!resetweekly", value="Reset the weekly leaderboard (admin only)", inline=False)
    embed.add_field(name="!resetstats [night/morning/day]", value="Reset stats for a shift (admin only)", inline=False)
    embed.add_field(name="─────────────────────────", value="**Chatter check-in format:**\n`PPV: 5 | Fans: 12 | Rev: $180`\nor just: `5 12 180`", inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.message.delete()
        try:
            await ctx.author.send(
                f"❌ You don't have permission to use that command.\n"
                f"Your available commands are:\n"
                f"`!startshift @YourName night/morning/day`\n"
                f"`!endshift @YourName`\n"
                f"`!activesale`"
            )
        except:
            pass
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands silently

@bot.command(name="diagnose")
@commands.has_permissions(administrator=True)
async def diagnose(ctx):
    """AI scans the bot state and auto-fixes any problems it finds."""
    await ctx.send("🔍 Running AI diagnosis... give me a moment.")

    now = now_eastern()
    guild = ctx.guild

    # ── Build a full snapshot of current bot state ──────────────────
    state_snapshot = []

    # Active chatters
    for uid, s in chatter_state.get(guild.id, {}).items():
        if not s.get("active"):
            continue
        member = guild.get_member(uid)
        name = member.display_name if member else f"Unknown({uid})"
        shift = s.get("shift", "?")
        shift_info = SHIFTS.get(shift, {})
        start_ts = s.get("shift_start_ts", 0)
        hours_active = (now_ts() - start_ts) / 3600 if start_ts else 0
        pending_since = (now_ts() - s.get("ping_sent_at", now_ts())) / 60 if s.get("pending") else 0
        last_checkin_mins = (now_ts() - s.get("last_checkin", now_ts())) / 60 if s.get("last_checkin") else None

        state_snapshot.append({
            "user_id": uid,
            "name": name,
            "shift": shift,
            "shift_hours": shift_info.get("hours", "?"),
            "shift_end_hour": shift_info.get("end"),
            "hours_active": round(hours_active, 2),
            "pending_ping": s.get("pending", False),
            "pending_since_mins": round(pending_since, 1) if s.get("pending") else 0,
            "last_checkin_mins_ago": round(last_checkin_mins, 1) if last_checkin_mins is not None else None,
            "active_sale": s.get("active_sale", False),
            "shift_revenue": s.get("shift_revenue", 0),
            "shift_checkins": s.get("shift_checkins", 0),
            "missed_checkins": s.get("missed_checkins", 0),
        })

    # Current time context
    context = {
        "current_time_eastern": now.strftime("%A %I:%M %p ET"),
        "current_hour": now.hour,
        "active_chatters": state_snapshot,
        "total_active": len(state_snapshot),
    }

    prompt = f"""You are a bot diagnostic AI for an OnlyFans chatting agency management Discord bot.

Current bot state:
{json.dumps(context, indent=2)}

Shift schedule:
- Night shift: 7PM - 3AM
- Morning shift: 3AM - 11AM  
- Day shift: 11AM - 7PM

Analyze the current state and identify ANY of these problems:
1. Chatter has been active for longer than 8 hours (their shift should have ended)
2. Chatter has a ping pending for more than 10 minutes (stuck pending state)
3. Chatter is active on the wrong shift for the current time (e.g. night shift chatter active at 2PM)
4. Any other anomalies you detect

For EACH problem found, specify:
- The user_id of the affected chatter
- What the problem is
- What fix to apply (one of: "force_end_shift", "clear_pending", or "no_fix_needed")

Respond ONLY in this exact JSON format with no other text:
{{
  "problems_found": [
    {{
      "user_id": 123456789,
      "name": "ChatterName",
      "problem": "description of problem",
      "fix": "force_end_shift"
    }}
  ],
  "summary": "one sentence summary"
}}

If no problems found, return problems_found as an empty array."""

    # ── Call Anthropic API ───────────────────────────────────────────
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": os.getenv("ANTHROPIC_API_KEY"),
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-opus-4-6",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            ) as resp:
                data = await resp.json()
                raw = data["content"][0]["text"].strip()
    except Exception as e:
        await ctx.send(f"❌ AI call failed: {e}")
        return

    # ── Parse AI response ────────────────────────────────────────────
    try:
        result = json.loads(raw)
    except:
        await ctx.send(f"❌ AI returned unreadable response:\n```{raw[:500]}```")
        return

    problems = result.get("problems_found", [])
    summary = result.get("summary", "Diagnosis complete.")

    if not problems:
        await ctx.send(f"✅ **AI Diagnosis: No problems found.**\n{summary}")
        return

    # ── Apply fixes ──────────────────────────────────────────────────
    fix_log = [f"🔧 **AI Diagnosis — {len(problems)} problem(s) found**\n_{summary}_\n"]

    for p in problems:
        uid = p.get("user_id")
        name = p.get("name", str(uid))
        problem = p.get("problem", "Unknown issue")
        fix = p.get("fix", "no_fix_needed")

        fix_log.append(f"\n**{name}** — {problem}")

        if fix == "force_end_shift" and uid in chatter_state.get(guild.id, {}):
            s = chatter_state[guild.id][uid]
            shift_key = s.get("shift", "")

            # Calculate hours and log them
            start_ts = s.get("shift_start_ts", now_ts())
            hours = (now_ts() - start_ts) / 3600
            w = get_weekly_stats(guild.id, uid, name)
            w["hours_worked"] = w.get("hours_worked", 0) + hours

            # End the shift
            s["active"] = False
            s["pending"] = False
            warn_key = f"{guild.id}_{uid}"
            end_shift_warned.pop(warn_key, None)

            # Free model slot
            assigned_model = get_chatter_model(guild.id, uid)
            if assigned_model and guild.id in models and assigned_model in models[guild.id]:
                slot_key = f"{assigned_model}_{shift_key}"
                if models[guild.id][assigned_model].get("active_slot") == slot_key:
                    models[guild.id][assigned_model].pop("active_slot", None)

            save_data()
            fix_log.append(f"  → ✅ Force-ended their shift ({hours:.1f}hrs logged)")

        elif fix == "clear_pending" and uid in chatter_state.get(guild.id, {}):
            s = chatter_state[guild.id][uid]
            s["pending"] = False
            s["alert_sent"] = False
            s["warning_sent"] = False
            s["next_ping"] = now_ts() + random_interval()
            fix_log.append(f"  → ✅ Cleared stuck pending state, rescheduled ping")

        else:
            fix_log.append(f"  → ℹ️ No automatic fix applied")

    await ctx.send("\n".join(fix_log))

# ─── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
