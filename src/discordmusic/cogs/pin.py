from discord import app_commands
from discord.ext import commands
import discord, json

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

async def setup(bot: commands.Bot):
    await bot.add_cog(PinBot(bot))
