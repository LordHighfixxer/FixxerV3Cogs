from __future__ import annotations
import asyncio
import logging
import string
import urllib.parse
from datetime import datetime
from functools import partial
from types import MappingProxyType
from typing import Any, Dict, Generator, List, Optional, cast
import aiohttp
import discord
import feedparser
from bs4 import BeautifulSoup as bs4
from redbot.core import checks, commands
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box, pagify

from .cleanup import html_to_text
from .converters import NonEveryoneRole, TriState
from .converters import FieldAndTerm, NonEveryoneRole, TriState

log = logging.getLogger("red.sinbadcogs.rss")

DONT_HTML_SCRUB = ["link", "source", "updated", "updated_parsed"]
USABLE_FIELDS = [
    "author",
    "author_detail",
    "description",
    "comments",
    "content",
    "contributors",
    "created",
    "updated",
    "updated_parsed",
    "link",
    "name",
    "published",
    "published_parsed",
    "publisher",
    "publisher_detail",
    "source",
    "summary",
    "summary_detail",
    "tags",
    "title",
    "title_detail",
]

USABLE_TEXT_FIELDS = [
    f
    for f in USABLE_FIELDS
    if f
    not in ("published", "published_parsed", "updated", "updated_parsed", "created",)
]


def debug_exc_log(lg: logging.Logger, exc: Exception, msg: str = "Exception in RSS"):
    if lg.getEffectiveLevel() <= logging.DEBUG:
        lg.exception(msg, exc_info=exc)
class RSS(commands.Cog):
    """
    An RSS cog.
    """

    __author__ = "mikeshardmind(Sinbad)"
    __version__ = "340.0.1"
    __version__ = "340.0.2"

    async def red_delete_data_for_user(self, **kwargs):
        """ Nothing to delete """
        return
    def format_help_for_context(self, ctx):
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\nCog Version: {self.__version__}"
    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=78631113035100160, force_registration=True
        )
        self.config.register_channel(feeds={})
        self.session = aiohttp.ClientSession()
        self.bg_loop_task: Optional[asyncio.Task] = None
    def init(self):
        self.bg_loop_task = asyncio.create_task(self.bg_loop())
        def done_callback(fut: asyncio.Future):
            try:
                fut.exception()
            except asyncio.CancelledError:
                pass
            except asyncio.InvalidStateError as exc:
                log.exception(
                    "We somehow have a done callback when not done?", exc_info=exc
                )
            except Exception as exc:
                log.exception("Unexpected exception in rss: ", exc_info=exc)
        self.bg_loop_task.add_done_callback(done_callback)
    def cog_unload(self):
        if self.bg_loop_task:
            self.bg_loop_task.cancel()
        asyncio.create_task(self.session.close())
    async def should_embed(self, channel: discord.TextChannel) -> bool:
        ret: bool = await self.bot.embed_requested(channel, channel.guild.me)
        return ret
    async def fetch_feed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        timeout = aiohttp.client.ClientTimeout(total=15)
        try:
            async with self.session.get(url, timeout=timeout) as response:
                data = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        except Exception as exc:
            debug_exc_log(
                log,
                exc,
                f"Unexpected exception type {type(exc)} encountered for feed url: {url}",
            )
            return None
        ret = feedparser.parse(data)
        self.bot.dispatch(
            # dispatch is versioned.
            # To remain compatible, accept kwargs and check version
            #
            # version: 1
            # response_regerator: Callable[[], feedparser.FeedParserDict]
            # bozo: Whether this was already a junk response.
            #
            # This may be dispatched any time a feed is fetched,
            # and if you use this, you should compare with prior info
            # The response regeneration exists to remove potential
            # of consumers accidentally breaking the cog by mutating
            # a response which has not been consumed by the cog yet.
            # re-parsing is faster than a deepcopy, and prevents needing it
            # should nothing be using the listener.
            "sinbadcogs_rss_fetch",
            listener_version=1,
            response_regenerator=partial(feedparser.parse, data),
            bozo=ret.bozo,
        )
        if ret.bozo:
            log.debug(f"Feed url: {url} is invalid.")
            return None
        return ret
    @staticmethod
    def process_entry_time(x):
        if "published_parsed" in x:
            return tuple(x.get("published_parsed"))[:5]
        if "updated_parsed" in x:
            return tuple(x.get("updated_parsed"))[:5]
        return (0,)
    async def find_feeds(self, site: str) -> List[str]:
        """
        Attempts to find feeds on a page
        """
        async with self.session.get(site) as response:
            data = await response.read()
        possible_feeds = set()
        html = bs4(data)
        feed_urls = html.findAll("link", rel="alternate")
        if len(feed_urls) > 1:
            for f in feed_urls:
                if t := f.get("type", None):
                    if "rss" in t or "xml" in t:
                        if href := f.get("href", None):
                            possible_feeds.add(href)
        parsed_url = urllib.parse.urlparse(site)
        scheme, hostname = parsed_url.scheme, parsed_url.hostname
        if scheme and hostname:
            base = "://".join((scheme, hostname))
            atags = html.findAll("a")
            for a in atags:
                if href := a.get("href", None):
                    if "xml" in href or "rss" in href or "feed" in href:
                        possible_feeds.add(base + href)
        return [site for site in possible_feeds if await self.fetch_feed(site)]
    async def format_and_send(
        self,
        *,
        destination: discord.TextChannel,
        response: feedparser.FeedParserDict,
        feed_name: str,
        feed_settings: dict,
        embed_default: bool,
        force: bool = False,
    ) -> Optional[List[int]]:
        """
        Formats and sends,
        returns the integer timestamp of latest entry in the feed which was sent
        """
        use_embed = feed_settings.get("embed_override", None)
        if use_embed is None:
            use_embed = embed_default

        assert isinstance(response.entries, list), "mypy"  # nosec

        match_rule = feed_settings.get("match_req", [])

        def meets_rule(entry):
            if not match_rule:
                return True

            field_name, term = match_rule

            d = getattr(entry, field_name, None)
            if not d:
                return False
            elif isinstance(d, list):
                for item in d:
                    if term in item:
                        return True
                return False
            elif isinstance(d, str):
                return term in d.casefold()

            return False

        if force:
            try:
                to_send = [response.entries[0]]
            except IndexError:
            _to_send = next(filter(meets_rule, response.entries), None)
            if not _to_send:
                return None
            to_send = [_to_send]
        else:
            last = feed_settings.get("last", None)
            last = tuple((last or (0,))[:5])

            to_send = sorted(
                [e for e in response.entries if self.process_entry_time(e) > last],
                [
                    e
                    for e in response.entries
                    if self.process_entry_time(e) > last and meets_rule(e)
                ],
                key=self.process_entry_time,
            )

        last_sent = None
        roles = feed_settings.get("role_mentions", [])
        for entry in to_send:
            color = destination.guild.me.color
            roles = feed_settings.get("role_mentions", [])

            kwargs = self.format_post(
                entry, use_embed, color, feed_settings.get("template", None), roles
            )
@@ -677,12 +710,65 @@ async def reset_template(

        await ctx.tick()

    @rss.command(
        name="setmatchreq",
        usage="<feedname> [channel] <field name> <match term>",
        hidden=True,
    )
    async def rss_set_match_req(
        self,
        ctx: commands.GuildContext,
        feed_name: str,
        channel: Optional[discord.TextChannel] = None,
        *,
        field_and_term: FieldAndTerm,
    ):
        """
        Sets a term which must appear in the given field for a feed to be published.
        """

        channel = channel or ctx.channel

        if field_and_term.field not in USABLE_TEXT_FIELDS:
            raise commands.BadArgument(
                f"Field must be one of: {', '.join(USABLE_TEXT_FIELDS)}"
            )

        async with self.config.channel(channel).feeds() as feeds:
            if feed_name not in feeds:
                await ctx.send(f"No feed named {feed_name} in {channel.mention}.")
                return

            feeds[feed_name]["match_req"] = list(field_and_term)
            await ctx.tick()

    @rss.command(name="removematchreq", hidden=True)
    async def feed_remove_match_req(
        self,
        ctx: commands.GuildContext,
        feed_name: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        """
        Remove the reqs on a feed update.
        """

        channel = channel or ctx.channel

        async with self.config.channel(channel).feeds() as feeds:
            if feed_name not in feeds:
                await ctx.send(f"No feed named {feed_name} in {channel.mention}.")
                return

            feeds[feed_name].pop("match_req", None)
            await ctx.tick()

    @checks.admin_or_permissions(manage_guild=True)
    @rss.command(name="rolementions")
    async def feedset_mentions(
        self,
        ctx,
        name,
        ctx: commands.GuildContext,
        name: str,
        channel: Optional[discord.TextChannel] = None,
        *non_everyone_roles: NonEveryoneRole,
    ):
        """
        Sets the roles which are mentioned when this feed updates.
        This will clear the setting if none.
        """
        roles = set(non_everyone_roles)
        if len(roles) > 4:
            return await ctx.send(
                "I'm judging you hard here. "
                "Fix your notification roles, "
                "don't mention this many (exiting without changes)."
            )
        if roles and max(roles) > ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(
                "I'm not letting you set a role mention for a role above your own."
            )
        channel = channel or ctx.channel
        async with self.config.channel(channel).feeds() as feeds:
            if name not in feeds:
                await ctx.send(f"No feed named {name} in {channel.mention}.")
                return
            feeds[name]["role_mentions"] = [r.id for r in roles]
        if roles:
            await ctx.send("I've set those roles to be mentioned.")
        else:
            await ctx.send("Roles won't be mentioned.")
        await ctx.tick()