import os
import re
import sys
import time
import shlex
import shutil
import inspect
import aiohttp
import discord
import asyncio
import pylast
import traceback
import random
import youtube_dl

from discord import utils
from discord.object import Object
from discord.enums import ChannelType
from discord.voice_client import VoiceClient
from discord.ext.commands.bot import _get_variable

from io import BytesIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta,datetime
from dateutil import relativedelta as REL
from random import choice, shuffle
from collections import defaultdict

from musicbot.playlist import Playlist
from musicbot.player import MusicPlayer
from musicbot.config import Config, ConfigDefaults
from musicbot.permissions import Permissions, PermissionsDefaults
from musicbot.utils import load_file, write_file, sane_round_int
from musicbot.lastfm import Lastfm, UserTrack
from musicbot.chartmaker import ChartMaker
from musicbot.database import LastFmSQLiteDatabase

from . import exceptions
from . import downloader
from .opus_loader import load_opus_lib
from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH


load_opus_lib()

def parse_mb_command(command,message):
    split = message.split('!{} '.format(command))

    params_len = len(split)

    main_param = []
    if params_len == 2:
        main_param = split[1]
        main_param = main_param.split(' ')

    if len(main_param) == 0:
        return None

    if len(main_param) == 1 and main_param[0] == '':
        return None
    return main_param

class WeeklyDay:
    def __init__(self, weekday, hour, minute):
        self.weekday = weekday
        self.hour = hour
        self.minute = minute

class SkipState:
    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Response:
    def __init__(self, content, reply=False, delete_after=0):
        self.content = content
        self.reply = reply
        self.delete_after = delete_after


class MusicBot(discord.Client):
    def __init__(self, config_file=ConfigDefaults.options_file, perms_file=PermissionsDefaults.perms_file):
        self.players = {}
        self.the_voice_clients = {}
        self.locks = defaultdict(asyncio.Lock)
        self.voice_client_connect_lock = asyncio.Lock()
        self.voice_client_move_lock = asyncio.Lock()

        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)
        self.downloader = downloader.Downloader(download_folder='audio_cache')
        self.days = [WeeklyDay(REL.MO, 17, 0),
                    WeeklyDay(REL.WE, 17, 0),
                    WeeklyDay(REL.FR, 17, 0)]

        self.exit_signal = None
        self.init_ok = False
        self.cached_client_id = None

        if not self.autoplaylist:
            print("Warning: Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False

        # TODO: Do these properly
        ssd_defaults = {'last_np_msg': None, 'auto_paused': False}
        self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

        self.lastfm = Lastfm(self.config)

    

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("only the owner can use this command", expire_in=30)

        return wrapper

    @staticmethod
    def _fixg(x, dp=2):
        return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')

    def _get_owner(self, voice=False):
        if voice:
            for server in self.servers:
                for channel in server.channels:
                    for m in channel.voice_members:
                        if m.id == self.config.owner_id:
                            return m
        else:
            return discord.utils.find(lambda m: m.id == self.config.owner_id, self.get_all_members())

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    # TODO: autosummon option to a specific channel
    async def _auto_summon(self):
        owner = self._get_owner(voice=True)
        if owner:
            self.safe_print("Found owner in \"%s\", attempting to join..." % owner.voice_channel.name)
            # TODO: Effort
            await self.cmd_summon(owner.voice_channel, owner, None)
            return owner.voice_channel

    async def _autojoin_channels(self, channels):
        joined_servers = []

        for channel in channels:
            if channel.server in joined_servers:
                print("Already joined a channel in %s, skipping" % channel.server.name)
                continue

            if channel and channel.type == discord.ChannelType.voice:
                self.safe_print("Attempting to autojoin %s in %s" % (channel.name, channel.server.name))

                chperms = channel.permissions_for(channel.server.me)

                if not chperms.connect:
                    self.safe_print("Cannot join channel \"%s\", no permission." % channel.name)
                    continue

                elif not chperms.speak:
                    self.safe_print("Will not join channel \"%s\", no permission to speak." % channel.name)
                    continue

                try:
                    player = await self.get_player(channel, create=True)

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist:
                        await self.on_player_finished_playing(player)

                    joined_servers.append(channel.server)
                except Exception as e:
                    if self.config.debug_mode:
                        traceback.print_exc()
                    print("Failed to join", channel.name)

            elif channel:
                print("Not joining %s on %s, that's a text channel." % (channel.name, channel.server.name))

            else:
                print("Invalid channel thing: " + channel)

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def generate_invite_link(self, *, permissions=None, server=None):
        if not self.cached_client_id:
            appinfo = await self.application_info()
            self.cached_client_id = appinfo.id

        return discord.utils.oauth_url(self.cached_client_id, permissions=permissions, server=server)

    async def get_voice_client(self, channel):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        with await self.voice_client_connect_lock:
            server = channel.server
            if server.id in self.the_voice_clients:
                return self.the_voice_clients[server.id]

            s_id = self.ws.wait_for('VOICE_STATE_UPDATE', lambda d: d.get('user_id') == self.user.id)
            _voice_data = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

            await self.ws.voice_state(server.id, channel.id)

            s_id_data = await asyncio.wait_for(s_id, timeout=10, loop=self.loop)
            voice_data = await asyncio.wait_for(_voice_data, timeout=10, loop=self.loop)
            session_id = s_id_data.get('session_id')

            kwargs = {
                'user': self.user,
                'channel': channel,
                'data': voice_data,
                'loop': self.loop,
                'session_id': session_id,
                'main_ws': self.ws
            }
            voice_client = VoiceClient(**kwargs)
            self.the_voice_clients[server.id] = voice_client

            retries = 3
            for x in range(retries):
                try:
                    print("Attempting connection...")
                    await asyncio.wait_for(voice_client.connect(), timeout=10, loop=self.loop)
                    print("Connection established.")
                    break
                except:
                    traceback.print_exc()
                    print("Failed to connect, retrying (%s/%s)..." % (x+1, retries))
                    await asyncio.sleep(1)
                    await self.ws.voice_state(server.id, None, self_mute=True)
                    await asyncio.sleep(1)

                    if x == retries-1:
                        raise exceptions.HelpfulError(
                            "Cannot establish connection to voice chat.  "
                            "Something may be blocking outgoing UDP connections.",

                            "This may be an issue with a firewall blocking UDP.  "
                            "Figure out what is blocking UDP and disable it.  "
                            "It's most likely a system firewall or overbearing anti-virus firewall.  "
                        )

            return voice_client

    async def mute_voice_client(self, channel, mute):
        await self._update_voice_state(channel, mute=mute)

    async def deafen_voice_client(self, channel, deaf):
        await self._update_voice_state(channel, deaf=deaf)

    async def move_voice_client(self, channel):
        await self._update_voice_state(channel)

    async def reconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        vc = self.the_voice_clients.pop(server.id)
        _paused = False

        player = None
        if server.id in self.players:
            player = self.players[server.id]
            if player.is_playing:
                player.pause()
                _paused = True

        try:
            await vc.disconnect()
        except:
            print("Error disconnecting during reconnect")
            traceback.print_exc()

        await asyncio.sleep(0.1)

        if player:
            new_vc = await self.get_voice_client(vc.channel)
            player.reload_voice(new_vc)

            if player.is_paused and _paused:
                player.resume()

    async def disconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await self.the_voice_clients.pop(server.id).disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in self.the_voice_clients.copy().values():
            await self.disconnect_voice_client(vc.channel.server)

    async def _update_voice_state(self, channel, *, mute=False, deaf=False):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        # I'm not sure if this lock is actually needed
        with await self.voice_client_move_lock:
            server = channel.server

            payload = {
                'op': 4,
                'd': {
                    'guild_id': server.id,
                    'channel_id': channel.id,
                    'self_mute': mute,
                    'self_deaf': deaf
                }
            }

            await self.ws.send(utils.to_json(payload))
            self.the_voice_clients[server.id].channel = channel

    async def get_player(self, channel, create=False) -> MusicPlayer:
        server = channel.server

        if server.id not in self.players:
            if not create:
                raise exceptions.CommandError(
                    'The bot is not in a voice channel.  '
                    'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

            voice_client = await self.get_voice_client(channel)

            playlist = Playlist(self)
            player = MusicPlayer(self, voice_client, playlist) \
                .on('play', self.on_player_play) \
                .on('resume', self.on_player_resume) \
                .on('pause', self.on_player_pause) \
                .on('stop', self.on_player_stop) \
                .on('finished-playing', self.on_player_finished_playing) \
                .on('entry-added', self.on_player_entry_added)

            player.skip_state = SkipState()
            self.players[server.id] = player

        return self.players[server.id]

    async def on_player_play(self, player, entry):
        await self.update_now_playing(entry)
        player.skip_state.reset()

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            if self.config.now_playing_mentions:
                newmsg = '%s - your song **%s** is now playing in %s!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Now playing in %s: **%s**' % (
                    player.voice_client.channel.name, entry.title)

            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)
            

    async def on_player_resume(self, entry, **_):
        await self.update_now_playing(entry)

    async def on_player_pause(self, entry, **_):
        await self.update_now_playing(entry, True)

    async def on_player_stop(self, **_):
        await self.update_now_playing()

    async def on_player_finished_playing(self, player, **_):
        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            while self.autoplaylist:
                song_url = choice(self.autoplaylist)
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)

                if not info:
                    self.autoplaylist.remove(song_url)
                    self.safe_print("[Info] Removing unplayable song from autoplaylist: %s" % song_url)
                    write_file(self.config.auto_playlist_file, self.autoplaylist)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    pass  # Wooo playlist
                    # Blarg how do I want to do this

                # TODO: better checks here
                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    print("Error adding song from autoplaylist:", e)
                    continue

                break

            if not self.autoplaylist:
                print("[Warning] No playable songs in the autoplaylist, disabling.")
                self.config.auto_playlist = False

    async def on_player_entry_added(self, playlist, entry, **_):
        pass

    async def update_now_playing(self, entry=None, is_paused=False):
        game = None

        if self.user.bot:
            activeplayers = sum(1 for p in self.players.values() if p.is_playing)
            if activeplayers > 1:
                game = discord.Game(name="music on %s servers" % activeplayers)
                entry = None

            elif activeplayers == 1:
                player = discord.utils.get(self.players.values(), is_playing=True)
                entry = player.current_entry

        if entry:
            prefix = u'\u275A\u275A ' if is_paused else ''

            name = u'{}{}'.format(prefix, entry.title)[:128]
            game = discord.Game(name=name)

        await self.change_status(game)


    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Warning: Cannot send message to %s, no permission" % dest.name)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot send message to %s, invalid channel?" % dest.name)

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Warning: Cannot delete message \"%s\", no permission" % message.clean_content)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot delete message \"%s\", message not found" % message.clean_content)

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot edit message \"%s\", message not found" % message.clean_content)
            if send_if_fail:
                if not quiet:
                    print("Sending instead")
                return await self.safe_send_message(message.channel, new)

    def safe_print(self, content, *, end='\n', flush=True):
        sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
        if flush: sys.stdout.flush()

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            if self.config.debug_mode:
                print("Could not send typing to %s, no permssion" % destination)

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password,**fields)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: # Can be ignored
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: # Can be ignored
            pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your Email or Password or Token in the options file.  "
                "Remember that each field should be on their own line.")

        finally:
            try:
                self._cleanup()
            except Exception as e:
                print("Error in cleanup:", e)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            print("Exception in", event)
            print(ex.message)

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            traceback.print_exc()

    async def on_resumed(self):
        for vc in self.the_voice_clients.values():
            vc.main_ws = self.ws

    async def on_ready(self):
        print('\rConnected!  Musicbot v%s\n' % BOTVERSION)

        if self.config.owner_id == self.user.id:
            raise exceptions.HelpfulError(
                "Your OwnerID is incorrect or you've used the wrong credentials.",

                "The bot needs its own account to function.  "
                "The OwnerID is the id of the owner, not the bot.  "
                "Figure out which one is which and use the correct information.")

        self.init_ok = True

        self.safe_print("Bot:   %s/%s#%s" % (self.user.id, self.user.name, self.user.discriminator))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            self.safe_print("Owner: %s/%s#%s\n" % (owner.id, owner.name, owner.discriminator))

            print('Server List:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        elif self.servers:
            print("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

            print('Server List:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        else:
            print("Owner unknown, bot is not on any servers.")
            if self.user.bot:
                print("\nTo make the bot join a server, paste this link in your browser.")
                print("Note: You should be logged into your main account and have \n"
                      "manage server permissions on the server you want the bot to join.\n")
                print("    " + await self.generate_invite_link())

        print()

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)
            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            print("Bound to text channels:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nNot binding to voice channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print()

        else:
            print("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)
            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            print("Autojoining voice chanels:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nCannot join text channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            print("Not autojoining any voice channels")
            autojoin_channels = set()

        print()
        print("Options:")

        self.safe_print("  Command prefix: " + self.config.command_prefix)
        print("  Default volume: %s%%" % int(self.config.default_volume * 100))
        print("  Skip threshold: %s votes or %s%%" % (
            self.config.skips_required, self._fixg(self.config.skip_ratio_required * 100)))
        print("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        print("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
        print("  Auto-Playlist: " + ['Disabled', 'Enabled'][self.config.auto_playlist])
        print("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
        print("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            print("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
        print("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
        print("  Downloaded songs will be %s" % ['deleted', 'saved'][self.config.save_videos])
        print()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                print("Deleting old audio cache")
            else:
                print("Could not delete old audio cache, moving on.")

        if self.config.autojoin_channels:
            await self._autojoin_channels(autojoin_channels)

        elif self.config.auto_summon:
            print("Attempting to autosummon...", flush=True)

            # waitfor + get value
            owner_vc = await self._auto_summon()

            if owner_vc:
                print("Done!", flush=True)  # TODO: Change this to "Joined server/channel"
                if self.config.auto_playlist:
                    print("Starting auto-playlist")
                    await self.on_player_finished_playing(await self.get_player(owner_vc))
            else:
                print("Owner not found in a voice channel, could not autosummon.")

        print()
        # t-t-th-th-that's all folks!


    async def chart_done_callback(self,file,channel,generatingMessageProc):
        with open(file, 'rb') as f:
            await self.send_file(channel, file)
            await self.delete_message(generatingMessageProc)

    async def chart_error_callback(self,error_message,channel,generatingMessageProc):
        await self.delete_message(generatingMessageProc)
        await self.safe_send_message(channel,error_message)
        
    def lastfm_user_from_mb_command(self,mb):
        if mb['has_lastfm_user']:
            return mb['lastfm_user']
        else:
            return None
    
    def discord_user_from_mb_command(self,mb):
        if mb['has_discord_user']:
            return mb['discord_user']
        else:
            return None
    
    def check_user_if_exists(self,user,users):
        try:
            exists = False
            for u in users:
                if u['discord_user'].id == user['discord_user'].id:
                    exists = True
            return exists
        except:
            return False

    # !!!! Metal Music Discord server stuff starts here  !!!! #
    def handle_mb_command(self,message,mentions_param,command):
        additional_params = list()
        # First, check if there are any mentions
        mentioned_users = list()
        if mentions_param:
            for user in mentions_param.copy():
                mentioned_users.append(user)

        # If there are mentioned user(s), assume that every other user in the message will also be mentioned,i.e., you cant do !nowplaying arkenther @arkenthera at the same time.
        users = list()
        if len(mentioned_users) > 0:
            for mention_user in mentioned_users:
                try:
                    lastfm_user = self.lastfm.db.get_lastfm_user(mention_user.id)
                    users.append( {'lastfm_user': lastfm_user, 'discord_user': mention_user, 'has_discord_user': True,'has_lastfm_user': True } )
                except:
                    users.append( { 'discord_user': mention_user, 'has_discord_user': True,'has_lastfm_user': False } )

            # Strip away the command and the mentions
            content = message.content
            content = content.replace("!{} ".format(command),"")

            content = re.sub(r"^<@.+?>","",content).strip()
            additional_params = content.split()
        else:
            # No mentioned users, assume we got Last.fm users
            users_in_content = parse_mb_command(command,message.content)

            # When the message has no user input : !nowplaying
            if users_in_content == None:
                try:
                    lastfm_user = self.lastfm.db.get_lastfm_user(message.author.id)
                    users.append( { 'lastfm_user': lastfm_user, 'discord_user': message.author, 'has_discord_user': True, 'has_lastfm_user': True } )
                except:
                    users.append( { 'discord_user': message.author, 'has_discord_user': True, 'has_lastfm_user': False } )
            else:
                # The message has at least one user: !nowplaying arkenthera
                # Check each user in our database
                # And discord member info
                # Also check our db to see if there is a registered user name and add it
                lastfm_users = list()
                try:
                    lastfm_users = self.lastfm.db.get_lastfm_users()
                except:
                    print("There is a problem with retrieving users.")
                
                for user_in_content in users_in_content:
                    found_on_db = False
                    for user in lastfm_users:
                        discord_id = user[0]
                        lastfm_user = user[1]

                        if lastfm_user == user_in_content:
                            discord_user = message.channel.server.get_member(str(discord_id))
                            if discord_user != None:
                                users.append( { 'lastfm_user': lastfm_user, 'discord_user': discord_user, 'has_discord_user': True, 'has_lastfm_user': True } )
                            else:
                                users.append( { 'lastfm_user': lastfm_user, 'has_discord_user': False, 'has_lastfm_user': True } )
                            found_on_db = True
                
                    if found_on_db == False:
                        users.append( { 'lastfm_user': user_in_content, 'has_discord_user': False, 'has_lastfm_user': True } )

                for user in lastfm_users:
                    discord_id = user[0]
                    lastfm_user = user[1]

                    discord_user = message.channel.server.get_member(str(discord_id))

                    if str(discord_id) == message.author.id:
                        user_to_append = None
                        if discord_user != None:
                            user_to_append = { 'lastfm_user': lastfm_user, 'discord_user': discord_user, 'has_discord_user': True, 'has_lastfm_user': True }
                        else:
                            user_to_append = { 'lastfm_user': lastfm_user, 'has_discord_user': False, 'has_lastfm_user': True }

                        if not self.check_user_if_exists(user_to_append,users):
                            users.append(user_to_append)
        return users,additional_params


    async def cmd_wdcountdown(self):
        """
        Usage:
            {command_prefix}wdcountdown

        Tells you when the next weekly pick is drawn.
        """
        try:
            now = datetime.now()

                    # self.days = [WeeklyDay(REL.MO, 18, 0),
                    # WeeklyDay(REL.WE, 18, 0),
                    # WeeklyDay(REL.FR, 18, 0)]

            dates = []
            for x in self.days:
                dateadd = now+REL.relativedelta(weekday=x.weekday,hour=x.hour,minute=x.minute,second=0)
                if now>dateadd: dateadd = dateadd+REL.relativedelta(weeks=1)
                
                dates.append(dateadd)
            
            print(dates)
            nextweekly = min([x - now for x in dates])
            return Response(str.format('Time until next weekly pick: {} days, {} hours, {} minutes', nextweekly.days, nextweekly.seconds//3600, (nextweekly.seconds//60)%60), reply=True)
        except Exception as error:
            print(error)
            return Response("There was a problem getting the date for next weekly.",reply=True)
    
    async def cmd_chart(self,message,user_mentions=None):
        """
        Usage:
            {command_prefix}chart [username] [type] [size]

        Returns Last.fm chart by using album info then creates an image.If you dont specify a user name, it will look for one in database.
        Valid types: 7day 1month 3month 6month 12month overall
        Valid sizes: 3x3 4x4 5x5

        Default options are type: overall, size: 5x5.
        """        

        valid_types = ["7day","1month","3month","6month","12month","overall"]
        valid_sizes = ["3x3","4x4","5x5"]

        # Parsing!
        split = message.content.split('!chart ')
        #print(split)

        first_element_is_username = False
        first_element_is_type = False
        first_element_is_size = False
        
        t = None
        s = None
        u = None

        if len(user_mentions) != 0:
            users,additional_params = self.handle_mb_command(message,user_mentions,"chart")
            u = users[0]["lastfm_user"]

            if len(additional_params) == 1:
                t = additional_params[0]
            
            if len(additional_params) == 2:
                t = additional_params[0]
                s = additional_params[1]
        else:

            if len(split) != 1:
                info = split[1].split(" ")
                #print(info)

                first_element = info[0]

                for t in valid_types:
                    if t == first_element:
                        first_element_is_type = True

                for s in valid_sizes:
                    if s == first_element:
                        first_element_is_size = True

                if not first_element_is_size and not first_element_is_type:
                    first_element_is_username = True

                #print("First Element is username: {} First Element is type: {} First Element is size: {}".format(first_element_is_username,first_element_is_type,first_element_is_size))
                if first_element_is_username:
                    u = info[0]
                
                if first_element_is_size:
                    s = info[0]

                if first_element_is_type:
                    t = info[0]

                if len(info) == 2 and first_element_is_username:
                    t = info[1]
                
                if len(info) == 3 and first_element_is_username:
                    t = info[1]
                    s = info[2]

                if len(info) == 2 and not first_element_is_username:
                    s = info[1]
            
            if t == None:
                t = "overall"

            if s == None:
                s = "5x5"
            print("User Name: {} Type: {} Size: {}".format(u,t,s))
            

            if s not in valid_sizes:
                valid_sizes_str = ""
                for s in valid_sizes:
                    valid_sizes_str += s + " "
                return Response("You inputted an invalid size <:DD:260520559383805952>. Here are the valid sizes: `{}`".format(valid_sizes_str))

            if t not in valid_types:
                valid_types_str = ""
                for tt in valid_types:
                    valid_types_str += tt + " "
                
                return Response("You inputted an invalid type <:DD:260520559383805952>. Here are the valid types: `{}`".format(valid_types_str))
        

        if t == None:
            t = "overall"

        if s == None:
            s = "5x5"

        if u == None:
            try:
                u = self.lastfm.db.get_lastfm_user(message.author.id)
            except:
                return Response("User could not be found. Try following up the command with your user name.",reply=True,delete_after=60)    

        # At this point if user is None, something is wrong
        if u == None:
            return Response("User could not be found. Try following up the command with your user name.",reply=True,delete_after=60)
    
        if s == "3x3":
            s = 3

        if s == "4x4":
            s = 4

        if s == "5x5":
            s = 5
        
        generatingMessageProc = await self.safe_send_message(message.channel,
                                        '<:watch:277842021119688705> Generating chart... Parameters: **{}** *{}* *{}* <:watch:277842021119688705>'.format(u,t,s))

        cm = ChartMaker(self.chart_done_callback,message.channel,self.lastfm,u,s,t,generatingMessageProc,self.chart_error_callback)
        await cm.start()
    
    async def cmd_bandinfo(self,message,bandName=None):
        """
        Usage:
            {command_prefix}bandinfo [artist_name]

        Returns an overview of the artist.
        """
        split = message.content.split('!bandinfo ')
        try:
            bandName = split[1]
            if bandName != None:
                artistInfo = self.lastfm.get_artist_info(bandName)
                return Response(artistInfo)
        except:
            return Response("There was an error retrieving the band info. Sorry!",delete_after=30)

    # !!! This command needs handling of database users !!!
    async def cmd_band(self,message,bandName=None):
        """
        Usage:
            {command_prefix}band [LASTFM_USER] [ARTIST]

        Returns the scrobble count of the artist by user.
        """        
        split = message.content.split('!band ')
        try:
            info = split[1].split(" ")
            userName = info[0]
            bandName = split[1].split(userName + " ")[1]
            

            if userName == None or len(userName) == 0 or bandName == None or len(bandName) == 0:
                return Response("There was an error with your command. Sorry!",delete_after=30)

            artistInfo = self.lastfm.get_user_artist_info(userName,bandName)
            return Response(artistInfo)
        except Exception as e:
            print(e)
            return Response("There was an error retrieving the band info. Sorry!",delete_after=30)
    
    async def cmd_lastfm(self,message,user_mentions=None):
        """
        Usage:
            {command_prefix}lastfm [username]

        Returns Last.fm summary of the user.
        """

        if message.content == "!lastfm *":
            markdown = self.lastfm.db.list_users()
            return Response(markdown)
        
        users,additional_params = self.handle_mb_command(message,user_mentions,'lastfm')

        markdown = ""
        Ok = False
        if len(user_mentions) == 0:
            username = self.lastfm_user_from_mb_command(users[0])
            if username == "*":
                markdown = self.lastfm.db.list_users()
                return Response(markdown)
            else:
                if username != None:
                    markdown = self.lastfm.get_user_summary(username)
                    Ok = True
        else:
            if len(users) > 1:
                Ok = False
                markdown = "This command doesnt support more than 1 users."
            if len(users) == 1:
                try:
                    username = self.lastfm_user_from_mb_command(users[0])
                    if username != None:
                        markdown = self.lastfm.get_user_summary(username)
                        Ok = True
                    else:
                        markdown = "User could not be found. Try following up the command with your user name."
                except:
                    markdown = "There was a problem retrieving Last.fm summary."

            if len(users) == 0:
                markdown = "User could not be found. Try following up the command with your user name."
        
        if Ok:
            return Response(markdown)
        else:
            return Response(markdown,delete_after=30,reply=True)

    async def cmd_leaderboards(self,message):
        """
        Usage:
            {command_prefix}leaderboards

        Returns top 10 scrobblers of users registered using !setlastfm.
        """        
        lastfm_users = list()
        try:
            lastfm_users = self.lastfm.db.get_lastfm_users()
        except:
            return Response("There was a problem retrieving all registered users!")

        generatingMessageProc = await self.safe_send_message(message.channel,
                                        '<:watch:277842021119688705> Preparing leaderboards... *PS: It takes long*<:watch:277842021119688705>')
        
        markdown = "```Scrobble Leaderboard for this week\n\n"
        scrobble_counts = list()
        max_counter = 0
        for luser in lastfm_users:
            discord_uid = luser[0]
            lastfm_username = luser[1]

            if max_counter > 10:
                break

            scrobble_count = self.lastfm.get_weekly_scrobble_count(lastfm_username)
            scrobble_counts.append({ 'discord_uid':discord_uid, 'lastfm_username':lastfm_username,'scrobble_count': scrobble_count })
            max_counter = max_counter + 1

        sorted_scrobbles = sorted(scrobble_counts, key=lambda k: k['scrobble_count'],reverse=True)
        
        for luser in sorted_scrobbles:
            discord_uid = luser['discord_uid']
            lastfm_username = luser['lastfm_username']
            discord_member_displayname = lastfm_username

            scrobble_count = luser['scrobble_count']

            if scrobble_count != 0:
                markdown += "{} ({}) - {} scrobbles.\n".format(discord_member_displayname,lastfm_username, scrobble_count)

        markdown += "```"

        await self.delete_message(generatingMessageProc)

        return Response(markdown)

    async def cmd_recent(self,message,user_mentions=None):
        """
        Usage:
            {command_prefix}recent [username]

        Returns Last.fm recent tracks of user.
        """

        users,additional_params = self.handle_mb_command(message,user_mentions,"recent")

        
        if len(users) == 0:
            return Response("There was a problem retrieving users..")
        else:
            mb_user = users[0]

            if len(additional_params) == 0:
                if len(users) != 1:
                    index = users[1]["lastfm_user"]
                else:
                    index = 0
            else:
                index = additional_params[0]
            try:
                index = int(index)
            except:
                index = 0
                return Response("Invalid index.")

            lastfm_user = self.lastfm_user_from_mb_command(mb_user)

            if lastfm_user == None:
                return Response("No Last.fm user found for this user.")
            else:
                try:
                    markdown = self.lastfm.get_recent_tracks(lastfm_user,index)
                    return Response(markdown)
                except Exception as error:
                    print(error)
                    return Response("There was a problem retrieving Last.fm recent tracks.",reply=True,delete_after=30)
    

    async def cmd_taste(self,message,user_mentions=None):
        """
        Usage:
            {command_prefix}taste [lastfmuser1] [lastfmuser2]

        Compares two users. User1 is omitable if you linked your Last.fm account using !setlastfm.
        """

        users,additional_params = self.handle_mb_command(message,user_mentions,'taste')
        response_text = ""
        procmsg = None

        if len(users) < 1:
            return Response("There are not enough users to compare.", reply=True, delete_after=30)

        if len(users) == 1:
            mb_user = users[0]
            if mb_user["discord_user"].id == message.author.id:
                return Response("You're the perfect match for yourself! <:FeelsAmazingMan:300685508604985344>")
            else:
                try:
                    users.append({ 'has_discord_user': True, 'has_lastfm_user': True, 'discord_user':message.author, 'lastfm_user': self.lastfm.db.get_lastfm_user(message.author.id)})
                except:
                    response_text = "There was a problem with the command."

        Ok = False

        # Take first two users
        discord_userA = self.discord_user_from_mb_command(users[0])
        lastfm_userA = self.lastfm_user_from_mb_command(users[0])

        discord_userB = self.discord_user_from_mb_command(users[1])
        lastfm_userB = self.lastfm_user_from_mb_command(users[1])

        user1 = lastfm_userA
        user2 = lastfm_userB

        if lastfm_userA == None or lastfm_userB == None:
            response_text = "There was a problem with the command.Could not find Last.fm users."
        else:
            try:
                procmsg = await self.safe_send_message(message.channel,
                                                       "<:cake:300221990080610305> *Working...* <:cake:300221990080610305>")

                # User 1
                user_display_nameA = ""
                if lastfm_userA != None:
                    user_display_nameA = lastfm_userA

                if discord_userA != None:
                    user_display_nameA = discord_userA.display_name

                user_display_nameB = ""
                if lastfm_userB != None:
                    user_display_nameB = lastfm_userB

                if discord_userB != None:
                    user_display_nameB = discord_userB.display_name

                taste_result = self.lastfm.taste(user1, user2)
                text = ""

                if taste_result['playCountUser1'] == 0 and taste_result[
                    'playCountUser2'] == 0:
                    text = "**{}** and **{}** haven't listened to any music yet. <:FeelsMetalHead:279991636144947200>".format(
                        user_display_nameA, user_display_nameB)

                if taste_result['playCountUser1'] == 0:
                    text = "**{}** hasn't listened to any music yet. <:FeelsMetalHead:279991636144947200>".format(
                        user_display_nameA)

                if taste_result['playCountUser2'] == 0:
                    text = "**{}** hasn't listened to any music yet. <:FeelsMetalHead:279991636144947200>".format(
                        user_display_nameB)

                common_artist_len = len(taste_result['common_artists'])

                if common_artist_len == 0:
                    text = "**{}** and **{}** don't listen to the same music.".format(
                        user_display_nameA, user_display_nameB)

                if common_artist_len == 3:
                    artistName1 = taste_result['common_artists'][0][
                        'artist'].item.name
                    artistName2 = taste_result['common_artists'][1][
                        'artist'].item.name
                    artistName3 = taste_result['common_artists'][2][
                        'artist'].item.name

                    text = "**{}** and **{}** both listen to **{}**, **{}** and **{}**.".format(
                        user_display_nameA, user_display_nameB, artistName1,
                        artistName2, artistName3)

                if common_artist_len == 2:
                    artistName1 = taste_result['common_artists'][0][
                        'artist'].item.name
                    artistName2 = taste_result['common_artists'][1][
                        'artist'].item.name

                    text = "**{}** and **{}** both listen to **{}** and **{}**.".format(
                        user_display_nameA, user_display_nameB, artistName1,
                        artistName2)

                if common_artist_len == 1:
                    artistName1 = taste_result['common_artists'][0][
                        'artist'].item.name
                    text = "**{}** and **{}** both listen to **{}**.".format(
                        user_display_nameA, user_display_nameB, artistName1)

                if common_artist_len > 3:
                    artistName1 = taste_result['common_artists'][0][
                        'artist'].item.name
                    artistName2 = taste_result['common_artists'][1][
                        'artist'].item.name
                    artistName3 = taste_result['common_artists'][2][
                        'artist'].item.name

                    text = "**{}** and **{}** both listen to **{}**, **{}**, **{}** and {} other".format(
                        user_display_nameA, user_display_nameB, artistName1,
                        artistName2, artistName3, (common_artist_len - 3))

                    if common_artist_len - 3 == 1:
                        text += " artist."
                    else:
                        text += " artists."
                response_text = text
                Ok = True
            except:
                pass
        
        if Ok:
            await self.delete_message(procmsg)
            return Response(response_text)
        else:
            return Response(response_text,reply=True,delete_after=30)




    async def cmd_setlastfm(self,message,username):
        """
        Usage:
            {command_prefix}setlastfm [username]

        Saves your last.fm user name so you can omit it in the future.
        """
        try:
            if self.lastfm.db.user_exists(message.author.id) == True:
                self.lastfm.db.update(message.author.id,username)
            else:
                self.lastfm.db.insert(message.author.id,username)

            return Response("Success! Next time you use any Last.fm command, you can omit your user name!",reply=True)
        except Exception as error:
            print(error)
            return Response("There was a problem saving your Last.fm information!",reply=True)


    async def cmd_nowplaying(self,message,user_mentions=None):
        """
        Usage:
            {command_prefix}nowplaying [username]

        Returns Last.fm 'currently scrobbling' song.
        """

        users,additional_params = self.handle_mb_command(message,user_mentions,'nowplaying')
        
        response_text = ""
        
        no_user_found = True

        
        if len(users) == 2 and users[0]["lastfm_user"] == "*":
            try:
                lastfm_users = self.lastfm.db.get_lastfm_users()
            except:
                return Response("There was a problem retrieving all registered users!")

            no_user_found = False

            listeners = list()

            for user in lastfm_users:
                lastfm_username = user[1]
                track = self.lastfm.get_now_playing(lastfm_username)

                if track != None:
                    discord_id = user[0]
                    discord_member = message.channel.server.get_member(str(discord_id))
                    if discord_member == None:
                        continue
                    display_name = discord_member.display_name

                    listeners.append(UserTrack(display_name, track.artist.name, track.title))

            if len(listeners) == 0:
                return Response("No one is listening to any music.")

            if len(listeners) == 1:
                response_text = "1 member is playing music:\n"
            else:
                response_text = "{} members are playing music:\n".format(len(listeners))

            for user_track in listeners:
                response_text += ":musical_note: {}.\n".format(self.lastfm.get_user_listening_text(user_track))
        else:
            user = users[0]
            discord_user = self.discord_user_from_mb_command(user)
            lastfm_user = self.lastfm_user_from_mb_command(user)

            no_user_found = False

            user_display_name = ""
            if lastfm_user != None:
                user_display_name = lastfm_user

            if discord_user != None:
                user_display_name = discord_user.display_name

            if lastfm_user == None:
                no_user_found = True

            user_track = self.lastfm.get_now_playing(lastfm_user)

            if user_track == None:
                response_text += "**{}** is not listening to any music. <:FeelsMetalHead:279991636144947200> \n".format(user_display_name)
            else:
                response_text += ":musical_note: {}.\n".format(self.lastfm.get_user_listening_text(UserTrack(user_display_name, user_track.artist.name, user_track.title)))
        
        
        if no_user_found:
            return Response("User could not be found. Try following up the command with your user name. Check out `!setlastfm`.",reply=True,delete_after=60)
        else:
            return Response(response_text)
    
    async def cmd_wdprivate(self,server,message):
        weekly_members = list()
        for member in server.members:
            roles = member.roles
            
            for r in roles:
                print(r)
                print(str(r))
                if str(r) == "Weekly":
                    weekly_members.append(member)

        for wmember in weekly_members:
            self.lastfm.db.insert_into_wd(wmember.id,0,0,None)
    
    async def cmd_wd(self,message,user_mentions=None):
        users,additional_params = self.handle_mb_command(message,user_mentions,'wd')

        weekly_users = self.lastfm.db.get_weekly_discussion_users()
        nonexcluded = list()
        last_winner = None
        for weekly_user in weekly_users:
            if weekly_user["last_winner"] == 1:
                last_winner = weekly_user
        
        if last_winner != None:
            last_winner_user = await self.get_user_info(last_winner["discord_uid"])
            last_winner_displayname = last_winner_user.display_name
        else:
            last_winner_displayname = None
        
        response_text = ""
        if len(user_mentions) == 0:
            if last_winner != None:
                # Print out the current
                yt_link = last_winner['yt_link']
                if yt_link == None:
                    return Response("This week's album has not yet been selected.")
                video = self.get_video_from_yt_link(last_winner['yt_link'])
                if video == None:
                    return Response("There was a problem retrieving the video!")

                response_text = "Current album is from: **{}** Title: **{}** Link: {}".format(last_winner_displayname,video['title'],last_winner['yt_link'])
            else:
                response_text = "No winner yet!"
        else:
            weekly_users = self.lastfm.db.get_weekly_discussion_users()
            response_text = "This user is not in the weekly list!"
            for weekly_user in weekly_users:
                wd_user_discord = await self.get_user_info(weekly_user["discord_uid"])
                if wd_user_discord == None:
                    return Response("There was a problem retrieving this user.")
                else:
                    if wd_user_discord.id == users[0]["discord_user"].id:
                        wd_user_yt_link = weekly_user["yt_link"]
                        if wd_user_yt_link == None:
                            response_text = "<@{}> has not shared an album yet!".format(wd_user_discord.id)
                        else:
                            video = self.get_video_from_yt_link(wd_user_yt_link)
                            response_text = "<@{}>'s album was {} {}".format(wd_user_discord.id,video["title"],wd_user_yt_link)

        return Response(response_text)

    def get_video_from_yt_link(self,url):
        ydl = youtube_dl.YoutubeDL({'outtmpl': '%(id)s%(ext)s'})
        video = None
        try:
            with ydl:
                result = ydl.extract_info(
                    url,
                    download=False # We just want to extract the info
                )
            

            if 'entries' in result:
                video = result['entries'][0]
            else:
                video = result
        except:
            return None
        return video
        

    async def cmd_wdset(self,message,user_mentions=None):
        """
        Usage:
            {command_prefix}wdset @user yt_link

        Set this weeks youtube link and user.Only usable by admins and mods.
        """

        users,additional_params = self.handle_mb_command(message,user_mentions,"wdset")

        ydl = youtube_dl.YoutubeDL({'outtmpl': '%(id)s%(ext)s'})

        if len(additional_params) != 1:
            return Response("There is a problem with the command. No video link found.")

        input_vid = additional_params[0]
        video = None
        try:
            with ydl:
                result = ydl.extract_info(
                    input_vid,
                    download=False # We just want to extract the info
                )
            

            if 'entries' in result:
                video = result['entries'][0]
            else:
                video = result
        except:
            return Response("There was a problem retrieving data from the youtube link!")

        if video == None:
            return Response("There was a problem retrieving data from the youtube link!")
        
        title = video['title']
        user = users[0]["discord_user"]

        response_text = "Current album is set to **{}** by **{}**. Metal on! <@&299516505169854477>".format(title,user.display_name)

        self.lastfm.db.update_weekly_dc_setlink(user.id,input_vid)
        return Response(response_text)
        


    async def cmd_weeklyroll(self,message):
        if message.author.id != self.config.owner_id:
            return Response("You can't do that.")
        
        weekly_users = self.lastfm.db.get_weekly_discussion_users()
        
        nonexcluded = list()
        for weekly_user in weekly_users:
            if weekly_user["exclude"] != 1:
                if weekly_user["last_winner"] != 1:
                    nonexcluded.append(weekly_user)
        
        weekly_user_count = len(nonexcluded)
        rnd = random.randint(0,weekly_user_count-1)

        print("{}th user is selected!".format(rnd))

        selected_user_id = str(nonexcluded[rnd]["discord_uid"])
        
        selected_member = await self.get_user_info(selected_user_id)
        
        if selected_member == None:
            return Response("There was a problem!Randomly selected user is not found. Please try again.")
        
        try:
            display_name = selected_member.display_name
            mention_str = selected_member.mention
            avatar = selected_member.avatar_url
            try:
                self.lastfm.db.update_weekly_dc(selected_user_id)
            except Exception as error:
                print("Could not update last winner!")
                print(error)
            
            return Response("I have randomly selected a user! Congrats to {}! Please share your album for this weeks discussion! {}".format(mention_str,avatar))
        except:
            return Response("There was a problem picking a random user. Please try again.")

    async def cmd_wdlist(self,message):
        try:
            weekly_users = self.lastfm.db.get_weekly_discussion_users()
            nonexcluded = list()
            last_winner = None
            for weekly_user in weekly_users:
                if weekly_user["last_winner"] == 1:
                    last_winner = weekly_user
                else:
                    if weekly_user["exclude"] != 1:
                        nonexcluded.append(weekly_user)
            
            if last_winner != None:
                last_winner_user = await self.get_user_info(last_winner["discord_uid"])
                last_winner_displayname = last_winner_user.display_name
            else:
                last_winner_displayname = "None"

            markdown = "```Markdown\n{} users in the weekly roll list.Last winner was {} and is excluded! \n\n".format(len(nonexcluded),last_winner_displayname)
            for user in nonexcluded:
                discord_uid = str(user["discord_uid"])
                yt_link = user["yt_link"]
                member = message.channel.server.get_member(discord_uid)
                if member == None:
                    member = await self.get_user_info(discord_uid)
                display_name = member.display_name

                markdown += "{} - {}\n".format(member,display_name)
            markdown += "```"
            return Response(markdown)
        except Exception as error:
            print(error)
            return Response("There was a problem retrieving weekly roll list.Please try again.")
        


        

    async def cmd_help(self, command=None):
        """
        Usage:
            {command_prefix}help [command]

        Prints a help message.
        If a command is specified, it prints a help message for that command.
        Otherwise, it lists the available commands.
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd:
                return Response(
                    "```\n{}```".format(
                        dedent(cmd.__doc__),
                        command_prefix=self.config.command_prefix
                    ),
                    delete_after=60
                )
            else:
                return Response("No such command", delete_after=10)

        else:
            helpmsg = "**Commands**\n```"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help':
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}{}".format(self.config.command_prefix, command_name))

            helpmsg += ", ".join(commands)
            helpmsg += "```"
            helpmsg += "https://github.com/SexualRhinoceros/MusicBot/wiki/Commands-list"

            return Response(helpmsg, reply=True, delete_after=60)

    async def cmd_blacklist(self, message, user_mentions, option, something):
        """
        Usage:
            {command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

        Add or remove users to the blacklist.
        Blacklisted users are forbidden from using bot commands.
        """

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                'Invalid option "%s" specified, use +, -, add, or remove' % option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                '%s users have been added to the blacklist' % (len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response('none of those users are in the blacklist.', reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    '%s users have been removed from the blacklist' % (old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {command_prefix}id [@user]

        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        Usage:
            {command_prefix}joinserver invite_link

        Asks the bot to join a server.  Note: Bot accounts cannot use invite links.
        """

        if self.user.bot:
            url = await self.generate_invite_link()
            return Response(
                "Bot accounts can't use invite links!  Click here to invite me: \n{}".format(url),
                reply=True, delete_after=30
            )

        try:
            if server_link:
                await self.accept_invite(server_link)
                return Response(":+1:")

        except:
            raise exceptions.CommandError('Invalid URL provided:\n{}\n'.format(server_link), expire_in=30)

    async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
        """
        Usage:
            {command_prefix}play song_link
            {command_prefix}play text to search for

        Adds the song to the playlist.  If a link is not provided, the first
        result from a youtube search is added to the queue.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])

        try:
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=30)

        if not info:
            raise exceptions.CommandError("That video cannot be played.", expire_in=30)

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # print("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,    # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise exceptions.CommandError(
                    "Error extracting info from search string, youtubedl returned no data.  "
                    "You may need to restart the bot if this continues to happen.", expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                return

            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things like the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

        if 'entries' in info:
            # I have to do exe extra checks anyways because you can request an arbitrary number of search results
            if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
                raise exceptions.PermissionsError("You are not allowed to request playlists", expire_in=30)

            # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
            num_songs = sum(1 for _ in info['entries'])

            if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
                raise exceptions.PermissionsError(
                    "Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
                    expire_in=30
                )

            # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
            if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
                raise exceptions.PermissionsError(
                    "Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
                        num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                    expire_in=30
                )

            if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                try:
                    return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                except exceptions.CommandError:
                    raise
                except Exception as e:
                    traceback.print_exc()
                    raise exceptions.CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

            t0 = time.time()

            # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
            # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
            # I don't think we can hook into it anyways, so this will have to do.
            # It would probably be a thread to check a few playlists and get the speed from that
            # Different playlists might download at different speeds though
            wait_per_song = 1.2

            procmesg = await self.safe_send_message(
                channel,
                'Gathering playlist information for {} songs{}'.format(
                    num_songs,
                    ', ETA: {} seconds'.format(self._fixg(
                        num_songs * wait_per_song)) if num_songs >= 10 else '.'))

            # We don't have a pretty way of doing this yet.  We need either a loop
            # that sends these every 10 seconds or a nice context manager.
            await self.send_typing(channel)

            # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
            #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

            entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

            tnow = time.time()
            ttime = tnow - t0
            listlen = len(entry_list)
            drop_count = 0

            if permissions.max_song_length:
                for e in entry_list.copy():
                    if e.duration > permissions.max_song_length:
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where this would ever break
                        # Unless the first entry starts being played, which would make this a race condition
                if drop_count:
                    print("Dropped %s songs" % drop_count)

            print("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                listlen,
                self._fixg(ttime),
                ttime / listlen,
                ttime / listlen - wait_per_song,
                self._fixg(wait_per_song * num_songs))
            )

            await self.safe_delete_message(procmesg)

            if not listlen - drop_count:
                raise exceptions.CommandError(
                    "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
                    expire_in=30
                )

            reply_text = "Enqueued **%s** songs to be played. Position in queue: %s"
            btext = str(listlen - drop_count)

        else:
            if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                raise exceptions.PermissionsError(
                    "Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

            except exceptions.WrongEntryTypeError as e:
                if e.use_url == song_url:
                    print("[Warning] Determined incorrect entry type, but suggested url is the same.  Help.")

                if self.config.debug_mode:
                    print("[Info] Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                    print("[Info] Using \"%s\" instead" % e.use_url)

                return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

            reply_text = "Enqueued **%s** to be played. Position in queue: %s"
            btext = entry.title

        if position == 1 and player.is_stopped:
            position = 'Up next!'
            reply_text %= (btext, position)

        else:
            try:
                time_until = await player.playlist.estimate_time_until(position, player)
                reply_text += ' - estimated time until playing: %s'
            except:
                traceback.print_exc()
                time_until = ''

            reply_text %= (btext, position, time_until)

        return Response(reply_text, delete_after=30)

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("That playlist cannot be played.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "Processing %s songs..." % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                print("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
                self.server_specific_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        print("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            self._fixg(ttime),
            ttime / num_songs,
            ttime / num_songs - wait_per_song,
            self._fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nAdditionally, the current song was skipped for being too long."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("Enqueued {} songs to be played in {} seconds".format(
            songs_added, self._fixg(ttime, 1)), delete_after=30)

    async def cmd_search(self, player, channel, author, permissions, leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query

        Searches a service for a video and adds it to the queue.
        - service: any one of the following services:
            - youtube (yt) (default if unspecified)
            - soundcloud (sc)
            - yahoo (yh)
        - number: return a number of video results and waits for user to choose one
          - defaults to 1 if unspecified
          - note: If your search query starts with a number,
                  you must put your query in quotes
            - ex: {command_prefix}search 2 "I ran seagulls"
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your playlist item limit (%s)" % permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise exceptions.CommandError(
                    "Please specify a search query.\n%s" % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("Please quote your search query properly.", expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("You cannot search for more than %s videos" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(channel, "Searching for videos...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("No videos found.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
                m.content.lower().startswith('exit'))

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            confirm_message = await self.safe_send_message(channel, "Is this ok? Type `y`, `n` or `exit`")
            response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return Response("Ok nevermind.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(self.config.command_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

                await self.cmd_play(player, channel, author, permissions, [], e['webpage_url'])

                return Response("Alright, coming right up!", delete_after=30)
            else:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

        return Response("Oh well :frowning:", delete_after=30)

    async def cmd_np(self, player, channel, server, message,username=None):
        """
        Usage:
            {command_prefix}np

        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                self.server_specific_data[server]['last_np_msg'] = None

            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = "Now Playing: **%s** added by **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str)
            else:
                np_text = "Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str)

            self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_summon(self, channel, author, voice_channel):
        """
        Usage:
            {command_prefix}summon

        Call the bot to the summoner's voice channel.
        """

        if not author.voice_channel:
            raise exceptions.CommandError('You are not in a voice channel!')

        voice_client = self.the_voice_clients.get(channel.server.id, None)
        if voice_client and voice_client.channel.server == author.voice_channel.server:
            await self.move_voice_client(author.voice_channel)
            return

        # move to _verify_vc_perms?
        chperms = author.voice_channel.permissions_for(author.voice_channel.server.me)

        if not chperms.connect:
            self.safe_print("Cannot join channel \"%s\", no permission." % author.voice_channel.name)
            return Response(
                "```Cannot join channel \"%s\", no permission.```" % author.voice_channel.name,
                delete_after=25
            )

        elif not chperms.speak:
            self.safe_print("Will not join channel \"%s\", no permission to speak." % author.voice_channel.name)
            return Response(
                "```Will not join channel \"%s\", no permission to speak.```" % author.voice_channel.name,
                delete_after=25
            )

        player = await self.get_player(author.voice_channel, create=True)

        if player.is_stopped:
            player.play()

        if self.config.auto_playlist:
            await self.on_player_finished_playing(player)

    async def cmd_pause(self, player):
        """
        Usage:
            {command_prefix}pause

        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError('Player is not playing.', expire_in=30)

    async def cmd_resume(self, player):
        """
        Usage:
            {command_prefix}resume

        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()

        else:
            raise exceptions.CommandError('Player is not paused.', expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Usage:
            {command_prefix}shuffle

        Shuffles the playlist.
        """

        player.playlist.shuffle()

        cards = [':spades:',':clubs:',':hearts:',':diamonds:']
        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(":ok_hand:", delete_after=15)

    async def cmd_clear(self, player, author):
        """
        Usage:
            {command_prefix}clear

        Clears the playlist.
        """

        player.playlist.clear()
        return Response(':put_litter_in_its_place:', delete_after=20)

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
        """
        Usage:
            {command_prefix}skip

        Skips the current song when enough votes are cast, or by the bot owner.
        """

        if player.is_stopped:
            raise exceptions.CommandError("Can't skip! The player is not playing!", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    # print(player.playlist.peek()._waiting_futures[0].__dict__)
                    return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")

        if author.id == self.config.owner_id \
                or permissions.instaskip \
                or author == player.current_entry.meta.get('author', None):

            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(self.config.skips_required,
                              sane_round_int(num_voice * self.config.skip_ratio_required)) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                'your skip for **{}** was acknowledged.'
                '\nThe vote to skip has been passed.{}'.format(
                    player.current_entry.title,
                    ' Next song coming up!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                'your skip for **{}** was acknowledged.'
                '\n**{}** more {} required to vote to skip this song.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'person is' if skips_remaining == 1 else 'people are'
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_volume(self, message, player, new_volume=None):
        """
        Usage:
            {command_prefix}volume (+/-)[volume]

        Sets the playback volume. Accepted values are from 1 to 100.
        Putting + or - before the volume will make the volume change relative to the current volume.
        """

        if not new_volume:
            return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError('{} is not a valid number'.format(new_volume), expire_in=20)

        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response('updated volume from %d to %d' % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    'Unreasonable volume provided: {}%. Provide a value between 1 and 100.'.format(new_volume), expire_in=20)

    async def cmd_queue(self, channel, player):
        """
        Usage:
            {command_prefix}queue

        Prints the current song queue.
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

        if player.current_entry:
            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append("Now Playing: **%s** added by **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
            else:
                lines.append("Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = '`{}.` **{}** added by **{}**'.format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = '`{}.` **{}**'.format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append('\n*... and %s more*' % unlisted)

        if not lines:
            lines.append(
                'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_clean(self, message, channel, server, author, search_range=50):
        """
        Usage:
            {command_prefix}clean [range]

        Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
                return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=15)

    async def cmd_pldump(self, channel, song_url):
        """
        Usage:
            {command_prefix}pldump url

        Dumps the individual urls of a playlist
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp":   lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await self.send_file(channel, fcontent, filename='playlist.txt', content="Here's the url dump for <%s>" % song_url)

        return Response(":mailbox_with_mail:", delete_after=20)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]

        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response(":mailbox_with_mail:", delete_after=20)


    async def cmd_perms(self, author, channel, server, permissions):
        """
        Usage:
            {command_prefix}perms

        Sends the user a list of their permissions.
        """

        lines = ['Command permissions in %s\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response(":mailbox_with_mail:", delete_after=20)


    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name

        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick

        Changes the bot's nickname.
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]

        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Unable to change avatar: %s" % e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)


    async def cmd_disconnect(self, server):
        await self.disconnect_voice_client(server)
        return Response(":hear_no_evil:", delete_after=20)

    async def cmd_restart(self, channel):
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal

    async def cmd_shutdown(self, channel):
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal

    async def on_message(self, message):
        await self.wait_until_ready()

        # if message.author.id == "174215624313012224":
        #     chance = random.randint(0,15)
        #     if chance == 0:
        #         await self.add_reaction(message,"puke:260735100843458561")

        message_content = message.content.strip()
        if 'meme' in message_content.lower():
            # Get lastfm user
            chance = random.randint(0,1)
            if chance == 0:
                try:
                    lastfm_user = self.lastfm.db.get_lastfm_user(message.author.id)
                    if lastfm_user != None:
                        # Get top albums
                        user_albums = self.lastfm.get_user_albums(lastfm_user)
                        if len(user_albums) >= 0:
                            top_album = user_albums[0]
                            if len(user_albums) >= 6:
                                top_album = user_albums[random.randint(0,6)]
                            artist_name = top_album.item.artist.name
                            text = "<:CoreYearly:308334429510696970> No, **{}** *IS* a meme. <:CoreYearly:308334429510696970>".format(artist_name)
                            await self.safe_send_message(message.channel,text)
                except:
                    pass
            else:
                print("Better luck next time")

        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            self.safe_print("Ignoring command from myself (%s)" % message.content)
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return  # if I want to log this I just move it under the prefix check

        command, *args = message_content.split()  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = getattr(self, 'cmd_%s' % command, None)
        if not handler:
            return

        if message.channel.is_private:
            print("Private message command" + str(command))
            if command != 'lastfmsupport':
                if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                    await self.send_message(message.channel, 'You cannot use this bot in private messages.')
                    return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            self.safe_print("[User blacklisted] {0.id}/{0.name} ({1})".format(message.author, message_content))
            return

        else:
            self.safe_print("[Command] {0.id}/{0.name} ({1})".format(message.author, message_content))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.safe_send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '%s, %s' % (message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            print("{0.__class__}: {0.message}".format(e))

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n%s\n```' % e.message,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            traceback.print_exc()
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

    async def on_voice_state_update(self, before, after):
        if not all([before, after]):
            return

        if before.voice_channel == after.voice_channel:
            return

        if before.server.id not in self.players:
            return

        my_voice_channel = after.server.me.voice_channel  # This should always work, right?

        if not my_voice_channel:
            return

        if before.voice_channel == my_voice_channel:
            joining = False
        elif after.voice_channel == my_voice_channel:
            joining = True
        else:
            return  # Not my channel

        moving = before == before.server.me

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(my_voice_channel)

        if after == after.server.me and after.voice_channel:
            player.voice_client.channel = after.voice_channel

        if not self.config.auto_pause:
            return

        if sum(1 for m in my_voice_channel.voice_members if m != after.server.me):
            if auto_paused and player.is_paused:
                print("[config:autopause] Unpausing")
                self.server_specific_data[after.server]['auto_paused'] = False
                player.resume()
        else:
            if not auto_paused and player.is_playing:
                print("[config:autopause] Pausing")
                self.server_specific_data[after.server]['auto_paused'] = True
                player.pause()

    async def on_server_update(self, before:discord.Server, after:discord.Server):
        if before.region != after.region:
            self.safe_print("[Servers] \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)


if __name__ == '__main__':
    bot = MusicBot()
    bot.run()
