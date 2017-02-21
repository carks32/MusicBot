import pylast
from .database import LastFmSQLiteDatabase
import datetime
import math

class Lastfm:
    def __init__(self,config):
        print("Initializing Lastfm")

        self.userNetworks = dict()

        self.api_key = config.lastfm_api_key
        self.api_secret = config.lastfm_api_secret
        self.db = LastFmSQLiteDatabase("lastfm.sqlite")

        user = config.lastfm_username
        password = config.lastfm_password

        try:
            print("Trying to login as {}-{}".format(user,password))
            self.InitializeUser(user,pylast.md5(password))
        except Exception as exception:
            print(exception)
            print("[Warning] Last.fm credentials are incorrect for some users.")

    def InitializeUser(self,user,password):
        network = pylast.LastFMNetwork(api_key = self.api_key, api_secret =
            self.api_secret, username = user, password_hash = password)

        self.default_network = network

    def get_default_user_network(self):
        return self.default_network


    def get_now_playing_markdown(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()
        try:
            np = libUser.get_now_playing()
            artist = np.artist

            markdown = ":musical_note: **{}** is listening to *{}* by **{}**.".format(user,np.title,str(artist.name))
        except:
            markdown = "**{}** is not listening to any music. <:FeelsMetalHead:279991636144947200>".format(user)

        return markdown

    def get_recent_tracks(self,user, index = 0):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)
		
        if(not(isinstance(index, int))):
            index = 0
        if(index > 49):
            index = 49
			
        recent_tracks = lastfm_user.get_recent_tracks(limit=10*(index+1))[-10:]

        markdown = "```Markdown\nRecent tracks of {} from {} to {}:\n".format(user, 10*index+1, 10*index+10)

        for track in recent_tracks:
            timestamp = track.timestamp
            date_ago = timestamp

            date = (datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(float(timestamp)))
            seconds_ago = date.total_seconds()

            if seconds_ago > 60 and seconds_ago < 60*60:
                date_ago = "{0:.0f} minutes ago".format(seconds_ago / 60)

            if seconds_ago >= 60*60 and seconds_ago < 60*60*24:
                hours_ago = seconds_ago / (60*60)

                if hours_ago > 1 and hours_ago < 2:
                    date_ago = "an hour ago"
                else:
                    date_ago = "{0:.0f} hours ago".format(math.floor(hours_ago))

            if seconds_ago >= 60*60*24:
                days_ago = seconds_ago / (60*60*24)

                date_ago = "{0:.0f} days ago".format(days_ago)
            
            markdown += "{} - {} - {}\n".format(track.track.get_artist().name,track.track.title,date_ago)
            

        markdown += "```"
        return markdown

    def get_weekly_scrobble_count(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        chart = libUser.get_weekly_track_charts()

        total = 0
        for c in chart:
            w = c.weight
            total += w
        return total

    def get_now_playing(self,user):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()
        try:
            np = libUser.get_now_playing()
            return np
        except:
            return None

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

    def get_user_albums(self,user,period="overall",size=5):
        network = self.get_default_user_network()
        lastfm_user = network.get_user(user)

        library = lastfm_user.get_library()
        libUser = library.get_user()

        limit = size * size

        print("Fetching albums for {} - Limit: {}".format(user,limit))

        return libUser.get_top_albums(limit=limit,period=period)

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

    def get_user_listening_text(self, user_track):
        return "**{}** is listening to *{}* by **{}**".format(
            user_track.username,
            user_track.track_artist_name,
            user_track.track_title)

# TODO: Add classes to separate concerns
class UserTrack:
    def __init__(self, username, track_title, track_artist_name):
        self.username = username
        self.track_title = track_title
        self.track_artist_name = track_artist_name