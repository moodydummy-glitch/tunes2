import discord
from discord.ext import commands
from discord import app_commands, Embed
import asyncio
import os
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiohttp import web
import random

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN")
SERVER_ID    = int(os.getenv("SERVER_ID", 0))
VOICE_CH_ID  = int(os.getenv("VOICE_CHANNEL_ID", 0))
TEXT_CH_ID   = int(os.getenv("TEXT_CHANNEL_ID", 0))
SP_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SP_CLIENT_SC = os.getenv("SPOTIFY_CLIENT_SECRET")

# ─── SPOTIFY ──────────────────────────────────────────────
sp = None
if SP_CLIENT_ID and SP_CLIENT_SC:
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SP_CLIENT_ID,
            client_secret=SP_CLIENT_SC
        ))
        print("✅ Spotify connected")
    except Exception as e:
        print(f"⚠️ Spotify failed: {e}")

# ─── INTENTS ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── YT-DLP (SoundCloud only — reliable on cloud servers) ──
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "scsearch",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

# ─── STATE PER GUILD ──────────────────────────────────────
queues     = {}   # guild_id -> [{"title","url","duration","thumbnail","uploader","webpage"}]
history    = {}   # guild_id -> [song_info]  (previously played)
volumes    = {}   # guild_id -> float 0-1
loops      = {}   # guild_id -> bool
shuffles   = {}   # guild_id -> bool
current    = {}   # guild_id -> song_info
np_message = {}   # guild_id -> discord.Message (now playing message)

def get_queue(gid): return queues.setdefault(gid, [])
def get_history(gid): return history.setdefault(gid, [])
def get_volume(gid): return volumes.get(gid, 0.5)
def is_looping(gid): return loops.get(gid, False)
def is_shuffling(gid): return shuffles.get(gid, False)

# ─── AUDIO FETCHING ───────────────────────────────────────
def resolve_spotify(url: str):
    """Convert Spotify URL to search query."""
    if not sp:
        return None
    try:
        if "track" in url:
            t = sp.track(url)
            return f"{t['name']} {t['artists'][0]['name']}"
        elif "playlist" in url:
            results = sp.playlist_tracks(url, limit=25)
            tracks = []
            for item in results["items"]:
                t = item.get("track")
                if t:
                    tracks.append(f"{t['name']} {t['artists'][0]['name']}")
            return tracks
        elif "album" in url:
            results = sp.album_tracks(url, limit=25)
            tracks = []
            for t in results["items"]:
                tracks.append(f"{t['name']} {t['artists'][0]['name']}")
            return tracks
    except Exception as e:
        print(f"⚠️ Spotify resolve error: {e}")
    return None

def fetch_audio(query: str):
    """Fetch audio info from SoundCloud."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            if query.startswith("http"):
                info = ydl.extract_info(query, download=False)
            else:
                info = ydl.extract_info(f"scsearch:{query}", download=False)
                info = info["entries"][0]
            return {
                "title":     info.get("title", "Unknown"),
                "url":       info["url"],
                "duration":  info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "uploader":  info.get("uploader", "Unknown"),
                "webpage":   info.get("webpage_url", ""),
            }
    except Exception as e:
        print(f"❌ Audio fetch error: {e}")
        return None

def fmt_duration(s):
    if not s: return "Live"
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

# ─── NOW PLAYING EMBED + BUTTONS ──────────────────────────
def build_np_embed(info, gid):
    vol  = int(get_volume(gid) * 100)
    loop = "🔁 On" if is_looping(gid)  else "➡️ Off"
    shuf = "🔀 On" if is_shuffling(gid) else "➡️ Off"
    q    = get_queue(gid)

    embed = Embed(
        title="▶️  Now Playing",
        description=f"### [{info['title']}]({info['webpage']})",
        color=0x1db954
    )
    if info.get("thumbnail"):
        embed.set_image(url=info["thumbnail"])
    embed.add_field(name="⏱️ Duration", value=fmt_duration(info["duration"]), inline=True)
    embed.add_field(name="🎤 Artist",   value=info["uploader"],                inline=True)
    embed.add_field(name="🔊 Volume",   value=f"{vol}%",                       inline=True)
    embed.add_field(name="🔁 Loop",     value=loop,                            inline=True)
    embed.add_field(name="🔀 Shuffle",  value=shuf,                            inline=True)
    embed.add_field(name="📋 Queue",    value=f"{len(q)} song(s)",             inline=True)
    embed.set_footer(text="🎵 Use the buttons below to control playback")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

class PlayerView(discord.ui.View):
    """Persistent interactive player buttons."""
    def __init__(self, gid: int):
        super().__init__(timeout=None)
        self.gid = gid

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        shuffles[gid] = not is_shuffling(gid)
        state = "enabled 🔀" if is_shuffling(gid) else "disabled"
        await interaction.response.send_message(f"Shuffle {state}!", ephemeral=True)
        await refresh_np(interaction.guild)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="previous")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        hist = get_history(gid)
        if not hist:
            return await interaction.response.send_message("No previous songs!", ephemeral=True)
        prev = hist.pop()
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            get_queue(gid).insert(0, current.get(gid, {}))
            get_queue(gid).insert(0, prev)
            vc.stop()
            await interaction.response.send_message(f"⏮️ Going back to **{prev['title']}**!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing!", ephemeral=True)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="pause_resume")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            button.emoji = "▶️"
            await interaction.response.edit_message(view=self)
        elif vc and vc.is_paused():
            vc.resume()
            button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("Nothing playing!", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭️ Skipped!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing!", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, custom_id="queue_btn")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = interaction.guild.id
        q = get_queue(gid)
        cur = current.get(gid)
        if not q and not cur:
            return await interaction.response.send_message("Queue is empty!", ephemeral=True)
        embed = Embed(title="📋 Queue", color=0x1db954)
        if cur:
            embed.add_field(
                name="▶️ Now Playing",
                value=f"[{cur['title']}]({cur['webpage']}) — {fmt_duration(cur['duration'])}",
                inline=False
            )
        if q:
            lines = [f"`{i+1}.` [{s['title']}]({s['webpage']}) — {fmt_duration(s['duration'])}"
                     for i, s in enumerate(q[:10])]
            if len(q) > 10:
                lines.append(f"*...and {len(q)-10} more*")
            embed.add_field(name="⏭️ Up Next", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def refresh_np(guild):
    """Update the now playing message embed."""
    gid = guild.id
    msg = np_message.get(gid)
    info = current.get(gid)
    if msg and info:
        try:
            await msg.edit(embed=build_np_embed(info, gid), view=PlayerView(gid))
        except Exception:
            pass

# ─── PLAYBACK ENGINE ──────────────────────────────────────
async def play_next(guild):
    gid = guild.id
    vc  = guild.voice_client
    if not vc or not vc.is_connected():
        return

    q = get_queue(gid)
    if not q:
        current[gid] = None
        # Update np embed to show nothing playing
        msg = np_message.get(gid)
        if msg:
            try:
                embed = Embed(
                    title="💤 Queue Empty",
                    description="Add songs with `/play`!",
                    color=0x95a5a6
                )
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
        return

    if is_shuffling(gid):
        random.shuffle(q)

    info = q.pop(0)

    # Save to history
    hist = get_history(gid)
    if current.get(gid):
        hist.append(current[gid])
        if len(hist) > 10:
            hist.pop(0)

    current[gid] = info

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTIONS),
        volume=get_volume(gid)
    )

    def after_playing(error):
        if error:
            print(f"❌ Playback error: {error}")
        if is_looping(gid) and current.get(gid):
            # Re-add current song to front of queue
            get_queue(gid).insert(0, current[gid])
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    vc.play(source, after=after_playing)

    # Update now playing message
    await refresh_np(guild)

# ─── VOICE HELPERS ────────────────────────────────────────
async def ensure_voice(interaction: discord.Interaction):
    """Make sure bot is in user's voice channel."""
    if not interaction.user.voice:
        await interaction.followup.send("❌ Join a voice channel first!")
        return None
    vc = interaction.guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id != interaction.user.voice.channel.id:
                await vc.move_to(interaction.user.voice.channel)
        elif vc and not vc.is_connected():
            try: await vc.disconnect(force=True)
            except: pass
            await asyncio.sleep(1)
            vc = await interaction.user.voice.channel.connect()
        else:
            vc = await interaction.user.voice.channel.connect()
    except discord.ClientException:
        vc = interaction.guild.voice_client
    return vc

# ─── STATUS LOOP ──────────────────────────────────────────
async def status_loop():
    await bot.wait_until_ready()
    statuses = [
        discord.Activity(type=discord.ActivityType.listening, name="/play to add songs 🎵"),
        discord.Activity(type=discord.ActivityType.playing,   name="music 24/7 🎧"),
        discord.Activity(type=discord.ActivityType.watching,  name="/queue to see songs 📋"),
        discord.Activity(type=discord.ActivityType.listening, name="/nowplaying for info 🎤"),
    ]
    i = 0
    while not bot.is_closed():
        await bot.change_presence(status=discord.Status.online, activity=statuses[i % len(statuses)])
        i += 1
        await asyncio.sleep(60)

# ─── VOICE RECONNECT ──────────────────────────────────────
async def voice_reconnect_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    while not bot.is_closed():
        await asyncio.sleep(60)
        if not VOICE_CH_ID: continue
        try:
            ch = bot.get_channel(VOICE_CH_ID)
            if not ch: ch = await bot.fetch_channel(VOICE_CH_ID)
            if ch and isinstance(ch, discord.VoiceChannel):
                vc = ch.guild.voice_client
                if not vc or not vc.is_connected():
                    await ch.connect(reconnect=True)
                    print(f"✅ Reconnected to voice: {ch.name}")
        except Exception as e:
            print(f"⚠️ Voice reconnect error: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id and before.channel and not after.channel:
        print("⚠️ Bot disconnected from voice, rejoining in 10s...")
        await asyncio.sleep(10)
        if VOICE_CH_ID:
            try:
                ch = bot.get_channel(VOICE_CH_ID) or await bot.fetch_channel(VOICE_CH_ID)
                if ch: await ch.connect(reconnect=True)
            except Exception as e:
                print(f"❌ Rejoin error: {e}")

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
        if os.path.exists("avatar.gif"):
            with open("avatar.gif", "rb") as f:
                await bot.user.edit(avatar=f.read())
            print("✅ Avatar set!")
        elif os.path.exists("avatar.png"):
            with open("avatar.png", "rb") as f:
                await bot.user.edit(avatar=f.read())
            print("✅ Avatar set!")
    except Exception as e:
        print(f"⚠️ Avatar: {e}")

    bot.loop.create_task(status_loop())
    bot.loop.create_task(voice_reconnect_loop())

    # Auto join voice
    if VOICE_CH_ID:
        try:
            ch = bot.get_channel(VOICE_CH_ID) or await bot.fetch_channel(VOICE_CH_ID)
            if ch and isinstance(ch, discord.VoiceChannel):
                await ch.connect(reconnect=True)
                print(f"✅ Joined voice: {ch.name}")
        except Exception as e:
            print(f"⚠️ Voice join: {e}")

    print("✅ All systems go! 🎵")

# ─── SLASH COMMANDS ───────────────────────────────────────

@tree.command(name="play", description="Play a song — supports SoundCloud, Spotify links")
@app_commands.describe(query="Song name, SoundCloud URL, or Spotify link")
async def play(interaction: discord.Interaction, query: str):
    if TEXT_CH_ID and interaction.channel_id != TEXT_CH_ID:
        return await interaction.response.send_message(
            f"❌ Use commands in <#{TEXT_CH_ID}>!", ephemeral=True
        )
    await interaction.response.defer()

    vc = await ensure_voice(interaction)
    if not vc: return

    songs_to_add = []

    # Handle Spotify
    if "spotify.com" in query:
        if not sp:
            return await interaction.followup.send("❌ Spotify not configured. Add `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` to Railway variables.")
        result = resolve_spotify(query)
        if not result:
            return await interaction.followup.send("❌ Could not read Spotify link.")
        if isinstance(result, list):
            await interaction.followup.send(f"🎵 Loading **{len(result)}** songs from Spotify...")
            for r in result:
                info = await asyncio.get_event_loop().run_in_executor(None, fetch_audio, r)
                if info: songs_to_add.append(info)
        else:
            info = await asyncio.get_event_loop().run_in_executor(None, fetch_audio, result)
            if info: songs_to_add.append(info)
    else:
        info = await asyncio.get_event_loop().run_in_executor(None, fetch_audio, query)
        if info: songs_to_add.append(info)

    if not songs_to_add:
        return await interaction.followup.send("❌ Could not find that song!")

    gid = interaction.guild.id
    q   = get_queue(gid)

    for s in songs_to_add:
        q.append(s)

    if not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild)

        # Send now playing message with buttons
        info = current.get(gid)
        if info:
            embed = build_np_embed(info, gid)
            view  = PlayerView(gid)

            # Delete old np message
            old = np_message.get(gid)
            if old:
                try: await old.delete()
                except: pass

            msg = await interaction.followup.send(embed=embed, view=view)
            np_message[gid] = msg
    else:
        if len(songs_to_add) == 1:
            embed = Embed(
                title="📋 Added to Queue",
                description=f"[{songs_to_add[0]['title']}]({songs_to_add[0]['webpage']})",
                color=0x1db954
            )
            embed.add_field(name="Position", value=f"#{len(q)}", inline=True)
            embed.add_field(name="Duration",  value=fmt_duration(songs_to_add[0]["duration"]), inline=True)
            if songs_to_add[0].get("thumbnail"):
                embed.set_thumbnail(url=songs_to_add[0]["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"✅ Added **{len(songs_to_add)}** songs to queue!")

@tree.command(name="nowplaying", description="Show the now playing card with controls")
async def nowplaying(interaction: discord.Interaction):
    gid  = interaction.guild.id
    info = current.get(gid)
    if not info:
        return await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    await interaction.response.defer()
    old = np_message.get(gid)
    if old:
        try: await old.delete()
        except: pass
    msg = await interaction.followup.send(embed=build_np_embed(info, gid), view=PlayerView(gid))
    np_message[gid] = msg

@tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped!")
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused!")
    else:
        await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

@tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed!")
    else:
        await interaction.response.send_message("❌ Nothing paused!", ephemeral=True)

@tree.command(name="volume", description="Set volume 0-100")
@app_commands.describe(level="Volume level 0 to 100")
async def volume(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        return await interaction.response.send_message("❌ Volume must be 0–100!", ephemeral=True)
    gid = interaction.guild.id
    volumes[gid] = level / 100
    vc = interaction.guild.voice_client
    if vc and vc.source:
        vc.source.volume = level / 100
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**")
    await refresh_np(interaction.guild)

@tree.command(name="loop", description="Toggle loop mode")
async def loop(interaction: discord.Interaction):
    gid = interaction.guild.id
    loops[gid] = not is_looping(gid)
    state = "enabled 🔁" if is_looping(gid) else "disabled"
    await interaction.response.send_message(f"Loop {state}!")
    await refresh_np(interaction.guild)

@tree.command(name="shuffle", description="Toggle shuffle mode")
async def shuffle(interaction: discord.Interaction):
    gid = interaction.guild.id
    shuffles[gid] = not is_shuffling(gid)
    state = "enabled 🔀" if is_shuffling(gid) else "disabled"
    await interaction.response.send_message(f"Shuffle {state}!")
    await refresh_np(interaction.guild)

@tree.command(name="queue", description="Show the queue")
async def queue_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    q   = get_queue(gid)
    cur = current.get(gid)
    if not q and not cur:
        return await interaction.response.send_message("📋 Queue is empty!", ephemeral=True)
    embed = Embed(title="📋 Queue", color=0x1db954)
    if cur:
        embed.add_field(
            name="▶️ Now Playing",
            value=f"[{cur['title']}]({cur['webpage']}) — {fmt_duration(cur['duration'])}",
            inline=False
        )
    if q:
        lines = [f"`{i+1}.` [{s['title']}]({s['webpage']}) — {fmt_duration(s['duration'])}"
                 for i, s in enumerate(q[:10])]
        if len(q) > 10:
            lines.append(f"*...and {len(q)-10} more*")
        embed.add_field(name="⏭️ Up Next", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="stop", description="Stop music and clear queue")
async def stop(interaction: discord.Interaction):
    gid = interaction.guild.id
    queues[gid]  = []
    current[gid] = None
    vc = interaction.guild.voice_client
    if vc: vc.stop()
    await interaction.response.send_message("⏹️ Stopped and queue cleared!")

@tree.command(name="clear", description="Clear the queue")
async def clear(interaction: discord.Interaction):
    queues[interaction.guild.id] = []
    await interaction.response.send_message("🗑️ Queue cleared!")

@tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        ch = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc: await vc.move_to(ch)
        else:  await ch.connect()
        await interaction.response.send_message(f"✅ Joined **{ch.name}**!")
    else:
        await interaction.response.send_message("❌ Join a voice channel first!", ephemeral=True)

@tree.command(name="leave", description="Leave voice channel")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        queues[interaction.guild.id]  = []
        current[interaction.guild.id] = None
        await vc.disconnect()
        await interaction.response.send_message("👋 Left!")
    else:
        await interaction.response.send_message("❌ Not in a voice channel!", ephemeral=True)

@tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = Embed(title="🎵 Music Bot Commands", color=0x1db954,
                  description="*Supports SoundCloud, Spotify links!*\n\u200b")
    embed.add_field(name="/play <song>",    value="Play a song or Spotify link", inline=False)
    embed.add_field(name="/nowplaying",     value="Show player card with buttons", inline=False)
    embed.add_field(name="/queue",          value="Show the queue", inline=False)
    embed.add_field(name="/skip",           value="Skip current song", inline=False)
    embed.add_field(name="/pause",          value="Pause playback", inline=False)
    embed.add_field(name="/resume",         value="Resume playback", inline=False)
    embed.add_field(name="/volume <0-100>", value="Set volume", inline=False)
    embed.add_field(name="/loop",           value="Toggle loop", inline=False)
    embed.add_field(name="/shuffle",        value="Toggle shuffle", inline=False)
    embed.add_field(name="/stop",           value="Stop and clear queue", inline=False)
    embed.add_field(name="/clear",          value="Clear queue", inline=False)
    embed.add_field(name="/join",           value="Join your voice channel", inline=False)
    embed.add_field(name="/leave",          value="Leave voice channel", inline=False)
    embed.set_footer(text="🎵 Use /play to get started!")
    await interaction.response.send_message(embed=embed)

# ─── KEEP ALIVE ───────────────────────────────────────────
async def handle(request):
    return web.Response(text="🎵 Music bot is alive!")

async def run_web():
    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web server on port {port}")

@bot.event
async def setup_hook():
    await run_web()

bot.run(BOT_TOKEN)
