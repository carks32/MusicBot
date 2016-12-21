import pylast


LASTFM_API_KEY = "1c15b9ea24af56c25eac1d40b24cf6b5"
LASTFM_API_SECRET = "2fc65d3ac585f3738b8c56a8b6013d6f"

from musicbot.core import YoutubeTitleParser
#from core import YoutubeTitleParser
import time


class Lastfm:
    def __init__(self,users,passwords):
        print("Initializing Lastfm")

        self.userNetworks = dict()

        self.parser = YoutubeTitleParser()
        self.scrobbleCache = dict()

        for user in users:
            index = users.index(user)
            password = passwords[index]

            self.InitializeUser(user,password)

    
    def InitializeUser(self,user,password):
        network = pylast.LastFMNetwork(api_key = LASTFM_API_KEY, api_secret =
            LASTFM_API_SECRET, username = user, password_hash = password)

        self.userNetworks[user] = network

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
        network = self.userNetworks['arkenthera']
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
        network = self.userNetworks['arkenthera']
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
        
        markdown = "```Markdown\nLast.fm overview of {}\n\n* Total Scrobbles: {}\n\n* Top Artists \n{}\n\n* Top Albums \n{}\n\n* Tags set by {} \n{}\n\n```".format(user,total_play_count,artistText,albumsText,user,tagsByUserText)
        return markdown
    
    def get_user_albums(self,user):
        network = self.userNetworks['arkenthera']
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        return libUser.get_top_albums()

    def compare_users(self,user1,user2):
        albumsA = self.get_user_albums(user1)
        albumsB = self.get_user_albums(user2)

        for album in albumsA:
            print(dir(album))


        
