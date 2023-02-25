"""
Micropython Socket.IO client.
"""

import ulogging as logging
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

def send_header(sock, header, *args):
    if __debug__:
        LOGGER.debug(str(header), *args)

    sock.write(header % args + '\r\n')

def _send_message_connect(sock, hostname, port, path):
    send_header(sock, b'POST %s HTTP/1.1', path)
    send_header(sock, b'Host: %s', hostname)
    send_header(sock, b'Content-Type: text/plain;charset=UTF-8')
    send_header(sock, b'Content-Length: 2')
    send_header(sock, b'')
    send_header(sock, b'40')

    header = sock.readline()[:-2]
    assert header == b'HTTP/1.1 200 OK', header

    length = 0

    while header:
        header = sock.readline()[:-2]
        if not header:
            break

        header, value = header.split(b': ')
        header = header.lower()
        if header == b'content-length':
            length = int(value)

    return sock.read(length)

def _end_connection(sock, hostname, port, path):
    send_header(sock, b'GET %s HTTP/1.1', path)
    send_header(sock, b'Host: %s', hostname)
    send_header(sock, b'')

    header = sock.readline()[:-2]
    assert header == b'HTTP/1.1 200 OK', header
    
    length = 0

    while header:
        header = sock.readline()[:-2]
        if not header:
            break

        header, value = header.split(b': ')
        header = header.lower()
        if header == b'content-length':
            length = int(value)

    data = sock.read(length)
    return decode_payload(data)

def _connect(sock, hostname, port, path):
    send_header(sock, b'GET %s HTTP/1.1', path)
    send_header(sock, b'Host: %s', hostname)
    send_header(sock, b'')

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
            assert value == b'text/plain; charset=UTF-8'
        elif header == b'content-length':
            length = int(value)

    assert length

    data = sock.read(length)
    return decode_payload(data)


def _connect_http(hostname, port, path, then):
    """Stage 1 do the HTTP connection to get our SID"""
    try:
        sock = socket.socket()
        addr = socket.getaddrinfo(hostname, port)
        sock.connect(addr[0][4])
        return then(sock, hostname, port, path)
    finally:
        sock.close()

def connect(uri, query=""):
    """Connect to a socket IO server."""
    uri = urlparse(uri)

    assert uri

    path = uri.path or '/' + 'socket.io/?EIO=4'

    if query:
        path += "&" + query

    # Start a connection, which will give us an SID to use to upgrade
    # the websockets connection
    packets = _connect_http(uri.hostname, uri.port, path, _connect)

    # The first packet should open the connection,
    # following packets might be initialisation messages for us
    packet_type, params = next(packets)

    assert packet_type == PACKET_OPEN
    params = json.loads(params)
    LOGGER.debug("Websocket parameters = %s", params)

    assert 'websocket' in params['upgrades']

    sid = params['sid']
    path += '&sid={}'.format(sid)

    # Do a POST message connect
    _ = _connect_http(uri.hostname, uri.port, path, _send_message_connect)

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
    res = _connect_http(uri.hostname, uri.port, path + '&transport=polling', _connect)
    
    assert next(res) == (PACKET_NOOP, ''), res

    # We should receive an answer to our probe
    packet = socketio._recv()
    assert packet == (PACKET_PONG, 'probe')
    
    # Send another follow-up poll?
    packet = _connect_http(uri.hostname, uri.port, path + '&transport=polling', _end_connection)
    
    assert next(packet) == (PACKET_NOOP, ''), packet

    # Upgrade the connection
    socketio._send_packet(PACKET_UPGRADE)
    
    # TODO: timeout after 25s?
    while socketio._recv() != (PACKET_NOOP, ''):
        print("CROQUETTE!")

    return socketio
