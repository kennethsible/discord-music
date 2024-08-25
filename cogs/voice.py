from discord import app_commands
from discord.ext import commands
import discord, json, re

with open('data/id_dict.json') as id_file:
    id_dict = json.load(id_file)

class VoiceBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_room = int(id_dict['voice-room'])
        self.bitrate = 128000 # default bitrate
        self.channels = []

    @app_commands.command(name='channel', description='Customize the settings of a temporary voice channel.')
    async def _channel(self, interaction: discord.Interaction, name: str = None, status: str = None, limit: int = None):
        channel = interaction.user.voice.channel
        if channel.id in self.channels:
            if name and len(name) > 0:
                await channel.edit(name=name)
            if status and len(status) > 0:
                await channel.edit(status=status)
            if limit and limit > 0:
                await channel.edit(user_limit=limit)
            await interaction.response.send_message(f'Updated Settings for <#{channel.id}>.', delete_after=5)
        else:
            await interaction.response.send_message(f'Permission Denied for <#{channel.id}>.', delete_after=5)

    async def create_channel(self, member, category):
        count = len([channel for channel in self.channels if member.nick in channel.name]) + 1
        channel = await category.create_voice_channel(f'{member.nick}\'s Channel #{count}', bitrate=self.bitrate)
        self.channels.append(channel.id)
        await member.move_to(channel)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel:
            if before.channel.id in self.channels and len(self.bot.get_channel(before.channel.id).members) == 0:
                self.channels.remove(before.channel.id)
                await self.bot.get_channel(before.channel.id).delete()
        if after.channel:
            if after.channel.id == self.voice_room:
                await self.create_channel(member, after.channel.category)

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceBot(bot))
