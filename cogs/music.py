import asyncio
import functools
import json
import random
import math

import discord
import yt_dlp
from async_timeout import timeout
from discord import app_commands
from discord.ext import commands
from discord.utils import get
from ytmusicapi import YTMusic

with open('data/id_dict.json') as id_file:
    id_dict = json.load(id_file)

class VoiceConnectionError(app_commands.AppCommandError): pass

class InvalidVoiceChannel(VoiceConnectionError): pass

class YTDLSource(discord.PCMVolumeTransformer):

    YTDL_OPTS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    } 

    ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

    def __init__(self, source: discord.FFmpegPCMAudio, channel: discord.VoiceChannel, author: discord.User, data: dict):
        super().__init__(source)
        self.channel = channel
        self.author = author
        self.data = data

    def create_embed(self):
        duration = self.convert_duration(self.data['duration'])
        return (discord.Embed(title='Now Playing',
                              description=f'[**{self.data["title"]}**]({self.data["webpage_url"]})\n',
                              color=discord.Color.blurple())
                 .add_field(name='Duration', value=duration)
                 .add_field(name='Requested By', value=self.author.mention)
                 .set_thumbnail(url=self.data['thumbnail']))

    def clone(self):
        source = discord.FFmpegPCMAudio(self.data['url'], **self.FFMPEG_OPTS)
        return YTDLSource(source, self.channel, self.author, self.data)

    @classmethod
    async def create_source(cls, interaction: discord.Interaction, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        # print(json.dumps(ydl.sanitize_info(info)))
        partial = functools.partial(cls.ytdl.extract_info, url=search, download=False)
        data = await loop.run_in_executor(None, partial)
        if 'entries' in data:
            playlist = asyncio.Queue()
            for entry in data['entries']:
                source = discord.FFmpegPCMAudio(entry['url'], **cls.FFMPEG_OPTS)
                await playlist.put(cls(source, interaction.channel, interaction.user, entry))
            return playlist
        source = discord.FFmpegPCMAudio(data['url'], **cls.FFMPEG_OPTS)
        return cls(source, interaction.channel, interaction.user, data)

    @staticmethod
    def convert_duration(duration: int):
        m, s = divmod(duration, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        duration = []
        if d > 0:
            duration.append(f'{d} days')
        if h > 0:
            duration.append(f'{h} hours')
        if m > 0:
            duration.append(f'{m} minutes')
        if s > 0:
            duration.append(f'{s} seconds')
        return ', '.join(duration)

class VoiceState(commands.Cog):

    def __init__(self, interaction: discord.Interaction):
        self.bot  = interaction.client

        self.voice = None
        self.next  = asyncio.Event()
        self.queue = asyncio.Queue()

        self.loop    = False
        self.volume  = .5
        self.active  = True
        self.current = None
        self.message = None

        self.bot.loop.create_task(self.audio_task())

    def reactivate(self):
        self.active = True
        self.bot.loop.create_task(self.audio_task())

    async def audio_task(self):
        while True:
            self.next.clear()
            try:
                async with timeout(300):
                    self.current = await self.queue.get()
                    await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=self.current.data['title']))
            except asyncio.TimeoutError:
                return self.bot.loop.create_task(self.stop())

            self.message = await self.current.channel.send(embed=self.current.create_embed())
            for emoji in ('\U000023EF', '\U000023ED', '\U0001F500', '\U0001F502'):
                await self.message.add_reaction(emoji)
            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)

            await self.next.wait()
            await self.bot.change_presence(activity=None)
            await self.message.clear_reactions()
            # try: await self.message.delete()
            # except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id != id_dict['bot']:
            channel = await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if str(payload.emoji) == '\U000023EF':
                if not self.voice.is_paused():
                    self.voice.pause()
            elif str(payload.emoji) == '\U000023ED':
                self.loop = False
                # if payload.member == self.current.author:
                self.skip()
                await self.message.clear_reactions()
                # elif payload.user_id not in self.skip_count:
                #     self.skip_count.add(payload.user_id)
                #     if len(self.skip_count) >= 2:
                #         self.skip()
                #         await self.message.clear_reactions()
            # elif str(payload.emoji) == '\U000023F9':
            #     self.queue._queue.clear()
            #     if self.playing():
            #         self.voice.stop()
            #         await self.bot.change_presence(activity=None)
            #         await self.message.clear_reactions()
            elif str(payload.emoji) == '\U0001F500':
                if not self.queue.empty():
                    random.shuffle(self.queue._queue)
                await message.remove_reaction(payload.emoji, payload.member)
            elif str(payload.emoji) == '\U0001F502':
                self.loop = True

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        channel = await self.bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        if str(payload.emoji) == '\U000023EF':
            reaction = get(message.reactions, emoji=payload.emoji.name)
            if reaction.count < 2:
                self.voice.resume()
        elif str(payload.emoji) == '\U0001F502':
            reaction = get(message.reactions, emoji=payload.emoji.name)
            if reaction.count < 2:
                self.loop = False

    def next_song(self, error=None):
        if error: raise VoiceConnectionError(str(error))
        if self.loop:
            # https://github.com/Rapptz/discord.py/issues/4003
            self.current = self.current.clone()
            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)
        else: self.next.set()

    def playing(self):
        return self.voice and self.current

    def skip(self):
        if self.playing():
            self.voice.stop()

    async def stop(self):
        self.queue._queue.clear()
        await self.bot.change_presence(activity=None)
        if self.voice:
            await self.voice.disconnect()
            self.voice = None
            self.active = False

class MusicBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ytmusic = YTMusic()
        self.voice_state = None

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.channel.id == id_dict['music-room'] and message.author.id != id_dict['bot']:
            await message.delete()

    @app_commands.command(name='play', description='Play a song or video from YouTube.')
    async def _play(self, interaction: discord.Interaction, search: str, yt_music: bool = False):
        await interaction.response.defer()
        await self.ensure_voice_state(interaction)
        voice_state = self.voice_state
        if not voice_state.voice:
            channel = interaction.user.voice.channel
            voice_state.voice = await channel.connect()

        if yt_music: # YouTube Music
            search = 'https://music.youtube.com/watch?v=' \
                + self.ytmusic.search(search, filter='songs')[0]['videoId']
        source = await YTDLSource.create_source(interaction, search, loop=self.bot.loop)
        if isinstance(source, asyncio.Queue):
            queue_size = 0
            while not source.empty():
                await voice_state.queue.put(source.get_nowait())
                queue_size += 1
            await interaction.followup.send('Playlist Enqueued.' if queue_size > 1 else 'Song Enqueued.')
        else:
            await voice_state.queue.put(source)
            await interaction.followup.send('Song Enqueued.')

    @app_commands.command(name='queue', description='Show the current queue of songs or videos.')
    async def _queue(self, interaction: discord.Interaction, page: int = 1):
        voice_state = self.voice_state
        await self.ensure_connection(voice_state)
        if voice_state.queue.empty() and not voice_state.playing():
            return await interaction.response.send_message('Queue Empty.')

        queue_list = [voice_state.current] + list(voice_state.queue._queue)
        page_count = math.ceil(len(queue_list) / 5)
        start = (page - 1) * 5

        description = ''
        for i, song in enumerate(queue_list[start:(start + 5)], start=start):
            description += f'`{i + 1}.` **{song.data["title"]}**\n'
        embed = discord.Embed(title=f'Queue ({len(queue_list)})', description=description,
            color=discord.Color.red()).set_footer(text=f'Page {page} of {page_count}')
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='volume', description='Set the volume of the current song or video.')
    async def _volume(self, interaction: discord.Interaction, value: int = None):
        voice_state = self.voice_state
        await self.ensure_connection(voice_state)
        if not voice_state.playing():
            return await interaction.response.send_message('Nothing Playing.')
        if value is None:
            # return await interaction.response.send_message(f'Current Volume ({int(voice_state.volume * 200)}%).')
            embed = discord.Embed(title='Volume', description=f'\U0001F509 {int(voice_state.volume * 200)}%', color=discord.Color.orange())
            return await interaction.response.send_message(embed=embed)
        if value < 0 or value > 200:
            return await interaction.response.send_message(f'Invalid Volume.')

        old_value = voice_state.current.volume
        voice_state.current.volume = 0.5 * (value / 100)
        voice_state.volume = voice_state.current.volume

        # await interaction.response.send_message(f'Volume Changed ({value}%).')
        embed = discord.Embed(title='Volume', description=f'\U0001F509 {int(old_value * 200)}% \U00002192 {int(voice_state.volume * 200)}%', color=discord.Color.orange())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='remove', description='Remove a song or video from the queue.')
    async def _remove(self, interaction: discord.Interaction, index: int):
        voice_state = self.voice_state
        await self.ensure_connection(voice_state)
        if voice_state.queue.empty() and not voice_state.playing():
            return await interaction.response.send_message('Empty Queue.')

        queue = voice_state.queue._queue
        if index == 1 and interaction.user == voice_state.current.author:
            voice_state.skip()
            await interaction.response.send_message('Song Removed.')
        elif interaction.user == queue[index - 2].author:
            del queue[index - 2]
            await interaction.response.send_message('Song Removed.')
        else:
            await interaction.response.send_message('Illegal Dequeue.')

    @app_commands.command(name='move', description='Move from one voice channel to another.')
    async def _move(self, interaction: discord.Interaction, channel: discord.VoiceChannel = None):
        voice_state = self.voice_state
        await self.ensure_connection(voice_state)
        channel = channel or interaction.user.voice.channel
        if voice_state.voice:
            await voice_state.voice.move_to(channel)
        else:
            voice_state.voice = await channel.connect()
        await interaction.response.send_message(f'Connected to <#{channel.id}>.')

    @app_commands.command(name='leave', description='Clear the queue and leave the channel.')
    async def _leave(self, interaction: discord.Interaction):
        voice_state = self.voice_state
        await self.ensure_connection(voice_state)
        channel = interaction.user.voice.channel
        await voice_state.stop()
        # await self.bot.change_presence(activity=None)
        # await self.message.clear_reactions()
        await interaction.response.send_message(f'Disconnected from <#{channel.id}>.')

    async def ensure_connection(self, voice_state: VoiceState):
        if not voice_state.voice:
            raise app_commands.AppCommandError(f'{self.bot.user.name} not connected to a voice channel.')

    async def ensure_voice_state(self, interaction: discord.Interaction):
        if not self.voice_state: # or not self.voice_state.active:
            self.voice_state = VoiceState(interaction)
            await self.bot.add_cog(self.voice_state)
        if not self.voice_state.active:
            self.voice_state.reactivate()
        if not interaction.user.voice or not interaction.user.voice.channel:
            raise app_commands.AppCommandError(f'{interaction.user.name} isn\'t connected to a voice channel.')
        if self.voice_state.voice:
            if self.voice_state.voice.channel != interaction.user.voice.channel:
                raise app_commands.AppCommandError(f'{self.bot.user.name} already connected to a voice channel.')

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicBot(bot))
