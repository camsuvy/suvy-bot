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

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─── STATE ─────────────────────────────────────────────────────────────────────
# { guild_id: { user_id: { "pending": bool, "ping_msg_id": int, "next_ping": float, "shift": str, "stats": {...} } } }
chatter_state = {}
shift_totals = {}   # { guild_id: { shift: { ppv, revenue, checkins } } }
weekly_stats = {}  # { guild_id: { user_id: { name, ppv, revenue, checkins } } }
chatter_daily = {}  # { guild_id: { user_id: { revenue, date } } }
last_stats = {}  # { guild_id: { user_id: { ppv, fans, revenue, count } } } — for anti-cheat
roster = {}  # { guild_id: { shift_key: [ user_id, ... ] } }  — expected chatters per shift
strikes = {}  # { guild_id: { user_id: { count, reasons: [] } } }
daily_goal = {}  # { guild_id: { goal: float, current: float, date: str } }
model_weekly_goal = 5000.0  # Default $5k per model per week (Mon-Sat)
models = {}  # { guild_id: { model_name: { chatters: [user_id], revenue: float, ppv: int } } }
chatter_model = {}  # { guild_id: { user_id: model_name } }

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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if guild_id not in daily_goal:
        daily_goal[guild_id] = {"goal": 0.0, "current": 0.0, "date": today}
    # Reset if new day
    if daily_goal[guild_id]["date"] != today:
        daily_goal[guild_id]["current"] = 0.0
        daily_goal[guild_id]["date"] = today
    return daily_goal[guild_id]

def get_chatter_daily(guild_id, user_id):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        ppv, fans, rev, msgs, convos = parse_stats(content)

        state["pending"] = False
        state["last_checkin"] = now_ts()
        state["stats"] = {"ppv": ppv, "fans": fans, "revenue": rev}
        state["alert_sent"] = False
        state["warning_sent"] = False
        next_in = random_interval()
        state["next_ping"] = now_ts() + next_in
        next_min = next_in // 60

        # Update shift totals
        shift_key = SHIFT_CHANNELS[channel_name]
        totals = get_shift_totals(guild.id, shift_key)
        totals["ppv"] += ppv
        totals["revenue"] += rev
        totals["checkins"] += 1

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
            embed.set_footer(text=f"Next ping in ~{next_min} min (random) • {fmt_time(now_ts())}")
            await log_ch.send(embed=embed)

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
    for guild in bot.guilds:
        # Check roster for no-shows (15 min after shift start) — Monday to Saturday only
        now = datetime.now(timezone.utc)
        if now.weekday() < 6:  # 0=Monday, 5=Saturday, 6=Sunday (skip Sunday)
            for shift_key, shift_info in SHIFTS.items():
                start_hour = shift_info["start"]
                if now.hour == start_hour and now.minute == 15:
                    expected = roster.get(guild.id, {}).get(shift_key, [])
                    for user_id in expected:
                        active = chatter_state.get(guild.id, {}).get(user_id, {}).get("active", False)
                        if not active:
                            member = guild.get_member(user_id)
                            owner = guild.get_member(OWNER_ID)
                            if owner and member:
                                try:
                                    await owner.send(
                                        f"🚨 **No-show alert!**\n"
                                        f"**{member.display_name}** was expected for {shift_info['name']} "
                                        f"and hasn't been started yet (15 min past shift start)."
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
            owner = guild.get_member(OWNER_ID)
            if owner:
                overall_goal = get_overall_daily_goal(guild.id)
                goal_data = get_daily_goal(guild.id)
                current_rev = goal_data.get("current", 0)
                pct = min(100, (current_rev / overall_goal * 100)) if overall_goal > 0 else 0

                recap_lines = [f"📊 **Daily Recap — {now.strftime('%A %b %d')}**\n"]
                recap_lines.append(f"💰 Total Revenue: ${current_rev:.2f} / ${overall_goal:.2f} ({pct:.0f}%)\n")

                # Per model breakdown
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

        if guild.id not in chatter_state:
            continue
        for user_id, state in chatter_state[guild.id].items():
            if not state.get("active"):
                continue

            # On Sunday after 3AM, stop all monitoring
            if now.weekday() == 6 and now.hour >= 3:
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

    # Check if shift slot is already taken for this model
    assigned_model = get_chatter_model(ctx.guild.id, member.id)
    if assigned_model and ctx.guild.id in models and assigned_model in models[ctx.guild.id]:
        slot_key = f"{assigned_model}_{shift_key}"
        current_slot = models[ctx.guild.id][assigned_model].get("active_slot")
        if current_slot == slot_key:
            for uid, s in chatter_state.get(ctx.guild.id, {}).items():
                if s.get("active") and s.get("shift") == shift_key and get_chatter_model(ctx.guild.id, uid) == assigned_model:
                    existing = ctx.guild.get_member(uid)
                    name = existing.display_name if existing else "Someone"
                    await ctx.send(f"❌ **{name}** is already working the {SHIFTS[shift_key]['name']} for **{assigned_model}**. Only one chatter per shift per model.")
                    return
        models[ctx.guild.id][assigned_model]["active_slot"] = slot_key

    state = get_state(ctx.guild.id, member.id)
    state["active"] = True
    state["shift"] = shift_key
    state["name"] = member.display_name
    state["pending"] = False
    state["alert_sent"] = False
    state["warning_sent"] = False
    state["shift_start_ts"] = now_ts()
    next_in = random_interval()
    state["next_ping"] = now_ts() + next_in

    embed = discord.Embed(
        title=f"✅ Shift Started — {member.display_name}",
        color=0x00ff88
    )
    embed.add_field(name="Shift", value=SHIFTS[shift_key]["name"], inline=True)
    embed.add_field(name="Hours", value=SHIFTS[shift_key]["hours"], inline=True)
    embed.add_field(name="First ping in", value=f"~{next_in // 60} min (random)", inline=True)

    # Auto-detect if they're late
    now = datetime.now(timezone.utc)
    shift_start_hour = SHIFTS[shift_key]["start"]
    current_hour = now.hour
    # Calculate minutes late (handle overnight shifts)
    minutes_late = 0
    if current_hour >= shift_start_hour:
        minutes_late = (current_hour - shift_start_hour) * 60 + now.minute
    elif shift_key == "night" and current_hour < 3:  # night shift crosses midnight
        minutes_late = (current_hour + 24 - shift_start_hour) * 60 + now.minute

    if minutes_late >= 5:
        embed.add_field(name="⚠️ Late", value=f"{minutes_late} minutes past shift start", inline=False)
        embed.color = 0xffaa00
        log_ch = await get_log_channel(ctx.guild)
        if log_ch:
            await log_ch.send(
                f"⏰ **{member.display_name}** started their shift **{minutes_late} minutes late** "
                f"({SHIFTS[shift_key]['name']})"
            )
        owner = ctx.guild.get_member(OWNER_ID)
        if owner and owner.id != ctx.author.id:
            try:
                await owner.send(
                    f"⏰ **{member.display_name}** just started their shift {minutes_late} minutes late.\n"
                    f"Shift: {SHIFTS[shift_key]['name']}"
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
            f"When pinged, reply with: `PPV: X | Fans: X | Rev: $X`\n"
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
    totals = get_shift_totals(ctx.guild.id, shift_key)

    # Free up the shift slot for this model
    assigned_model = get_chatter_model(ctx.guild.id, member.id)
    if assigned_model and ctx.guild.id in models and assigned_model in models[ctx.guild.id]:
        slot_key = f"{assigned_model}_{shift_key}"
        if models[ctx.guild.id][assigned_model].get("active_slot") == slot_key:
            models[ctx.guild.id][assigned_model].pop("active_slot", None)

    # Track hours worked this week
    shift_start_ts = state.get("shift_start_ts", now_ts())
    hours_worked = (now_ts() - shift_start_ts) / 3600
    w = get_weekly_stats(ctx.guild.id, member.id, member.display_name)
    w["hours_worked"] = w.get("hours_worked", 0) + hours_worked

    state["active"] = False
    state["pending"] = False

    embed = discord.Embed(title=f"⏹ Shift Ended — {member.display_name}", color=0xff6600)
    embed.add_field(name="Shift", value=SHIFTS.get(shift_key, {}).get("name", "—"), inline=True)
    embed.add_field(name="Total Check-ins", value=str(totals["checkins"]), inline=True)
    embed.add_field(name="Total PPVs", value=str(totals["ppv"]), inline=True)
    embed.add_field(name="Total Revenue", value=f"${totals['revenue']:.2f}", inline=True)
    await ctx.send(embed=embed)

    # Post daily summary to stats-log
    log_ch = await get_log_channel(ctx.guild)
    if log_ch:
        summary = discord.Embed(
            title=f"📋 Daily Shift Summary — {member.display_name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        summary.add_field(name="Shift", value=SHIFTS.get(shift_key, {}).get("name", "—"), inline=True)
        summary.add_field(name="Check-ins", value=str(totals["checkins"]), inline=True)
        summary.add_field(name="PPVs Sent", value=str(totals["ppv"]), inline=True)
        summary.add_field(name="Revenue", value=f"${totals['revenue']:.2f}", inline=True)
        avg_rev = totals["revenue"] / totals["checkins"] if totals["checkins"] > 0 else 0
        summary.add_field(name="Avg Rev/Check-in", value=f"${avg_rev:.2f}", inline=True)
        if assigned_model:
            summary.add_field(name="Model", value=assigned_model, inline=True)
        summary.set_footer(text="Shift complete")
        await log_ch.send(embed=summary)

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

@bot.command(name="strikes")
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

@bot.command(name="roster")
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
        value="The bot will ping you randomly during your shift.\nYou have **5 minutes** to reply with your stats.\nNo response = manager gets alerted immediately.",
        inline=False
    )
    embed.add_field(
        name="Check-in Format",
        value="`PPV: X | Fans: X | Rev: $X`\nExample: `PPV: 8 | Fans: 22 | Rev: $180`",
        inline=False
    )
    embed.add_field(
        name="Screen Share",
        value=f"Join the **{shift_info['name']} voice channel** and share your screen at the start of every shift.",
        inline=False
    )
    embed.add_field(
        name="Rules",
        value="✅ Always be online during your shift hours\n✅ Respond to pings within 5 minutes\n✅ Keep your stats accurate\n❌ Going AFK without notice = strike",
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

# ─── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
