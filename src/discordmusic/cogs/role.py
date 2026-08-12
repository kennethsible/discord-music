import json
import re

import discord
import webcolors
from discord import app_commands
from discord.ext import commands


class RoleBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        with open('/data/roles.json') as roles_file:
            self.roles = json.load(roles_file, object_hook=lambda x: {int(i): x[i] for i in x})

    @app_commands.command(name='role', description='Create a custom role for a member.')
    async def _role(
        self, interaction: discord.Interaction, who: discord.User, name: str, color: str = '#ffffff'
    ):
        if not re.match(r'#[A-Fa-f0-9]{6}', color):
            try:
                color = webcolors.name_to_hex(color.replace(' ', '').lower()).lstrip('#')
            except ValueError:
                raise app_commands.AppCommandError(
                    f"'{color}' is not a valid css3 or hexadecimal color."
                )
        if who.id in self.roles:
            role = interaction.guild.get_role(self.roles[who.id])
            await role.edit(name=name, color=discord.Color(int('0x' + color.lstrip('#'), 16)))
        else:
            role = await interaction.guild.create_role(
                name=name, color=discord.Color(int('0x' + color, 16))
            )
            self.roles[who.id] = role.id
            await who.add_roles(role)
        await interaction.response.send_message(
            f'Created Role <@&{role.id}> for <@!{who.id}>.',
            allowed_mentions=discord.AllowedMentions.none(),
        )
        with open('/data/roles.json', 'w') as roles_file:
            json.dump(self.roles, roles_file)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleBot(bot))
