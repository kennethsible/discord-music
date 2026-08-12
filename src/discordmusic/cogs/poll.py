import json

import discord
from discord import app_commands
from discord.ext import commands

with open('/data/id_dict.json') as id_file:
    id_dict = json.load(id_file)


class PollBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.emojis = [
            '\u0031\ufe0f\u20e3',
            '\u0032\ufe0f\u20e3',
            '\u0033\ufe0f\u20e3',
            '\u0034\ufe0f\u20e3',
            '\u0035\ufe0f\u20e3',
            '\u0036\ufe0f\u20e3',
            '\u0037\ufe0f\u20e3',
            '\u0038\ufe0f\u20e3',
            '\u0039\ufe0f\u20e3',
            '\U0001f51f',
        ]

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        channel = await self.bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        if message.author.id == id_dict['bot'] != payload.member.id:
            # only allow poll-specific reactions
            if (
                '[Poll]' in message.content or '[Multi-Poll]' in message.content
            ) and payload.emoji.name not in self.emojis + ['\u2705', '\u274e']:
                await message.clear_reaction(payload.emoji)
            # only allow one reaction per user if not multiple-choice
            if '[Poll]' in message.content:
                for reaction in message.reactions:
                    if reaction.emoji == payload.emoji.name:
                        continue
                    users = [user async for user in reaction.users()]
                    if any(payload.user_id == user.id for user in users):
                        return await message.remove_reaction(payload.emoji, payload.member)

    @app_commands.command(name='poll', description='Create an emoji reaction poll.')
    async def _poll(
        self,
        interaction: discord.Interaction,
        question: str,
        answers: str = None,
        multiple_answers: bool = False,
    ):
        content = f'**[{"Multi-" if multiple_answers else ""}Poll] {question}**'
        if answers is None:
            await interaction.response.send_message(content)
            message = await interaction.original_response()
            for emoji in ('\u2705', '\u274e'):
                await message.add_reaction(emoji)
        else:
            answers = answers.split(',')
            if len(answers) > 10:
                raise app_commands.AppCommandError(
                    f'{interaction.user.name} provided too many answers for a poll.'
                )
            for emoji, option in zip(self.emojis, answers):
                content += f'\n{emoji} {option.lstrip()}'
            await interaction.response.send_message(content)
            message = await interaction.original_response()
            for i, emoji in enumerate(self.emojis):
                await message.add_reaction(emoji)
                if i == len(answers) - 1:
                    break


async def setup(bot: commands.Bot):
    await bot.add_cog(PollBot(bot))
