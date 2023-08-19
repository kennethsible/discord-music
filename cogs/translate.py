from discord import app_commands
from discord.ext import commands
import discord, torch, toml, json

from translation.manager import Manager, Tokenizer
from translation.translate import translate_string
from translation.detect import detect_lang

with open('data/id_dict.json') as id_file:
    id_dict = json.load(id_file)

class TranslateBot(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        with open('translation/model.config') as file:
            config = toml.load(file)
        device = torch.device('cpu')

        self.model_detect = torch.load('translation/data/model_detect')

        # self.manager_ende = Manager('en', 'de', config, device, vocab_file='translation/data/vocab.ende')
        # self.tokenizer_ende = Tokenizer('en', 'de', 'translation/data/codes.ende')
        # self.manager_ende.load_model('translation/data/model_large.ende')
        # self.manager_ende.model.eval()

        self.manager_deen = Manager('de', 'en', config, device, vocab_file='translation/data/vocab.deen')
        self.tokenizer_deen = Tokenizer('de', 'en', 'translation/data/codes.deen')
        self.manager_deen.load_model('translation/data/model_large.deen')
        self.manager_deen.model.eval()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id == id_dict['bot']: return
        src_lang = detect_lang(message.content, self.model_detect)
        if src_lang is None: return
        if src_lang != 'en':
            if src_lang != 'de': return
            title = 'German'
            output = translate_string(message.content, self.manager_deen, self.tokenizer_deen)
            embed = discord.Embed(title=f"Translation ({'to' if src_lang == 'en' else 'from'} {title})",
                description=output, color=discord.Color.green())
            await message.reply(embed=embed)

    @app_commands.command(name='translate', description='Translate from one natural language to another.')
    async def _translate(self, interaction: discord.Interaction, string: str, src_lang: str = None, tgt_lang: str = None):
        await interaction.response.defer()
        if src_lang is None:
            src_lang = detect_lang(string, self.model_detect)
        if tgt_lang is None:
            tgt_lang = 'en'

        # if src_lang == 'en' and tgt_lang == 'de':
        #     title = 'German'
        #     output = translate_string(string, self.manager_ende, self.tokenizer_ende)
        if src_lang == 'de' and tgt_lang == 'en':
            title = 'German'
            output = translate_string(string, self.manager_deen, self.tokenizer_deen)
        else:
            return await interaction.followup.send('Unsupported Translation.')

        embed = discord.Embed(title=f"Translation ({'to' if src_lang == 'en' else 'from'} {title})",
            description=output, color=discord.Color.green())
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(TranslateBot(bot))
