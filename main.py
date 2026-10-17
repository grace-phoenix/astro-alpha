import json
import logging
import os
from pathlib import Path

import discord
from discord import app_commands

from aiohttp import web

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
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
        await start_web_server()
        await self.tree.sync()  # global sync; can take a few minutes to show up


bot = WelcomeBot()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.TransformerError):
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
        f"**Variables:** {VARIABLES_HELP}",
        ephemeral=True,
    )


bot.tree.add_command(welcome)

bot.run(TOKEN)
