import asyncio
import io
import os
import random
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import data
import maps_live
import storage
import us_geo

MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 3 * 60 * 60  # 3 hours

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # needed to read typed quiz answers

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# guild_id -> next scheduled UTC datetime for a random trivia message
_next_fire: dict[int, datetime] = {}

# user_id -> current correct-answer streak (in-memory, resets on bot restart).
# Shared across area-code and county quizzes on purpose (it's "your" streak).
STREAKS: dict[int, int] = {}

# (user_id, channel_id) currently running a quiz loop, to stop double-starts.
# Shared between /quiz and /countyquiz so you can't run two at once in a channel.
ACTIVE_SESSIONS: set[tuple[int, int]] = set()

DOT = "."

MODE_CHOICES_AREACODE = [
    app_commands.Choice(name="Bot shows an area code \u2192 you guess the place", value="code_to_place"),
    app_commands.Choice(name="Bot shows a place \u2192 you guess the area code", value="place_to_code"),
]
MODE_CHOICES_COUNTY = [
    app_commands.Choice(name="Bot shows an area code \u2192 you guess the county", value="code_to_county"),
    app_commands.Choice(name="Bot shows a county \u2192 you guess the area code", value="county_to_code"),
]
ANSWER_MODE_CHOICES = [
    app_commands.Choice(name="Type your answer in chat", value="typed"),
    app_commands.Choice(name="Pick from multiple-choice buttons", value="buttons"),
]


# ---------------------------------------------------------------------------
# Fact messages (the passive "trivia" text) \u2014 area code and county are
# completely separate templates/builders.
# ---------------------------------------------------------------------------

AREACODE_FACT_TEMPLATES = [
    "📞 Did you know? Area code **{code}** serves **{place}** ({country}).",
    "📍 Area code **{code}** belongs to **{place}**, in {country}.",
    "🗺️ If you see area code **{code}**, the call is probably from **{place}** ({country}).",
    "🔎 Trivia: **{place}** ({country}) uses area code **{code}**.",
]

COUNTY_FACT_TEMPLATES = [
    "🗺️ Did you know? **{county} County, {state}** is served by area code **{code}**.",
    "📍 **{county} County** ({state}) \u2014 dial area code **{code}** to reach it.",
    "🔎 Trivia: area code **{code}** covers **{county} County, {state}**.",
    "📞 **{county} County, {state}** falls under area code **{code}**.",
]


def build_areacode_fact_message(record: dict) -> str:
    template = random.choice(AREACODE_FACT_TEMPLATES)
    return template.format(
        code=record["area_code"],
        place=data.location_label(record),
        country=record["country_name"],
    )


def build_county_fact_message(cr: dict) -> str:
    template = random.choice(COUNTY_FACT_TEMPLATES)
    return template.format(
        code=cr["area_code"],
        county=cr["county"],
        state=cr["subdivision"],
    )


def get_areacode_map_file(record: dict) -> Optional[discord.File]:
    """Map for the AREA CODE track. Shows where that area code actually
    reaches \u2014 every county it covers, zoomed to the state \u2014 not just
    the state as a whole. Falls back to a plain state highlight only when
    we have no county-coverage data for this record (e.g. an overlay code
    or a non-US record with no `counties` list).

    This is completely separate from the county track's map (see
    get_county_map_file below): that one always shows exactly one county
    and never an area code's full footprint. Rendered live from the
    in-memory map for that state, not read from disk."""
    subdivision = record.get("subdivision")
    if not subdivision:
        return None
    try:
        counties = data.all_counties_for_area_code(record["area_code"])
        png_bytes = maps_live.render_county_png(subdivision, counties) if counties else None
        if png_bytes is None:
            png_bytes = maps_live.render_state_png(subdivision)
    except Exception:
        print(f"[maps] failed to render area code map for {record.get('area_code')!r}:")
        traceback.print_exc()
        return None
    if png_bytes is None:
        return None
    return discord.File(io.BytesIO(png_bytes), filename=f"map_{record['area_code']}_areacode.png")


def get_county_map_file(cr: dict) -> Optional[discord.File]:
    """Map for the COUNTY track. Shows exactly the ONE specific county this
    record is about \u2014 never every county sharing that area code, since
    that would make it impossible to tell which single county is meant.

    Completely separate from the area-code track's map (see
    get_areacode_map_file above). Falls back to a plain state highlight
    only if that one county isn't found in the county geometry. Rendered
    live from the in-memory map for that state, not read from disk."""
    subdivision = cr.get("subdivision")
    try:
        png_bytes = maps_live.render_county_png(subdivision, [cr["county"]])
    except Exception:
        print(f"[maps] failed to render county map for {cr.get('county')!r}:")
        traceback.print_exc()
        png_bytes = None
    if png_bytes is not None:
        return discord.File(io.BytesIO(png_bytes), filename=f"map_{cr['area_code']}_{cr['county']}.png")
    if not subdivision:
        return None
    try:
        png_bytes = maps_live.render_state_png(subdivision)
    except Exception:
        print(f"[maps] failed to render fallback state map for {subdivision!r}:")
        traceback.print_exc()
        return None
    if png_bytes is None:
        return None
    return discord.File(io.BytesIO(png_bytes), filename=f"map_{subdivision}.png")


# ---------------------------------------------------------------------------
# Autocomplete helpers
# ---------------------------------------------------------------------------

async def country_autocomplete(interaction: discord.Interaction, current: str):
    current = data.clean_token(current).lower()
    choices = []
    for key, name in data.get_countries():
        if current in name.lower() or current in key.lower():
            choices.append(app_commands.Choice(name=name, value=key))
    return choices[:25]


async def subdivisions_autocomplete(interaction: discord.Interaction, current: str):
    country_key = getattr(interaction.namespace, "country", None) or "US"

    current = current.strip().strip(data.QUOTE_CHARS)
    prefix = ""
    fragment = current
    if "," in current:
        head, _, fragment = current.rpartition(",")
        prefix = data.clean_token(head) + ", "
    fragment = data.clean_token(fragment).lower()

    if country_key in data.COUNTRIES_WITH_SUBDIVISIONS:
        subs = data.get_subdivisions(country_key)
    else:
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
# Quiz answer UI (button mode) \u2014 shared by both tracks
# ---------------------------------------------------------------------------

class QuizButton(discord.ui.Button):
    def __init__(self, label: str, is_correct: bool, view: "QuizView"):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.is_correct = is_correct
        self.quiz_view = view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.quiz_view.asker_id:
            await interaction.response.send_message(
                "This quiz question isn't for you \u2014 start your own!", ephemeral=True
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
# Shared small helpers
# ---------------------------------------------------------------------------

def bump_streak(user_id: int, is_correct: bool) -> int:
    if is_correct:
        STREAKS[user_id] = STREAKS.get(user_id, 0) + 1
    else:
        STREAKS[user_id] = 0
    return STREAKS[user_id]


def pick_random_from_list(records: list[dict], exclude_key=None, key_fn=lambda r: r["area_code"]):
    if not records:
        return None
    pool = records
    if exclude_key is not None and len(pool) > 1:
        narrowed = [r for r in pool if key_fn(r) != exclude_key]
        if narrowed:
            pool = narrowed
    return random.choice(pool)


def humanize_delta(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


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
# Area-code question building/grading
# ---------------------------------------------------------------------------

def build_areacode_question(mode_value: str, record: dict, context_note: str = ""):
    suffix = f" {context_note}" if context_note else ""
    if mode_value == "code_to_place":
        question_text = f"📞 Which place uses area code **{record['area_code']}**?{suffix}"
        correct_label = data.location_label(record)
        value_fn = data.location_label
    else:
        question_text = f"📍 What is the area code for **{data.location_label(record)}**?{suffix}"
        correct_label = record["area_code"]
        value_fn = lambda r: r["area_code"]
    return question_text, correct_label, value_fn


def grade_areacode_answer(mode_value: str, record: dict, answer_text: str) -> bool:
    answer = answer_text.strip().lower()
    if mode_value == "code_to_place":
        acceptable = {record["city"].lower()}
        if record["subdivision"]:
            acceptable.add(record["subdivision"].lower())
            acceptable.add(record["subdivision_name"].lower())
        acceptable.add(record["country_name"].lower())
        return any(a in answer or answer in a for a in acceptable if a)
    return answer.replace(" ", "") == record["area_code"].replace(" ", "")


# ---------------------------------------------------------------------------
# County question building/grading
# ---------------------------------------------------------------------------

def build_county_question(mode_value: str, cr: dict, context_note: str = ""):
    suffix = f" {context_note}" if context_note else ""
    if mode_value == "code_to_county":
        question_text = f"🗺️ Which county does area code **{cr['area_code']}** cover?{suffix}"
        correct_label = data.county_label(cr)
        value_fn = data.county_label
    else:
        question_text = f"📞 What area code covers **{data.county_label(cr)}**?{suffix}"
        correct_label = cr["area_code"]
        value_fn = lambda r: r["area_code"]
    return question_text, correct_label, value_fn


def grade_county_answer(mode_value: str, cr: dict, answer_text: str) -> bool:
    answer = answer_text.strip().lower()
    if mode_value == "code_to_county":
        acceptable = {
            cr["county"].lower(),
            f"{cr['county']} county".lower(),
            cr["subdivision"].lower(),
            cr["subdivision_name"].lower(),
        }
        return any(a in answer or answer in a for a in acceptable if a)
    return answer.replace(" ", "") == cr["area_code"].replace(" ", "")


# ---------------------------------------------------------------------------
# Generic continuing quiz session engine \u2014 shared by the area-code and
# county tracks. Every track-specific bit (how to build a question, how to
# grade it, which map to attach, how to pick the next record) is passed in.
# ---------------------------------------------------------------------------

async def run_quiz_session(
    interaction: discord.Interaction,
    mode_value: str,
    answer_mode_value: str,
    num_options: int,
    get_record,             # callable(exclude_key) -> Optional[dict]
    record_key_fn,          # callable(record) -> Hashable, identifies a record for exclusion
    question_builder,       # callable(mode_value, record, context_note) -> (text, correct_label, value_fn)
    grader,                 # callable(mode_value, record, answer_text) -> bool
    map_file_fn,            # callable(record) -> Optional[discord.File]
    get_distractor_pool,    # callable() -> list[dict]
    get_broadened_pool,     # callable() -> list[dict]  (fallback when the primary pool is too small)
    broaden_label: str,
    empty_message: str,
    context_note_fn,        # callable(record) -> str
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

    # Defer immediately, before any record-picking or map-rendering work,
    # so Discord's 3-second "must acknowledge the interaction" window is
    # never at risk of being missed \u2014 everything after this point uses
    # a followup message instead of interaction.response.
    await interaction.response.defer()

    user = interaction.user
    ACTIVE_SESSIONS.add(session_key)
    try:
        first = True
        last_key = None
        while True:
            record = get_record(last_key)
            if record is None:
                if first:
                    await interaction.followup.send(empty_message, ephemeral=True)
                else:
                    await interaction.channel.send(empty_message)
                return
            last_key = record_key_fn(record)

            question_text, correct_label, value_fn = question_builder(mode_value, record, context_note_fn(record))
            map_file = map_file_fn(record)

            try:
                if answer_mode_value == "buttons":
                    pool = get_distractor_pool()
                    widened_note = ""
                    if len(pool) < num_options:
                        widened_note = f"\n_(not enough matches for {num_options} options \u2014 pulling extra choices from {broaden_label})_"
                        pool = get_broadened_pool()
                    distractors = data.pick_distractors(record, pool, value_fn, num_options - 1)
                    options = distractors + [correct_label]
                    random.shuffle(options)
                    correct_index = options.index(correct_label)

                    view = QuizView(user.id, options, correct_index, timeout=30)
                    full_question = question_text + widened_note

                    if first:
                        if map_file:
                            await interaction.followup.send(full_question, view=view, file=map_file)
                        else:
                            await interaction.followup.send(full_question, view=view)
                        view.message = await interaction.original_response()
                    else:
                        if map_file:
                            view.message = await interaction.channel.send(full_question, view=view, file=map_file)
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
                    hint = "add a `.` after your answer to end, e.g. `907.`"
                    prompt = f"{question_text}\n_(30 seconds to answer \u2014 {hint})_"
                    if first:
                        if map_file:
                            await interaction.followup.send(prompt, file=map_file)
                        else:
                            await interaction.followup.send(prompt)
                    else:
                        if map_file:
                            await interaction.channel.send(prompt, file=map_file)
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

                    is_correct = grader(mode_value, record, answer_text)
                    streak = bump_streak(user.id, is_correct)
                    await msg.reply(format_result(user, is_correct, correct_label, streak))

                    if end_now:
                        return
            except Exception:
                traceback.print_exc()
                try:
                    await interaction.channel.send(
                        "⚠️ Something went wrong running that quiz question, so the session was stopped. "
                        "Please try again \u2014 if it keeps happening, check the bot's logs."
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
# Lifecycle / setup commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    try:
        test_png = maps_live.render_state_png("CA")
        print(f"[maps] OK \u2014 live map rendering works ({len(test_png)} bytes for a test map).")
    except Exception:
        print("[maps] WARNING: map rendering failed at startup \u2014 trivia/quiz messages will be "
              "sent WITHOUT images until this is fixed. Common cause: matplotlib isn't installed "
              "(`pip install -r requirements.txt`). Full error:")
        traceback.print_exc()

    await bot.tree.sync()
    if not trivia_loop.is_running():
        trivia_loop.start()
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.tree.command(name="setchannel", description="Set the channel where random trivia (area code + county, mixed randomly) will be posted.")
@app_commands.describe(channel="Channel to post trivia in (defaults to the current channel)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    storage.set_trivia_channel(interaction.guild_id, target.id)
    _next_fire[interaction.guild_id] = datetime.now(timezone.utc) + timedelta(
        seconds=random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
    )
    await interaction.response.send_message(
        f"✅ Random trivia (area codes and counties, picked randomly) will now be posted in {target.mention}."
    )


# ---------------------------------------------------------------------------
# Area-code track: /trivia, /quiz, /quizhistory, /recenttrivia
# ---------------------------------------------------------------------------

@bot.tree.command(name="trivia", description="Post a random AREA CODE trivia message right now.")
async def trivia(interaction: discord.Interaction):
    await interaction.response.defer()
    record = data.pick_random_record()
    if interaction.guild_id:
        storage.add_trivia_history(interaction.guild_id, "areacode", record)
    map_file = get_areacode_map_file(record)
    if map_file:
        await interaction.followup.send(build_areacode_fact_message(record), file=map_file)
    else:
        await interaction.followup.send(build_areacode_fact_message(record))


@bot.tree.command(name="quiz", description="Quiz yourself on area codes.")
@app_commands.describe(
    mode="What the bot shows vs. what you have to guess",
    country="Country to quiz on",
    subdivisions="Optional: comma-separated states/provinces to include, e.g. NY, CA, TX (you can list as many as you want)",
    answer_mode="How you answer the question",
    num_options="Number of choices to show (buttons mode only, default 4)",
)
@app_commands.choices(mode=MODE_CHOICES_AREACODE, answer_mode=ANSWER_MODE_CHOICES)
@app_commands.autocomplete(country=country_autocomplete, subdivisions=subdivisions_autocomplete)
async def quiz(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    country: str,
    answer_mode: app_commands.Choice[str],
    subdivisions: Optional[str] = None,
    num_options: Optional[app_commands.Range[int, 2, 8]] = 4,
):
    country = data.clean_token(country)
    sub_list = [data.clean_token(s) for s in subdivisions.split(",")] if subdivisions else None
    country_name = data.country_display_name(country)

    await run_quiz_session(
        interaction,
        mode_value=mode.value,
        answer_mode_value=answer_mode.value,
        num_options=num_options,
        get_record=lambda excl: data.pick_random_record(country=country, subdivisions=sub_list, exclude_area_code=excl),
        record_key_fn=lambda r: r["area_code"],
        question_builder=build_areacode_question,
        grader=grade_areacode_answer,
        map_file_fn=get_areacode_map_file,
        get_distractor_pool=lambda: data.filter_records(country=country, subdivisions=sub_list),
        get_broadened_pool=lambda: data.filter_records(country=country),
        broaden_label=f"all of {country_name}",
        empty_message="No area codes match that filter. Try a different country/subdivision combo.",
        context_note_fn=lambda r: f"_(country: {country_name})_",
    )


@bot.tree.command(
    name="quizhistory",
    description="Quiz yourself using the area codes recently sent as trivia in this server.",
)
@app_commands.describe(
    mode="What the bot shows vs. what you have to guess",
    answer_mode="How you answer the question",
    num_options="Number of choices to show (buttons mode only, default 4)",
)
@app_commands.choices(mode=MODE_CHOICES_AREACODE, answer_mode=ANSWER_MODE_CHOICES)
async def quizhistory(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    answer_mode: app_commands.Choice[str],
    num_options: Optional[app_commands.Range[int, 2, 8]] = 4,
):
    guild_id = interaction.guild_id
    history_records = [e["record"] for e in storage.get_trivia_history(guild_id, kind="areacode")] if guild_id else []

    await run_quiz_session(
        interaction,
        mode_value=mode.value,
        answer_mode_value=answer_mode.value,
        num_options=num_options,
        get_record=lambda excl: pick_random_from_list(history_records, excl, key_fn=lambda r: r["area_code"]),
        record_key_fn=lambda r: r["area_code"],
        question_builder=build_areacode_question,
        grader=grade_areacode_answer,
        map_file_fn=get_areacode_map_file,
        get_distractor_pool=lambda: history_records,
        get_broadened_pool=lambda: data.filter_records(),
        broaden_label="the full area code dataset",
        empty_message=(
            "No area code trivia has been sent in this server yet \u2014 wait for a random fact "
            "(or run `/trivia` a few times) before quizzing on history."
        ),
        context_note_fn=lambda r: "_(from recently sent trivia)_",
    )


@bot.tree.command(name="recenttrivia", description="Show the most recently sent AREA CODE trivia facts in this server.")
@app_commands.describe(count="How many to show (default 10, max 25)")
async def recenttrivia(
    interaction: discord.Interaction,
    count: Optional[app_commands.Range[int, 1, 25]] = 10,
):
    guild_id = interaction.guild_id
    history = storage.get_trivia_history(guild_id, kind="areacode") if guild_id else []
    if not history:
        await interaction.response.send_message(
            "No area code trivia has been sent in this server yet. Set a channel with `/setchannel` "
            "or post one now with `/trivia`.",
            ephemeral=True,
        )
        return

    recent = list(reversed(history[-count:]))
    now = datetime.now(timezone.utc)
    lines = []
    for entry in recent:
        r = entry["record"]
        sent_at = datetime.fromisoformat(entry["sent_at"])
        ago = humanize_delta(now - sent_at)
        lines.append(f"📞 **{r['area_code']}** \u2014 {data.location_label(r)} ({r['country_name']}) \u2014 {ago} ago")

    await interaction.response.send_message("🕘 **Recently sent area code trivia:**\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# County track: /countytrivia, /countyquiz, /countyquizhistory, /countyrecenttrivia
# ---------------------------------------------------------------------------

@bot.tree.command(name="countytrivia", description="Post a random COUNTY trivia message right now.")
@app_commands.describe(state="Optional: comma-separated states to pick from, e.g. NY, CA, TX (you can list as many as you want)")
@app_commands.autocomplete(state=subdivisions_autocomplete)
async def countytrivia(interaction: discord.Interaction, state: Optional[str] = None):
    await interaction.response.defer()
    sub_list = [data.clean_token(s) for s in state.split(",")] if state else None
    cr = data.pick_random_county_record(subdivisions=sub_list)
    if cr is None:
        message = (
            "No county data matches that filter. Try different states, or leave it blank for all of the US."
            if sub_list else
            "No county data available yet."
        )
        await interaction.followup.send(message, ephemeral=True)
        return
    if interaction.guild_id:
        storage.add_trivia_history(interaction.guild_id, "county", cr)
    map_file = get_county_map_file(cr)
    if map_file:
        await interaction.followup.send(build_county_fact_message(cr), file=map_file)
    else:
        await interaction.followup.send(build_county_fact_message(cr))


@bot.tree.command(name="countyquiz", description="Quiz yourself on US counties and which area code covers them.")
@app_commands.describe(
    mode="What the bot shows vs. what you have to guess",
    state="Optional: comma-separated states to include, e.g. NY, CA, TX (you can list as many as you want)",
    answer_mode="How you answer the question",
    num_options="Number of choices to show (buttons mode only, default 4)",
)
@app_commands.choices(mode=MODE_CHOICES_COUNTY, answer_mode=ANSWER_MODE_CHOICES)
@app_commands.autocomplete(state=subdivisions_autocomplete)
async def countyquiz(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    answer_mode: app_commands.Choice[str],
    state: Optional[str] = None,
    num_options: Optional[app_commands.Range[int, 2, 8]] = 4,
):
    sub_list = [data.clean_token(s) for s in state.split(",")] if state else None

    await run_quiz_session(
        interaction,
        mode_value=mode.value,
        answer_mode_value=answer_mode.value,
        num_options=num_options,
        get_record=lambda excl: data.pick_random_county_record(subdivisions=sub_list, exclude=excl),
        record_key_fn=lambda r: (r["area_code"], r["county"]),
        question_builder=build_county_question,
        grader=grade_county_answer,
        map_file_fn=get_county_map_file,
        get_distractor_pool=lambda: data.get_county_records(subdivisions=sub_list),
        get_broadened_pool=lambda: data.get_county_records(),
        broaden_label="all covered counties",
        empty_message="No county data matches that filter. Try different states, or leave it blank for all of the US.",
        context_note_fn=lambda r: "",
    )


@bot.tree.command(
    name="countyquizhistory",
    description="Quiz yourself using the counties recently sent as trivia in this server.",
)
@app_commands.describe(
    mode="What the bot shows vs. what you have to guess",
    answer_mode="How you answer the question",
    num_options="Number of choices to show (buttons mode only, default 4)",
)
@app_commands.choices(mode=MODE_CHOICES_COUNTY, answer_mode=ANSWER_MODE_CHOICES)
async def countyquizhistory(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    answer_mode: app_commands.Choice[str],
    num_options: Optional[app_commands.Range[int, 2, 8]] = 4,
):
    guild_id = interaction.guild_id
    history_records = [e["record"] for e in storage.get_trivia_history(guild_id, kind="county")] if guild_id else []

    await run_quiz_session(
        interaction,
        mode_value=mode.value,
        answer_mode_value=answer_mode.value,
        num_options=num_options,
        get_record=lambda excl: pick_random_from_list(history_records, excl, key_fn=lambda r: (r["area_code"], r["county"])),
        record_key_fn=lambda r: (r["area_code"], r["county"]),
        question_builder=build_county_question,
        grader=grade_county_answer,
        map_file_fn=get_county_map_file,
        get_distractor_pool=lambda: history_records,
        get_broadened_pool=lambda: data.get_county_records(),
        broaden_label="all covered counties",
        empty_message=(
            "No county trivia has been sent in this server yet \u2014 wait for a random fact "
            "(or run `/countytrivia` a few times) before quizzing on history."
        ),
        context_note_fn=lambda r: "_(from recently sent trivia)_",
    )


@bot.tree.command(name="countyrecenttrivia", description="Show the most recently sent COUNTY trivia facts in this server.")
@app_commands.describe(count="How many to show (default 10, max 25)")
async def countyrecenttrivia(
    interaction: discord.Interaction,
    count: Optional[app_commands.Range[int, 1, 25]] = 10,
):
    guild_id = interaction.guild_id
    history = storage.get_trivia_history(guild_id, kind="county") if guild_id else []
    if not history:
        await interaction.response.send_message(
            "No county trivia has been sent in this server yet. Set a channel with `/setchannel` "
            "or post one now with `/countytrivia`.",
            ephemeral=True,
        )
        return

    recent = list(reversed(history[-count:]))
    now = datetime.now(timezone.utc)
    lines = []
    for entry in recent:
        r = entry["record"]
        sent_at = datetime.fromisoformat(entry["sent_at"])
        ago = humanize_delta(now - sent_at)
        lines.append(f"🗺️ **{r['area_code']}** \u2014 {data.county_label(r)} \u2014 {ago} ago")

    await interaction.response.send_message("🕘 **Recently sent county trivia:**\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Background random trivia loop \u2014 randomly alternates between an
# area-code fact and a county fact every time it fires.
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
                    kind = random.choice(["areacode", "county"])
                    if kind == "areacode":
                        record = data.pick_random_record()
                        text = build_areacode_fact_message(record)
                        map_file = get_areacode_map_file(record)
                    else:
                        record = data.pick_random_county_record()
                        if record is None:
                            kind, record = "areacode", data.pick_random_record()
                            text = build_areacode_fact_message(record)
                            map_file = get_areacode_map_file(record)
                        else:
                            text = build_county_fact_message(record)
                            map_file = get_county_map_file(record)

                    if map_file:
                        await channel.send(text, file=map_file)
                    else:
                        await channel.send(text)
                    storage.add_trivia_history(guild_id, kind, record)
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
