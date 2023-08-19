from nltk.tokenize import word_tokenize
from collections import Counter

from discord import app_commands
from discord.ext import commands
import discord, random, json

class QuoteBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        with open('data/quotes.json') as quotes_file:
            self.quotes = Counter(json.load(quotes_file))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user: return
        for qbot in self.quotes:
            for alias in qbot.split(', '):
                if alias.lower() in word_tokenize(message.content.lower()):
                    await message.channel.send(random.choice(self.quotes[qbot])
                        .replace('<@>', f'<@{message.author.id}>'))

async def setup(bot: commands.Bot):
    await bot.add_cog(QuoteBot(bot))
