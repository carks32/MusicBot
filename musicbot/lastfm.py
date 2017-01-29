import pylast
import time


class Lastfm:
    def __init__(self,config):
        print("Initializing Lastfm")

        self.userNetworks = dict()

        self.parser = YoutubeTitleParser()
        self.scrobbleCache = dict()

        self.api_key = config.lastfm_api_key
        self.api_secret = config.lastfm_api_secret

        users = config.lastfm_users
        passwords = config.lastfm_passwords

        for user in users:
            index = users.index(user)
            password = passwords[index]

            try:
                print("Trying to login as {}-{}".format(user,password))
                self.InitializeUser(user,password)
            except Exception as exception:
                print(exception)
                print("[Warning] Last.fm credentials are incorrect for some users.")


    
    def InitializeUser(self,user,password):
        network = pylast.LastFMNetwork(api_key = self.api_key, api_secret =
            self.api_secret, username = user, password_hash = password)

        self.userNetworks[user] = network

    def get_default_user_network(self):
        for key,value in self.userNetworks.items():
            # Awful hack
            return value

    def update_now_playing(self,user,title,duration):
        if user in self.userNetworks:
            network = self.userNetworks[user]

            parser = YoutubeTitleParser()
            parser.split_artist_title(title)

            song_name = parser.song_name
            artist_name = parser.artist_name

            search = network.search_for_track(artist_name,song_name)
            next_page = search.get_next_page()
            if(len(next_page) > 0):
                firstResult = next_page[0]
                song_name = firstResult.get_correction()
                artist_name = str(firstResult.get_artist())
            
                network.update_now_playing(artist=artist_name, title=song_name,duration=duration)

                self.scrobbleCache[user] = title

    def scrobble(self,user):
        if user in self.userNetworks:
            network = self.userNetworks[user]

            if user in self.scrobbleCache:
                title = self.scrobbleCache[user]

                if len(title) > 0:
                    parser = YoutubeTitleParser()
                    parser.split_artist_title(title)

                    song_name = parser.song_name
                    artist_name = parser.artist_name

                    search = network.search_for_track(artist_name,song_name)
                    next_page = search.get_next_page()
                    if(len(next_page) > 0):
                        firstResult = next_page[0]
                        song_name = firstResult.get_correction()
                        artist_name = str(firstResult.get_artist())

                        network.scrobble(artist=artist_name, title=song_name, timestamp=int(time.time()))

                    del self.scrobbleCache[user]

    def add_tags_from_video_title(self,user,title,tags):
        track = self.get_track_from_video_title(user,title)
        if track != None:
            tags = tags.split(",")
            try:
                track.add_tags(tags)
                return True
            except:
                return False

        else:
            return False


    def get_user_tags_from_video_title(self,user,title):
        track = self.get_track_from_video_title(user,title)
        if track != None:
            tags = track.get_tags()
            if len(tags) > 0:
                return tags
            else:
                return None
        else:
            return None

    
    def get_track_from_video_title(self,user,title):
        if user in self.userNetworks:
            network = self.userNetworks[user]
            if len(title) > 0:
                parser = YoutubeTitleParser()
                parser.split_artist_title(title)

                song_name = parser.song_name
                artist_name = parser.artist_name

                try:
                    search = network.search_for_track(artist_name,song_name)
                    next_page = search.get_next_page()
                    if(len(next_page) > 0):
                        firstResult = next_page[0]
                        return firstResult
                    else:
                        return None
                except:
                    return None
        else:
            return None


    def get_now_playing(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()
        try:
            np = libUser.get_now_playing()
            artist = np.artist

            markdown = "**{}** is currently listening to *{}* by **{}**".format(user,np.title,str(artist.name))
        except:
            markdown = "**{}** is currently not listening to anything.".format(user)

        return markdown

    def get_user_summary(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()

        MAX_ARTIST_COUNT = 5

        #albums = library.get_albums(artist=None)
        #artists = library.get_artists()

        libUser = library.get_user()
        total_play_count = libUser.get_playcount()

        top_albums = libUser.get_top_albums()
        top_artists = libUser.get_top_artists()
        top_tracks = libUser.get_top_tracks()
        top_tags = libUser.get_top_tags()


        # Top albums
        top_albums_to_be_listed = list()
        counter = 0
        for album in top_albums:
            top_albums_to_be_listed.append(album)

            if counter > MAX_ARTIST_COUNT:
                break
            counter = counter + 1

        # Top Artists
        top_artists_to_be_listed = list()
        counter = 0
        for artist in top_artists:
            top_artists_to_be_listed.append(artist)

            if counter > MAX_ARTIST_COUNT:
                break
            counter = counter + 1
            
        # Top Tracks
        top_tracks_to_be_listed = list()
        counter = 0
        for track in top_tracks:
            top_tracks_to_be_listed.append(track)

            if counter > MAX_ARTIST_COUNT:
                break
            counter = counter + 1
        
        # Top Tags
        top_tags_to_be_listed = list()
        counter = 0
        for tag in top_tags:
            top_tags_to_be_listed.append(tag)

            if counter > MAX_ARTIST_COUNT:
                break
            counter = counter + 1
        

        artistText = ''
        for artist in top_artists_to_be_listed:
            tags = artist.item.get_top_tags()
            top_tag = 'Unknown'
            play_count = 0
            if len(tags) > 0:
                top_tag = tags[0].item.name

            play_count = artist.weight
            
            artistText += "{} ({}) {} plays \n".format(artist.item.name,top_tag,play_count)

        albumsText = ''
        for album in top_albums_to_be_listed:
            album_name = album.item.title
            artist_name = album.item.artist.name
            weight = album.weight

            albumsText += "{} by {} {} plays \n".format(album_name,artist_name,weight)

        tagsByUserText = ''
        for tag in top_tags_to_be_listed:
            tag_name = tag.item.name
            tagsByUserText += "{} \n".format(tag_name,weight)
        
        markdown = "```Markdown\nLast.fm overview of {}\n\n* Total Scrobbles: {} plays\n\n* Top Artists \n{}\n\n* Top Albums \n{}\n\n* Tags set by {} \n{}\n\n http://www.last.fm/user/{}```".format(user,total_play_count,artistText,albumsText,user,tagsByUserText,user)
        return markdown

    def get_user_artist_info(self,user,artistName):
        user_artists = self.get_user_artists(user)

        for artist in user_artists:
            if artist.item.name.lower() == artistName.lower():
                markdown = "**{}** has scrobbled *{}* **{}** times.".format(user,artist.item.name,artist.weight)
                return markdown

        return "Looks like **{}** hasn't discovered this band yet <:DD:260520559383805952>".format(user)
    
    def get_user_albums(self,user,period="overall"):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        print("Fetching albums for {}".format(user))

        return libUser.get_top_albums(limit=500,period=period)

    def get_user_artists(self,user,period="overall"):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        return libUser.get_top_artists(limit=500,period=period)

    def get_user_tags(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        return libUser.get_top_tags()

    def get_user_totalplaycount(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        return libUser.get_playcount()
    
    def make_album_image(self,user,period):
        # Download images
        # Make grid
        # return file handle so the bot will upload back.
        print("not implemented")

    # Not working
    def compare_users(self,user1,user2):

        artistsA = self.get_user_artists(user1)
        artistsB = self.get_user_artists(user2)

        totalPlayCountA = self.get_user_totalplaycount(user1)
        totalPlayCountB = self.get_user_totalplaycount(user2)

        artistALen = len(artistsA)
        artistBLen = len(artistsB)

        print(totalPlayCountA)
        print(totalPlayCountB)

        print(artistALen)
        print(artistBLen)

        common_playcount = 0

        for a in artistsA:
            nameA = a.item.name
            for b in artistsB:
                nameB = b.item.name

                if nameA == nameB:
                    common_playcount = common_playcount + 1

        print(common_playcount)
        # common_artists = dict()
        # for a in artistsA:
        #     nameA = a.item.name
        #     for b in artistsB:
        #         nameB = b.item.name
                
        #         if nameA == nameB:
        #             percentage = 0

        #             weightA = float(a.weight)
        #             weightB = float(b.weight)
        #             # if weightA > weightB:
        #             #     percentage = weightB / weightA
        #             # else:
        #             #     percentage = weightA / weightB

        #             weightA = (weightA / (common_playcount)) * 100
        #             weightB = (weightB / (common_playcount)) * 100

        #             percentage = (weightA + weightB) / 2

        #             print("Percentage for {} is {}".format(nameA,percentage))

        #             common_artists[b.item.name] = { 'weightA': weightA, 'weightB': weightB, 'percentage': percentage }
        
    
        # percentageTotal = 0
        # for key,item in common_artists.items():
        #     percentageTotal = percentageTotal + float(item['percentage'])

            

        # result = percentageTotal / len(common_artists)

        # if result >= 100:
        #     result = 100
        # print(result)
        # print("Common artist len {}".format(len(common_artists)))
        

        



    def get_artist_info(self,artistName):

        network = self.get_default_user_network()
        try:
            artist = network.get_artist(artistName)

            bio_summary = artist.get_bio_summary()
            cover_image = artist.get_cover_image()
            top_tags = artist.get_top_tags()
            play_count = artist.get_playcount()

            tags = ''
            for tag in top_tags:
                name = tag.item.name
                tags += "{},".format(name)

            tags = tags[:len(tags) - 1] #Lul

            markdown = "{}\n```Markdown\n{}\n\n* Total Plays for {}: {} \n\n\n* Tags: {}\n\n{}\n\n```".format(cover_image,artistName,artistName,play_count,tags,bio_summary)
            return markdown
        except Exception as e:
            return e



        





# Youtube Title parsing
# Should be in its own file

import re


class YoutubeTitleParser(object):
    song_name = None
    artist_name = None

    def __init__(self, title=None):
        self.song_name = ''
        self.artist_name = ''
        self.separators = separators = [
                                    ' -- ',
                                    '--',
                                    ' - ',
                                    ' – ',
                                    ' — ',
                                    ' _ ',
                                    '-',
                                    '–',
                                    '—',
                                    ':',
                                    '|',
                                    '///',
                                    ' / ',
                                    '_',
                                    '/',
                                    '@'
        ]
        if title:
            self.split_artist_title(title)

    def parse_song(self, title=None):
        parts = title.split('-', 1)
        if len(parts) > 1:
            self.artist_name = parts[0]
            self.song_name = parts[1]
        else:
            self.song_name = parts[0]
            self.artist_name = ''

    @staticmethod
    def _clean_fluff(string):
        result = re.sub(r'/\s*\[[^\]]+\]$/', '', string=string)  # [whatever] at the end
        result = re.sub(r'/^\s*\[[^\]]+\]\s*/', '', string=result)  # [whatever] at the start
        result = re.sub(r'/\s*\[\s*(M/?V)\s*\]/', '', string=result)  # [MV] or [M/V]
        result = re.sub(r'/\s*\(\s*(M/?V)\s*\)/', '', string=result)  # (MV) or (M/V)
        result = re.sub(r'/[\s\-–_]+(M/?V)\s*/', '', string=result)  # MV or M/V at the end
        result = re.sub(r'/(M/?V)[\s\-–_]+/', '', string=result)  # MV or M/V at the start
        result = re.sub(r'/\s*\([^\)]*\bver(\.|sion)?\s*\)$/i', '', string=result)  # (whatever version)
        result = re.sub(r'/\s*[a-z]*\s*\bver(\.|sion)?$/i', '', string=result)  # ver. and 1 word before (no parens)
        result = re.sub(r'/\s*(of+icial\s*)?(music\s*)?video/i', '', string=result)  # (official)? (music)? video
        result = re.sub(r'/\s*(ALBUM TRACK\s*)?(album track\s*)/i', '', string=result)  # (ALBUM TRACK)
        result = re.sub(r'/\s*\(\s*of+icial\s*\)/i', '', string=result)  # (official)
        result = re.sub(r'/\s*\(\s*[0-9]{4}\s*\)/i', '', string=result)  # (1999)
        result = re.sub(r'/\s+\(\s*(HD|HQ)\s*\)$/', '', string=result)  # HD (HQ)
        result = re.sub(r'/[\s\-–_]+(HD|HQ)\s*$/', '', string=result)  # HD (HQ)

        return result

    @staticmethod
    def _clean_title(title):
        result = re.sub('/\s*\*+\s?\S+\s?\*+$/', '', title)
        result = re.sub('/\s*video\s*clip/i', '', result)  # **NEW**
        result = re.sub('/\s*video\s*clip/i', '', result)  # video clip
        result = re.sub('/\s+\(?live\)?$/i', '', result)  # live
        result = re.sub('/\(\s*\)/', '', result)  # Leftovers after e.g. (official video)
        result = re.sub('/^(|.*\s)"(.*)"(\s.*|)$/', '$2', result)  # Artist - The new "Track title" featuring someone
        result = re.sub('/^(|.*\s)\'(.*)\'(\s.*|)$/', '$2', result)  # 'Track title'
        result = re.sub('/^[/\s,:;~\-–_\s"]+/', '', result)  # trim starting white chars and dash
        result = re.sub('/[/\s,:;~\-–_\s"]+$/', '', result)  # trim trailing white chars and dash
        return result

    @staticmethod
    def _clean_artist(artist):
        result = re.sub('/\s*[0-1][0-9][0-1][0-9][0-3][0-9]\s*/', '', artist)  # date formats ex. 130624
        result = re.sub('/^[/\s,:;~\-–_\s"]+/', '', result)  # trim starting white chars and dash
        result = re.sub('/[/\s,:;~\-–_\s"]+$/', '', result)  # trim starting white chars and dash

        return result

    def split_artist_title(self, title):
        parts = None
        for separator in self.separators:
            if separator in title:
                parts = title.split('{}'.format(separator), 1)
                break

        if parts:
            self.song_name = self._clean_title(parts[1])
            self.song_name = self._clean_fluff(self.song_name)
            self.artist_name = self._clean_artist(parts[0])
        else:
            self.song_name = title
            self.artist_name = ''