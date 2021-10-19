""" A Discord Music Bot """

import discord, youtube_dl, asyncio
import functools, random, math
from async_timeout import timeout
from discord.ext import commands

from discord_slash import SlashCommand, SlashContext, cog_ext
from discord_slash.utils.manage_commands import create_option

bot = commands.Bot(command_prefix='!')
slash = SlashCommand(bot, sync_commands=True)

class VoiceConnectionError(commands.CommandError): pass

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

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTS)

    def __init__(self, ctx: SlashContext, source: discord.FFmpegPCMAudio, *, data: dict):
        super().__init__(source)
        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

    def create_embed(self):
        duration = self.convert_duration(self.data['duration'])
        return (discord.Embed(title='Now Playing',
                              description=f'```css\n{self.data["title"]}\n```',
                              color=discord.Color.blurple())
                 .add_field(name='Duration', value=duration)
                 .add_field(name='Requested By', value=self.requester.mention)
                 .set_thumbnail(url=self.data['thumbnail']))

    @classmethod
    async def create_source(cls, ctx: SlashContext, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        partial = functools.partial(cls.ytdl.extract_info, url=search, download=False)
        data = await loop.run_in_executor(None, partial)
        if 'entries' in data:
            playlist = asyncio.Queue()
            for entry in data['entries']:
                await playlist.put(cls(ctx, discord.FFmpegPCMAudio(entry['url'], **cls.FFMPEG_OPTS), data=entry))
            return playlist
        return cls(ctx, discord.FFmpegPCMAudio(data['url'], **cls.FFMPEG_OPTS), data=data)

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

    def __init__(self, ctx: SlashContext):
        self.bot  = ctx.bot
        self._cog = ctx.cog
        self._guild   = ctx.guild
        self._channel = ctx.channel

        self.voice = None
        self.next  = asyncio.Event()
        self.queue = asyncio.Queue()

        self.volume  = .5
        self.active  = True
        self.current = None
        self.message = None
        self.skip_count = set()

        ctx.bot.loop.create_task(self.audio_task())

    async def audio_task(self):
        while True:
            self.next.clear()
            try:
                async with timeout(180):
                    self.current = await self.queue.get()
                    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=self.current.data['title']))
            except asyncio.TimeoutError:
                return self.bot.loop.create_task(self.stop())

            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)
            self.message = await self.current.channel.send(embed=self.current.create_embed())

            await self.next.wait()
            self.current = None
            try: await self.message.delete()
            except discord.HTTPException: pass

    def next_song(self, error=None):
        if error: raise VoiceConnectionError(str(error))
        self.next.set()

    def playing(self):
        return self.voice and self.current

    def skip(self):
        self.skip_count.clear()
        if self.playing():
            self.voice.stop()

    async def stop(self):
        self.queue._queue.clear()
        await bot.change_presence(activity=None)
        if self.voice:
            await self.voice.disconnect()
            self.voice = None
            self.active = False

class Music(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_state = {}
        self.kernel_event = asyncio.Event()
        self.kernel_id = None

    def get_voice_state(self, ctx: SlashContext):
        try:
            voice_state = self.voice_state[ctx.guild.id]
        except KeyError:
            voice_state = VoiceState(ctx)
            self.voice_state[ctx.guild.id] = voice_state

        if not voice_state or not voice_state.active:
            voice_state = VoiceState(ctx)
            self.voice_state[ctx.guild.id] = voice_state
        return voice_state

    @cog_ext.cog_slash(
        name='connect',
        description='Connects to a voice channel.',
        guild_ids=[840757649002725386],
        options=[
            create_option(
                name='channel',
                description='voice channel',
                required=False,
                option_type=7
            )
        ]
    )
    async def _connect(self, ctx: SlashContext, channel: discord.VoiceChannel = None):
        if not self.kernel_id and channel:
            return await ctx.send('You cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()
        
        voice_state = self.get_voice_state(ctx)

        self.channel = channel or ctx.author.voice.channel
        if voice_state.voice:
            await voice_state.voice.move_to(self.channel)
        else:
            voice_state.voice = await self.channel.connect()
        await ctx.send('Connected.')

    @cog_ext.cog_slash(
        name='play',
        description='Plays or queues a song or playlist.',
        guild_ids=[840757649002725386],
        options=[
            create_option(
                name='search',
                description='search query',
                required=True,
                option_type=3
            )
        ]
    )
    async def _play(self, ctx: SlashContext, *, search: str):
        await ctx.defer()
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            await ctx.invoke(self._connect)

        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
        if isinstance(source, asyncio.Queue):
            length = 0
            while not source.empty():
                await voice_state.queue.put(source.get_nowait())
                length += 1
            await ctx.send('Playlist Enqueued.' if length > 1 else 'Song Enqueued.')
        else:
            await voice_state.queue.put(source)
            await ctx.send('Song Enqueued.')

    @cog_ext.cog_slash(
        name='pause',
        description='Pauses the current song.',
        guild_ids=[840757649002725386]
    )
    async def _pause(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        if voice_state.playing():
            voice_state.voice.pause()
            await ctx.send('Song Paused.')

    @cog_ext.cog_slash(
        name='resume',
        description='Resumes a paused song.',
        guild_ids=[840757649002725386]
    )
    async def _resume(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        if voice_state.voice.is_paused():
            voice_state.voice.resume()
            await ctx.send('Song Resumed.')

    @cog_ext.cog_slash(
        name='skip',
        description='Skips the current song.',
        guild_ids=[840757649002725386]
    )
    async def _skip(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')

        member_count = math.ceil((len(bot.get_channel(self.channel.id).members) - 1)/2)
        voter = ctx.author
        if voter == voice_state.current.requester:
            voice_state.skip()
            await ctx.send('Song Skipped.')
        elif voter.id not in voice_state.skip_count:
            voice_state.skip_count.add(voter.id)
            vote_count = len(voice_state.skip_count)
            if vote_count >= member_count:
                voice_state.skip()
                await ctx.send('Song Skipped.')
            else:
                await ctx.send(f'Skip Vote **{vote_count}/{member_count}**.')
        else:
            await ctx.send('Already Voted.')

    @cog_ext.cog_slash(
        name='queue',
        description='Shows the queue (or a specific page).',
        guild_ids=[840757649002725386],
        options=[
            create_option(
                name='page',
                description='int',
                required=False,
                option_type=4
            )
        ]
    )
    async def _queue(self, ctx: SlashContext, *, page: int = 1):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if voice_state.queue.empty() and not voice_state.playing():
            return await ctx.send('Queue Empty.')

        page_total = 4
        queue_list = [voice_state.current] + list(voice_state.queue._queue)
        page_count = math.ceil(len(queue_list) / page_total)
        start = (page - 1) * page_total
        end = start + page_total
        queue_string = ''
        for i, song in enumerate(queue_list[start:end], start=start):
            queue_string += f'`{i + 1}.` [**{song.data["title"]}**]({song.data["url"]})\n'
        embed = (discord.Embed(description=f'**Queue ({len(queue_list)})**\n\n{queue_string}')
                 .set_footer(text=f'Page {page}/{page_count}'))
        await ctx.send(embed=embed)

    @cog_ext.cog_slash(
        name='current',
        description='Shows the current song.',
        guild_ids=[840757649002725386]
    )
    async def _current(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')

        await ctx.send(embed=voice_state.current.create_embed())

    @cog_ext.cog_slash(
        name='volume',
        description='Sets the volume for the current song.',
        guild_ids=[840757649002725386],
        options=[
            create_option(
                name='volume',
                description='int between 0 and 100',
                required=True,
                option_type=4
            )
        ]
    )
    async def _volume(self, ctx: SlashContext, *, volume: int):
        if not self.kernel_id:
            return await ctx.send('You cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')
        if volume < 0 or volume > 100:
            return await ctx.send('Between 0 and 100.')

        voice_state.current.volume = volume / 100
        embed = discord.Embed(title="Volume Message",
        description=f'Volume Changed By **{ctx.author.name}**')
        embed.add_field(name="Current Volume", value=volume, inline=True)
        await ctx.send(embed=embed)

    @cog_ext.cog_slash(
        name='remove',
        description='Removes a song from the queue at a given index.',
        guild_ids=[840757649002725386],
        options=[
            create_option(
                name='index',
                description='int',
                required=True,
                option_type=4
            )
        ]
    )
    async def _remove(self, ctx: SlashContext, index: int):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if voice_state.queue.empty() and not voice_state.playing():
            return await ctx.send('Empty Queue.')

        queue = voice_state.queue._queue
        if index == 1 and ctx.author == voice_state.current.requester:
            voice_state.skip()
            await ctx.send('Song Removed.')
        elif ctx.author == queue[index - 2].requester:
            del queue[index - 2]
            await ctx.send('Song Removed.')
        else:
            await ctx.send('Illegal Dequeue.')

    @cog_ext.cog_slash(
        name='shuffle',
        description='Shuffles the queue.',
        guild_ids=[840757649002725386]
    )
    async def _shuffle(self, ctx: SlashContext):
        if not self.kernel_id:
            return await ctx.send('You cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if voice_state.queue.empty():
            return await ctx.send('Empty Queue.')

        random.shuffle(voice_state.queue._queue)
        await ctx.send('Queue Shuffled.')

    @cog_ext.cog_slash(
        name='stop',
        description='Stops playing music and clears the queue.',
        guild_ids=[840757649002725386]
    )
    async def _stop(self, ctx: SlashContext):
        if not self.kernel_id:
            return await ctx.send('You cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        voice_state.queue._queue.clear()
        if voice_state.playing():
            voice_state.voice.stop()
            await bot.change_presence(activity=None)
            await ctx.send('Voice State Stopped.')

    @cog_ext.cog_slash(
        name='leave',
        description='Clears the queue and leaves the channel.',
        guild_ids=[840757649002725386]
    )
    async def _leave(self, ctx: SlashContext):
        if not self.kernel_id:
            return await ctx.send('You cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        await voice_state.stop()
        del self.voice_state[ctx.guild.id]
        await ctx.send('Auf Wiedersehen!')

    @cog_ext.cog_slash(
        name='kernel',
        description='Elevates a member to kernel mode.',
        guild_ids=[840757649002725386]
    )
    async def _kernel(self, ctx: SlashContext):
        if self.kernel_id:
            if self.kernel_id == ctx.author.id:
                return await ctx.send('You are already elevated to kernel mode.')
            return await ctx.send('Please wait for kernel mode to be released.')

        self.kernel_id = ctx.author.id
        await ctx.send(f'<@{self.kernel_id}> **elevated** to kernel mode.')

        try:
            async with timeout(10):
                await self.kernel_event.wait()
        except asyncio.TimeoutError: pass
        await ctx.send(f'<@{self.kernel_id}> **released** from kernel mode.')
        self.kernel_event.clear()
        self.kernel_id = None

bot.add_cog(Music(bot))

@bot.event
async def on_ready():
    print(f'{bot.user.name} Initialized. ({bot.user.id})')

music_room, bot_id = None, None
with open('idref.txt', 'r') as id:
    music_room, bot_id = int(id.readline()), int(id.readline())
    
@bot.event
async def on_message(message):
    global music_room, bot_id
    if message.channel.id == music_room and message.author.id != bot_id:
        await message.delete()

with open('token.txt', 'r') as token:
    bot.run(token.readline())
