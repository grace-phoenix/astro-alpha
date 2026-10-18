import json
import logging
import os
import re
from collections import defaultdict, deque
from pathlib import Path
import asyncio
import time

import discord
from discord import app_commands

from aiohttp import web

LOGIN_COOLDOWN = 60  # seconds between login attempts
_attempt_task: asyncio.Task | None = None
_last_attempt = 0.0


async def attempt_login():
    """Log in, then keep the gateway connection running until it drops."""
    global _last_attempt
    _last_attempt = time.monotonic()
    try:
        await bot.login(TOKEN)
    except discord.HTTPException as e:
        if e.status == 429:
            logging.warning("Discord rate limited the login. Will retry on a later ping.")
        else:
            logging.error("Login failed with HTTP %s: %s", e.status, e.text)
        return
    except Exception:
        logging.exception("Unexpected error during login. Will retry on a later ping.")
        return

    logging.info("Login succeeded, connecting to the gateway")
    try:
        await bot.connect(reconnect=True)  # runs until the connection ends
    except Exception:
        logging.exception("Gateway connection ended. Will reconnect on a later ping.")


def ensure_login_attempt():
    """Start a login attempt if the bot is offline, none is running, and the cooldown has passed."""
    global _attempt_task
    if bot.is_ready():
        return
    if _attempt_task is not None and not _attempt_task.done():
        return
    if time.monotonic() - _last_attempt < LOGIN_COOLDOWN:
        return
    _attempt_task = asyncio.create_task(attempt_login())


async def keepalive(request: web.Request) -> web.Response:
    ensure_login_attempt()
    return web.Response(text="online" if bot.is_ready() else "starting")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", keepalive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()

TOKEN = os.environ["DISCORD_TOKEN"]
CONFIG_PATH = Path("welcome_config.json")

DEFAULT_MESSAGE = "Welcome to {server}, {mention}! You are member #{count}."
DEFAULTS = {
    "enabled": True,
    "channel_id": None,
    "message": DEFAULT_MESSAGE,
    "delete_after": 0,  # seconds, 0 = never delete
    # --- confessions ---
    "confess_enabled": True,
    "confess_channel_id": None,
    "confess_max_len": 1000,  # characters in a confession
    "reply_max_len": 500,  # characters in a reply
    "confess_rate_count": 3,  # posts allowed per window, 0 = no limit
    "confess_rate_minutes": 10,  # window length
    "confess_manager_role_ids": [],  # extra roles allowed to use /configure (never mutate this list)
    "confess_counter": 0,  # last confession number used
}

logging.basicConfig(level=logging.INFO)


# ---------- tiny JSON "database" ----------
class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, guild_id: int) -> dict:
        return {**DEFAULTS, **self.data.get(str(guild_id), {})}

    def update(self, guild_id: int, **changes) -> None:
        cfg = self.data.setdefault(str(guild_id), {})
        cfg.update(changes)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


config = Config(CONFIG_PATH)


# ---------- message templating ----------
VARIABLES_HELP = (
    "`{user}` = username, `{mention}` = @mention, "
    "`{server}` = server name, `{count}` = member count"
)


def render(template: str, member: discord.Member) -> str:
    # Plain replace (not str.format) so stray { } in the text can't crash it.
    return (
        template.replace("{user}", member.display_name)
        .replace("{mention}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )


async def send_welcome(member: discord.Member, channel: discord.abc.Messageable, cfg: dict):
    await channel.send(
        render(cfg["message"], member),
        delete_after=cfg["delete_after"] or None,
        # only the new member can be pinged; no @everyone / roles
        allowed_mentions=discord.AllowedMentions(
            users=[member], roles=False, everyone=False
        ),
    )


# ---------- bot ----------
intents = discord.Intents.default()
intents.members = True  # privileged: enable "Server Members Intent" in the dev portal


class WelcomeBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(ConfessionView())  # keeps old confession buttons working after restarts
        if os.environ.get("SYNC_COMMANDS") == "1":
            await self.tree.sync()


bot = WelcomeBot()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use this command."
    elif isinstance(error, app_commands.TransformerError):
        msg = "I can't see that channel. Give me View Channel and Send Messages there."
    else:
        logging.exception("Command error", exc_info=error)
        msg = "Something went wrong. Check the bot's console."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    cfg = config.get(member.guild.id)
    if not cfg["enabled"] or not cfg["channel_id"]:
        return
    channel = member.guild.get_channel(cfg["channel_id"])
    if channel is None:
        logging.warning("Welcome channel missing in guild %s", member.guild.id)
        return
    try:
        await send_welcome(member, channel, cfg)
    except discord.Forbidden:
        logging.warning("No permission to send in #%s", channel.name)
    except discord.HTTPException as e:
        logging.warning("Failed to send welcome: %s", e)


# ---------- slash commands: /welcome ... ----------
welcome = app_commands.Group(
    name="welcome",
    description="Configure the welcome message",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)

@welcome.command(name="channel", description="Choose the channel for welcome messages")
@app_commands.describe(channel="Channel to post welcomes in")
async def welcome_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages):
        await interaction.response.send_message(
            f"I can't send messages in {channel.mention}. Fix my permissions there first.",
            ephemeral=True,
        )
        return
    config.update(interaction.guild_id, channel_id=channel.id)
    await interaction.response.send_message(
        f"Welcome messages will go to {channel.mention}.", ephemeral=True
    )


@welcome.command(name="message", description="Set the welcome text")
@app_commands.describe(text="Variables: {user} {mention} {server} {count}")
async def welcome_message(
    interaction: discord.Interaction, text: app_commands.Range[str, 1, 1500]
):
    config.update(interaction.guild_id, message=text)
    await interaction.response.send_message(
        f"Message saved.\nAvailable variables: {VARIABLES_HELP}", ephemeral=True
    )


@welcome.command(name="autodelete", description="Delete the welcome message after N seconds")
@app_commands.describe(seconds="0 turns auto-delete off (max 86400 = 24h)")
async def welcome_autodelete(
    interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 86400]
):
    config.update(interaction.guild_id, delete_after=seconds)
    msg = "Auto-delete turned off." if seconds == 0 else f"Welcomes will be deleted after {seconds}s."
    await interaction.response.send_message(msg, ephemeral=True)


@welcome.command(name="toggle", description="Turn welcome messages on or off")
@app_commands.describe(enabled="True to enable, False to disable")
async def welcome_toggle(interaction: discord.Interaction, enabled: bool):
    config.update(interaction.guild_id, enabled=enabled)
    await interaction.response.send_message(
        f"Welcome messages {'enabled' if enabled else 'disabled'}.", ephemeral=True
    )


@welcome.command(name="test", description="Send a test welcome as if you just joined")
async def welcome_test(interaction: discord.Interaction):
    cfg = config.get(interaction.guild_id)
    channel = interaction.guild.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
    if channel is None:
        await interaction.response.send_message(
            "No channel set yet. Use `/welcome channel` first.", ephemeral=True
        )
        return
    try:
        await send_welcome(interaction.user, channel, cfg)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I'm not allowed to send messages in {channel.mention}.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"Test sent to {channel.mention}.", ephemeral=True)


@welcome.command(name="show", description="Show the current welcome settings")
async def welcome_show(interaction: discord.Interaction):
    cfg = config.get(interaction.guild_id)
    channel = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set"
    delete = f"{cfg['delete_after']}s" if cfg["delete_after"] else "off"
    await interaction.response.send_message(
        f"**Enabled:** {cfg['enabled']}\n"
        f"**Channel:** {channel}\n"
        f"**Auto-delete:** {delete}\n"
        f"**Message:** {cfg['message']}\n"
        f"**Variables:** {VARIABLES_HELP}\nxoxo Grace (good moral character)",
        ephemeral=True,
    )


# ======================================================================
#  ANONYMOUS CONFESSIONS
#    /confess    -> anyone
#    /configure  -> Manage Server, or roles added with /configure addrole
# ======================================================================
CONFESS_MAX_LEN_CAP = 1800  # highest value the confession limit can be set to
REPLY_MAX_LEN_CAP = 1000  # an embed field holds 1024 characters, so replies stay under that
EMBED_TOTAL_LIMIT = 6000  # Discord's cap on all the text in one embed
EMBED_MAX_FIELDS = 25  # Discord's cap on fields in one embed

# Nothing here links a post to its author. The only per-user data is a list of
# timestamps for the rate limit, kept in memory and never saved with any text.
_post_times: dict[tuple[int, int], deque] = {}
_guild_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_message_locks: dict[int, asyncio.Lock] = {}


def message_lock(message_id: int) -> asyncio.Lock:
    """One lock per confession message so two simultaneous replies can't overwrite each other."""
    if len(_message_locks) > 500:
        for key in [k for k, lock in _message_locks.items() if not lock.locked()]:
            del _message_locks[key]
    return _message_locks.setdefault(message_id, asyncio.Lock())


# ---------- permissions ----------
def is_guild_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


def is_confess_manager(member: discord.Member) -> bool:
    if is_guild_admin(member):
        return True
    allowed = set(config.get(member.guild.id)["confess_manager_role_ids"])
    return any(role.id in allowed for role in member.roles)


# ---------- rate limit ----------
def rate_limit_wait(guild_id: int, user_id: int, cfg: dict) -> float:
    """Seconds the user must wait before posting again (0 = free to post)."""
    count = cfg["confess_rate_count"]
    if count <= 0:
        return 0.0
    window = cfg["confess_rate_minutes"] * 60
    key = (guild_id, user_id)
    times = _post_times.get(key)
    if not times:
        return 0.0
    now = time.monotonic()
    while times and now - times[0] >= window:
        times.popleft()
    if not times:
        _post_times.pop(key, None)
        return 0.0
    if len(times) < count:
        return 0.0
    return window - (now - times[0])


def record_post(guild_id: int, user_id: int) -> None:
    _post_times.setdefault((guild_id, user_id), deque()).append(time.monotonic())


def posting_problem(interaction: discord.Interaction, cfg: dict, need_channel: bool = True) -> str | None:
    """A user-facing reason the person can't post right now, or None if they can."""
    if not cfg["confess_enabled"]:
        return "Confessions are turned off on this server right now."
    if need_channel:
        channel_id = cfg["confess_channel_id"]
        if not channel_id or interaction.guild.get_channel(channel_id) is None:
            return "Confessions haven't been set up here yet. Ask a moderator to choose a channel."
    wait = rate_limit_wait(interaction.guild_id, interaction.user.id, cfg)
    if wait > 0:
        return f"You're posting too fast. You can post again <t:{int(time.time() + wait) + 1}:R>."
    return None


async def fail_with_echo(interaction: discord.Interaction, reason: str, text: str) -> None:
    """Tell the person why it failed and hand their text back so it isn't lost."""
    await interaction.followup.send(
        f"{reason}\n\nYour text, so you don't lose it:\n>>> {text[:1800]}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------- embeds ----------
# A confession is one embed. Each reply is one field of that embed, named like
#   "Reply #1"   "↳ Reply #2 · to #1"   "↳ ↳ Reply #3 · to #2"
# so everything can be read back from the message itself (no database needed).
REPLY_NAME_RE = re.compile(r"#(\d+)(?: · to #(\d+))?")


def build_confession_embed(number: int, text: str) -> discord.Embed:
    return discord.Embed(
        title=f"Anonymous Confession #{number}", description=text, colour=discord.Colour.blurple()
    )


def build_reply_embed(text: str, reply_to: int | None) -> discord.Embed:
    """Used only when the original message can't be edited (the reply becomes its own post)."""
    description = text if reply_to is None else f"*Replying to reply #{reply_to}*\n{text}"
    return discord.Embed(title="Anonymous Reply", description=description, colour=discord.Colour.teal())


def parse_replies(embed: discord.Embed) -> list[tuple[int, int | None]]:
    """[(reply number, parent reply number or None)] in display order."""
    entries = []
    for position, field in enumerate(embed.fields, start=1):
        match = REPLY_NAME_RE.search(field.name or "")
        if match:
            parent = int(match.group(2)) if match.group(2) else None
            entries.append((int(match.group(1)), parent))
        else:
            entries.append((position, None))
    return entries


def parse_target(raw: str | None) -> int | None:
    match = re.search(r"\d+", raw or "")
    return int(match.group()) if match else None


def add_reply_to_embed(
    embed: discord.Embed, text: str, target: int | None
) -> tuple[discord.Embed | None, int | None]:
    """Insert a reply under `target` (None = under the post itself).

    Returns (new embed, the target actually used). The embed is None if the reply
    wouldn't fit in Discord's embed limits. The target used is None if the
    requested reply number doesn't exist.
    """
    entries = parse_replies(embed)
    parent_of = dict(entries)
    effective = target if target in parent_of else None

    if len(embed.fields) >= EMBED_MAX_FIELDS:
        return None, effective

    def is_inside(number: int | None, root: int) -> bool:
        seen = set()
        while number is not None and number not in seen:
            if number == root:
                return True
            seen.add(number)
            number = parent_of.get(number)
        return False

    def ancestors(number: int) -> int:
        count, seen = 0, set()
        while parent_of.get(number) is not None and number not in seen:
            seen.add(number)
            number = parent_of[number]
            count += 1
        return count

    if effective is None:
        index, depth = len(entries), 0
    else:
        # right after the last reply that sits under the one being answered
        index = max(i for i, (n, _) in enumerate(entries) if is_inside(n, effective)) + 1
        depth = ancestors(effective) + 1

    number = max(parent_of, default=0) + 1
    name = "↳ " * depth + f"Reply #{number}" + (f" · to #{effective}" if effective is not None else "")

    new_embed = embed.copy()
    new_embed.insert_field_at(index, name=name, value=text, inline=False)
    if len(new_embed) > EMBED_TOTAL_LIMIT:
        return None, effective
    return new_embed, effective


# ---------- posting ----------
async def submit_confession(interaction: discord.Interaction, text: str) -> None:
    """Shared by /confess send and the pop-up form."""
    await interaction.response.defer(ephemeral=True)
    text = text.strip()
    guild = interaction.guild
    cfg = config.get(guild.id)

    if not text:
        await interaction.followup.send("That confession was empty, so nothing was posted.", ephemeral=True)
        return

    problem = posting_problem(interaction, cfg)
    if problem is None and len(text) > cfg["confess_max_len"]:
        problem = (
            f"That confession is too long. The limit is {cfg['confess_max_len']} characters "
            f"(yours was {len(text)})."
        )
    channel = guild.get_channel(cfg["confess_channel_id"]) if cfg["confess_channel_id"] else None
    if problem is None and not isinstance(channel, discord.TextChannel):
        problem = "I can't find the confession channel any more. Ask a moderator to set it again."
    if problem:
        await fail_with_echo(interaction, problem, text)
        return

    try:
        async with _guild_locks[guild.id]:
            number = config.get(guild.id)["confess_counter"] + 1
            posted = await channel.send(
                embed=build_confession_embed(number, text),
                view=ConfessionView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            config.update(guild.id, confess_counter=number)
    except discord.HTTPException as e:
        logging.warning("Could not post a confession in guild %s: %s", guild.id, e)
        await fail_with_echo(
            interaction,
            "I couldn't post in the confession channel. A moderator needs to check my permissions there.",
            text,
        )
        return

    record_post(guild.id, interaction.user.id)
    await interaction.followup.send(f"Your confession was posted anonymously: {posted.jump_url}", ephemeral=True)


async def submit_reply(
    interaction: discord.Interaction,
    channel_id: int,
    message_id: int,
    text: str,
    target_raw: str | None,
) -> None:
    """Add a reply to a confession: edit the original message if possible, otherwise post a new one."""
    await interaction.response.defer(ephemeral=True)
    text = text.strip()
    guild = interaction.guild
    cfg = config.get(guild.id)

    if not text:
        await interaction.followup.send("That reply was empty, so nothing was posted.", ephemeral=True)
        return

    problem = posting_problem(interaction, cfg, need_channel=False)
    if problem is None and len(text) > cfg["reply_max_len"]:
        problem = (
            f"That reply is too long. The limit is {cfg['reply_max_len']} characters "
            f"(yours was {len(text)})."
        )
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None and cfg["confess_channel_id"]:
        channel = guild.get_channel(cfg["confess_channel_id"])
    if problem is None and not isinstance(channel, (discord.TextChannel, discord.Thread)):
        problem = "I can't find the channel that confession was in any more."
    if problem:
        await fail_with_echo(interaction, problem, text)
        return

    requested = parse_target(target_raw)
    effective = requested
    posted = None
    edited = False

    async with message_lock(message_id):
        original = None
        try:
            original = await channel.fetch_message(message_id)
        except discord.HTTPException:
            pass  # deleted, or I can't read history: fall back to a new message below

        if original is not None and original.author.id == interaction.client.user.id and original.embeds:
            new_embed, effective = add_reply_to_embed(original.embeds[0], text, requested)
            if new_embed is not None:
                try:
                    await original.edit(embed=new_embed)
                    posted, edited = original, True
                except discord.HTTPException as e:
                    logging.warning("Could not edit confession %s: %s", message_id, e)

        if posted is None:
            # Couldn't edit (embed full, message gone, no permission...): the reply still gets posted.
            reference = discord.MessageReference(
                message_id=message_id, channel_id=channel.id, guild_id=guild.id, fail_if_not_exists=False
            )
            try:
                posted = await channel.send(
                    embed=build_reply_embed(text, effective),
                    view=ConfessionView(),
                    reference=reference,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                logging.warning("Could not post a reply in guild %s: %s", guild.id, e)
                await fail_with_echo(
                    interaction,
                    "I couldn't post your reply. A moderator needs to check my permissions in that channel.",
                    text,
                )
                return

    record_post(guild.id, interaction.user.id)
    if edited:
        msg = f"Your reply was added anonymously: {posted.jump_url}"
    else:
        msg = (
            "I couldn't add it to the original message (it may be full or gone), "
            f"so I posted your reply as a new message instead: {posted.jump_url}"
        )
    if requested is not None and effective is None:
        msg += f"\nI couldn't find reply #{requested}, so this went under the post itself."
    await interaction.followup.send(msg, ephemeral=True)


# ---------- pop-up forms and buttons ----------
async def reply_ephemeral(interaction: discord.Interaction, msg: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        logging.warning("Could not deliver a message to the user")


class ConfessModal(discord.ui.Modal):
    def __init__(self, max_len: int):
        super().__init__(title="New anonymous confession")
        self.confession = discord.ui.TextInput(
            label="Your confession",
            style=discord.TextStyle.paragraph,
            placeholder="It's posted anonymously.",
            max_length=max_len,
        )
        self.add_item(self.confession)

    async def on_submit(self, interaction: discord.Interaction):
        await submit_confession(interaction, self.confession.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.exception("Confession form error", exc_info=error)
        await reply_ephemeral(interaction, "Something went wrong while posting. Please try again.")


class ReplyModal(discord.ui.Modal):
    def __init__(self, max_len: int, channel_id: int, message_id: int, reply_numbers: list[int]):
        super().__init__(title="Reply anonymously")
        self.channel_id = channel_id
        self.message_id = message_id
        self.reply = discord.ui.TextInput(
            label="Your reply",
            style=discord.TextStyle.paragraph,
            placeholder="It's posted anonymously.",
            max_length=max_len,
        )
        if reply_numbers:
            hint = f"Blank = reply to the post. Or a reply number (1-{max(reply_numbers)})."
        else:
            hint = "Leave blank to reply to the post."
        self.target = discord.ui.TextInput(
            label="Reply to reply number (optional)",
            placeholder=hint,
            required=False,
            max_length=6,
        )
        self.add_item(self.reply)
        # self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        await submit_reply(
            interaction, self.channel_id, self.message_id, self.reply.value, self.target.value
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.exception("Reply form error", exc_info=error)
        await reply_ephemeral(interaction, "Something went wrong while posting. Please try again.")


class ConfessionView(discord.ui.View):
    """The two buttons under every confession. Fixed custom_ids make them survive restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logging.exception("Confession button error", exc_info=error)
        await reply_ephemeral(interaction, "Something went wrong. Please try again.")

    @discord.ui.button(
        label="New confession", emoji="🔽", style=discord.ButtonStyle.primary, custom_id="confess:new"
    )
    async def new_confession_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = config.get(interaction.guild_id)
        problem = posting_problem(interaction, cfg)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_modal(ConfessModal(cfg["confess_max_len"]))

    @discord.ui.button(
        label="Reply", emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="confess:reply"
    )
    async def reply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = config.get(interaction.guild_id)
        problem = posting_problem(interaction, cfg, need_channel=False)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        message = interaction.message
        numbers = [n for n, _ in parse_replies(message.embeds[0])] if message.embeds else []
        await interaction.response.send_modal(
            ReplyModal(cfg["reply_max_len"], interaction.channel_id, message.id, numbers)
        )


# ---------- slash commands: /confess ----------
confess = app_commands.Group(
    name="confess",
    description="Post an anonymous confession",
    guild_only=True,
)


@confess.command(name="send", description="Post an anonymous confession")
@app_commands.describe(message="Your confession. Nobody will see who sent it")
async def confess_send(
    interaction: discord.Interaction, message: app_commands.Range[str, 1, CONFESS_MAX_LEN_CAP]
):
    await submit_confession(interaction, message)


# ---------- slash commands: /configure ----------
class ConfigureGroup(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Returning False raises CheckFailure, handled by the error handler.
        return is_confess_manager(interaction.user)


configure = ConfigureGroup(
    name="configure",
    description="Configure anonymous confessions",
    guild_only=True,
)


@configure.command(name="channel", description="Choose the channel confessions are posted in")
@app_commands.describe(channel="Channel for confessions")
async def configure_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)
    needed = [
        ("View Channel", perms.view_channel),
        ("Send Messages", perms.send_messages),
        ("Embed Links", perms.embed_links),
        ("Read Message History", perms.read_message_history),
    ]
    missing = [name for name, ok in needed if not ok]
    if missing:
        await interaction.response.send_message(
            f"I'm missing permissions in {channel.mention}: **{', '.join(missing)}**. "
            "Give me those there, then try again.",
            ephemeral=True,
        )
        return
    config.update(interaction.guild_id, confess_channel_id=channel.id)
    await interaction.response.send_message(
        f"Confessions will be posted in {channel.mention}.", ephemeral=True
    )


@configure.command(name="toggle", description="Turn confessions on or off")
@app_commands.describe(enabled="True to enable, False to disable")
async def configure_toggle(interaction: discord.Interaction, enabled: bool):
    config.update(interaction.guild_id, confess_enabled=enabled)
    await interaction.response.send_message(
        f"Confessions {'enabled' if enabled else 'disabled'}.", ephemeral=True
    )


@configure.command(name="ratelimit", description="Limit how often one person can post (confessions and replies)")
@app_commands.describe(
    count="Posts allowed per window. 0 turns the limit off",
    minutes="Window length in minutes (leave empty to keep the current one)",
)
async def configure_ratelimit(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 0, 100],
    minutes: app_commands.Range[int, 1, 1440] | None = None,
):
    changes = {"confess_rate_count": count}
    if minutes is not None:
        changes["confess_rate_minutes"] = minutes
    config.update(interaction.guild_id, **changes)
    cfg = config.get(interaction.guild_id)
    if count == 0:
        msg = "Rate limit turned off."
    else:
        msg = f"Each person can now post {count} time(s) per {cfg['confess_rate_minutes']} minute(s)."
    await interaction.response.send_message(msg, ephemeral=True)


@configure.command(name="maxlength", description="Set the maximum length of confessions and replies")
@app_commands.describe(
    confession=f"Max characters in a confession (up to {CONFESS_MAX_LEN_CAP})",
    reply=f"Max characters in a reply (up to {REPLY_MAX_LEN_CAP})",
)
async def configure_maxlength(
    interaction: discord.Interaction,
    confession: app_commands.Range[int, 1, CONFESS_MAX_LEN_CAP] | None = None,
    reply: app_commands.Range[int, 1, REPLY_MAX_LEN_CAP] | None = None,
):
    changes = {}
    if confession is not None:
        changes["confess_max_len"] = confession
    if reply is not None:
        changes["reply_max_len"] = reply
    if not changes:
        await interaction.response.send_message(
            "Give me at least one of `confession` or `reply`.", ephemeral=True
        )
        return
    config.update(interaction.guild_id, **changes)
    cfg = config.get(interaction.guild_id)
    await interaction.response.send_message(
        f"Limits are now {cfg['confess_max_len']} characters for confessions "
        f"and {cfg['reply_max_len']} for replies.",
        ephemeral=True,
    )


@configure.command(name="addrole", description="Let a role use the /configure commands")
@app_commands.describe(role="Role to allow")
async def configure_addrole(interaction: discord.Interaction, role: discord.Role):
    if not is_guild_admin(interaction.user):
        await interaction.response.send_message(
            "Only members with **Manage Server** can change which roles have access.", ephemeral=True
        )
        return
    if role.is_default():
        await interaction.response.send_message(
            "That would give everyone access. Pick a specific role.", ephemeral=True
        )
        return
    ids = list(config.get(interaction.guild_id)["confess_manager_role_ids"])
    if role.id not in ids:
        ids.append(role.id)
        config.update(interaction.guild_id, confess_manager_role_ids=ids)
    await interaction.response.send_message(
        f"Members with {role.mention} can now use `/configure`.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@configure.command(name="removerole", description="Stop a role from using the /configure commands")
@app_commands.describe(role="Role to remove")
async def configure_removerole(interaction: discord.Interaction, role: discord.Role):
    if not is_guild_admin(interaction.user):
        await interaction.response.send_message(
            "Only members with **Manage Server** can change which roles have access.", ephemeral=True
        )
        return
    ids = [r for r in config.get(interaction.guild_id)["confess_manager_role_ids"] if r != role.id]
    config.update(interaction.guild_id, confess_manager_role_ids=ids)
    await interaction.response.send_message(
        f"{role.mention} can no longer use `/configure`.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@configure.command(name="show", description="Show the current confession settings")
async def configure_show(interaction: discord.Interaction):
    cfg = config.get(interaction.guild_id)
    channel = f"<#{cfg['confess_channel_id']}>" if cfg["confess_channel_id"] else "not set"
    if cfg["confess_rate_count"] > 0:
        rate = f"{cfg['confess_rate_count']} post(s) per {cfg['confess_rate_minutes']} minute(s)"
    else:
        rate = "off"
    roles = ", ".join(f"<@&{r}>" for r in cfg["confess_manager_role_ids"]) or "none"
    await interaction.response.send_message(
        f"**Confessions:** {'on' if cfg['confess_enabled'] else 'off'}\n"
        f"**Channel:** {channel}\n"
        f"**Max length:** {cfg['confess_max_len']} (confession), {cfg['reply_max_len']} (reply)\n"
        f"**Rate limit:** {rate}\n"
        f"**Extra roles that can use /configure:** {roles} (plus anyone with Manage Server)\nxoxo Grace (good moral character)",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(welcome)
bot.tree.add_command(confess)
bot.tree.add_command(configure)

async def main():
    await start_web_server()
    ensure_login_attempt()  # first try at startup; later tries come from pings
    await asyncio.Event().wait()  # keep the process alive forever


if __name__ == "__main__":
    asyncio.run(main())
