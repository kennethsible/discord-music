""" A Discord Music Bot """

import discord, youtube_dl, asyncio
import functools, random, math, json
from async_timeout import timeout
from discord.ext import commands, tasks
from datetime import datetime
from dateparser import parse
from pytz import timezone

from discord_slash import SlashCommand, SlashContext, cog_ext
from discord_slash.utils.manage_commands import create_option
from discord_slash.error import SlashCommandError

with open('id_dict.json', 'r') as file:
    id_dict = json.load(file)

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)
slash = SlashCommand(bot, sync_commands=True)

class VoiceConnectionError(SlashCommandError): pass

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
                              description=f'[**{self.data["title"]}**]({self.data["webpage_url"]})\n',
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
                async with timeout(300):
                    self.current = await self.queue.get()
                    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=self.current.data['title']))
            except asyncio.TimeoutError:
                return self.bot.loop.create_task(self.stop())

            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)
            self.message = await self.current.channel.send(embed=self.current.create_embed())

            await self.next.wait()
            self.current = None
            self.skip_count.clear()
            await bot.change_presence(activity=None)
            # try: await self.message.delete()
            # except discord.HTTPException: pass

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
        self.kernel_count = {}
        self.kernel_id = None
        self.channel = None

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
        guild_ids=[id_dict['guild']],
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
        await self.ensure_voice_state(ctx)
        if channel and (self.kernel_id != ctx.author.id):
            raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of kernel mode.')
        if channel: self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        self.channel = channel or ctx.author.voice.channel
        if voice_state.voice:
            await voice_state.voice.move_to(self.channel)
        else:
            voice_state.voice = await self.channel.connect()
        await ctx.send(f'Connected to <#{self.channel.id}>.')

    @cog_ext.cog_slash(
        name='play',
        description='Plays or queues a song or playlist.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='search',
                description='string query or direct link',
                required=True,
                option_type=3
            )
        ]
    )
    async def _play(self, ctx: SlashContext, *, search: str):
        await ctx.defer()
        await self.ensure_voice_state(ctx)
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
        guild_ids=[id_dict['guild']]
    )
    async def _pause(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)

        if voice_state.playing():
            voice_state.voice.pause()
            await ctx.send('Song Paused.')

    @cog_ext.cog_slash(
        name='resume',
        description='Resumes a paused song.',
        guild_ids=[id_dict['guild']]
    )
    async def _resume(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)

        if voice_state.voice.is_paused():
            voice_state.voice.resume()
            await ctx.send('Song Resumed.')

    @cog_ext.cog_slash(
        name='skip',
        description='Skips the current song.',
        guild_ids=[id_dict['guild']]
    )
    async def _skip(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')

        member_count = len(bot.get_channel(self.channel.id).members) - 1
        total = (member_count/2) + 1 if member_count % 2 == 0 else math.ceil(member_count/2)
        if ctx.author == voice_state.current.requester:
            voice_state.skip()
            await ctx.send('Song Skipped.')
        elif ctx.author.id not in voice_state.skip_count:
            voice_state.skip_count.add(ctx.author.id)
            vote_count = len(voice_state.skip_count)
            if vote_count >= total:
                voice_state.skip()
                await ctx.send('Song Skipped.')
            else:
                await ctx.send(f'Skip Vote at **{vote_count}/{int(total)}**.')
        else:
            await ctx.send('Already Voted.')

    @cog_ext.cog_slash(
        name='queue',
        description='Shows the queue (or a specific page).',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='page',
                description='int (see queue)',
                required=False,
                option_type=4
            )
        ]
    )
    async def _queue(self, ctx: SlashContext, *, page: int = 1):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
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
        guild_ids=[id_dict['guild']]
    )
    async def _current(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')

        await ctx.send(embed=voice_state.current.create_embed())

    @cog_ext.cog_slash(
        name='volume',
        description='Sets the volume for the current song.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='value',
                description='0 <= volume <= 100',
                required=True,
                option_type=4
            )
        ]
    )
    async def _volume(self, ctx: SlashContext, *, volume: int):
        if self.kernel_id != ctx.author.id:
            raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')
        if volume < 0 or volume > 100:
            return await ctx.send('0 <= volume <= 100.')

        voice_state.current.volume = volume / 100
        embed = discord.Embed(title="Volume Message",
        description=f'Volume Changed By **{ctx.author.name}**')
        embed.add_field(name="Current Volume", value=volume, inline=True)
        await ctx.send(embed=embed)

    @cog_ext.cog_slash(
        name='remove',
        description='Removes a song from the queue at a given index.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='index',
                description='int (see queue)',
                required=True,
                option_type=4
            )
        ]
    )
    async def _remove(self, ctx: SlashContext, index: int):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
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
        guild_ids=[id_dict['guild']]
    )
    async def _shuffle(self, ctx: SlashContext):
        if self.kernel_id != ctx.author.id:
            raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if voice_state.queue.empty():
            return await ctx.send('Empty Queue.')

        random.shuffle(voice_state.queue._queue)
        await ctx.send('Queue Shuffled.')

    @cog_ext.cog_slash(
        name='stop',
        description='Stops playing music and clears the queue.',
        guild_ids=[id_dict['guild']]
    )
    async def _stop(self, ctx: SlashContext):
        if self.kernel_id != ctx.author.id:
            raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)

        voice_state.queue._queue.clear()
        if voice_state.playing():
            voice_state.voice.stop()
            await bot.change_presence(activity=None)
            await ctx.send('Voice State Stopped.')

    @cog_ext.cog_slash(
        name='leave',
        description='Clears the queue and leaves the channel.',
        guild_ids=[id_dict['guild']]
    )
    async def _leave(self, ctx: SlashContext):
        if self.kernel_id != ctx.author.id:
            raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of kernel mode.')
        self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)

        await voice_state.stop()
        del self.voice_state[ctx.guild.id]
        await ctx.send('Auf Wiedersehen!')

    @cog_ext.cog_slash(
        name='kernel',
        description='Elevates a member to kernel mode.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='member',
                description='channel member',
                required=True,
                option_type=6
            )
        ]
    )
    async def _kernel(self, ctx: SlashContext, member: discord.User):
        if self.kernel_id:
            if self.kernel_id == ctx.author.id:
                raise SlashCommandError(f'{ctx.author.name} already elevated to kernel mode.')
            raise SlashCommandError(f'{ctx.author.name} cannot request kernel mode during lockdown.')

        if not ctx.author.guild_permissions.administrator:
            member_count = len(bot.get_channel(self.channel.id).members) - 1
            total = (member_count/2) + 1 if member_count % 2 == 0 else math.ceil(member_count/2)
            if member.id not in self.kernel_count:
                self.kernel_count[member.id] = set()
            if ctx.author.id not in self.kernel_count[member.id]:
                self.kernel_count[member.id].add(ctx.author.id)
                vote_count = len(self.kernel_count[member.id])
                if vote_count < total:
                    return await ctx.send(f'<@{member.id}> Kernel Vote at **{vote_count}/{total}**.')
            else:
                return await ctx.send('Already Voted.')

        self.kernel_id = member.id
        await ctx.send(f'<@{self.kernel_id}> **elevated** to kernel mode.')
        try:
            async with timeout(10):
                await self.kernel_event.wait()
        except asyncio.TimeoutError: pass
        await ctx.send(f'<@{self.kernel_id}> **released** from kernel mode.')
        self.kernel_event.clear()
        self.kernel_count.clear()
        self.kernel_id = None

    async def ensure_connection(self, voice_state: VoiceState):
        if not voice_state.voice:
            raise SlashCommandError(f'{bot.user.name} not connected to a voice channel.')

    async def ensure_voice_state(self, ctx: SlashContext):
        voice_state = self.get_voice_state(ctx)
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise SlashCommandError(f'{ctx.author.name} isn\'t connected to a voice channel.')
        if voice_state.voice:
            if voice_state.voice.channel != ctx.author.voice.channel:
                raise SlashCommandError(f'{bot.user.name} already connected to a voice channel.')

bot.add_cog(Music(bot))

class RemindMe(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminders = set()
        self.remind_task.start()

    @tasks.loop(seconds=1)
    async def remind_task(self):
        now = timezone('EST').localize(datetime.now())
        completed = set()
        for reminder in self.reminders:
            author, channel, message, when_dt = reminder
            if abs((now - when_dt).total_seconds()) < 1.:
                completed.add(reminder)
                await channel.send(f'<@{author.id}> {message}')
        for reminder in completed:
            self.reminders.remove(reminder)

    @cog_ext.cog_slash(
        name='remindme',
        description='Sets a reminder for a certain amount of time.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='what',
                    description='message',
                    required=True,
                    option_type=3
                ),
                create_option(
                    name='when',
                    description='date/time',
                    required=True,
                    option_type=3
                )
            ]
    )
    async def _remindme(self, ctx: SlashContext, *, what: str, when: str):
        when_dt = timezone('EST').localize(parse(when))
        self.reminders.add((ctx.author, ctx.channel, what, when_dt))
        await ctx.send(f'I will message <@{ctx.author.id}> at {when_dt.strftime("%I:%M:%S %p")} on {when_dt.strftime("%m-%d-%Y")}!')

bot.add_cog(RemindMe(bot))

@bot.event
async def on_ready():
    print(f'{bot.user.name} Initialized.')

quotes = json.load(open('quotes.json'))

@bot.event
async def on_message(message):
    if message.channel.id == id_dict['music_room'] and message.author.id != id_dict['bot']:
        await message.delete()
    ##### Quote Bot #####
    if message.author == bot.user: return
    for qbot in quotes:
        for alias in qbot.split(', '):
            if alias in message.content.upper():
                await message.channel.send(random.choice(quotes[qbot])
                    .replace('<@>', f'<@{message.author.id}>'))

with open('token.txt', 'r') as token:
    bot.run(token.readline())
