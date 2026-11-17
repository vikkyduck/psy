"""Only a genuine group attachment can become a recording in the regular authenticated upload pipeline."""
import asyncio
import hashlib
import json
import re

from .psy_instance import GROUP, PUBLIC_URL as URL
MAX_SIZE = 20 * 1024**2
CHUNK = 1024**2
SOCKET = "/run/psy-group-upload.sock"
ACCEPTED = {"queued","processing","delivery_pending","sending","done"}
AUDIO_EXT = {"mp3","wav","m4a","aac","ogg","opus","flac","mp4","mov","webm","mkv",
             "mpga","mpeg","wma","aiff","caf"}


def metadata(msg):
    if msg.chat.id != GROUP or not msg.from_user or msg.from_user.is_bot or msg.sender_chat:
        return None
    for attr, default in (("voice","Голосовое.ogg"),("audio","Запись.mp3"),("video_note","Видео.mp4"),
                          ("video","Видео.mp4"),("document","")):
        media = getattr(msg,attr,None)
        if media:
            name = getattr(media,"file_name",None) or default
            name = re.sub(r"[^\w .()-]","_",name.rsplit("/",1)[-1]).strip(". ")
            if name and name.casefold().rsplit(".",1)[-1] in AUDIO_EXT:
                if len(name) > 140:
                    # Shorten the stem. The extension decides the route and the decoder.
                    stem, ext = name.rsplit(".",1)
                    name = stem[:139-len(ext)] + "." + ext
                return media,name
    return None


async def token_for(uid, thread, reply):
    reader,writer = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET),5)
    try:
        writer.write(json.dumps(dict(uid=uid,thread=thread,reply=reply)).encode()+b"\n")
        await writer.drain()
        token = (await asyncio.wait_for(reader.readline(),8)).decode().strip()
        if not re.fullmatch("[A-Za-z0-9_-]{43}",token):
            raise RuntimeError("upload session unavailable")
        return token
    finally:
        writer.close()
        await writer.wait_closed()


def identity(data):
    blocks = b"".join(hashlib.sha256(data[i:i+CHUNK]).digest() for i in range(0,len(data),CHUNK))
    return {"fingerprint": hashlib.sha256(data[:65536]).hexdigest(), "content_id": hashlib.sha256(blocks).hexdigest()}


async def send_data(http, filename, data):
    # Retrying create with an identical user/chat/reply/content reopens the same job after a lost response.
    info = dict(filename=filename, size=len(data), language="ru", **identity(data))
    async def call(method,path,**options):
        response = await http.request(method,path,**options)
        if response.status_code not in (200,201):
            raise RuntimeError("upload service rejected request")
        return response.json()
    job = await call("POST","/api/jobs",json=info)
    if job.get("state") in ACCEPTED:
        return
    ident, offset = job.get("id"),job.get("offset")
    if not isinstance(ident,str) or not re.fullmatch("[a-f0-9]{32}",ident) or type(offset) is not int or not 0<=offset<=len(data):
        raise RuntimeError("invalid upload job")
    path="/api/jobs/"+ident
    while offset<len(data):
        block=data[offset:offset+CHUNK]
        data_part = await call("PATCH",path,content=block,headers={"Upload-Offset":str(offset),"Content-Type":"application/octet-stream"})
        if data_part.get("offset")!=offset+len(block):
            raise RuntimeError("unconfirmed upload offset")
        offset+=len(block)
    if (await call("POST",path+"/complete")).get("state") not in ACCEPTED:
        raise RuntimeError("recording not accepted")


async def submit(msg):
    import httpx
    recording = metadata(msg)
    if not recording:
        return False
    media, filename = recording
    declared = getattr(media,"file_size",None)
    if type(declared) is not int or not 0 < declared <= MAX_SIZE:
        return False
    thread = msg.message_thread_id if msg.is_topic_message else None
    # Obtain the narrow grant first. Without the broker the group still has the Mini App.
    token = await token_for(msg.from_user.id, thread, msg.message_id)
    tgfile = await media.get_file()
    if not (isinstance(tgfile.file_path,str) and tgfile.file_path.startswith("https://api.telegram.org/file/bot")):
        return False
    data=bytes(await tgfile.download_as_bytearray())
    if len(data) != declared:
        return False
    async with httpx.AsyncClient(base_url=URL, headers={"X-Upload-Session":token}, timeout=30,
                                 follow_redirects=False, trust_env=False) as client:
        for attempt in range(2):
            try:
                await send_data(client,filename,data)
                return True
            except (httpx.TimeoutException,httpx.TransportError):
                if attempt:
                    raise
                await asyncio.sleep(1)
