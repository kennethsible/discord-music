from collections import Counter

from discord import app_commands
from discord.ext import commands
import discord, random, json, re

class QuoteBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        with open('data/quotes.json') as quotes_file:
            self.quotes = Counter(json.load(quotes_file))
        self.alias_map: dict[str, list[str]] = {}
        for aliases, responses in self.quotes.items():
            for alias in aliases.split(', '):
                self.alias_map[alias.lower()] = responses

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user: return
        tokens = set(re.findall(r'\b\w+\b', message.content.lower()))
        alias_match = tokens.intersection(self.alias_map)
        if alias_match:
            alias = next(iter(alias_match))
            response = random.choice(self.alias_map[alias]).replace(
                '<@>', f'<@{message.author.id}>'
            )
            await message.channel.send(response)

async def setup(bot: commands.Bot):
    await bot.add_cog(QuoteBot(bot))
