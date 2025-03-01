import hashlib
import base64
import json
import os
import socket
import struct
import ssl
import threading
import time
import traceback

import errno
import codecs
from collections import deque
from select import select
from http.server import BaseHTTPRequestHandler  # pylint: disable=import-error
from io import StringIO, BytesIO

unicode = str  # pylint: disable=redefined-builtin

__all__ = [
    'WebSocket',
    'WebSocketServer'
]

_VALID_STATUS_CODES = [1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 3000, 3999, 4000, 4999]

HANDSHAKE_STR = (
    'HTTP/1.1 101 Switching Protocols\r\n'
    'Upgrade: WebSocket\r\n'
    'Connection: Upgrade\r\n'
    'Sec-WebSocket-Accept: %(acceptstr)s\r\n\r\n'
)

FAILED_HANDSHAKE_STR = (
    'HTTP/1.1 426 Upgrade Required\r\n'
    'Upgrade: WebSocket\r\n'
    'Connection: Upgrade\r\n'
    'Sec-WebSocket-Version: 13\r\n'
    'Content-Type: text/plain\r\n\r\n'
    'This service requires use of the WebSocket protocol\r\n'
)

GUID_STR = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

STREAM = 0x0
TEXT = 0x1
BINARY = 0x2
CLOSE = 0x8
PING = 0x9
PONG = 0xA

HEADERB1 = 1
HEADERB2 = 3
LENGTHSHORT = 4
LENGTHLONG = 5
MASK = 6
PAYLOAD = 7

MAXHEADER = 65536
MAXPAYLOAD = 33554432


def _check_unicode(val):
    return isinstance(val, str)


class HTTPRequest(BaseHTTPRequestHandler):
    def __init__(self, request_text):  # pylint: disable=super-init-not-called
        self.rfile = BytesIO(request_text)
        self.raw_requestline = self.rfile.readline()
        self.error_code = self.error_message = None
        self.parse_request()


class WebSocket(object):  # pylint: disable=too-many-instance-attributes
    def __init__(self, server, sock, address):
        self.server = server
        self.client = sock
        self.address = address

        self.handshaked = False
        self.headerbuffer = bytearray()
        self.headertoread = 2048

        self.fin = 0
        self.data = bytearray()
        self.opcode = 0
        self.hasmask = 0
        self.maskarray = None
        self.length = 0
        self.lengtharray = None
        self.index = 0
        self.request = None
        self.usingssl = False

        self.frag_start = False
        self.frag_type = BINARY
        self.frag_buffer = None
        self.frag_decoder = codecs.getincrementaldecoder('utf-8')(errors='strict')
        self.closed = False
        self.sendq = deque()

        self.state = HEADERB1

        # restrict the size of header and payload for security reasons
        self.maxheader = MAXHEADER
        self.maxpayload = MAXPAYLOAD

    def handle(self):
        """
          Called when websocket frame is received.
          To access the frame data call self.data.

          If the frame is Text then self.data is a unicode object.
          If the frame is Binary then self.data is a bytearray object.
        """
        pass

    def connected(self):
        """
          Called when a websocket client connects to the server.
        """
        pass

    def handle_close(self):
        """
          Called when a websocket server gets a Close frame from a client.
        """
        pass

    def _handle_packet(self):  # pylint: disable=too-many-branches, too-many-statements
        if self.opcode == CLOSE:
            pass
        elif self.opcode == STREAM:
            pass
        elif self.opcode == TEXT:
            pass
        elif self.opcode == BINARY:
            pass
        elif self.opcode in (PONG, PING):
            if len(self.data) > 125:
                raise Exception('control frame length can not be > 125')
        else:
            # unknown or reserved opcode so just close
            raise Exception('unknown opcode')

        if self.opcode == CLOSE:
            status = 1000
            reason = u''
            length = len(self.data)

            if length == 0:
                pass
            elif length >= 2:
                status = struct.unpack_from('!H', self.data[:2])[0]
                reason = self.data[2:]

                if status not in _VALID_STATUS_CODES:
                    status = 1002

                if reason:
                    try:
                        reason = reason.decode('utf8', errors='strict')
                    except Exception:  # pylint: disable=broad-except
                        status = 1002
            else:
                status = 1002

            self.close(status, reason)
        elif self.fin == 0:
            if self.opcode != STREAM:
                if self.opcode in (PING, PONG):
                    raise Exception('control messages can not be fragmented')

                self.frag_type = self.opcode
                self.frag_start = True
                self.frag_decoder.reset()

                if self.frag_type == TEXT:
                    self.frag_buffer = []
                    utf_str = self.frag_decoder.decode(self.data, final=False)
                    if utf_str:
                        self.frag_buffer.append(utf_str)
                else:
                    self.frag_buffer = bytearray()
                    self.frag_buffer.extend(self.data)
            else:
                if self.frag_start is False:
                    raise Exception('fragmentation protocol error')

                if self.frag_type == TEXT:
                    utf_str = self.frag_decoder.decode(self.data, final=False)
                    if utf_str:
                        self.frag_buffer.append(utf_str)
                else:
                    self.frag_buffer.extend(self.data)
        else:
            if self.opcode == STREAM:
                if self.frag_start is False:
                    raise Exception('fragmentation protocol error')

                if self.frag_type == TEXT:
                    utf_str = self.frag_decoder.decode(self.data, final=True)
                    self.frag_buffer.append(utf_str)
                    self.data = u''.join(self.frag_buffer)
                else:
                    self.frag_buffer.extend(self.data)
                    self.data = self.frag_buffer

                self.handle()

                self.frag_decoder.reset()
                self.frag_type = BINARY
                self.frag_start = False
                self.frag_buffer = None
            elif self.opcode == PING:
                self._send_message(False, PONG, self.data)
            elif self.opcode == PONG:
                pass
            else:
                if self.frag_start is True:
                    raise Exception('fragmentation protocol error')

                if self.opcode == TEXT:
                    try:
                        self.data = self.data.decode('utf8', errors='strict')
                    except Exception:
                        raise Exception('invalid utf-8 payload')

                self.handle()

    def _handle_data(self):
        # do the HTTP header and handshake
        if self.handshaked is False:
            try:
                data = self.client.recv(self.headertoread)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                # SSL socket not ready to read yet, wait and try again
                return
            if not data:
                raise Exception('remote socket closed')

            # accumulate
            self.headerbuffer.extend(data)

            if len(self.headerbuffer) >= self.maxheader:
                raise Exception('header exceeded allowable size')

            # indicates end of HTTP header
            if b'\r\n\r\n' in self.headerbuffer:
                self.request = HTTPRequest(self.headerbuffer)

                # handshake rfc 6455
                try:
                    key = self.request.headers['Sec-WebSocket-Key']
                    k = key.encode('ascii') + GUID_STR.encode('ascii')
                    k_s = base64.b64encode(hashlib.sha1(k).digest()).decode('ascii')
                    hs = HANDSHAKE_STR % {'acceptstr': k_s}
                    self.sendq.append((BINARY, hs.encode('ascii')))
                    self.handshaked = True
                    self.connected()
                except Exception as e:
                    hs = FAILED_HANDSHAKE_STR
                    self._send_buffer(hs.encode('ascii'), True)
                    self.client.close()
                    raise Exception('handshake failed: {}'.format(e))  # pylint: disable=consider-using-f-string
        else:
            try:
                data = self.client.recv(16384)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                # SSL socket not ready to read yet, wait and try again
                return
            if not data:
                raise Exception('remote socket closed')
            for d in data:
                self._parse_message(d)
            for d in data:
                self._parse_message(ord(d))

    def close(self, status=1000, reason=u''):
        """
           Send Close frame to the client. The underlying socket is only closed
           when the client acknowledges the Close frame.

           status is the closing identifier.
           reason is the reason for the close.
         """
        try:
            if self.closed is False:
                close_msg = bytearray()
                close_msg.extend(struct.pack("!H", status))
                if _check_unicode(reason):
                    close_msg.extend(reason.encode('utf-8'))
                else:
                    close_msg.extend(reason)

                self._send_message(False, CLOSE, close_msg)
        finally:
            self.closed = True

    def _send_buffer(self, buff, send_all=False):
        size = len(buff)
        tosend = size
        already_sent = 0

        while tosend > 0:
            try:
                # i should be able to send a bytearray
                sent = self.client.send(buff[already_sent:])
                if sent == 0:
                    raise RuntimeError('socket connection broken')

                already_sent += sent
                tosend -= sent
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                # SSL socket not ready to send yet, wait and try again
                if send_all:
                    continue
                return buff[already_sent:]
            except socket.error as e:
                # if we have full buffers then wait for them to drain and try again
                if e.errno in [errno.EAGAIN, errno.EWOULDBLOCK]:
                    if send_all:
                        continue

                    return buff[already_sent:]

                raise e

        return None

    def send_fragment_start(self, data):
        """
            Send the start of a data fragment stream to a websocket client.
            Subsequent data should be sent using sendFragment().
            A fragment stream is completed when sendFragmentEnd() is called.

            If data is a unicode object then the frame is sent as Text.
            If the data is a bytearray object then the frame is sent as Binary.
        """
        opcode = BINARY
        if _check_unicode(data):
            opcode = TEXT

        self._send_message(True, opcode, data)

    def send_fragment(self, data):
        """
            see sendFragmentStart()

            If data is a unicode object then the frame is sent as Text.
            If the data is a bytearray object then the frame is sent as Binary.
        """
        self._send_message(True, STREAM, data)

    def send_fragment_end(self, data):
        """
          see sendFragmentEnd()

          If data is a unicode object then the frame is sent as Text.
          If the data is a bytearray object then the frame is sent as Binary.
        """
        self._send_message(False, STREAM, data)

    def send_message(self, data):
        """
          Send websocket data frame to the client.

          If data is a unicode object then the frame is sent as Text.
          If the data is a bytearray object then the frame is sent as Binary.
        """
        opcode = BINARY
        if _check_unicode(data):
            opcode = TEXT

        self._send_message(False, opcode, data)

    def _send_message(self, fin, opcode, data):
        payload = bytearray()

        b1 = 0
        b2 = 0
        if fin is False:
            b1 |= 0x80

        b1 |= opcode

        if _check_unicode(data):
            data = data.encode('utf-8')

        length = len(data)
        payload.append(b1)

        if length <= 125:
            b2 |= length
            payload.append(b2)
        elif 126 <= length <= 65535:
            b2 |= 126
            payload.append(b2)
            payload.extend(struct.pack("!H", length))
        else:
            b2 |= 127
            payload.append(b2)
            payload.extend(struct.pack("!Q", length))

        if length > 0:
            payload.extend(data)

        self.sendq.append((opcode, payload))

    def _parse_message(self, byte):  # pylint: disable=too-many-branches, too-many-statements
        # read in the header
        if self.state == HEADERB1:
            self.fin = byte & 0x80
            self.opcode = byte & 0x0F
            self.state = HEADERB2

            self.index = 0
            self.length = 0
            self.lengtharray = bytearray()
            self.data = bytearray()

            rsv = byte & 0x70
            if rsv != 0:
                raise Exception('RSV bit must be 0')
        elif self.state == HEADERB2:
            mask = byte & 0x80
            length = byte & 0x7F

            if self.opcode == PING and length > 125:
                raise Exception('ping packet is too large')

            self.hasmask = mask == 128

            if length <= 125:
                self.length = length

                # if we have a mask we must read it
                if self.hasmask is True:
                    self.maskarray = bytearray()
                    self.state = MASK
                else:
                    # if there is no mask and no payload we are done
                    if self.length <= 0:
                        try:
                            self._handle_packet()
                        finally:
                            self.state = HEADERB1
                            self.data = bytearray()

                    # we have no mask and some payload
                    else:
                        # self.index = 0
                        self.data = bytearray()
                        self.state = PAYLOAD
            elif length == 126:
                self.lengtharray = bytearray()
                self.state = LENGTHSHORT
            elif length == 127:
                self.lengtharray = bytearray()
                self.state = LENGTHLONG
        elif self.state == LENGTHSHORT:
            self.lengtharray.append(byte)

            if len(self.lengtharray) > 2:
                raise Exception('short length exceeded allowable size')

            if len(self.lengtharray) == 2:
                self.length = struct.unpack_from('!H', self.lengtharray)[0]

                if self.hasmask is True:
                    self.maskarray = bytearray()
                    self.state = MASK
                else:
                    # if there is no mask and no payload we are done
                    if self.length <= 0:
                        try:
                            self._handle_packet()
                        finally:
                            self.state = HEADERB1
                            self.data = bytearray()

                    # we have no mask and some payload
                    else:
                        # self.index = 0
                        self.data = bytearray()
                        self.state = PAYLOAD
        elif self.state == LENGTHLONG:
            self.lengtharray.append(byte)

            if len(self.lengtharray) > 8:
                raise Exception('long length exceeded allowable size')

            if len(self.lengtharray) == 8:
                self.length = struct.unpack_from('!Q', self.lengtharray)[0]

                if self.hasmask is True:
                    self.maskarray = bytearray()
                    self.state = MASK
                else:
                    # if there is no mask and no payload we are done
                    if self.length <= 0:
                        try:
                            self._handle_packet()
                        finally:
                            self.state = HEADERB1
                            self.data = bytearray()

                    # we have no mask and some payload
                    else:
                        # self.index = 0
                        self.data = bytearray()
                        self.state = PAYLOAD

        # MASK STATE
        elif self.state == MASK:
            self.maskarray.append(byte)

            if len(self.maskarray) > 4:
                raise Exception('mask exceeded allowable size')

            if len(self.maskarray) == 4:
                # if there is no mask and no payload we are done
                if self.length <= 0:
                    try:
                        self._handle_packet()
                    finally:
                        self.state = HEADERB1
                        self.data = bytearray()

                # we have no mask and some payload
                else:
                    # self.index = 0
                    self.data = bytearray()
                    self.state = PAYLOAD

        # PAYLOAD STATE
        elif self.state == PAYLOAD:
            if self.hasmask is True:
                self.data.append(byte ^ self.maskarray[self.index % 4])
            else:
                self.data.append(byte)

            # if length exceeds allowable size then we except and remove the connection
            if len(self.data) >= self.maxpayload:
                raise Exception('payload exceeded allowable size')

            # check if we have processed length bytes; if so we are done
            if (self.index + 1) == self.length:
                try:
                    self._handle_packet()
                finally:
                    # self.index = 0
                    self.state = HEADERB1
                    self.data = bytearray()
            else:
                self.index += 1


class WebSocketServer(object):
    request_queue_size = 5
    closing = False

    # pylint: disable=too-many-arguments
    def __init__(self, host, port, websocketclass, certfile=None, keyfile=None,
                 ssl_version=ssl.PROTOCOL_TLSv1_2, select_interval=0.1, ssl_context=None):
        self.websocketclass = websocketclass
        if not host:
            host = '127.0.0.1'
        fam = socket.AF_INET6 if host is None else 0
        host_info = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, socket.AI_PASSIVE)
        self.serversocket = socket.socket(host_info[0][0], host_info[0][1], host_info[0][2])
        self.serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.serversocket.bind(host_info[0][4])
        self.serversocket.listen(self.request_queue_size)
        self.select_interval = select_interval
        self.connections = {}
        self.listeners = [self.serversocket]

        self._using_ssl = bool(ssl_context or (certfile and keyfile))
        if ssl_context is None and self._using_ssl:
            self.context = ssl.SSLContext(ssl_version)
            self.context.load_cert_chain(certfile, keyfile)
        else:
            self.context = ssl_context

    def _decorate_socket(self, sock):  # pylint: disable=no-self-use
        if self._using_ssl:
            return self.context.wrap_socket(sock, server_side=True)

        return sock

    def _construct_websocket(self, sock, address):
        ws = self.websocketclass(self, sock, address)
        if self._using_ssl:
            ws.usingssl = True

        return ws

    def close(self):
        self.closing = True
        self.serversocket.close()

        for desc, conn in self.connections.items():  # pylint: disable=unused-variable
            conn.close()
            self._handle_close(conn)

    def _handle_close(self, client):  # pylint: disable=no-self-use
        client.client.close()
        # only call handle_close when we have a successful websocket connection
        if client.handshaked:
            try:
                client.handle_close()
            except Exception:  # pylint: disable=broad-except
                pass

    def handle_request(self):  # pylint: disable=too-many-branches, too-many-statements, too-many-locals
        writers = []
        for fileno in self.listeners:
            if fileno == self.serversocket:
                continue
            client = self.connections[fileno]
            if client.sendq:
                writers.append(fileno)

        if self.select_interval:
            r_list, w_list, x_list = select(self.listeners, writers, self.listeners, self.select_interval)
        else:
            r_list, w_list, x_list = select(self.listeners, writers, self.listeners)

        for ready in w_list:
            client = self.connections[ready]
            try:
                while client.sendq:
                    opcode, payload = client.sendq.popleft()
                    remaining = client._send_buffer(payload)  # pylint: disable=protected-access
                    if remaining is not None:
                        client.sendq.appendleft((opcode, remaining))
                        break

                    if opcode == CLOSE:
                        raise Exception('received client close')

            except Exception:  # pylint: disable=broad-except
                self._handle_close(client)
                del self.connections[ready]
                self.listeners.remove(ready)

        for ready in r_list:
            if ready == self.serversocket:
                sock = None
                try:
                    sock, address = self.serversocket.accept()
                    newsock = self._decorate_socket(sock)
                    newsock.setblocking(False)  # pylint: disable=no-member
                    fileno = newsock.fileno()  # pylint: disable=no-member
                    self.connections[fileno] = self._construct_websocket(newsock, address)
                    self.listeners.append(fileno)
                except Exception:  # pylint: disable=broad-except
                    if sock is not None:
                        sock.close()
            else:
                if ready not in self.connections:
                    continue
                client = self.connections[ready]
                try:
                    client._handle_data()  # pylint: disable=protected-access
                except Exception:  # pylint: disable=broad-except
                    self._handle_close(client)
                    del self.connections[ready]
                    self.listeners.remove(ready)

        for failed in x_list:
            if failed == self.serversocket:
                self.close()
                raise Exception('server socket failed')

            if failed not in self.connections:
                continue
            client = self.connections[failed]
            self._handle_close(client)
            del self.connections[failed]
            self.listeners.remove(failed)

    def serve_forever(self):
        try:
            while True:
                self.handle_request()
        except Exception:
            if not self.closing:  # ignore errors if we are closing
                raise


class BroadcastHandler(WebSocket):
    clients = []

    @classmethod
    def broadcast(cls, o):
        s = json.dumps(o)
        for client in cls.clients:
            client.send_message(s)

    def connected(self):
        self.clients.append(self)

    def handle_close(self):
        self.clients.remove(self)


from injector import Act, Process, run_admin, enable_privilege


class ActWs(Act):
    def __init__(self):
        super().__init__()
        self.ws_server = WebSocketServer('', 24399, BroadcastHandler)
        self.ws_thread = threading.Thread(target=self.ws_server.serve_forever)

    def on_damage(self, source, target, damage, flags, action_id):
        BroadcastHandler.broadcast({
            'type': 'damage',
            'data': {
                'source': source,
                'target': target,
                'action_id': action_id,
                'damage': damage,
                'flags': flags
            }
        })

    def on_enter_area(self):
        BroadcastHandler.broadcast({
            'type': 'enter_area'
        })

    def install(self):
        super().install()
        self.ws_thread.start()

    def uninstall(self):
        self.ws_server.close()
        self.ws_thread.join()
        super().uninstall()


def injected_main():
    print(f'i am in pid={os.getpid()}')
    ActWs.reload()
    print('Act installed, if you want to reload, close the game and run this script again.')


def main():
    run_admin()
    enable_privilege()
    while True:
        try:
            process = Process.from_name('granblue_fantasy_relink.exe')
        except ValueError:
            print('granblue_fantasy_relink.exe not found, waiting...')
            time.sleep(5)
            continue
        break
    process.injector.wait_inject()
    process.injector.reg_std_out(lambda _, s: print(s, end=''))
    process.injector.reg_std_err(lambda _, s: print(s, end=''))
    process.injector.add_path(os.path.dirname(__file__))
    process.injector.run("import importlib;importlib.reload(__import__('injector'));importlib.reload(__import__('act_ws')).injected_main()")


if __name__ == '__main__':
    try:
        main()
    except:
        traceback.print_exc()
    finally:
        os.system('pause')
