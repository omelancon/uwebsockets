"""
Micropython Socket.IO client.
"""

import logging
import ure as re
import ujson as json
import ussl as ssl
import usocket as socket
from ucollections import namedtuple

from .protocol import *
from .transport import SocketIO

LOGGER = logging.getLogger(__name__)

URL_RE = re.compile(r'(https?)://([A-Za-z0-9\-\.]+)(?:\:([0-9]+))?(/.+)?')
URI = namedtuple('URI', ('protocol', 'hostname', 'port', 'path'))


def urlparse(uri):
    """Parse http:// URLs"""
    match = URL_RE.match(uri)
    if match:
        return URI(match.group(1), match.group(2), int(match.group(3)), match.group(4))


def _connect(sock, hostname, port, path):
    def send_header(header, *args):
        if __debug__:
            LOGGER.debug(str(header), *args)

        sock.write(header % args + '\r\n')

    send_header(b'GET %s HTTP/1.1', path)
    send_header(b'Host: %s:%s', hostname, port)
    send_header(b'')

    header = sock.readline()[:-2]
    assert header == b'HTTP/1.1 200 OK', header

    length = None

    while header:
        header = sock.readline()[:-2]
        if not header:
            break

        header, value = header.split(b': ')
        header = header.lower()
        if header == b'content-type':
            assert value == b'application/octet-stream'
        elif header == b'content-length':
            length = int(value)

    assert length

    data = sock.read(length)
    return decode_payload(data)


def _connect_http(hostname, port, path):
    """Stage 1 do the HTTP connection to get our SID"""
    try:
        sock = socket.socket()
        addr = socket.getaddrinfo(hostname, port)
        sock.connect(addr[0][4])
        return _connect(sock, hostname, port, path)
    finally:
        sock.close()


def _connect_https(hostname, port, path):
    """Stage 1 do the HTTPS connection to get our SID"""
    try:
        sock = socket.socket()
        addr = socket.getaddrinfo(hostname, port)
        ssock = ssl.wrap_socket(sock)
        ssock.connect(addr[0][4])
        return _connect(ssock, hostname, port, path)
    finally:
        ssock.close()


def connect(uri, query=""):
    """Connect to a socket IO server."""
    uri = urlparse(uri)

    assert uri

    path = uri.path or '/' + 'socket.io/?EIO=4'

    if query:
        path += "&" + query

    # Start a connection, which will give us an SID to use to upgrade
    # the websockets connection
    do_connect = _connect_http if path.protocol == 'http' else _connect_https
    
    packets = do_connect(uri.hostname, uri.port, path)

    # The first packet should open the connection,
    # following packets might be initialisation messages for us
    packet_type, params = next(packets)

    assert packet_type == PACKET_OPEN
    params = json.loads(params)
    LOGGER.debug("Websocket parameters = %s", params)

    assert 'websocket' in params['upgrades']

    sid = params['sid']
    path += '&sid={}'.format(sid)

    if __debug__:
        LOGGER.debug("Connecting to websocket SID %s", sid)

    # Start a websocket and send a probe on it
    ws_uri = 'ws://{hostname}:{port}{path}&transport=websocket'.format(
        hostname=uri.hostname,
        port=uri.port,
        path=path)

    socketio = SocketIO(ws_uri, **params)

    # handle rest of the packets once we're in the main loop
    @socketio.on('connection')
    def on_connect(data):
        for packet_type, data in packets:
            socketio._handle_packet(packet_type, data)

    socketio._send_packet(PACKET_PING, 'probe')

    # Send a follow-up poll
    do_connect(uri.hostname, uri.port, path + '&transport=polling')

    # We should receive an answer to our probe
    packet = socketio._recv()
    assert packet == (PACKET_PONG, 'probe')

    # Upgrade the connection
    socketio._send_packet(PACKET_UPGRADE)
    packet = socketio._recv()
    assert packet == (PACKET_NOOP, '')

    return socketio
