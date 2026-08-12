from discord.ext import commands
from aiohttp import web
import asyncio

class Server(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.site = None
        self.bot.loop.create_task(self.start_server())

    async def get_status(self, request):
        activity = self.bot.guilds[0].get_member(self.bot.user.id).activity
        activity_name = activity.name if activity else 'Nothing Playing'
        voice_client = self.bot.guilds[0].voice_client
        channel_name = voice_client.channel.name if voice_client else '-'
        # 'ping': round(self.bot.latency * 1000)
        return web.json_response({'activity': activity_name, 'channel': channel_name})

    async def start_server(self):
        app = web.Application()
        app.router.add_get('/api', self.get_status)

        runner = web.AppRunner(app)
        await runner.setup()

        self.api = web.TCPSite(runner, '0.0.0.0', 8093)

        await self.bot.wait_until_ready()
        await self.api.start()
        print('API Server Started.')

    def __unload(self):
        asyncio.ensure_future(self.api.stop())
        print('API Server Stopped.')

async def setup(bot: commands.Bot):
    await bot.add_cog(Server(bot))
