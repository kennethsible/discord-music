from translation.detect import Model
from discord.ext import commands
import discord, json

class DiscordBot(commands.Bot):

    cogs = (
        'music',
        'quote',
        'poll',
        'pin',
        'remind',
        'insult',
        'translate',
    )

    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.all())

    async def setup_hook(self):
        for cog in self.cogs:
            await self.load_extension('cogs.' + cog)
        await bot.tree.sync()

    async def on_ready(self):
        print(f'{bot.user.name} Initialized.')

bot = DiscordBot()
with open('data/token.txt') as token:
    bot.run(token.readline())
