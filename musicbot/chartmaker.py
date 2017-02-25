import asyncio
from contextlib import closing
import aiohttp # $ pip install aiohttp
from PIL import Image
import os
import posixpath
try:
    from urlparse import urlsplit
    from urllib import unquote
except ImportError: # Python 3
    from urllib.parse import urlsplit, unquote

IMAGE_DOWNLOAD_PATH = "lastfm_album_cache"

def url2filename(url):
    """Return basename corresponding to url.
    >>> print(url2filename('http://example.com/path/to/file%C3%80?opt=1'))
    fileÀ
    >>> print(url2filename('http://example.com/slash%2fname')) # '/' in name
    Traceback (most recent call last):
    ...
    ValueError
    """
    urlpath = urlsplit(url).path
    basename = posixpath.basename(unquote(urlpath))
    if (os.path.basename(basename) != basename or
        unquote(posixpath.basename(urlpath)) != basename):
        raise ValueError  # reject '%2f' or 'dir%5Cbasename.ext' on Windows
    return basename

@asyncio.coroutine
def download(url, session, semaphore, chunk_size=1<<15):
    with (yield from semaphore): # limit number of concurrent downloads
        if url == None:
            return None
        filename = url2filename(url)
        response = yield from session.get(url)
        with closing(response), open(os.path.join(IMAGE_DOWNLOAD_PATH,filename), 'wb') as file:
            while True: # save file
                chunk = yield from response.content.read(chunk_size)
                if not chunk:
                    break
                file.write(chunk)
    return filename, (response.status, tuple(response.headers.items()))


def done_callback(future):
    print("Done.")

class ChartMaker:
    def __init__(self,donecallback,channel,lastfm,user,size,period,generatingMessageProc,errorCallback):
        self.lastfm = lastfm
        self.size = size
        self.user = user
        self.period = period
        self.donecallback = donecallback
        self.channel = channel
        self.generatingMessageProc = generatingMessageProc
        self.errorCallback = errorCallback

        # Get albums
        self.user_albums = self.lastfm.get_user_albums(self.user,self.period,self.size)


    async def start(self):
        if len(self.user_albums) < self.size * self.size:
            error_string = "<:x:277841619464617984> Error! Error! *{}* has **{}** albums but you requested **{}**.<:x:277841619464617984>".format(self.user,len(self.user_albums),str(self.size*self.size))
            await self.errorCallback(error_string,self.channel,self.generatingMessageProc)
            return

        self.selected_albums = list()
        self.selected_album_cover_urls = list()

        for i in range(self.size*self.size):
            self.selected_albums.append(self.user_albums[i])
            try:
                self.selected_album_cover_urls.append(self.user_albums[i].item.get_cover_image())
            except:
                self.selected_album_cover_urls.append("http://i.imgur.com/Gvmwrg8.png")
                print("Error retrieving album image")
                print(self.user_albums[i])


        urls = self.selected_album_cover_urls
        self.session = aiohttp.ClientSession()
        semaphore = asyncio.Semaphore(10)
        download_tasks = (download(url, self.session, semaphore) for url in urls)
        tasks = asyncio.gather(*download_tasks)
        tasks.add_done_callback(self.downloads_complete)
        print("Starting downloading images..")
        asyncio.ensure_future(asyncio.gather(*download_tasks))
    
    
    async def make_grid(self,user,size_multiplier,image_handles):
        images = list()
        for image_handle in image_handles:
            if image_handle == '':
                images.append(None)
            else:
                images.append(Image.open(os.path.join(IMAGE_DOWNLOAD_PATH,image_handle)))


        image_size = size_multiplier * (300)

        size_per_cover = (image_size / (size_multiplier))
        new_im = Image.new('RGB',(image_size,image_size)) # 900x900

        for i in range(size_multiplier*size_multiplier):
            if images[i] != None:
                images[i].thumbnail((size_per_cover,size_per_cover))

        for i in range(size_multiplier):
            for j in range(size_multiplier):
                x_pos = int(size_per_cover) * j
                y_pos = int(size_per_cover) * i
                
                this_image = images[i*size_multiplier + j]
                if this_image != None:
                    new_im.paste(this_image,(x_pos,y_pos))

        # Clean up
        for image_handle in image_handles:
            if image_handle != '':
                os.remove(os.path.join(IMAGE_DOWNLOAD_PATH,image_handle))
        
        final_image_path = os.path.join(IMAGE_DOWNLOAD_PATH,"{}_{}.jpg".format(user,str(size_multiplier)))
        new_im.save(final_image_path,"JPEG",quality=80, optimize=True)
        
        await self.donecallback(final_image_path,self.channel,self.generatingMessageProc)
    
    
    def downloads_complete(self,future):
        self.session.close()
        images = list()

        print("Download complete.")

        for cover in self.selected_album_cover_urls:
            filename = ""
            if cover != None:
                filename = url2filename(cover)
            images.append(filename)

        asyncio.ensure_future(self.make_grid(self.user,self.size,images))