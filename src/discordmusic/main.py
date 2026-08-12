import json
import os

import discord
from discord.ext import commands, tasks


class DiscordBot(commands.Bot):
    cogs = (
        'music',
        'quote',
        'poll',
        'pin',
        'role',
        'remind',
        'insult',
        'voice',
        'server',
    )

    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.all())

    async def setup_hook(self):
        for cog in self.cogs:
            await self.load_extension('discordmusic.cogs.' + cog)
        await bot.tree.sync()

    async def on_ready(self):
        print(f'{bot.user.name} Initialized.')


bot = DiscordBot()
bot.run(os.getenv('DISCORD_TOKEN'))
