"""Race Result System decoder interface."""

from __future__ import division

import Queue
import logging
import socket
import datetime

from . import (decoder, DECODER_LOG_LEVEL)
from metarace import tod

LOG = logging.getLogger(u'metarace.decoder.rrs')
LOG.setLevel(logging.DEBUG)

RRS_TCP_PORT = 3601
RRS_PROTOCOL = u'3.0'  # desired target protocol level
RRS_PASSLEN = 20  # passing record length
RRS_LOWBATT = 2.0  # Warn if battery voltage is below this many volts
RRS_SYNFMT = u'SETTIME;{:04d}-{:02d}-{:02d};{:02d}:{:02d}:{:02d}.{:03d}'
RRS_EOL = u'\r\n'
RRS_MARKER = u'99999'
RRS_ENCODING = u'iso8859-1'
RRS_IOTIMEOUT = 1.0  # Socket I/O timeout in seconds


class rrs(decoder):
    """RRS thread object class."""

    def __init__(self):
        decoder.__init__(self)
        self._io = None
        self._rdbuf = b''
        self._curfile = None
        self._lastpassing = None
        self._dorefetch = True
        self._fetchpending = False
        self._pending_command = None
        self._allowstored = False

    # API overrides
    def sync(self, data=None):
        self.stop_session(data)
        self._cqueue.put_nowait((u'_sync', data))
        self.start_session(data)

    def start_session(self, data=None):
        self.write(u'STARTOPERATION')
        self.write(u'PASSINGS')

    def stop_session(self, data=None):
        self.write(u'STOPOPERATION')

    def status(self, data=None):
        self.write(u'GETSTATUS')

    def clear(self, data=None):
        self.stop_session(data)
        self.write(u'CLEARFILES')
        self.start_session(data)

    # Device-specific functions
    def _close(self):
        if self._io is not None:
            LOG.debug(u'Close connection')
            cp = self._io
            self._io = None
            try:
                cp.shutdown(socket.SHUT_RDWR)
            except Exception as e:
                LOG.debug(u'%s: shutdown socket: %s', e.__class__.__name__, e)
            cp.close()

    def _sane(self, data=None):
        if sysconf.has_option(u'rrs', u'allowstored'):
            self._allowstored = strops.confopt_bool(
                sysconf.get(u'rrs', u'allowstored'))
            LOG.info(u'Allow stored passings: %r', self._allowstored)
        for m in [
                u'GETPROTOCOL',
                u'SETPROTOCOL;{}'.format(RRS_PROTOCOL),
                u'SETPUSHPREWARNS;0',
                u'SETPUSHPASSINGS;1;0',
                u'GETCONFIG;GENERAL;BOXNAME',
                u'GETSTATUS',
                u'PASSINGS',
        ]:
            self.write(m)

    def _port(self, port):
        """Re-establish connection to supplied device port."""
        self._close()
        addr = (port, RRS_TCP_PORT)
        LOG.debug(u'Connecting to %r', addr)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(RRS_IOTIMEOUT)
        s.connect(addr)
        self._io = s
        self.sane()

    def _sync(self, data=None):
        a = datetime.datetime.now()
        syncmd = RRS_SYNFMT.format(a.year, a.month, a.day, a.hour, a.minute,
                                   a.second, a.microsecond // 1000)
        self._write(syncmd)

    def _replay(self, file):
        """RRS-specific replay"""
        if file.isdigit():
            LOG.debug(u'Replay passings from %r', file)
            cmd = u'GETFILE;{:d}'.format(file)
            self._write(cmd)
        else:
            LOG.error(u'Invalid file specified: %r', file)

    def _write(self, msg):
        if self._io is not None:
            ob = (msg + RRS_EOL)
            self._io.sendall(ob.encode(RRS_ENCODING))
            #LOG.debug(u'SEND: %r', ob)

    def _passing(self, pv):
        """Process a RRS protocol passing."""
        if len(pv) == RRS_PASSLEN:
            today = datetime.date.today().isoformat()
            istr = pv[0]  # <PassingNo>
            tagid = pv[1]  # <Bib/TranspCode>
            date = pv[2]  # <Date>
            timestr = pv[3]  # <Time>
            loopid = pv[10]  # <LoopID>
            wuc = pv[12]  # <WakeupCounter>
            battery = pv[13]  # <Battery>
            adata = pv[15]  # <InternalActiveData>
            bname = pv[16]  # <BoxName>
            hits = pv[5]  # <Hits>
            rssi = pv[6]  # <MaxRSSI>

            # An error here will invalidate the whole passing
            pid = int(istr)
            if self._lastpassing is not None:
                expectpid = self._lastpassing + 1
                if pid != expectpid:
                    LOG.debug(u'Ignore out of sequence passing: %r != %r', pid,
                              expectpid)
                    return
            self._lastpassing = pid

            if not loopid:
                loopid = u'1'  # add fake loop id for passives
            activestore = False
            if adata:
                activestore = (int(adata) & 0x40) == 0x40
            if tagid.startswith(RRS_MARKER):
                tagid = u''

            if battery and tagid:
                try:
                    bv = float(battery)
                    if bv < RRS_LOWBATT:
                        LOG.warning(u'Low battery %s: %0.1fV', tagid, bv)
                except Exception as e:
                    LOG.debug(u'%s reading battery voltage: %s',
                              e.__class__.__name__, e)

            if hits and rssi and tagid:
                try:
                    hitcount = int(hits, 16)
                    rssival = int(rssi, 16)
                    twofour = -90 + ((rssival & 0x70) >> 2)
                    lstrength = 1 + (rssival & 0x0f)
                    if lstrength < 5 or twofour < -82 or hitcount < 4:
                        LOG.warning(
                            u'Poor read %s: Hits:%d RSSI:%ddBm Loop:%ddB',
                            tagid, hitcount, twofour, lstrength)
                except Exception as e:
                    LOG.debug(u'%s reading hits/RSSI: %s',
                              e.__class__.__name__, e)

            # emit a decoder log line TBD
            LOG.log(DECODER_LOG_LEVEL, u';'.join(pv))

            # accept valid passings and trigger callback
            t = tod.mktod(timestr)
            if t is not None:
                t.index = istr
                t.chan = loopid
                t.refid = tagid
                t.source = bname
                if not activestore or self._allowstored:
                    self._trig(t)
                else:
                    pass
        else:
            LOG.info(u'Non-passing message: %r', pv)

    def _statusmsg(self, pv):
        """Process a RRS protocol status message."""
        if len(pv) > 10:
            pwr = pv[2]  # <HasPower>
            opmode = pv[4]  # <IsInOperationMode>
            uhf = pv[8]  # <ReaderIsHealthy>
            batt = pv[9]  # <BatteryCharge>
            if batt == u'-1':
                batt = u'[estimating]'
            else:
                batt += u'%'

            if opmode == u'1':
                LOG.info(u'Started, pwr:%r, uhf:%r, batt:%s', pwr, uhf, batt)
            else:
                LOG.warning(u'Not started, pwr:%r, uhf:%r, batt:%s', pwr, uhf,
                            batt)

    def _configmsg(self, pv):
        """Process a RRS protocol config message."""
        if len(pv) > 3:
            if pv[1] == u'BOXNAME':
                boxid = pv[3]  # <DeviceId>
                LOG.info(u'%r connected', boxid)

    def _protocolmsg(self, pv):
        """Respond to protocol message from decoder."""
        if len(pv) == 3:
            if RRS_PROTOCOL > pv[2]:
                LOG.error(u'Protocol %r unsupported (max %r), update firmware',
                          RRS_PROTOCOL, pv[2])

    def _passingsmsg(self, pv):
        """Handle update of current passing count."""
        try:
            newfile = None
            if len(pv) > 1:
                newfile = int(pv[1])
            newidx = int(pv[0])
            if newfile != self._curfile:
                LOG.debug(u'New passing file %r', newfile)
                self._curfile = newfile
                self._lastpassing = newidx
            else:
                if self._lastpassing is None or newidx < self._lastpassing:
                    # assume new connection or new file
                    LOG.debug(u'Last passing %r updated to %r',
                              self._lastpassing, newidx)
                    self._lastpassing = newidx
                else:
                    # assume a broken connection and fetch missed passings
                    LOG.debug(u'Missed %r passings, last passing = %r',
                              newidx - self._lastpassing, self._lastpassing)
            self._fetchpending = False
        except Exception as e:
            LOG.debug(u'%s reading passing count: %s', e.__class__.__name__, e)

    def _procmsg(self, msg):
        """Process a decoder response message."""
        #LOG.debug(u'RECV: %r', msg)
        mv = msg.strip().split(u';')
        if mv[0].isdigit():  # Requested passing
            self._pending_command = u'PASSING'
            self._passing(mv)
        elif mv[0] == u'#P':  # Pushed passing
            self._passing(mv[1:])
        elif mv[0] == u'GETSTATUS':
            self._statusmsg(mv[1:])
        elif mv[0] == u'GETCONFIG':
            self._configmsg(mv[1:])
        elif mv[0] == u'GETPROTOCOL':
            self._protocolmsg(mv[1:])
        elif mv[0] == u'PASSINGS':
            self._passingsmsg(mv[1:])
        elif mv[0] == u'SETTIME':
            LOG.info(u'Time set to: %r %r', mv[1], mv[2])
        elif mv[0] == u'STARTOPERATION':
            self._curfile = None
            self._lastpassing = None
            LOG.info(u'Start session')
        elif mv[0] == u'STOPOPERATION':
            self._curfile = None
            self._lastpassing = None
            LOG.info(u'Stop session')
        elif mv[0].startswith(u'ONLY '):
            self._fetchpending = False
        elif mv[0] == u'':
            LOG.debug(u'End of requested passings')
            self._fetchpending = False
            self._pending_command = None
            self._dorefetch = True
        else:
            pass  # Ignore other responses

    def _procline(self):
        """Read and process whole line from decoder, return command status."""
        idx = self._rdbuf.find(b'\n')
        if idx < 0:
            inb = self._io.recv(512)
            if inb == b'':
                LOG.info(u'Connection closed by peer')
                self._close()
            else:
                self._rdbuf += inb
            idx = self._rdbuf.find(b'\n')
        if idx >= 0:
            l = self._rdbuf[0:idx + 1].decode(RRS_ENCODING)
            self._rdbuf = self._rdbuf[idx + 1:]
            self._procmsg(l)
        return self._pending_command is None

    def _refetch(self):
        """Poll decoder for new passings."""
        if not self._fetchpending:
            if self._dorefetch and self._lastpassing is not None:
                self._fetchpending = True
                self._dorefetch = False
                cmd = u'{:d}:32'.format(self._lastpassing + 1)
                self.write(cmd)

    def run(self):
        """Decoder main loop."""
        LOG.debug(u'Starting')
        self._running = True
        while self._running:
            try:
                c = None
                if self._io is not None:
                    # Read responses until response complete or timeout
                    try:
                        while not self._procline():
                            pass
                    except socket.timeout:
                        self._dorefetch = True
                    self._refetch()
                    c = self._cqueue.get_nowait()
                else:
                    c = self._cqueue.get()
                self._cqueue.task_done()
                self._proccmd(c)
            except Queue.Empty:
                pass
            except socket.error as e:
                self._close()
                LOG.error(u'%s: %s', e.__class__.__name__, e)
            except Exception as e:
                LOG.error(u'%s: %s', e.__class__.__name__, e)
        self.setcb()
        LOG.debug(u'Exiting')
