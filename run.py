import asyncio
import inspect
import os
import queue
import re
import traceback
from threading import Thread
import aiohttp
import gc
import time
import sys
from itertools import cycle
import sentry_sdk
sentry_sdk.init("https://5322a6d1b40841d7a6000e45a3c61a03@sentry.io/1406854")

import discord
from dueutil.permissions import Permission

import generalconfig as gconf
from dueutil import loader, servercounts
from dueutil.game import players, stats, emojis
from dueutil.game.stats import Stat
from dueutil.game.helpers import imagecache
from dueutil.game.configs import dueserverconfig
from dueutil import permissions
from dueutil import util, events, dbconn

MAX_RECOVERY_ATTEMPTS = 1000

stopped = False
bot_key = ""
shard_count = 0
shard_clients = []
shard_names = []

# I'm not sure of the root cause of this error & it only happens once in months.
ERROR_OF_DEATH = "Timeout context manager should be used inside a task"

""" 
DueUtil: The most 1337 (worst) discord bot ever.     
This bot is not well structured...
(c) MacDue & DeveloperAnonymous - All rights reserved
(Sections of this bot are MIT and GPL)
"""

class DueUtilClient(discord.Client):
    """
    DueUtil shard client
    """

    def __init__(self, **details):
        self.shard_id = details["shard_id"]
        self.queue_tasks = queue.Queue()
        self.name = shard_names[self.shard_id]
        self.loaded = False
        self.session = aiohttp.ClientSession()
        self.start_time = time.time()
        super(DueUtilClient, self).__init__(**details)
        asyncio.ensure_future(self.__check_task_queue(), loop=self.loop)

    @asyncio.coroutine
    def __check_task_queue(self):

        while True:
            try:
                task_details = self.queue_tasks.get(False)
                task = task_details["task"]
                args = task_details.get('args', ())
                kwargs = task_details.get('kwargs', {})
                if inspect.iscoroutinefunction(task):
                    yield from task(*args, **kwargs)
                else:
                    task(args, kwargs)
            except queue.Empty:
                pass
            yield from asyncio.sleep(0.1)

    def run_task(self, task, *args, **kwargs):

        """
        Runs a task from within this clients thread
        """
        self.queue_tasks.put({"task": task, "args": args, "kwargs": kwargs})

    @asyncio.coroutine
    def on_server_join(self, server):
        server_count = util.get_server_count()
        if server_count % 100 == 0:
            yield from util.say(gconf.announcement_channel,
                                ":confetti_ball: I'm on __**%d SERVERS**__ now!1!111!\n@everyone" % server_count)

        util.logger.info("Joined server name: %s id: %s", server.name, server.id)
        yield from util.set_up_roles(server)
        server_stats = self.server_stats(server)
        yield from util.duelogger.info(("DueUtil has joined the server **"
                                        + util.ultra_escape_string(server.name) + "**!\n"
                                        + "``Member count →`` " + str(server_stats["member_count"]) + "\n"
                                        + "``Bot members →``" + str(server_stats["bot_count"]) + "\n"
                                        + ("**BOT SERVER**" if server_stats["bot_server"] else "")))

        # Message to help out new server admins.
        for channel in server.channels:
            if channel.type == discord.ChannelType.text:
                try:
                    yield from self.send_message(channel, ":wave: __Thanks for adding me!__\n"
                                     + "If you would like to customize me to fit your "
                                     + "server take a quick look at the admins "
                                     + "guide at <https://dueutil.xyz/howto/#adming>.\n"
                                     + "It shows how to change the command prefix here, and set which "
                                     + "channels I or my commands can be used in (along with a bunch of other stuff).")
                    break
                except discord.Forbidden:
                    continue
        
        # Update stats
        yield from servercounts.update_server_count(self)

    @staticmethod
    def server_stats(server):
        member_count = len(server.members)
        bot_count = sum(member.bot for member in server.members)
        bot_percent = int((bot_count / member_count) * 100)
        bot_server = bot_percent > 70
        return {"member_count": member_count, "bot_percent": bot_percent,
                "bot_count": bot_count, "bot_server": bot_server}

    @asyncio.coroutine
    def on_error(self, event, *args):
        ctx = args[0] if len(args) == 1 else None
        ctx_is_message = isinstance(ctx, discord.Message)
        error = sys.exc_info()[1]
        if ctx is None:
            yield from util.duelogger.error(("**DueUtil experienced an error!**\n"
                                             + "__Stack trace:__ ```" + traceback.format_exc() + "```"))
            util.logger.error("None message/command error: %s", error)
        elif isinstance(error, util.DueUtilException):
            # A normal dueutil user error
            try:
                if error.channel is not None:
                    yield from self.send_message(error.channel, error.get_message())
                else:
                    yield from self.send_message(ctx.channel, error.get_message())
            except:
                util.logger.warning("Unable to send Exception message")
            return
        elif isinstance(error, util.DueReloadException):
            loader.reload_modules()
            yield from util.say(error.channel, loader.get_loaded_modules())
            return
        elif isinstance(error, discord.errors.Forbidden):
            if ctx_is_message:
                channel = ctx.channel
                if isinstance(error, util.SendMessagePermMissing):
                    util.logger.warning("Missing send permissions in channel %s (%s)", channel.name, channel.id)
                else:
                    try:
                        # Attempt to warn user
                        perms = ctx.server.me.permissions_in(ctx.channel)
                        yield from util.say(ctx.channel,
                                            "The action could not be performed as I'm **missing permissions**! Make sure I have the following permissions:\n"
                                            + "- Manage Roles %s;\n" % (":white_check_mark:" if perms.manage_roles else ":x:")
                                            + "- Manage messages %s;\n" % (":white_check_mark:" if perms.manage_messages else ":x:")
                                            + "- Embed links %s;\n" % (":white_check_mark:" if perms.embed_links else ":x:")
                                            + "- Attach files %s;\n" % (":white_check_mark:" if perms.attach_files else ":x:")
                                            + "- Read Message History %s;\n" % (":white_check_mark:" if perms.read_message_history else ":x:")
                                            + "- Use external emojis %s;\n" % (":white_check_mark:" if perms.external_emojis else ":x:")
                                            + "- Add reactions%s" % (":white_check_mark:" if perms.add_reactions else ":x:")
                                            )
                    except util.SendMessagePermMissing:
                        pass  # They've block sending messages too.
                    except discord.errors.Forbidden: 
                        pass
                return
        elif isinstance(error, discord.HTTPException):
            util.logger.error("Discord HTTP error: %s", error)
        elif isinstance(error, aiohttp.errors.ClientResponseError):
            if ctx_is_message:
                util.logger.error("%s: ctx from %s: %s", error, ctx.author.id, ctx.content)
            else:
                util.logger.error(error)
        elif isinstance(error, RuntimeError) and ERROR_OF_DEATH in str(error):
            util.logger.critical("Something went very wrong and the error of death came for us: %s", error)
            os._exit(1)
        elif ctx_is_message:
            yield from self.send_message(ctx.channel, (":bangbang: **Something went wrong...**"))
            trigger_message = discord.Embed(title="Trigger", type="rich", color=gconf.DUE_COLOUR)
            trigger_message.add_field(name="Message", value=ctx.author.mention + ":\n" + ctx.content)
            yield from util.duelogger.error(("**Message/command triggred error!**\n"
                                             + "__Stack trace:__ ```" + traceback.format_exc()[-1500:] + "```"),
                                            embed=trigger_message)
        # Log exception on sentry.
        util.sentry_client.captureException()
        traceback.print_exc()

    
    @asyncio.coroutine
    def on_message(self, message):
        if (message.author == self.user
            or message.author.bot
            or not loaded()
            or message.channel.is_private):
            return
        
        # Live support
        # if message.channel.is_private or (message.server == util.get_server('617912143303671810') and players.find_player(message.channel.name)):
        #     support_server = util.get_server('617912143303671810')
            
        #     # User writes us
        #     if message.channel.is_private:
        #         user_id = message.author.id
        #         user = message.author
                
        #         if message.content.lower().startswith("!requestsupport"):
        #             try:
        #                 if not util.find_channel(user_id):
        #                     yield from self.create_channel(server=support_server, name=user_id, type=0)
        #                     yield from self.add_reaction(message, emojis.CHECK_REACT)
        #                 else:
        #                     yield from self.add_reaction(message, emojis.CROSS_REACT)
                            
        #             except:
        #                 yield from self.add_reaction(message, emojis.CROSS_REACT)
        #             return

        #         elif message.content.lower().startswith("!close"):
        #             embed = discord.Embed(type="rich", colour=gconf.DUE_COLOUR)
        #             embed.add_field(name="Support Closed", value="Thank you for using **DueUtil live support**!\n"
        #                                                         + "*Please note that we delete any archive of our previous messages.*")
        #             embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/363777039813050368/618213084795895823/due3Logo.png")
        #             embed.set_footer(text="If your question was not fully answered or if you still have a question, please answer `!requestsupport` to this message with your question!")
        #             try:
        #                 channel = util.find_channel(user_id)
        #                 if channel:
        #                     msg = yield from util.say(user, "Closing support... Please wait.", client=self)
        #                 yield from self.delete_channel(channel)
        #                 yield from util.edit_message(msg, embed=embed, client=self)
        #                 yield from self.add_reaction(message, emojis.CHECK_REACT)
        #             except:
        #                 yield from self.add_reaction(message, emojis.CROSS_REACT)
        #             return

        #         channel = util.find_channel(user_id)
        #         if channel:
        #             try:
        #                 msg = message.content if message.content else "No content"
        #                 attachments = message.attachments

        #                 embed = discord.Embed(title=(message.author.name + "#" + message.author.discriminator), type="rich", colour=gconf.DUE_COLOUR)
        #                 embed.add_field(name="Message:", value=msg)

        #                 if len(attachments) > 0:
        #                     attachment = attachments[0]
        #                     filename = attachment['filename']
        #                     extension = filename.split('.')
        #                     extension = extension[len(extension) - 1]
        #                     if extension in gconf.SUPPORTED_FILES:
        #                         embed.set_image(url=attachment.get('url'))
        #                     else:
        #                         yield from util.say(user, "Supported files are: " + ', '.join(gconf.SUPPORTED_FILES), client=self)
        #                 yield from self.move_channel(channel, 4)
        #                 yield from util.say(channel, embed=embed)
        #                 yield from self.add_reaction(message, emojis.CHECK_REACT)
        #             except:
        #                 yield from self.add_reaction(message, emojis.CROSS_REACT)
            
        #     # We reply 
        #     elif (message.server == util.get_server('617912143303671810') and players.find_player(message.channel.name)):
        #         user = yield from self.get_user_info(message.channel.name)
        #         channel = message.channel

        #         if message.content.lower().startswith("!close"):
        #             embed = discord.Embed(title="DueUtil live support", type="rich", colour=gconf.DUE_COLOUR)
        #             embed.add_field(name="Support Closed", value="Thank you for using **DueUtil live support**!\n"
        #                                                         + "*Please note that we delete any archive of our previous messages.*")
        #             embed.add_field(name="Closed by:", value=f"{message.author.name}#{message.author.discriminator}")
        #             embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/363777039813050368/618213084795895823/due3Logo.png")
        #             embed.set_footer(text="If your question was not answered, please answer `!requestsupport` to re-open a ticket!")
        #             try:
        #                 yield from self.delete_channel(channel)
        #                 yield from util.say(user, embed=embed, client=self)
        #             except:
        #                 yield from self.add_reaction(message, emojis.CROSS_REACT)
        #             return

        #         try:
        #             msg = message.content if message.content else "No content"
        #             attachments = message.attachments

        #             embed = discord.Embed(title="DueUtil live support", type="rich", colour=gconf.DUE_COLOUR)
        #             embed.add_field(name=f"{message.author.name}#{message.author.discriminator}", value=msg)
        #             embed.set_footer(text="Make sure to report to @DeveloperAnonymous#9830 for any abuse from the live squad!",
        #                             icon_url="https://cdn.discordapp.com/attachments/363777039813050368/618213084795895823/due3Logo.png")

        #             if len(attachments) > 0:
        #                 attachment = attachments[0]
        #                 filename = attachment['filename']
        #                 extension = filename.split('.')
        #                 extension = extension[len(extension) - 1]
        #                 if extension in gconf.SUPPORTED_FILES:
        #                     embed.set_image(url=attachment.get('url'))
        #                 else:
        #                     yield from util.say(channel, "Supported files are: " + ', '.join(gconf.SUPPORTED_FILES))
                        
        #             yield from util.say(user, embed=embed, client=self)
        #             yield from self.add_reaction(message, emojis.CHECK_REACT)
        #         except:
        #             yield from self.add_reaction(message, emojis.CROSS_REACT)
        #     return


        owner = discord.Member(user={"id": config["owner"]})
        if not permissions.has_permission(owner, Permission.DUEUTIL_OWNER):
            permissions.give_permission(owner, Permission.DUEUTIL_OWNER)
        mentions_self_regex = "<@.?"+self.user.id+">"
        if re.match("^"+mentions_self_regex, message.content):
            message.content = re.sub(mentions_self_regex + "\s*",
                                    dueserverconfig.server_cmd_key(message.server),
                                    message.content)
            
        yield from events.on_message_event(message)

    @asyncio.coroutine
    def on_member_update(self, before, after):
        player = players.find_player(before.id)
        if player is not None:
            old_image = player.get_avatar_url(member=before)
            new_image = player.get_avatar_url(member=after)
            if old_image != new_image:
                imagecache.uncache(old_image)
            member = after
            if (member.server.id == gconf.THE_DEN and any(role.id == gconf.DONOR_ROLE_ID for role in member.roles)):
                player.donor = True
                player.save()

    @asyncio.coroutine
    def on_server_remove(self, server):
        for collection in dbconn.db.collection_names():
            if collection != "Player":
                dbconn.db[collection].delete_many({'_id': {'$regex': '%s.*' % server.id}})
                dbconn.db[collection].delete_many({'_id': server.id})
        yield from util.duelogger.info("DueUtil been removed from the server **%s**"
                                       % util.ultra_escape_string(server.name))
        # Update stats
        yield from servercounts.update_server_count(self)

    @asyncio.coroutine
    def change_avatar(self, channel, avatar_name):
        try:
            avatar = open("avatars/" + avatar_name.strip(), "rb")
            avatar_object = avatar.read()
            yield from self.edit_profile(avatar=avatar_object)
            yield from self.send_message(channel, ":white_check_mark: Avatar now **" + avatar_name + "**!")
        except FileNotFoundError:
            yield from self.send_message(channel, ":bangbang: **Avatar change failed!**")

    @asyncio.coroutine
    def on_ready(self):
        shard_number = shard_clients.index(self) + 1
        game = discord.Game(name="dueutil.xyz | shard %d/%d" % (shard_number, shard_count))
        try:
            yield from self.change_presence(game=game, afk=False)
        except Exception as e:
            util.logger.error("Failed to change presence")
        util.logger.info("\nLogged in shard %d as\n%s\nWith account @%s ID:%s \n-------",
                         shard_number, self.name, self.user.name, self.user.id)
        self.loaded = True
        if loaded():
            yield from util.duelogger.bot("DueUtil has *(re)*started\n"
                                          + "Bot version → ``%s``" % gconf.VERSION)


class ShardThread(Thread):
    """
    Thread for a shard client
    """

    def __init__(self, event_loop, shard_number):
        self.event_loop = event_loop
        self.shard_number = shard_number
        super().__init__()

    def run(self, level=1):
        asyncio.set_event_loop(self.event_loop)
        client = DueUtilClient(shard_id=self.shard_number, shard_count=shard_count)
        shard_clients.append(client)
        try:
            asyncio.run_coroutine_threadsafe(client.run(bot_key), client.loop)
        except Exception as client_exception:
            util.logger.exception(client_exception, exc_info=True)
            if level < MAX_RECOVERY_ATTEMPTS:
                util.logger.warning("Bot recovery attempted for shard %d" % self.shard_number)
                shard_clients.remove(client)
                self.event_loop = asyncio.new_event_loop()
                self.run(level + 1)
            else:
                util.logger.critical("FATAL ERROR: Shard down! Recovery failed")
        finally:
            util.logger.critical("Shard is down! Bot needs restarting!")
            # Should restart bot
            os._exit(1)


def run_due():
    start_time = time.time()
    if not os.path.exists("assets/imagecache/"):
        os.makedirs("assets/imagecache/")
    loader.load_modules(packages=loader.GAME)
    if not stopped:
        loader.load_modules(packages=loader.COMMANDS)
        util.logger.info("Modules loaded after %ds", time.time() - start_time)
        shard_time = time.time()
        for shard_number in range(0, shard_count):
            loaded_clients = len(shard_clients)
            shard_thread = ShardThread(asyncio.new_event_loop(), shard_number)
            shard_thread.start()
        
        ### Tasks
        loop = asyncio.get_event_loop()
        from dueutil import tasks
        for task in tasks.tasks:
            asyncio.ensure_future(task(), loop=loop)
        loop.run_forever()
        


def loaded():
    return len(shard_clients) == shard_count and all(client.loaded for client in shard_clients)


if __name__ == "__main__":
    util.logger.info("Starting DueUtil!")
    config = gconf.other_configs
    bot_key = config["botToken"]
    shard_count = config["shardCount"]
    shard_names = config["shardNames"]
    owner = discord.Member(user={"id": config["owner"]})
    if not permissions.has_permission(owner, Permission.DUEUTIL_OWNER):
        permissions.give_permission(owner, Permission.DUEUTIL_OWNER)
    util.load(shard_clients)
    run_due()