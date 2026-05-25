import discord
from discord.ext import commands
from discord import app_commands, Embed
import wavelink
import asyncio
import os
import random
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN")
SERVER_ID    = int(os.getenv("SERVER_ID", 0))
VOICE_CH_ID  = int(os.getenv("VOICE_CHANNEL_ID", 0))
TEXT_CH_ID   = int(os.getenv("TEXT_CHANNEL_ID", 0))

# ─── FREE LAVALINK NODES (Multiple fallbacks) ─────────────
# Sources: Spotify, YouTube, SoundCloud, Apple Music, Deezer etc.
# Using multiple nodes — bot will auto-failover if one goes down
LAVALINK_NODES = [
    # Node 1 — Serenetia (v4, SSL)
    {"uri": "https://lavalinkv4.serenetia.com:443", "password": "https://seretia.link/discord"},
    # Node 2 — Jirayu (v4, SSL)
    {"uri": "https://lavalink.jirayu.net:443", "password": "youshallnotpass"},
    # Node 3 — TriniumHost (v4, SSL)
    {"uri": "https://lavalink-v4.triniumhost.com:443", "password": "free"},
    # Node 4 — MilloHost (v4, SSL)
    {"uri": "https://lava-v4.millohost.my.id:443", "password": "https://discord.gg/mjS5J2K3ep"},
]

# ─── INTENTS ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── STATE PER GUILD ──────────────────────────────────────
np_message = {}   # guild_id -> discord.Message

# ─── HELPERS ──────────────────────────────────────────────
def fmt_duration(ms):
    if not ms: return "Live"
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

def build_np_embed(player: wavelink.Player, track: wavelink.Playable):
    vol   = player.volume
    loop  = player.queue.mode == wavelink.QueueMode.loop
    shuf  = player.queue.mode == wavelink.QueueMode.normal and getattr(player, "_shuffle", False)
    qlen  = len(player.queue)

    embed = Embed(
        title="▶️  Now Playing",
        description=f"### [{track.title}]({track.uri})",
        color=0x1db954
    )
    if track.artwork:
        embed.set_image(url=track.artwork)
    embed.add_field(name="⏱️ Duration", value=fmt_duration(track.length), inline=True)
    embed.add_field(name="🎤 Artist",   value=track.author or "Unknown",  inline=True)
    embed.add_field(name="🔊 Volume",   value=f"{vol}%",                  inline=True)
    embed.add_field(name="🔁 Loop",     value="On" if loop else "Off",    inline=True)
    embed.add_field(name="📋 Queue",    value=f"{qlen} song(s)",          inline=True)
    embed.set_footer(text="🎵 Use the buttons below to control playback")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

async def refresh_np(guild: discord.Guild):
    gid = guild.id
    msg = np_message.get(gid)
    player: wavelink.Player = guild.voice_client
    if msg and player and player.current:
        try:
            await msg.edit(
                embed=build_np_embed(player, player.current),
                view=PlayerView(gid)
            )
        except Exception:
            pass

# ─── PLAYER BUTTONS ───────────────────────────────────────
class PlayerView(discord.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=None)
        self.gid = gid

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Not playing!", ephemeral=True)
        if player.queue.history and len(player.queue.history) > 0:
            prev = player.queue.history[-1]
            await player.play(prev)
            await interaction.response.send_message(f"⏮️ Playing previous: **{prev.title}**", ephemeral=True)
            await refresh_np(interaction.guild)
        else:
            await interaction.response.send_message("❌ No previous songs!", ephemeral=True)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="pause_resume")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Not playing!", ephemeral=True)
        await player.pause(not player.paused)
        button.emoji = "▶️" if player.paused else "⏸️"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player or not player.current:
            return await interaction.response.send_message("❌ Not playing!", ephemeral=True)
        await player.skip(force=True)
        await interaction.response.send_message("⏭️ Skipped!", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="loop")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Not playing!", ephemeral=True)
        if player.queue.mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.normal
            await interaction.response.send_message("🔁 Loop **disabled**!", ephemeral=True)
        else:
            player.queue.mode = wavelink.QueueMode.loop
            await interaction.response.send_message("🔁 Loop **enabled**!", ephemeral=True)
        await refresh_np(interaction.guild)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, custom_id="queue_view")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player: wavelink.Player = interaction.guild.voice_client
        if not player:
            return await interaction.response.send_message("❌ Not playing!", ephemeral=True)
        embed = Embed(title="📋 Queue", color=0x1db954)
        if player.current:
            embed.add_field(
                name="▶️ Now Playing",
                value=f"[{player.current.title}]({player.current.uri}) — {fmt_duration(player.current.length)}",
                inline=False
            )
        if player.queue:
            lines = [
                f"`{i+1}.` [{t.title}]({t.uri}) — {fmt_duration(t.length)}"
                for i, t in enumerate(list(player.queue)[:10])
            ]
            if len(player.queue) > 10:
                lines.append(f"*...and {len(player.queue)-10} more*")
            embed.add_field(name="⏭️ Up Next", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="⏭️ Up Next", value="Queue is empty!", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── WAVELINK EVENTS ──────────────────────────────────────
@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"✅ Lavalink node ready: {payload.node.uri}")

@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    if not player or not player.guild:
        return
    guild = player.guild
    gid   = guild.id
    track = payload.track

    embed = build_np_embed(player, track)
    view  = PlayerView(gid)

    # Delete old now playing message
    old = np_message.get(gid)
    if old:
        try: await old.delete()
        except: pass

    # Send to text channel if set, otherwise find last used channel
    ch = None
    if TEXT_CH_ID:
        ch = bot.get_channel(TEXT_CH_ID)
    if not ch:
        # Try to find a suitable channel
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                ch = channel
                break

    if ch:
        try:
            msg = await ch.send(embed=embed, view=view)
            np_message[gid] = msg
        except Exception as e:
            print(f"⚠️ Could not send now playing: {e}")

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if not player or not player.guild:
        return
    # If queue is empty update embed
    if not player.queue and not player.current:
        gid = player.guild.id
        msg = np_message.get(gid)
        if msg:
            try:
                embed = Embed(
                    title="💤 Queue Empty",
                    description="Add songs with `/play`!",
                    color=0x95a5a6
                )
                await msg.edit(embed=embed, view=None)
            except: pass

@bot.event
async def on_wavelink_inactive_player(player: wavelink.Player):
    """Auto disconnect after 5 mins of inactivity — rejoin home channel."""
    gid = player.guild.id
    print(f"💤 Player inactive in guild {gid}")
    await player.disconnect()
    # Rejoin home voice channel
    if VOICE_CH_ID:
        await asyncio.sleep(3)
        ch = bot.get_channel(VOICE_CH_ID)
        if ch and isinstance(ch, discord.VoiceChannel):
            try:
                await ch.connect(cls=wavelink.Player)
                print(f"✅ Rejoined home channel: {ch.name}")
            except Exception as e:
                print(f"⚠️ Rejoin error: {e}")

# ─── STATUS LOOP ──────────────────────────────────────────
async def status_loop():
    await bot.wait_until_ready()
    statuses = [
        discord.Activity(type=discord.ActivityType.listening, name="/play to add songs 🎵"),
        discord.Activity(type=discord.ActivityType.playing,   name="music 24/7 🎧"),
        discord.Activity(type=discord.ActivityType.watching,  name="/queue to see songs 📋"),
        discord.Activity(type=discord.ActivityType.listening, name="Spotify & YouTube 🎤"),
        discord.Activity(type=discord.ActivityType.watching,  name="/nowplaying for info 🎵"),
    ]
    i = 0
    while not bot.is_closed():
        await bot.change_presence(
            status=discord.Status.online,
            activity=statuses[i % len(statuses)]
        )
        i += 1
        await asyncio.sleep(60)

# ─── EVENTS ───────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Bot online: {bot.user}")
    try:
        guild = discord.Object(id=SERVER_ID)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    # Set avatar
    try:
        for fname in ["avatar.gif", "avatar.png"]:
            if os.path.exists(fname):
                with open(fname, "rb") as f:
                    await bot.user.edit(avatar=f.read())
                print(f"✅ Avatar set from {fname}!")
                break
    except Exception as e:
        print(f"⚠️ Avatar: {e}")

    bot.loop.create_task(status_loop())

    # Auto join home voice channel
    if VOICE_CH_ID:
        await asyncio.sleep(3)
        ch = bot.get_channel(VOICE_CH_ID)
        if not ch:
            try: ch = await bot.fetch_channel(VOICE_CH_ID)
            except: pass
        if ch and isinstance(ch, discord.VoiceChannel):
            try:
                await ch.connect(cls=wavelink.Player)
                print(f"✅ Joined voice: {ch.name}")
            except Exception as e:
                print(f"⚠️ Voice join: {e}")

    print("✅ All systems go! 🎵")

@bot.event
async def on_voice_state_update(member, before, after):
    """Rejoin home channel if bot gets disconnected."""
    if member.id != bot.user.id:
        return
    if before.channel and not after.channel:
        print("⚠️ Disconnected from voice, rejoining in 10s...")
        await asyncio.sleep(10)
        if VOICE_CH_ID:
            ch = bot.get_channel(VOICE_CH_ID)
            if ch and isinstance(ch, discord.VoiceChannel):
                try:
                    await ch.connect(cls=wavelink.Player)
                    print(f"✅ Rejoined: {ch.name}")
                except Exception as e:
                    print(f"❌ Rejoin error: {e}")

# ─── SLASH COMMANDS ───────────────────────────────────────

@tree.command(name="play", description="Play a song — Spotify, YouTube, SoundCloud, Apple Music, Deezer")
@app_commands.describe(query="Song name, Spotify link, YouTube link, SoundCloud link etc.")
async def play(interaction: discord.Interaction, query: str):
    if TEXT_CH_ID and interaction.channel_id != TEXT_CH_ID:
        return await interaction.response.send_message(
            f"❌ Use commands in <#{TEXT_CH_ID}>!", ephemeral=True
        )

    if not interaction.user.voice:
        return await interaction.response.send_message(
            "❌ Join a voice channel first!", ephemeral=True
        )

    await interaction.response.defer()

    player: wavelink.Player = interaction.guild.voice_client

    # Connect if not in voice
    if not player or not player.connected:
        try:
            player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
        except Exception as e:
            return await interaction.followup.send(f"❌ Could not join voice: {e}")
    elif player.channel != interaction.user.voice.channel:
        await player.move_to(interaction.user.voice.channel)

    # Set volume default
    await player.set_volume(70)

    # Search / load tracks
    try:
        tracks = await wavelink.Playable.search(query)
    except Exception as e:
        return await interaction.followup.send(f"❌ Search error: {e}")

    if not tracks:
        return await interaction.followup.send("❌ No results found! Try a different search.")

    # Handle playlists vs single tracks
    if isinstance(tracks, wavelink.Playlist):
        added = len(tracks.tracks)
        for track in tracks.tracks:
            await player.queue.put_wait(track)
        await interaction.followup.send(
            f"📋 Added playlist **{tracks.name}** — **{added}** songs!"
        )
    else:
        track = tracks[0]
        await player.queue.put_wait(track)
        if not player.playing:
            pass  # will auto play
        else:
            embed = Embed(
                title="📋 Added to Queue",
                description=f"[{track.title}]({track.uri})",
                color=0x1db954
            )
            embed.add_field(name="Duration", value=fmt_duration(track.length), inline=True)
            embed.add_field(name="Artist",   value=track.author or "Unknown",  inline=True)
            embed.add_field(name="Position", value=f"#{len(player.queue)}",    inline=True)
            if track.artwork:
                embed.set_thumbnail(url=track.artwork)
            await interaction.followup.send(embed=embed)

    # Start playing if not already
    if not player.playing:
        await player.play(player.queue.get())
        await interaction.followup.send("✅ Starting playback...")

@tree.command(name="nowplaying", description="Show the now playing card with controls")
async def nowplaying(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.current:
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    await interaction.response.defer()
    gid = interaction.guild.id
    old = np_message.get(gid)
    if old:
        try: await old.delete()
        except: pass
    msg = await interaction.followup.send(
        embed=build_np_embed(player, player.current),
        view=PlayerView(gid)
    )
    np_message[gid] = msg

@tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.current:
        await player.skip(force=True)
        await interaction.response.send_message("⏭️ Skipped!")
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and not player.paused:
        await player.pause(True)
        await interaction.response.send_message("⏸️ Paused!")
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player and player.paused:
        await player.pause(False)
        await interaction.response.send_message("▶️ Resumed!")
    else:
        await interaction.response.send_message("❌ Nothing paused!", ephemeral=True)

@tree.command(name="volume", description="Set volume 0-100")
@app_commands.describe(level="Volume level 0 to 100")
async def volume(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        return await interaction.response.send_message("❌ Volume must be 0–100!", ephemeral=True)
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        await player.set_volume(level)
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**")
        await refresh_np(interaction.guild)
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="loop", description="Toggle loop mode")
async def loop(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        return await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)
    if player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal
        await interaction.response.send_message("🔁 Loop **disabled**!")
    else:
        player.queue.mode = wavelink.QueueMode.loop
        await interaction.response.send_message("🔁 Loop **enabled**!")
    await refresh_np(interaction.guild)

@tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player or not player.queue:
        return await interaction.response.send_message("❌ Queue is empty!", ephemeral=True)
    player.queue.shuffle()
    await interaction.response.send_message("🔀 Queue shuffled!")

@tree.command(name="queue", description="Show the queue")
async def queue_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if not player:
        return await interaction.response.send_message("📋 Queue is empty!", ephemeral=True)
    embed = Embed(title="📋 Queue", color=0x1db954)
    if player.current:
        embed.add_field(
            name="▶️ Now Playing",
            value=f"[{player.current.title}]({player.current.uri}) — {fmt_duration(player.current.length)}",
            inline=False
        )
    if player.queue:
        lines = [
            f"`{i+1}.` [{t.title}]({t.uri}) — {fmt_duration(t.length)}"
            for i, t in enumerate(list(player.queue)[:10])
        ]
        if len(player.queue) > 10:
            lines.append(f"*...and {len(player.queue)-10} more*")
        embed.add_field(name="⏭️ Up Next", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="⏭️ Up Next", value="Queue is empty!", inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="stop", description="Stop music and clear queue")
async def stop(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        player.queue.clear()
        await player.stop()
        await interaction.response.send_message("⏹️ Stopped and queue cleared!")
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="clear", description="Clear the queue")
async def clear(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        player.queue.clear()
        await interaction.response.send_message("🗑️ Queue cleared!")
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Join a voice channel first!", ephemeral=True)
    ch = interaction.user.voice.channel
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        await player.move_to(ch)
    else:
        await ch.connect(cls=wavelink.Player)
    await interaction.response.send_message(f"✅ Joined **{ch.name}**!")

@tree.command(name="leave", description="Leave voice channel")
async def leave(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client
    if player:
        player.queue.clear()
        await player.disconnect()
        await interaction.response.send_message("👋 Left!")
    else:
        await interaction.response.send_message("❌ Not in a voice channel!", ephemeral=True)

@tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = Embed(
        title="🎵 Music Bot Commands",
        description="*Supports Spotify, YouTube, SoundCloud, Apple Music, Deezer and more!*\n\u200b",
        color=0x1db954
    )
    embed.add_field(name="/play <song>",    value="Play — Spotify/YouTube/SoundCloud links or song name", inline=False)
    embed.add_field(name="/nowplaying",     value="Show player card with buttons", inline=False)
    embed.add_field(name="/queue",          value="Show the queue", inline=False)
    embed.add_field(name="/skip",           value="Skip current song", inline=False)
    embed.add_field(name="/pause",          value="Pause playback", inline=False)
    embed.add_field(name="/resume",         value="Resume playback", inline=False)
    embed.add_field(name="/volume <0-100>", value="Set volume", inline=False)
    embed.add_field(name="/loop",           value="Toggle loop mode", inline=False)
    embed.add_field(name="/shuffle",        value="Shuffle the queue", inline=False)
    embed.add_field(name="/stop",           value="Stop and clear queue", inline=False)
    embed.add_field(name="/clear",          value="Clear queue only", inline=False)
    embed.add_field(name="/join",           value="Join your voice channel", inline=False)
    embed.add_field(name="/leave",          value="Leave voice channel", inline=False)
    embed.set_footer(text="🎵 Powered by Lavalink + HeavenCloud")
    await interaction.response.send_message(embed=embed)

# ─── KEEP ALIVE ───────────────────────────────────────────
async def handle(request):
    return web.Response(text="🎵 Music bot alive!")

async def run_web():
    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web server on port {port}")

# ─── SETUP HOOK ───────────────────────────────────────────
@bot.event
async def setup_hook():
    await run_web()
    # Connect to free Lavalink nodes
    nodes = [
        wavelink.Node(uri=n["uri"], password=n["password"])
        for n in LAVALINK_NODES
    ]
    await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=100)
    print(f"✅ Connecting to {len(nodes)} Lavalink node(s)...")

bot.run(BOT_TOKEN)
