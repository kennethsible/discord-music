from discord import app_commands
from discord.ext import commands, tasks
import discord, json

from datetime import datetime
from dateparser import parse
from pytz import timezone

with open('data/id_dict.json') as id_file:
    id_dict = json.load(id_file)

class RemindBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sleep_timers = []
        with open('data/reminders.json') as reminders_file:
            self.reminders = json.load(reminders_file)
        self.remind_task.start()
        self.counter = 0

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
            with open('data/reminders.json', 'w') as reminders_file:
                json.dump(list(self.reminders), reminders_file)

    @app_commands.command(name='remind', description='Set a reminder for a specific date/time.')
    async def _remind(self, interaction: discord.Interaction, what: str, when: str, who: discord.User = None):
        who_id = interaction.user.id if who is None else who.id
        when = timezone('EST').localize(parse(when))
        self.reminders.append((who_id, interaction.channel.id, what, when.isoformat()))
        with open('data/reminders.json', 'w') as reminders_file:
                json.dump(list(self.reminders), reminders_file)
        await interaction.response.send_message(f'<@{who_id}> will be reminded at {when.strftime("%I:%M:%S %p")} on {when.strftime("%m-%d-%Y")}.')

    @app_commands.command(name='sleep', description='Set a sleep timer to disconnect from a voice channel.')
    async def _sleep(self, interaction: discord.Interaction, when: str):
        when = timezone('EST').localize(parse(when))
        self.sleep_timers.append((interaction.user.id, when))
        await interaction.response.send_message(f'<@{interaction.user.id}> will be disconnected at {when.strftime("%I:%M:%S %p")} on {when.strftime("%m-%d-%Y")}.')

async def setup(bot: commands.Bot):
    await bot.add_cog(RemindBot(bot))
