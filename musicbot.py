""" A Discord Music Bot (and More!) """

import discord, youtube_dl, asyncio
import functools, random, math, json, re
from async_timeout import timeout
from discord.ext import commands, tasks
from collections import Counter
from datetime import datetime
from dateparser import parse
from pytz import timezone

from discord_slash import SlashCommand, SlashContext, cog_ext
from discord_slash.utils.manage_commands import create_option
from discord_slash.error import SlashCommandError

with open('id_dict.json') as id_file:
    id_dict = json.load(id_file)

bot = commands.Bot(command_prefix='!', self_bot=True,
    help_command=None, intents=discord.Intents.all())
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

        self.loop = False
        self.volume  = .5
        self.active  = True
        self.current = None
        self.message = None
        self.skip_count = set()

        ctx.bot.loop.create_task(self.audio_task())

    async def audio_task(self):
        while True:
            self.next.clear()
            if not self.loop:
                try:
                    async with timeout(300):
                        self.current = await self.queue.get()
                        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=self.current.data['title']))
                except asyncio.TimeoutError:
                    return self.bot.loop.create_task(self.stop())
            else:
                self.current.original = discord.FFmpegPCMAudio(self.current.data['url'], **YTDLSource.FFMPEG_OPTS)

            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)
            if not self.loop:
                self.message = await self.current.channel.send(embed=self.current.create_embed())

            await self.next.wait()
            if not self.loop:
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

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.channel.id == id_dict['music_room'] and message.author.id != id_dict['bot']:
            await message.delete()

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
        # if channel and (self.kernel_id != ctx.author.id):
        #     raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of privileged mode.')
        # if channel: self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        self.channel = channel or ctx.author.voice.channel
        if voice_state.voice:
            await voice_state.voice.move_to(self.channel)
        else:
            voice_state.voice = await self.channel.connect()
        await ctx.send(f'Connected to <#{self.channel.id}>.')

    @cog_ext.cog_slash(
        name='loop',
        description='Loops the current playing song.',
        guild_ids=[id_dict['guild']]
    )
    async def _loop(self, ctx: SlashContext):
        await self.ensure_voice_state(ctx)

        voice_state = self.get_voice_state(ctx)
        voice_state.loop = not voice_state.loop
        await ctx.send(f"Looping ({'Enabled' if voice_state.loop else 'Disabled'}).")

    @cog_ext.cog_slash(
        name='play',
        description='Plays or queues a song or playlist.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='search',
                description='string',
                required=True,
                option_type=3
            )
        ]
    )
    async def _play(self, ctx: SlashContext, search: str):
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
                description='int > 0',
                required=False,
                option_type=4
            )
        ]
    )
    async def _queue(self, ctx: SlashContext, page: int = 1):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if voice_state.queue.empty() and not voice_state.playing():
            return await ctx.send('Queue Empty.')

        page_total = 5
        queue_list = [voice_state.current] + list(voice_state.queue._queue)
        page_count = math.ceil(len(queue_list) / page_total)
        start = (page - 1) * page_total
        end = start + page_total
        description = ''
        for i, song in enumerate(queue_list[start:end], start=start):
            # description += f'`{i + 1}.` [**{song.data["title"]}**]({song.data["url"]})\n'
            description += f'`{i + 1}.` **{song.data["title"]}**\n'
        embed = discord.Embed(title=f'Queue ({len(queue_list)})', description=description,
            color=discord.Color.red()).set_footer(text=f'Page {page}/{page_count}')
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
                description='0 <= int <= 100',
                required=True,
                option_type=4
            )
        ]
    )
    async def _volume(self, ctx: SlashContext, value: int):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')
        if value < 0 or value > 100:
            return await ctx.send('0 <= volume <= 100.')

        voice_state.current.volume = value / 100
        embed = discord.Embed(title=f'Current Volume at {value}%',
        description=f'Volume Changed by <@{ctx.author.id}>')
        await ctx.send(embed=embed)

    @cog_ext.cog_slash(
        name='remove',
        description='Removes a song from the queue at a given index.',
        guild_ids=[id_dict['guild']],
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
        # if self.kernel_id != ctx.author.id:
        #     raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of privileged mode.')
        # self.kernel_event.set()

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
        # if self.kernel_id != ctx.author.id:
        #     raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of privileged mode.')
        # self.kernel_event.set()

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
        # if self.kernel_id != ctx.author.id:
        #     raise SlashCommandError(f'{ctx.author.name} cannot execute a privileged command outside of kernel mode.')
        # self.kernel_event.set()

        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)

        await voice_state.stop()
        del self.voice_state[ctx.guild.id]
        await ctx.send('Disconnected.')

    # @cog_ext.cog_slash(
    #     name='elevate',
    #     description='Elevates a member to privileged mode.',
    #     guild_ids=[id_dict['guild']],
    #     options=[
    #         create_option(
    #             name='member',
    #             description='user',
    #             required=True,
    #             option_type=6
    #         )
    #     ]
    # )
    async def _elevate(self, ctx: SlashContext, member: discord.User):
        if self.kernel_id:
            if self.kernel_id == ctx.author.id:
                raise SlashCommandError(f'{ctx.author.name} already elevated to privileged mode.')
            raise SlashCommandError(f'{ctx.author.name} cannot request elevation during lockdown.')

        if not ctx.author.guild_permissions.administrator:
            member_count = len(bot.get_channel(self.channel.id).members) - 1
            total = (member_count/2) + 1 if member_count % 2 == 0 else math.ceil(member_count/2)
            if member.id not in self.kernel_count:
                self.kernel_count[member.id] = set()
            if ctx.author.id not in self.kernel_count[member.id]:
                self.kernel_count[member.id].add(ctx.author.id)
                vote_count = len(self.kernel_count[member.id])
                if vote_count < total:
                    return await ctx.send(f'<@{member.id}> Elevation Vote at **{vote_count}/{total}**.')
            else:
                return await ctx.send('Already Voted.')

        self.kernel_id = member.id
        await ctx.send(f'<@{self.kernel_id}> **elevated** to privileged mode.')
        try:
            async with timeout(10):
                await self.kernel_event.wait()
        except asyncio.TimeoutError: pass
        await ctx.send(f'<@{self.kernel_id}> **released** from privileged mode.')
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

class RemindBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sleep_timers = []
        with open('reminders.json') as reminders_file:
            self.reminders = json.load(reminders_file)
        self.remind_task.start()

    @tasks.loop(seconds=1)
    async def remind_task(self):
        def diff(A, B):
            return [x for x in A if x not in B]
        now = timezone('EST').localize(datetime.now())
        completed, reminded = [], False
        for reminder in self.reminders:
            who_id, channel_id, message, when = reminder
            channel = self.bot.get_channel(channel_id)
            when = datetime.fromisoformat(when)
            if abs((now - when).total_seconds()) < 1.:
                completed.append(reminder)
                await channel.send(f'<@{who_id}> {message}')
                reminded = True
        if len(completed) > 0:
            self.reminders = diff(self.reminders, completed)
        guild = self.bot.get_guild(id_dict['guild'])
        for timer in self.sleep_timers:
            who_id, when = timer
            if abs((now - when).total_seconds()) < 1.:
                completed.append(timer)
                await guild.get_member(who_id).move_to(None)
        if len(completed) > 0:
            self.sleep_timers = diff(self.sleep_timers, completed)
        if reminded:
            with open('reminders.json', 'w') as reminders_file:
                json.dump(list(self.reminders), reminders_file)

    @cog_ext.cog_slash(
        name='remind',
        description='Sets a reminder for a specific date/time.',
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
                    description='date/time (in natural language)',
                    required=True,
                    option_type=3
                ),
                create_option(
                    name='who',
                    description='user',
                    required=False,
                    option_type=6
                )
            ]
    )
    async def _remind(self, ctx: SlashContext, what: str, when: str, who: discord.User = None):
        who_id = ctx.author.id if who is None else who.id
        when = timezone('EST').localize(parse(when))
        self.reminders.append((who_id, ctx.channel.id, what, when.isoformat()))
        with open('reminders.json', 'w') as reminders_file:
                json.dump(list(self.reminders), reminders_file)
        await ctx.send(f'<@{who_id}> will be reminded at {when.strftime("%I:%M:%S %p")} on {when.strftime("%m-%d-%Y")}.')

    @cog_ext.cog_slash(
        name='sleep',
        description='Sets a sleep timer to disconnect the member from a voice channel.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='when',
                    description='time (in natural language)',
                    required=True,
                    option_type=3
                )
            ]
    )
    async def _sleep(self, ctx: SlashContext, when: str):
        when = timezone('EST').localize(parse(when))
        self.sleep_timers.append((ctx.author.id, when))
        await ctx.send(f'<@{ctx.author.id}> will be disconnected at {when.strftime("%I:%M:%S %p")} on {when.strftime("%m-%d-%Y")}.')

class QuoteBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        with open('quotes.json') as quotes_file:
            self.quotes = Counter(json.load(quotes_file))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == bot.user: return
        for qbot in self.quotes:
            for alias in qbot.split(', '):
                if alias in message.content.upper():
                    await message.channel.send(random.choice(self.quotes[qbot])
                        .replace('<@>', f'<@{message.author.id}>'))

class EStatBot(commands.Cog):
    # TODO verify guild id and check for renamed emojis

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update = False
        with open('estats.json') as estats_file:
            self.estats = Counter(json.load(estats_file))
        self.estat_task.start()

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.update: self.update = True
        emojis = re.findall(r'(<:[^:]+[^>]+>)', message.content)
        if emojis: self.estats.update(emojis)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if not self.update: self.update = True
        self.estats.update(re.findall(r'(<:[^:]+[^>]+>)', str(payload.emoji)))

    @tasks.loop(minutes=30)
    async def estat_task(self):
        if self.update:
            with open('estats.json', 'w') as estats_file:
                json.dump(self.estats, estats_file)
            self.update = False

    @cog_ext.cog_slash(
        name='estat',
        description='Shows emoji statistics for messages and reactions.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='page',
                    description='int',
                    required=False,
                    option_type=4
                )
            ]
    )
    async def _estat(self, ctx: SlashContext, page: int = 1):
        page_total = 5
        page_count = math.ceil(len(self.estats) / page_total)
        start = (page - 1) * page_total
        end = start + page_total
        description = ''
        for i, (emoji, count) in enumerate(self.estats.most_common()[start:end], start=start):
            description += f'`{i + 1}.` {emoji} \u2192 {count}\n'
        embed = discord.Embed(title='Emoji Stats', description=description,
            color=discord.Color.blue()).set_footer(text=f'Page {page}/{page_count}')
        await ctx.send(embed=embed)

class PollBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.emojis = ('\u0031\uFE0F\u20E3', '\u0032\uFE0F\u20E3', '\u0033\uFE0F\u20E3', '\u0034\uFE0F\u20E3',
                       '\u0035\uFE0F\u20E3', '\u0036\uFE0F\u20E3', '\u0037\uFE0F\u20E3', '\u0038\uFE0F\u20E3',
                       '\u0039\uFE0F\u20E3', '\U0001F51F')

    @cog_ext.cog_slash(
        name='poll',
        description='Creates a simple emoji reaction poll.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='question',
                    description='string',
                    required=True,
                    option_type=3
                ),
                create_option(
                    name='options',
                    description='string (comma-separated; max of 10)',
                    required=False,
                    option_type=3
                )
            ]
    )
    async def _poll(self, ctx: SlashContext, question: str, options: str = None):
        question = f'**{question}**'
        if options is None:
            message = await ctx.send(question)
            for emoji in ('\u2705', '\u274E'):
                await message.add_reaction(emoji)
        else:
            options = options.split(',')
            if len(options) > 10:
                raise SlashCommandError(f'{ctx.author.name} provided too many options for a poll.')
            for emoji, option in zip(self.emojis, options):
                question += f'\n{emoji} {option.lstrip()}'
            message = await ctx.send(question)
            for i, emoji in enumerate(self.emojis):
                await message.add_reaction(emoji)
                if i == len(options) - 1: break

class PinBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if str(payload.emoji) == '\U0001F4CC':
            channel = await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            await message.pin()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if str(payload.emoji) == '\U0001F4CC':
            channel = await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if not '\U0001F4CC' in (reaction.emoji for reaction in message.reactions):
                await message.unpin()

for cog in (Music, RemindBot, QuoteBot, EStatBot, PollBot, PinBot):
    bot.add_cog(cog(bot))

@bot.event
async def on_ready():
    print(f'{bot.user.name} Initialized.')

with open('token.txt') as token:
    bot.run(token.readline())
