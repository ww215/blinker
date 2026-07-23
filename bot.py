import asyncio
import os
import random
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
    if not country_key or country_key not in data.COUNTRIES_WITH_SUBDIVISIONS:
        return []

    # support comma-separated lists: only autocomplete the last fragment
    prefix = ""
    fragment = current
    if "," in current:
        head, _, fragment = current.rpartition(",")
        prefix = head.strip() + ", "
    fragment = fragment.strip().lower()

    subs = data.get_subdivisions(country_key)
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

        result = "✅ Correct!" if self.is_correct else f"❌ Not quite. Correct answer: **{self.quiz_view.correct_label}**"
        await interaction.response.edit_message(content=f"{self.quiz_view.question_text}\n\n{result}", view=self.quiz_view)
        self.quiz_view.stop()


class QuizView(discord.ui.View):
    def __init__(self, asker_id: int, question_text: str, correct_label: str, options: list[str], correct_index: int):
        super().__init__(timeout=30)
        self.asker_id = asker_id
        self.question_text = question_text
        self.correct_label = correct_label
        for i, opt in enumerate(options):
            self.add_item(QuizButton(opt, i == correct_index, self))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await bot.tree.sync()
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
    subdivisions="Optional: comma-separated states/provinces to include (e.g. NY, CA)",
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
    sub_list = [s for s in subdivisions.split(",")] if subdivisions else None
    record = data.pick_random_record(country=country, subdivisions=sub_list)

    if record is None:
        await interaction.response.send_message(
            "No area codes match that filter. Try a different country/subdivision combo.", ephemeral=True
        )
        return

    country_name = data.country_display_name(country)
    mode_value = mode.value

    if mode_value == "code_to_place":
        question_text = f"📞 Which place uses area code **{record['area_code']}**? _(country: {country_name})_"
        correct_label = data.location_label(record)
        value_fn = data.location_label
    else:
        question_text = f"📍 What is the area code for **{data.location_label(record)}**? _(country: {country_name})_"
        correct_label = record["area_code"]
        value_fn = lambda r: r["area_code"]

    if answer_mode.value == "buttons":
        pool = data.filter_records(country=country, subdivisions=sub_list)
        if len(pool) < 2:
            pool = data.filter_records(country=country)
        distractors = data.pick_distractors(record, pool, value_fn, num_options - 1)
        options = distractors + [correct_label]
        random.shuffle(options)
        correct_index = options.index(correct_label)

        view = QuizView(interaction.user.id, question_text, correct_label, options, correct_index)
        await interaction.response.send_message(question_text, view=view)
        return

    # typed mode
    await interaction.response.send_message(f"{question_text}\n_(you have 30 seconds \u2014 just type your answer)_")

    def check(m: discord.Message):
        return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

    try:
        msg = await bot.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        await interaction.followup.send(f"⌛ Time's up! Correct answer: **{correct_label}**")
        return

    answer = msg.content.strip().lower()
    if mode_value == "code_to_place":
        acceptable = {record["city"].lower()}
        if record["subdivision"]:
            acceptable.add(record["subdivision"].lower())
            acceptable.add(record["subdivision_name"].lower())
        acceptable.add(record["country_name"].lower())
        is_correct = any(a in answer or answer in a for a in acceptable if a)
    else:
        is_correct = answer.replace(" ", "") == record["area_code"].replace(" ", "")

    if is_correct:
        await msg.reply(f"✅ Correct! **{correct_label}**")
    else:
        await msg.reply(f"❌ Not quite. Correct answer: **{correct_label}**")


# ---------------------------------------------------------------------------
# Background random trivia loop
# ---------------------------------------------------------------------------

@tasks.loop(seconds=1)
async def trivia_loop():
    now = datetime.now(timezone.utc)
    for guild_id, channel_id in storage.all_trivia_channels().items():
        next_time = _next_fire.get(guild_id)
        if next_time is None:
            _next_fire[guild_id] = now + timedelta(
                seconds=random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
            )
            continue
        if now >= next_time:
            channel = bot.get_channel(channel_id)
            if channel is not None:
                record = data.pick_random_record()
                try:
                    await channel.send(build_fact_message(record))
                except discord.HTTPException:
                    pass
            _next_fire[guild_id] = now + timedelta(
                seconds=random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
            )


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable before running the bot.")
    bot.run(token)


if __name__ == "__main__":
    main()
