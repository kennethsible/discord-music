import json

import discord
import requests
from discord import app_commands
from discord.ext import commands


class InsultBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_url = 'https://evilinsult.com/generate_insult.php?lang=en&type=json'

    @app_commands.command(name='insult', description='Send a random evil insult to someone.')
    async def _insult(self, interaction: discord.Interaction, who: discord.User):
        response = requests.get(self.api_url)
        await interaction.response.send_message(
            f'<@{who.id}> {json.loads(response.text)["insult"]}'
        )

    @app_commands.command(name='meowfacts', description='Send a random cat fact to someone.')
    async def _meowfacts(self, interaction: discord.Interaction, who: discord.User):
        response = requests.get('https://meowfacts.herokuapp.com/')
        await interaction.response.send_message(
            f'<@{who.id}> {json.loads(response.text)["data"][0]}'
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(InsultBot(bot))
