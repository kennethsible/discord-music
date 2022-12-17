""" A Discord Music Bot (and More!) """

from async_timeout import timeout
from discord.ext import commands, tasks
from discord.utils import get
from collections import Counter
from datetime import datetime
from dateparser import parse
from pytz import timezone
from ytmusicapi import YTMusic
from nltk.tokenize import word_tokenize

from discord_slash import SlashCommand, SlashContext, cog_ext
from discord_slash.utils.manage_commands import create_option, create_choice
from discord_slash.error import SlashCommandError

import discord, asyncio, requests, youtube_dl
import functools, random, math, json, re

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
    async def create_source(cls, ctx: SlashContext, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        partial = functools.partial(cls.ytdl.extract_info, url=search, download=False)
        data = await loop.run_in_executor(None, partial)
        if 'entries' in data:
            playlist = asyncio.Queue()
            for entry in data['entries']:
                source = discord.FFmpegPCMAudio(entry['url'], **cls.FFMPEG_OPTS)
                await playlist.put(cls(source, ctx.channel, ctx.author, entry))
            return playlist
        source = discord.FFmpegPCMAudio(data['url'], **cls.FFMPEG_OPTS)
        return cls(source, ctx.channel, ctx.author, data)

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

        self.loop    = False
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

            self.message = await self.current.channel.send(embed=self.current.create_embed())
            for emoji in ('\U000023EF', '\U000023ED', '\U000023F9', '\U0001F500', '\U0001F502'):
                await self.message.add_reaction(emoji)
            self.current.volume = self.volume
            self.voice.play(self.current, after=self.next_song)

            await self.next.wait()
            self.skip_count.clear()
            await bot.change_presence(activity=None)
            await self.message.clear_reactions()
            # try: await self.message.delete()
            # except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.user_id != id_dict['bot']:
            channel = await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if str(payload.emoji) == '\U000023EF':
                if not self.voice.is_paused():
                    self.voice.pause()
            elif str(payload.emoji) == '\U000023ED':
                self.loop = False
                if payload.member == self.current.author:
                    self.skip()
                    await self.message.clear_reactions()
                elif payload.user_id not in self.skip_count:
                    self.skip_count.add(payload.user_id)
                    if len(self.skip_count) >= 2:
                        self.skip()
                        await self.message.clear_reactions()
            elif str(payload.emoji) == '\U000023F9':
                self.queue._queue.clear()
                if self.playing():
                    self.voice.stop()
                    await self.bot.change_presence(activity=None)
                    await self.message.clear_reactions()
            elif str(payload.emoji) == '\U0001F500':
                if not self.queue.empty():
                    random.shuffle(self.queue._queue)
                await message.remove_reaction(payload.emoji, payload.member)
            elif str(payload.emoji) == '\U0001F502':
                self.loop = True

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
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

class MusicBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ytmusic = YTMusic()
        self.voice_state = None

    def get_voice_state(self, ctx: SlashContext):
        if not self.voice_state or not self.voice_state.active:
            voice_state = VoiceState(ctx)
            self.bot.add_cog(voice_state)
            self.voice_state = voice_state
        return self.voice_state

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.channel.id == id_dict['music-room'] and message.author.id != id_dict['bot']:
            await message.delete()

    @cog_ext.cog_slash(
        name='play',
        description='Play a song or video from YouTube.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='search',
                description='string or YouTube URL',
                required=True,
                option_type=3
            ),
            create_option(
                name='source',
                description='audio source',
                required=False,
                option_type=4,
                choices=[
                    create_choice(value=0, name="YouTube"),
                    create_choice(value=1, name="YouTube Music")
                ]
            )
        ]
    )
    async def _play(self, ctx: SlashContext, search: str, source: int = 0):
        await ctx.defer()
        await self.ensure_voice_state(ctx)
        voice_state = self.get_voice_state(ctx)
        if not voice_state.voice:
            channel = ctx.author.voice.channel
            voice_state.voice = await channel.connect()

        if source == 1: # YouTube Music
            search = 'https://music.youtube.com/watch?v=' \
                + self.ytmusic.search(search, filter='songs')[0]['videoId']
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
        if isinstance(source, asyncio.Queue):
            queue_size = 0
            while not source.empty():
                await voice_state.queue.put(source.get_nowait())
                queue_size += 1
            await ctx.send('Playlist Enqueued.' if queue_size > 1 else 'Song Enqueued.')
        else:
            await voice_state.queue.put(source)
            await ctx.send('Song Enqueued.')

    @cog_ext.cog_slash(
        name='queue',
        description='Show the current queue of songs or videos.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='page',
                description='page number',
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

        queue_list = [voice_state.current] + list(voice_state.queue._queue)
        page_count = math.ceil(len(queue_list) / 5)
        start = (page - 1) * 5

        description = ''
        for i, song in enumerate(queue_list[start:(start + 5)], start=start):
            description += f'`{i + 1}.` **{song.data["title"]}**\n'
        embed = discord.Embed(title=f'Queue ({len(queue_list)})', description=description,
            color=discord.Color.red()).set_footer(text=f'Page {page}/{page_count}')
        await ctx.send(embed=embed)

    @cog_ext.cog_slash(
        name='volume',
        description='Set the volume of the current song or video.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='value',
                description='volume from 0 to 100',
                required=False,
                option_type=4
            )
        ]
    )
    async def _volume(self, ctx: SlashContext, value: int = None):
        voice_state = self.get_voice_state(ctx)
        await self.ensure_connection(voice_state)
        if not voice_state.playing():
            return await ctx.send('Nothing Playing.')
        if value is None:
            return await ctx.send(f'Current Volume ({int(voice_state.volume * 100)}).')
        if value < 0 or value > 100:
            return await ctx.send('Invalid Volume.')

        # voice_state.volume = value / 100 # global change
        voice_state.current.volume = voice_state.volume
        await ctx.send(f'Volume Changed ({value}).')

    @cog_ext.cog_slash(
        name='remove',
        description='Remove a song or video from the queue.',
        guild_ids=[id_dict['guild']],
        options=[
            create_option(
                name='index',
                description='position in the queue',
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
        if index == 1 and ctx.author == voice_state.current.author:
            voice_state.skip()
            await ctx.send('Song Removed.')
        elif ctx.author == queue[index - 2].author:
            del queue[index - 2]
            await ctx.send('Song Removed.')
        else:
            await ctx.send('Illegal Dequeue.')

    @cog_ext.cog_slash(
        name='move',
        description='Move from one voice channel to another.',
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
    async def _move(self, ctx: SlashContext, channel: discord.VoiceChannel = None):
        voice_state = self.get_voice_state(ctx)
        channel = channel or ctx.author.voice.channel
        if voice_state.voice:
            await voice_state.voice.move_to(channel)
        else:
            voice_state.voice = await channel.connect()
        await ctx.send(f'Connected to <#{channel.id}>.')

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
                await channel.send(f'**[Reminder]** <@{who_id}> {message}')
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
        description='Set a reminder for a specific date/time.',
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
        await ctx.send(f'<@{who_id}> will be reminded at {when.strftime("%I:%M:%S %p")} on {when.strftime("%m-%d-%Y")}.', hidden=True)

    @cog_ext.cog_slash(
        name='sleep',
        description='Set a sleep timer to disconnect from a voice channel.',
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
                if alias.lower() in word_tokenize(message.content.lower()):
                    await message.channel.send(random.choice(self.quotes[qbot])
                        .replace('<@>', f'<@{message.author.id}>'))

class EStatBot(commands.Cog):

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
        description='Show emoji statistics for messages and reactions.',
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

class WFreqBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @cog_ext.cog_slash(
        name='wfreq',
        description='Show a list of most frequent words or n-grams.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='who',
                    description='user',
                    required=True,
                    option_type=6
                ),
                create_option(
                    name='length',
                    description='int',
                    required=False,
                    option_type=4
                ),
                create_option(
                    name='limit',
                    description='int',
                    required=False,
                    option_type=4
                )
            ]
    )
    async def _wfreq(self, ctx: SlashContext, who: discord.User, *, length: int = None, limit: int = None):
        await ctx.defer()
        messages = Counter()
        async for message in ctx.channel.history(limit=limit):
            if message.author.id == who.id:
                string = message.content.lower()
                if len(string) == 0: continue
                words = string.split(' ')
                if length is None or len(words) == length:
                    messages[string] += 1
                # for i in range(len(msg) - length + 1):
                #     messages[' '.join(msg[i:(i + length)])] += 1
        # async for message in ctx.channel.history(limit=limit):
        #     if message.author.id == who.id:
        #         msg = message.content.lower().split(' ')
        #         for i in range(len(msg) - length + 1):
        #             sub_msg = ' '.join(msg[i:(i + length)])
        #             if sub_msg in messages:
        #                 messages[sub_msg] += 1
        # messages = Counter([message.content for message in await ctx.channel.history(limit=limit).flatten()
        #     if message.author.id == who.id and len(message.content.split(' ')) == length])
        description = ''
        for i, (ngram, count) in enumerate(messages.most_common()[:5]):
            description += f'`{i + 1}.` {ngram} \u2192 {count}\n'
        embed = discord.Embed(title=f'{length if length else "N"}-Gram Frequency', description=description,
            color=discord.Color.green()).set_footer(text=who.display_name)
        await ctx.send(embed=embed)

class RoleBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        with open('roles.json') as roles_file:
            self.roles = json.load(roles_file, object_hook=lambda x: {int(i): x[i] for i in x})

    @cog_ext.cog_slash(
        name='role',
        description='Create a custom role and assign that role to a someone.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='who',
                    description='user',
                    required=True,
                    option_type=6
                ),
                create_option(
                    name='name',
                    description='string',
                    required=True,
                    option_type=3
                ),
                create_option(
                    name='color',
                    description='string (hexadecimal)',
                    required=True,
                    option_type=3
                )
            ]
    )
    async def _role(self, ctx: SlashContext, who: discord.User, name: str, color: str):
        if who.id in self.roles:
            role = ctx.guild.get_role(self.roles[who.id])
            await role.edit(name=name, color=discord.Color(int('0x' + color, 16)))
        else:
            role = await ctx.guild.create_role(name=name, color=discord.Color(int('0x' + color, 16)))
            self.roles[who.id] = role.id
            await who.add_roles(role)
        await ctx.send(f'Created Role <@&{role.id}> for <@!{who.id}>.', allowed_mentions=discord.AllowedMentions.none())
        with open('roles.json', 'w') as roles_file:
            json.dump(self.roles, roles_file)

class PollBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.emojis = ['\u0031\uFE0F\u20E3', '\u0032\uFE0F\u20E3', '\u0033\uFE0F\u20E3', '\u0034\uFE0F\u20E3',
                       '\u0035\uFE0F\u20E3', '\u0036\uFE0F\u20E3', '\u0037\uFE0F\u20E3', '\u0038\uFE0F\u20E3',
                       '\u0039\uFE0F\u20E3', '\U0001F51F']

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        channel = await self.bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        if message.author.id == id_dict['bot'] != payload.member.id:
            if ('[Poll]' in message.content or '[Multi-Poll]' in message.content) \
                and payload.emoji.name not in self.emojis + ['\u2705', '\u274E']:
                    await message.clear_reaction(payload.emoji)
            if '[Poll]' in message.content:
                for reaction in message.reactions:
                    if reaction.emoji == payload.emoji.name: continue
                    users = await reaction.users().flatten()
                    if any(payload.user_id == user.id for user in users):
                        return await message.remove_reaction(payload.emoji, payload.member)

    @cog_ext.cog_slash(
        name='poll',
        description='Create a simple emoji reaction poll.',
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
                    description='comma-separated; max of 10',
                    required=False,
                    option_type=3
                ),
                create_option(
                    name='multiple',
                    description='allow multiple answers',
                    required=False,
                    option_type=5
                )
            ]
    )
    async def _poll(self, ctx: SlashContext, question: str, options: str = None, multiple: bool = False):
        question = f'**[{"Multi-" if multiple else ""}Poll] {question}**'
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

class InsultBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_url = 'https://evilinsult.com/generate_insult.php?lang=en&type=json'

    @cog_ext.cog_slash(
        name='insult',
        description='Send a random evil insult to someone.',
        guild_ids=[id_dict['guild']],
            options=[
                create_option(
                    name='who',
                    description='user',
                    required=True,
                    option_type=6
                )
            ]
    )
    async def _insult(self, ctx: SlashContext, who: discord.User):
        response = requests.get(self.api_url)
        await ctx.send(f'<@{who.id}> {json.loads(response.text)["insult"]}')

for cog in (MusicBot, RemindBot, QuoteBot, EStatBot, RoleBot, PollBot, PinBot, InsultBot):
    bot.add_cog(cog(bot))

@bot.event
async def on_ready():
    print(f'{bot.user.name} Initialized.')

with open('token.txt') as token:
    bot.run(token.readline())
