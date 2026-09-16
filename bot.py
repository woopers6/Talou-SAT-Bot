import asyncio
import io
import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import scraper
from database import ANSWER_TYPES, DIFFICULTIES, Filters, QuestionBank
from renderer import Renderer

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = os.environ.get("GUILD_ID", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/questions.db")
CACHE_DIR = os.environ.get("CACHE_DIR", "data/cache")
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "24"))
RENDER_PAGES = int(os.environ.get("RENDER_PAGES", "2"))
RENDER_SCALE = float(os.environ.get("RENDER_SCALE", "2"))

SECTION_CHOICES = [app_commands.Choice(name="Math", value=2), app_commands.Choice(name="Reading and Writing", value=1)]
DIFFICULTY_CHOICES = [app_commands.Choice(name=name, value=code) for code, name in DIFFICULTIES.items()]
ANSWER_TYPE_CHOICES = [app_commands.Choice(name=name, value=code) for code, name in ANSWER_TYPES.items()]
QUESTION_ID = r"[0-9a-zA-Z]{1,20}"
ERROR_MESSAGE = "Something went wrong while getting that question. Please try again."

log = logging.getLogger("bot")


class SATBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.bank = QuestionBank(DATABASE_PATH)
        self.renderer = Renderer(cache_dir=CACHE_DIR, pages=RENDER_PAGES, scale=RENDER_SCALE)
        self.sync_lock = asyncio.Lock()

    async def setup_hook(self):
        await self.renderer.start()
        self.add_dynamic_items(ChoiceButton, AnswerButton, RevealButton, NextButton)
        self.tree.add_command(question_command)
        self.tree.add_command(lookup_command)
        self.tree.on_error = on_tree_error
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.refresh_questions.change_interval(hours=REFRESH_HOURS)
        self.refresh_questions.start()

    async def close(self):
        await self.renderer.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s with %s questions", self.user, self.bank.count())

    @tasks.loop(hours=24)
    async def refresh_questions(self):
        async with self.sync_lock:
            try:
                stats = await asyncio.to_thread(asyncio.run, scraper.sync(DATABASE_PATH))
                complete = stats["failed"] == 0
            except Exception:
                log.exception("Question bank refresh failed")
                complete = False
        if complete:
            self.refresh_questions.change_interval(hours=REFRESH_HOURS)
        else:
            log.info("Retrying the question bank refresh in 30 minutes")
            self.refresh_questions.change_interval(minutes=30)


bot = SATBot()


def build_view(question, filters):
    view = discord.ui.View(timeout=None)
    if question.answer_type == "mcq":
        for letter in question.choice_letters:
            view.add_item(ChoiceButton(question.question_id, letter))
    elif question.spr_answers:
        view.add_item(AnswerButton(question.question_id))
    view.add_item(RevealButton(question.question_id))
    if filters is not None:
        view.add_item(NextButton(filters))
    return view


async def send_question(interaction, question, filters):
    await interaction.response.defer(thinking=True)
    image = await bot.renderer.render(question)
    file = discord.File(io.BytesIO(image), filename=f"{question.question_id}.jpg")
    await interaction.followup.send(
        content=f"Question ID: `{question.question_id}`", file=file, view=build_view(question, filters)
    )


async def bank_not_ready(interaction):
    if bot.bank.count() == 0:
        await interaction.response.send_message(
            "The question bank is still downloading. Try again in a few minutes.", ephemeral=True
        )
        return True
    return False


async def domain_autocomplete(interaction, current):
    section = getattr(interaction.namespace, "section", None)
    current = current.lower()
    return [
        app_commands.Choice(name=name.strip()[:100], value=code)
        for code, name, _ in bot.bank.domains(section)
        if current in name.lower()
    ][:25]


async def skill_autocomplete(interaction, current):
    section = getattr(interaction.namespace, "section", None)
    domain = getattr(interaction.namespace, "domain", None)
    current = current.lower()
    return [
        app_commands.Choice(name=name.strip()[:100], value=code)
        for code, name, _, _ in bot.bank.skills(section, domain)
        if current in name.lower()
    ][:25]


@app_commands.command(name="question", description="Get a random question from the SAT Suite Question Bank")
@app_commands.describe(
    section="Math or Reading and Writing",
    domain="Content domain",
    skill="Skill within the domain",
    difficulty="Question difficulty",
    score_band="College Board score band from 1 (easiest) to 7 (hardest)",
    answer_format="Multiple choice or student-produced response",
    exclude_active="Leave out questions that also appear in Bluebook practice tests",
)
@app_commands.choices(section=SECTION_CHOICES, difficulty=DIFFICULTY_CHOICES, answer_format=ANSWER_TYPE_CHOICES)
@app_commands.autocomplete(domain=domain_autocomplete, skill=skill_autocomplete)
async def question_command(
    interaction: discord.Interaction,
    section: app_commands.Choice[int] | None = None,
    domain: str | None = None,
    skill: str | None = None,
    difficulty: app_commands.Choice[str] | None = None,
    score_band: app_commands.Range[int, 1, 7] | None = None,
    answer_format: app_commands.Choice[str] | None = None,
    exclude_active: bool = False,
):
    if await bank_not_ready(interaction):
        return
    section_id = section.value if section else None
    if domain and domain not in {code for code, _, _ in bot.bank.domains(section_id)}:
        await interaction.response.send_message("Pick a domain from the list.", ephemeral=True)
        return
    if skill and skill not in {code for code, _, _, _ in bot.bank.skills(section_id, domain)}:
        await interaction.response.send_message("Pick a skill from the list.", ephemeral=True)
        return
    filters = Filters(
        section_id=section_id,
        domain_code=domain,
        skill_code=skill,
        difficulty=difficulty.value if difficulty else None,
        score_band=score_band,
        answer_type=answer_format.value if answer_format else None,
        exclude_active=exclude_active,
    )
    question = bot.bank.random_question(filters)
    if question is None:
        await interaction.response.send_message("No questions match those filters.", ephemeral=True)
        return
    await send_question(interaction, question, filters)


@app_commands.command(name="lookup", description="Show a specific question by its Question ID")
@app_commands.describe(question_id="The 8 character Question ID, like 6d99b141")
async def lookup_command(interaction: discord.Interaction, question_id: str):
    if await bank_not_ready(interaction):
        return
    question = bot.bank.get(question_id)
    if question is None:
        await interaction.response.send_message(f"No question found with ID `{question_id}`.", ephemeral=True)
        return
    await send_question(interaction, question, None)


class ChoiceButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"sat:c:(?P<qid>{QUESTION_ID}):(?P<letter>[A-Z])"):
    def __init__(self, question_id, letter):
        self.question_id = question_id
        self.letter = letter
        super().__init__(
            discord.ui.Button(
                label=letter, style=discord.ButtonStyle.secondary, custom_id=f"sat:c:{question_id}:{letter}", row=0
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["qid"], match["letter"])

    async def callback(self, interaction):
        question = bot.bank.get(self.question_id)
        if question is None:
            await interaction.response.send_message("That question is no longer in the question bank.", ephemeral=True)
            return
        if self.letter in question.correct_choices:
            message = f"✅ **{self.letter}** is correct!"
        else:
            message = f"❌ **{self.letter}** is not correct. Try again or press **Show answer**."
        await interaction.response.send_message(message, ephemeral=True)


class AnswerModal(discord.ui.Modal, title="Enter your answer"):
    response = discord.ui.TextInput(label="Answer", placeholder="Example: 3/4 or 0.75", max_length=20)

    def __init__(self, question_id):
        super().__init__()
        self.question_id = question_id

    async def on_submit(self, interaction):
        question = bot.bank.get(self.question_id)
        if question is None:
            await interaction.response.send_message("That question is no longer in the question bank.", ephemeral=True)
            return
        value = self.response.value.strip()
        if question.check_spr(value):
            message = f"✅ **{value}** is correct!"
        else:
            message = f"❌ **{value}** is not correct. Try again or press **Show answer**."
        await interaction.response.send_message(message, ephemeral=True)


class AnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"sat:s:(?P<qid>{QUESTION_ID})"):
    def __init__(self, question_id):
        self.question_id = question_id
        super().__init__(
            discord.ui.Button(
                label="Enter answer", style=discord.ButtonStyle.secondary, custom_id=f"sat:s:{question_id}", row=0
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["qid"])

    async def callback(self, interaction):
        question = bot.bank.get(self.question_id)
        if question is None:
            await interaction.response.send_message("That question is no longer in the question bank.", ephemeral=True)
            return
        if not question.spr_answers:
            await interaction.response.send_message(
                "This question does not have an answer key to check against. Press **Show answer** instead.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AnswerModal(self.question_id))


class RevealButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"sat:r:(?P<qid>{QUESTION_ID})"):
    def __init__(self, question_id):
        self.question_id = question_id
        super().__init__(
            discord.ui.Button(
                label="Show answer", style=discord.ButtonStyle.success, custom_id=f"sat:r:{question_id}", row=1
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["qid"])

    async def callback(self, interaction):
        question = bot.bank.get(self.question_id)
        if question is None:
            await interaction.response.send_message("That question is no longer in the question bank.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            image = await bot.renderer.render(question, reveal=True)
        except Exception:
            log.exception("Rendering the answer for %s failed", question.question_id)
            await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
            return
        file = discord.File(io.BytesIO(image), filename=f"{question.question_id}-answer.jpg")
        await interaction.followup.send(file=file, ephemeral=True)


class NextButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sat:n:(?P<filters>[0-9A-Za-z.&|]*)"):
    def __init__(self, filters):
        self.filters = filters
        super().__init__(
            discord.ui.Button(
                label="New question", style=discord.ButtonStyle.primary, custom_id=f"sat:n:{filters.encode()}", row=1
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(Filters.decode(match["filters"]))

    async def callback(self, interaction):
        question = bot.bank.random_question(self.filters)
        if question is None:
            await interaction.response.send_message("No questions match those filters anymore.", ephemeral=True)
            return
        try:
            await send_question(interaction, question, self.filters)
        except Exception:
            log.exception("Sending question %s failed", question.question_id)
            await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)


async def on_tree_error(interaction, error):
    log.error("Command failed", exc_info=error)
    if interaction.response.is_done():
        await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
    else:
        await interaction.response.send_message(ERROR_MESSAGE, ephemeral=True)


@bot.event
async def on_error(event, *args, **kwargs):
    log.exception("Unhandled error in %s", event)


def main():
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file")
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
