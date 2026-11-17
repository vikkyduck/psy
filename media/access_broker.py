"""One peer-authenticated, narrowly scoped Mini App lookup per systemd-accepted socket."""
import http.client
import hashlib
import secrets
import time
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
import psy_upload_common as C
import recording_grant as grant

MAX_REQUEST = 32 * 1024
MAX_RESPONSE = 16 * 1024


def telegram_member(uid, token):
    connection=http.client.HTTPSConnection('api.telegram.org',timeout=6)
    try:
        body=json.dumps({'chat_id':C.GROUP,'user_id':uid}).encode()
        connection.request('POST','/bot'+token+'/getChatMember',body=body,headers={'Content-Type':'application/json'})
        response=connection.getresponse()
        if response.status!=200:
            raise ValueError('membership unavailable')
        content=response.read(MAX_RESPONSE+1)
        if len(content)>MAX_RESPONSE:
            raise ValueError('response too large')
        data=json.loads(content)
        if data.get('ok') is not True or not isinstance(data.get('result'),dict):
            raise ValueError('invalid membership response')
        return C.member_allowed(data['result'])
    finally:
        connection.close()


def dispatch(request,token,member_lookup=telegram_member):
    if not isinstance(request,dict):
        return {'status':400}
    if request.get('action') in ('login','session','bind','redeliver'):
        try:
            action = request['action']
            if action == 'login':
                ctx = C.validate_init_context(request['init_data'], token)
                ticket = request.get('ticket')
                uid = ctx['uid']
                if ticket is not None:
                    if (not isinstance(ticket,dict) or ticket['uid'] != uid or ticket['expires'] <= time.time()
                            or not grant.checked('ticket',grant.ticket_fields(ticket),ticket.get('proof'))):
                        raise PermissionError()
                    dest = grant.route(ticket)
                elif ctx['start_param'] == 'psy_group':
                    dest = {'uid':uid,'chat':C.GROUP}
                elif not ctx['start_param'] and uid == C.OWNER:
                    dest = {'uid':uid,'chat':C.OWNER}
                else:
                    raise PermissionError()
                secret = secrets.token_urlsafe(32)
                return {'status':200,'secret':secret,'session':grant.session(secret,dest,C.now()+grant.TTL)}
            session = request['session']
            if (not grant.valid_session(session) or hashlib.sha256(request['secret'].encode()).hexdigest()
                    != session['hash']):
                raise PermissionError()
            if action == 'session':
                return {'status':200}
            if action == 'bind':
                return {'status':200, 'job':grant.bind(session, request['shape'], request.get('parent'))}
            return {'status':200, 'retry':grant.redelivery(session, request['job'],request.get('confirmed'))}
        except PermissionError:
            return {'status':403}
        except (ValueError,KeyError,TypeError,AttributeError):
            return {'status':401}
    if request.get('action')=='init' and set(request)=={'action','init_data'}:
        try:
            ctx=C.validate_init_context(request['init_data'],token)
            if ctx['start_param'] not in ('','psy_group') or ctx['uid'] >= 2**52:
                return {'status':403}
            return {'status':200,**ctx}
        except (ValueError,KeyError,TypeError,AttributeError):
            return {'status':401}
    if request.get('action')=='member' and set(request)=={'action','uid'}:
        uid=request['uid']
        if type(uid) is not int or not 0<uid<2**52:
            return {'status':400}
        if uid==C.OWNER:
            # The owner keeps the group destination without being a group member.
            return {'status':200,'member':True}
        try:
            return {'status':200,'member':bool(member_lookup(uid,token))}
        except Exception:
            return {'status':503}
    return {'status':400}


def serve(peer,expected_uid):
    _,uid,_=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    if uid!=expected_uid:return
    peer.settimeout(6)
    with peer.makefile('rb') as stream:
        raw=stream.readline(MAX_REQUEST+1)
    if not raw.endswith(b'\n') or len(raw)>MAX_REQUEST:return
    try:
        request=json.loads(raw)
    except (ValueError,RecursionError):
        reply={'status':400}
    else:
        token,_=C.bot_settings()
        reply=dispatch(request,token)
    peer.sendall(json.dumps(reply,separators=(',',':')).encode()+b'\n')


if __name__=='__main__':
    try:
        with socket.socket(fileno=0) as peer:
            serve(peer,pwd.getpwnam('psy-upload').pw_uid)
    except Exception:
        sys.exit(1)
