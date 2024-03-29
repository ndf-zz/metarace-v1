"""DHI scoreboard sender class.

This module provides a thread object which collects
and dispatches DHI messages intended for a Galactica or
Caprica scoreboard.

"""

import threading
import Queue
import logging
import socket
import metarace
import serial

from metarace import unt4
from metarace import strops

# Caprica encoding is UTF-8, Galactica is undefined - probably CP1252
ENCODING = 'utf-8'
LINELEN = 24
PAGELEN = 7
SERIALBAUD = 115200

# dispatch thread queue commands
TCMDS = (u'EXIT', u'PORT', u'MSG')

# module log object
_log = logging.getLogger(u'metarace.sender')
_log.setLevel(logging.DEBUG)


class serialport(object):
    """Scoreboard communication port object."""

    def __init__(self, addr, baudrate):
        """Constructor.

        Parameters:

          addr -- serial device filename
          baudrate -- serial line speed

        """
        _log.debug(u'Serial connection %s @ %d baud.', addr, baudrate)
        self.__s = serial.Serial(addr, baudrate, rtscts=False)
        self.send = self.__s.write
        self.running = True

    def sendall(self, buf):
        """Send all of buf to port."""
        msglen = len(buf)
        sent = 0
        while sent < msglen:
            out = self.send(buf[sent:])
            sent += out

    def close(self):
        """Shutdown socket object."""
        self.running = False
        try:
            self.__s.close()
        except:
            pass


class scbport(object):
    """Scoreboard communication port object."""

    def __init__(self, addr, protocol):
        """Constructor.

        Parameters:

          addr -- socket style 2-tuple (host, port)
          protocol -- one of socket.SOCK_STREAM or socket.SOCK_DGRAM

        """
        self.__s = socket.socket(socket.AF_INET, protocol)
        if protocol == socket.SOCK_STREAM:
            # set the TCP 'no delay' option
            self.__s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _log.debug(u'Opening TCP socket %s', repr(addr))
        else:  # assume Datagram (UDP)
            # enable broadcast send
            self.__s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            _log.debug(u'Opening UDP socket %s', repr(addr))
        self.__s.connect(addr)
        self.send = self.__s.send
        self.running = True

    def sendall(self, buf):
        """Send all of buf to port."""
        msglen = len(buf)
        sent = 0
        #_log.debug(u'Sending: %r', buf)
        while sent < msglen:
            out = self.send(buf[sent:])
            if out == 0:
                raise socket.error(u'DHI sender socket broken')
            sent += out

    def close(self):
        """Shutdown socket object."""
        self.running = False
        try:
            self.__s.shutdown(socket.SHUT_RDWR)
        except:
            pass


def mkport(port=None):
    """Create a new scbport object.

    port is a string specifying the address as follows:

        [PROTOCOL:]ADDRESS[:PORT]

    Where:

        PROTOCOL :: TCP or UDP	(optional)
        ADDRESS :: hostname or IP address or device file
        PORT :: port name or number (optional)

    """
    nprot = socket.SOCK_STREAM  # default is TCP
    naddr = u'localhost'  # default is localhost
    nport = 1946  # default is 1946 (Caprica DHI)

    # import system defaults if required
    if metarace.sysconf.has_option(u'sender', u'portspec'):
        if not port or port == u'DEFAULT':
            port = metarace.sysconf.get(u'sender', u'portspec')

    if port == u'DEBUG':  # force use of the hardcoded UDP endpoint
        _log.debug(u'Using debug port: UDP:localhost:5060')
        nprot = socket.SOCK_DGRAM
        naddr = u'localhost'
        nport = 5060
    else:
        vels = [u'TCP', u'localhost', u'1946']
        aels = port.translate(strops.PRINT_UTRANS).strip().split(u':')
        if len(aels) == 3:
            vels[0] = aels[0].upper()
            vels[1] = aels[1]
            vels[2] = aels[2]
        elif len(aels) == 2:
            if aels[0].upper() in [u'TCP', u'UDP']:
                # assume PROT:ADDR
                vels[0] = aels[0].upper()
                vels[1] = aels[1]
            else:
                vels[1] = aels[0]
                vels[2] = aels[1]
        elif len(aels) == 1:
            vels[1] = aels[0]
        else:
            raise socket.error(u'Invalid port specification string')

        # 'import' the vels...
        if vels[0] == u'TCP':
            nprot = socket.SOCK_STREAM
            nport = 1946
        elif vels[0] == u'UDP':
            nprot = socket.SOCK_DGRAM
            nport = 5060
        else:
            raise socket.error(u'Invalid protocol specified.')
        naddr = vels[1]
        # override port if supplied
        if vels[2].isdigit():
            nport = int(vels[2])
        else:
            nport = socket.getservbyname(vels[2])

    # split port string into [PROTOCOL:]ADDR[:PORT]
    if u'/dev/' in naddr:
        # assume device file for a serial port
        baud = SERIALBAUD
        if metarace.sysconf.has_option(u'sender', u'serialbaud'):
            baud = strops.confopt_posint(
                metarace.sysconf.get(u'sender', u'serialbaud'), SERIALBAUD)
        return serialport(naddr, baud)
    else:
        return scbport((naddr, nport), nprot)


def mksender(port=None):
    cols = LINELEN
    rows = PAGELEN
    encoding = ENCODING
    if metarace.sysconf.has_option(u'sender', u'linelen'):
        cols = metarace.sysconf.get(u'sender', u'linelen')
    if metarace.sysconf.has_option(u'sender', u'pagelen'):
        rows = metarace.sysconf.get(u'sender', u'pagelen')
    if metarace.sysconf.has_option(u'sender', u'encoding'):
        encoding = metarace.sysconf.get(u'sender', u'encoding')
    ret = sender(port)
    ret.linelen = cols
    ret.pagelen = rows
    ret.encoding = encoding
    return ret


class sender(threading.Thread):
    """Caprica/Galactica DHI sender thread."""

    def clrall(self):
        """Clear all lines in DHI database."""
        self.sendmsg(unt4.GENERAL_CLEARING)

    def clrline(self, line):
        """Clear the specified line in DHI database."""
        self.sendmsg(unt4.unt4(xx=0, yy=int(line), erl=True))

    def setline(self, line, msg):
        """Set the specified DHI database line to msg."""
        msg = strops.truncpad(msg, self.linelen, u'l', False)
        self.sendmsg(unt4.unt4(xx=0, yy=int(line), erl=True, text=msg))

    def flush(self):
        """Send an empty update to force timeout clock to zero."""
        self.sendmsg(unt4.GENERAL_EMPTY)

    def linefill(self, line, char=u'_'):
        """Use char to fill the specified line."""
        msg = char * self.linelen
        self.sendmsg(unt4.unt4(xx=0, yy=int(line), text=msg))

    def postxt(self, line, oft, msg):
        """Position msg at oft on line in DHI database."""
        self.sendmsg(unt4.unt4(xx=int(oft), yy=int(line), text=msg))

    def setoverlay(self, newov):
        """Request overlay newov to be displayed on the scoreboard."""
        if self.curov != newov:
            self.sendmsg(newov)
            self.curov = newov

    def __init__(self, port=None):
        """Constructor."""
        threading.Thread.__init__(self)
        self.daemon = True
        self.name = u'sender'
        self.port = None
        self.linelen = LINELEN  # overridden by mksender
        self.pagelen = PAGELEN  # overridden by mksender
        self.encoding = ENCODING  # overridden by mksender
        self.ignore = False
        self.curov = None
        self.queue = Queue.Queue()
        self.running = False
        if port is not None:
            self.setport(port)

    def sendmsg(self, unt4msg=None):
        """Pack and send a unt4 message to the DHI."""
        self.queue.put_nowait((u'MSG', unt4msg.pack()))

    def write(self, msg=None):
        """Send the provided raw msg to the DHI."""
        self.queue.put_nowait((u'MSG', msg))

    def exit(self, msg=None):
        """Request thread termination."""
        self.running = False
        self.queue.put_nowait((u'EXIT', msg))

    def wait(self):
        """Suspend calling thread until cqueue is empty."""
        self.queue.join()

    def setport(self, port=None):
        """Dump command queue contents and (re)open DHI port.

        Specify hostname and port for TCP connection as follows:

            tcp:hostname:16372

        Or use DEBUG for a fallback UDP socket:

	    UDP:localhost:5060

        """
        try:
            while True:
                self.queue.get_nowait()
                self.queue.task_done()
        except Queue.Empty:
            pass
        self.queue.put_nowait((u'PORT', port))

    def set_ignore(self, ignval=False):
        """Set or clear the ignore flag."""
        self.ignore = bool(ignval)

    def connected(self):
        """Return true if SCB connected."""
        return self.port is not None and self.port.running

    def run(self):
        self.running = True
        _log.debug(u'Starting')
        while self.running:
            m = self.queue.get()
            self.queue.task_done()
            try:
                if m[0] == u'MSG' and not self.ignore and self.port:
                    #_log.debug(u'Sending message : ' + repr(m[1]))
                    self.port.sendall(m[1].encode(self.encoding, 'replace'))
                elif m[0] == u'EXIT':
                    _log.debug(u'Request to close: %s', m[1])
                    self.running = False
                elif m[0] == u'PORT':
                    if self.port is not None:
                        self.port.close()
                        self.port = None
                    if m[1] not in [None, u'', u'none', u'NULL']:
                        _log.debug(u'Re-Connect port: %s', m[1])
                        self.port = mkport(m[1])
                        self.curov = None
                    else:
                        _log.debug(u'Not connected.')

            except IOError as e:
                _log.error(u'IO Error: %s', e)
                if self.port is not None:
                    self.port.close()
                self.port = None
            except Exception as e:
                _log.error(u'%s: %s', e.__class__.__name__, e)
        if self.port is not None:
            self.port.close()
        _log.info(u'Exiting')
