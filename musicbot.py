""" A Discord Music Bot """

import discord, youtube_dl, asyncio
import functools, random, math
from async_timeout import timeout
from discord.ext import commands

bot = commands.Bot(command_prefix='!')

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

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict):
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
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        partial = functools.partial(cls.ytdl.extract_info, url=search, download=False)
        data = await loop.run_in_executor(None, partial)
        if 'entries' in data:
            playlist = []
            for entry in data['entries']:
                playlist.append(cls(ctx, discord.FFmpegPCMAudio(entry['url'], **cls.FFMPEG_OPTS), data=entry))
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

    def __init__(self, ctx: commands.Context):
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
            except asyncio.TimeoutError:
                return self.bot.loop.create_task(self.stop())

            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)
            self.message = await self.current.channel.send(embed=self.current.create_embed())

            await self.next.wait()
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
        if self.voice:
            await self.voice.disconnect()
            self.voice = None
            self.active = False

class Music(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_state = {}

    def get_voice_state(self, ctx: commands.Context):
        try:
            voice_state = self.voice_state[ctx.guild.id]
        except KeyError:
            voice_state = VoiceState(ctx)
            self.voice_state[ctx.guild.id] = voice_state

        if not voice_state or not voice_state.active:
            voice_state = VoiceState(ctx)
            self.voice_state[ctx.guild.id] = voice_state
        return voice_state

    @commands.command(name='connect')
    async def _connect(self, ctx: commands.Context, channel: discord.VoiceChannel = None):
        voice_state = self.get_voice_state(ctx)

        self.channel = channel or ctx.author.voice.channel
        if voice_state.voice:
            await voice_state.voice.move_to(self.channel)
        else:
            voice_state.voice = await self.channel.connect()

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            await ctx.invoke(self._connect)

        async with ctx.typing():
            source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            if not isinstance(source, list):
                return await voice_state.queue.put(source)
            for entry in source:
                await voice_state.queue.put(entry)

    @commands.command(name='pause')
    async def _pause(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        if voice_state.playing():
            voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    async def _resume(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        if voice_state.voice.is_paused():
            voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')

        member_count = math.ceil((len(bot.get_channel(self.channel.id).members) - 1)/2)
        voter = ctx.message.author
        if voter == voice_state.current.requester:
            voice_state.skip()
            await ctx.message.add_reaction('⏭')
        elif voter.id not in voice_state.skip_count:
            voice_state.skip_count.add(voter.id)
            vote_count = len(voice_state.skip_count)
            if vote_count >= member_count:
                await ctx.message.add_reaction('⏭')
                voice_state.skip()
            else:
                await ctx.send(f'Skip Vote **{vote_count}/{member_count}**.')
        else:
            await ctx.send('Already Voted.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if voice_state.queue.empty():
            return await ctx.send('Queue Empty.')

        page_total = 10
        queue_list = [voice_state.current] + list(voice_state.queue._queue)
        page_count = math.ceil(len(queue_list) / page_total)
        start = (page - 1) * page_total
        end = start + page_total
        queue_string = ''
        for i, song in enumerate(queue_list[start:end], start=start):
            queue_string += f'`{i + 1}.` [**{song.data["title"]}**]({song.data["url"]})\n'
        embed = (discord.Embed(description=f'**{len(queue_list)} Track List**\n\n{queue_string}')
                 .set_footer(text=f'Page {page}/{page_count}'))
        await ctx.send(embed=embed)

    @commands.command(name='current')
    async def _current(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        await ctx.send(embed=voice_state.current.create_embed())

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: float):
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

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if voice_state.queue.empty():
            return await ctx.send('Empty Queue.')

        if ctx.message.author == voice_state.current.requester and index == 1:
            voice_state.skip()
            return await ctx.message.add_reaction('✅')
        if ctx.message.author == voice_state.queue._queue[index - 2].requester:
            del voice_state.queue._queue[index - 2]
            return await ctx.message.add_reaction('✅')
        await ctx.send('Illegal Dequeue.')

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')
        if voice_state.queue.empty():
            return await ctx.send('Empty Queue.')

        random.shuffle(voice_state.queue._queue)
        await ctx.message.add_reaction('✅')

    @commands.command(name='stop')
    async def _stop(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        if voice_state.playing():
            voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='leave')
    async def _leave(self, ctx: commands.Context):
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            return await ctx.send('Not Connected.')

        await voice_state.stop()
        del self.voice_state[ctx.guild.id]
        await ctx.message.add_reaction('✅')

bot.add_cog(Music(bot))

@bot.event
async def on_ready():
    print(f'{bot.user.name} Initialized. ({bot.user.id})')

with open('token.txt', 'r') as token:
    bot.run(token.readline())
