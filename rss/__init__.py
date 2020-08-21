import warnings

from .core import RSS

warnings.filterwarnings("once", category=DeprecationWarning, module="feedparser")

__red_end_user_data_statement__ = (
    "This cog does not persistently store data or metadata about users."
)


async def setup(bot):
    await bot.send_to_owners(
        "This cog is a direct v3.4 port of the rss cog without modifications from SinbadCogs "
     )
    cog = RSS(bot)
    bot.add_cog(cog)
    cog.init()