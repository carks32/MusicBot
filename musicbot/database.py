import sqlite3
import os

DATABASE_PATH = "databases"

class LastFmSQLiteDatabase:
    def __init__(self,file_name):
        self.sqlite_file = os.path.join(DATABASE_PATH,file_name)

        self.db_connection = sqlite3.connect(self.sqlite_file)
        self.sqlite = self.db_connection.cursor()

        self.create_table()

    # If the table not exists, create
    def create_table(self):
        try:
            self.sqlite.execute('CREATE TABLE {tn} ({nf} {ft} PRIMARY KEY,lastfm_uname TEXT)'.format(tn="lastfm",nf="discord_uid",ft="INTEGER"))
        except:
            print("Last.fm table already exists!")


    def insert(self,discord_uid,lastfm_username):
        # Try casting discord_uid to int,otherwise fail
        try:
            discord_uid = int(discord_uid)
        except:
            print("Probably invalid user id")
            raise Exception("Invalid user id {}".format(discord_uid))
            return # Necessary ?
        
        print("Inserting {} - {}".format(discord_uid,lastfm_username))
        
        query = "INSERT INTO 'lastfm' ('discord_uid','lastfm_uname') VALUES ({},'{}')".format(discord_uid,str(lastfm_username))
        print(query)
        self.sqlite.execute(query)
        
        self.db_connection.commit()
    
    def user_exists(self,discord_uid):
        # Try casting discord_uid to int,otherwise fail
        try:
            discord_uid = int(discord_uid)
        except:
            raise Exception("Invalid user id {}".format(discord_uid))
            return False# Necessary ?

        query = "SELECT * FROM lastfm WHERE discord_uid={}".format(discord_uid)

        self.sqlite.execute(query)

        result = self.sqlite.fetchone()

        if result:
            return True
        else:
            return False

    # Retrieve last.fm user name from database
    #
    def get_lastfm_user(self,discord_uid,username=None):
        # Try casting discord_uid to int,otherwise fail
        try:
            discord_uid = int(discord_uid)
        except:
            print("Probably invalid user id")
            raise Exception("Invalid user id {}".format(discord_uid))
            return # Necessary?

        query = "SELECT * FROM lastfm WHERE discord_uid={}".format(discord_uid)

        self.sqlite.execute(query)

        result = self.sqlite.fetchone()

        if result:
            user = result[1]
            print("Lastfm username for {} is {}".format(discord_uid,user))
            return user
        else:
            raise Exception("This discord user ({}) doesnt exist in the Last.fm database!".format(discord_uid))

    
    def update(self,discord_uid,lastfm_username):
        try:
            discord_uid = int(discord_uid)
        except:
            print("Probably invalid user id")
            return

        print("Updating {} - {}".format(discord_uid,lastfm_username))
        
        query = "UPDATE 'lastfm' SET lastfm_uname=('{}') WHERE discord_uid=({})".format(str(lastfm_username),discord_uid)
        
        print(query)
        self.sqlite.execute(query)
            
        self.db_connection.commit()
        
    def list_users(self):
        query = "SELECT * FROM lastfm"

        self.sqlite.execute(query)

        results = self.sqlite.fetchall()

        markdown = '```Markdown\n{} Last.fm users on our database: \n'.format(len(results))
        for result in results:
            markdown += result[1] + "\n"
        
        markdown += '```'
        
        return markdown

        

    def close(self):
        self.db_connection.close()