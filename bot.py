import asyncio
import os
import random
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import data
import storage

MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 3 * 60 * 60  # 3 hours

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # needed to read typed quiz answers

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# guild_id -> next scheduled UTC datetime for a random trivia message
_next_fire: dict[int, datetime] = {}

# user_id -> current correct-answer streak (in-memory, resets on bot restart)
STREAKS: dict[int, int] = {}

# (user_id, channel_id) currently running a /quiz loop, to stop double-starts
ACTIVE_SESSIONS: set[tuple[int, int]] = set()

DOT = "."

FACT_TEMPLATES = [
    "📞 Did you know? Area code **{code}** serves **{place}** ({country}).",
    "📍 Area code **{code}** belongs to **{place}**, in {country}.",
    "🗺️ If you see area code **{code}**, the call is probably from **{place}** ({country}).",
    "🔎 Trivia: **{place}** ({country}) uses area code **{code}**.",
]


def build_fact_message(record: dict) -> str:
    template = random.choice(FACT_TEMPLATES)
    return template.format(
        code=record["area_code"],
        place=data.location_label(record),
        country=record["country_name"],
    )


# ---------------------------------------------------------------------------
# Autocomplete helpers
# ---------------------------------------------------------------------------

async def country_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    choices = []
    for key, name in data.get_countries():
        if current in name.lower() or current in key.lower():
            choices.append(app_commands.Choice(name=name, value=key))
    return choices[:25]


async def subdivisions_autocomplete(interaction: discord.Interaction, current: str):
    country_key = interaction.namespace.country

    # support comma-separated lists: only autocomplete the last fragment
    prefix = ""
    fragment = current
    if "," in current:
        head, _, fragment = current.rpartition(",")
        prefix = head.strip() + ", "
    fragment = fragment.strip().lower()

    if country_key and country_key in data.COUNTRIES_WITH_SUBDIVISIONS:
        subs = data.get_subdivisions(country_key)
    else:
        # country not picked yet (Discord doesn't guarantee fill order) \u2014
        # fall back to suggestions from every country that has subdivisions
        # so the field is never dead/empty.
        seen = {}
        for c in data.COUNTRIES_WITH_SUBDIVISIONS:
            for code, name in data.get_subdivisions(c):
                seen[code] = name
        subs = sorted(seen.items())

    matches = [
        (code, name) for code, name in subs
        if fragment in code.lower() or fragment in name.lower()
    ]

    choices = []
    for code, name in matches[:25]:
        full_value = f"{prefix}{code}"
        choices.append(app_commands.Choice(name=f"{code} - {name}", value=full_value))
    return choices


# ---------------------------------------------------------------------------
# Quiz answer UI (button mode)
# ---------------------------------------------------------------------------

class QuizButton(discord.ui.Button):
    def __init__(self, label: str, is_correct: bool, view: "QuizView"):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.is_correct = is_correct
        self.quiz_view = view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.quiz_view.asker_id:
            await interaction.response.send_message(
                "This quiz question isn't for you \u2014 start your own with /quiz!", ephemeral=True
            )
            return

        for child in self.quiz_view.children:
            child.disabled = True
            if isinstance(child, QuizButton) and child.is_correct:
                child.style = discord.ButtonStyle.success
            elif child is self:
                child.style = discord.ButtonStyle.danger

        # Only disable/color the buttons here \u2014 the correct/incorrect verdict
        # is sent as a brand-new message by the quiz loop, not edited in.
        await interaction.response.edit_message(view=self.quiz_view)
        self.quiz_view.answered = self.is_correct
        self.quiz_view.stop()


class QuizView(discord.ui.View):
    def __init__(self, asker_id: int, options: list[str], correct_index: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.asker_id = asker_id
        self.answered: Optional[bool] = None  # True/False once clicked, None if it timed out
        self.message: Optional[discord.Message] = None
        for i, opt in enumerate(options):
            self.add_item(QuizButton(opt, i == correct_index, self))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Quiz helpers (question building, grading, streaks, continuation)
# ---------------------------------------------------------------------------

def build_question(mode_value: str, record: dict, country_name: str):
    if mode_value == "code_to_place":
        question_text = f"📞 Which place uses area code **{record['area_code']}**? _(country: {country_name})_"
        correct_label = data.location_label(record)
        value_fn = data.location_label
    else:
        question_text = f"📍 What is the area code for **{data.location_label(record)}**? _(country: {country_name})_"
        correct_label = record["area_code"]
        value_fn = lambda r: r["area_code"]
    return question_text, correct_label, value_fn


def grade_typed_answer(mode_value: str, record: dict, answer_text: str) -> bool:
    answer = answer_text.strip().lower()
    if mode_value == "code_to_place":
        acceptable = {record["city"].lower()}
        if record["subdivision"]:
            acceptable.add(record["subdivision"].lower())
            acceptable.add(record["subdivision_name"].lower())
        acceptable.add(record["country_name"].lower())
        return any(a in answer or answer in a for a in acceptable if a)
    return answer.replace(" ", "") == record["area_code"].replace(" ", "")


def bump_streak(user_id: int, is_correct: bool) -> int:
    if is_correct:
        STREAKS[user_id] = STREAKS.get(user_id, 0) + 1
    else:
        STREAKS[user_id] = 0
    return STREAKS[user_id]


def format_result(user: discord.abc.User, is_correct: bool, correct_label: str, streak: int) -> str:
    if is_correct:
        fire = " 🔥" * min(streak // 3, 3) if streak >= 3 else ""
        return f"✅ {user.mention} **Correct!** It's **{correct_label}**.{fire}\nStreak: **{streak}**"
    return f"❌ {user.mention} Not quite. Correct answer: **{correct_label}**.\nStreak reset to **0**."


async def wait_for_dot(user_id: int, channel_id: int, timeout: float) -> bool:
    """Returns True if the user sent a lone '.' in the channel within timeout."""
    def check(m: discord.Message):
        return m.author.id == user_id and m.channel.id == channel_id and m.content.strip() == DOT

    try:
        await bot.wait_for("message", check=check, timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not trivia_loop.is_running():
        trivia_loop.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.tree.command(name="setchannel", description="Set the channel where random area code trivia will be posted.")
@app_commands.describe(channel="Channel to post trivia in (defaults to the current channel)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    storage.set_trivia_channel(interaction.guild_id, target.id)
    _next_fire[interaction.guild_id] = datetime.now(timezone.utc) + timedelta(
        seconds=random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
    )
    await interaction.response.send_message(f"✅ Random area code trivia will now be posted in {target.mention}.")


@bot.tree.command(name="trivia", description="Post a random area code trivia message right now.")
async def trivia(interaction: discord.Interaction):
    record = data.pick_random_record()
    await interaction.response.send_message(build_fact_message(record))


@bot.tree.command(name="quiz", description="Quiz yourself on area codes.")
@app_commands.describe(
    mode="What the bot shows vs. what you have to guess",
    country="Country to quiz on",
    subdivisions="Optional: comma-separated states/provinces to include, e.g. 'NY, CA, TX' (you can list as many as you want)",
    answer_mode="How you answer the question",
    num_options="Number of choices to show (buttons mode only, default 4)",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Bot shows an area code \u2192 you guess the place", value="code_to_place"),
        app_commands.Choice(name="Bot shows a place \u2192 you guess the area code", value="place_to_code"),
    ],
    answer_mode=[
        app_commands.Choice(name="Type your answer in chat", value="typed"),
        app_commands.Choice(name="Pick from multiple-choice buttons", value="buttons"),
    ],
)
@app_commands.autocomplete(country=country_autocomplete, subdivisions=subdivisions_autocomplete)
async def quiz(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    country: str,
    answer_mode: app_commands.Choice[str],
    subdivisions: Optional[str] = None,
    num_options: Optional[app_commands.Range[int, 2, 8]] = 4,
):
    """
    Runs a continuing quiz session: after each question is resolved, the bot
    waits 3 seconds \u2014 do nothing and a new question with the same
    settings fires automatically; send '.' in that window to stop. You can
    also stop right when answering by appending '.' to your typed answer
    (e.g. '907.').
    """
    session_key = (interaction.user.id, interaction.channel_id)
    if session_key in ACTIVE_SESSIONS:
        await interaction.response.send_message(
            "You already have a quiz running in this channel \u2014 answer it, or send `.` to end it, before starting a new one.",
            ephemeral=True,
        )
        return

    sub_list = [s for s in subdivisions.split(",")] if subdivisions else None
    country_name = data.country_display_name(country)
    mode_value = mode.value
    user = interaction.user

    ACTIVE_SESSIONS.add(session_key)
    try:
        first = True
        last_area_code = None
        while True:
            record = data.pick_random_record(
                country=country, subdivisions=sub_list, exclude_area_code=last_area_code
            )
            if record is None:
                text = "No area codes match that filter. Try a different country/subdivision combo."
                if first:
                    await interaction.response.send_message(text, ephemeral=True)
                else:
                    await interaction.channel.send(text)
                return
            last_area_code = record["area_code"]

            question_text, correct_label, value_fn = build_question(mode_value, record, country_name)

            try:
                if answer_mode.value == "buttons":
                    pool = data.filter_records(country=country, subdivisions=sub_list)
                    widened_note = ""
                    if len(pool) < num_options:
                        widened_note = f"\n_(not enough matches for {num_options} options in that subdivision \u2014 pulling extra choices from all of {country_name})_"
                        pool = data.filter_records(country=country)
                    distractors = data.pick_distractors(record, pool, value_fn, num_options - 1)
                    options = distractors + [correct_label]
                    random.shuffle(options)
                    correct_index = options.index(correct_label)

                    view = QuizView(user.id, options, correct_index, timeout=30)
                    full_question = question_text + widened_note

                    if first:
                        await interaction.response.send_message(full_question, view=view)
                        view.message = await interaction.original_response()
                    else:
                        view.message = await interaction.channel.send(full_question, view=view)

                    dot_task = asyncio.create_task(wait_for_dot(user.id, interaction.channel_id, 30))
                    view_task = asyncio.create_task(view.wait())
                    done, pending = await asyncio.wait({dot_task, view_task}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    if dot_task in done and dot_task.result():
                        for child in view.children:
                            child.disabled = True
                        try:
                            await view.message.edit(view=view)
                        except discord.HTTPException:
                            pass
                        await interaction.channel.send(
                            f"🏁 {user.mention} ended the quiz. Final streak: **{STREAKS.get(user.id, 0)}**"
                        )
                        return

                    if view.answered is None:
                        STREAKS[user.id] = 0
                        await interaction.channel.send(
                            f"⌛ {user.mention}, time's up! Correct answer was **{correct_label}**. Quiz ended."
                        )
                        return

                    streak = bump_streak(user.id, view.answered)
                    await interaction.channel.send(format_result(user, view.answered, correct_label, streak))

                else:  # typed
                    hint = "add a `.` to answer and end at the same time, e.g. `907.`" if mode_value == "place_to_code" else "add a `.` after your answer to end, e.g. `New York.`"
                    prompt = f"{question_text}\n_(30 seconds to answer \u2014 {hint})_"
                    if first:
                        await interaction.response.send_message(prompt)
                    else:
                        await interaction.channel.send(prompt)

                    def check(m: discord.Message):
                        return m.author.id == user.id and m.channel.id == interaction.channel_id

                    try:
                        msg = await bot.wait_for("message", check=check, timeout=30)
                    except asyncio.TimeoutError:
                        STREAKS[user.id] = 0
                        await interaction.channel.send(
                            f"⌛ {user.mention}, time's up! Correct answer: **{correct_label}**. Quiz ended."
                        )
                        return

                    content = msg.content.strip()
                    if content == DOT:
                        await interaction.channel.send(
                            f"🏁 {user.mention} ended the quiz. Final streak: **{STREAKS.get(user.id, 0)}**"
                        )
                        return

                    end_now = content.endswith(DOT)
                    answer_text = content[:-1].strip() if end_now else content

                    is_correct = grade_typed_answer(mode_value, record, answer_text)
                    streak = bump_streak(user.id, is_correct)
                    await msg.reply(format_result(user, is_correct, correct_label, streak))

                    if end_now:
                        return
            except Exception:
                traceback.print_exc()
                try:
                    await interaction.channel.send(
                        "⚠️ Something went wrong running that quiz question, so the session was stopped. "
                        "Please try `/quiz` again \u2014 if it keeps happening, check the bot's logs."
                    )
                except discord.HTTPException:
                    pass
                return

            # give the player a 3-second window to stop with '.', otherwise
            # a fresh question with the same settings fires automatically
            if await wait_for_dot(user.id, interaction.channel_id, 3):
                await interaction.channel.send(
                    f"🏁 Quiz ended. Final streak: **{STREAKS.get(user.id, 0)}**"
                )
                return

            first = False
    finally:
        ACTIVE_SESSIONS.discard(session_key)


# ---------------------------------------------------------------------------
# Background random trivia loop
# ---------------------------------------------------------------------------

async def trivia_tick(now: datetime) -> None:
    """One tick of the random-trivia scheduler. Extracted as its own function
    so it can be unit-tested with a fake clock, independent of discord.py's
    real 1-second task loop."""
    for guild_id, channel_id in storage.all_trivia_channels().items():
        next_time = _next_fire.get(guild_id)
        if next_time is None:
            _next_fire[guild_id] = now + timedelta(
                seconds=random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
            )
            continue
        if now >= next_time:
            try:
                channel = bot.get_channel(channel_id)
                if channel is not None:
                    record = data.pick_random_record()
                    await channel.send(build_fact_message(record))
            except Exception:
                traceback.print_exc()
            finally:
                _next_fire[guild_id] = now + timedelta(
                    seconds=random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
                )


@tasks.loop(seconds=1)
async def trivia_loop():
    try:
        await trivia_tick(datetime.now(timezone.utc))
    except Exception:
        # A single bad tick must never permanently kill the background
        # scheduler (tasks.loop stops for good on an unhandled exception).
        traceback.print_exc()


@trivia_loop.error
async def trivia_loop_error(exc: Exception):
    traceback.print_exc()
    if not trivia_loop.is_running():
        trivia_loop.restart()


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable before running the bot.")
    bot.run(token)


if __name__ == "__main__":
    main()
