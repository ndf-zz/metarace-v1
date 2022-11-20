"""Race Result USB Decoder interface."""

from __future__ import division

import threading
import Queue
import decimal
import logging
import serial
import time

from . import (decoder, DECODER_LOG_LEVEL)
from metarace import tod
from metarace import sysconf
from metarace import strops

LOG = logging.getLogger(u'metarace.decoder.rru')
LOG.setLevel(logging.DEBUG)

RRU_BAUD = 19200
RRU_PASSLEN = 12
RRU_BEACONLEN = 17
RRU_LOWBATT = 2.1  # Warn if battery voltage is below this many volts
RRU_REFCHECK = 1800  # check ref after this many timeouts
RRU_REFTHRESH = 0x2a30000  # 2 x 86400 x 256
RRU_ENCODING = u'iso8859-1'
RRU_MARKER = u'_____127'  # trigger marker
RRU_EOL = u'\n'
# Serial port I/O read timeout in seconds
RRU_IOTIMEOUT = 1.0
# List of handled responses from decoder
REPLIES = [
    u'ASCII',
    u'CONFSET',
    u'CONFGET',
    u'INFOGET',
    u'SITESURVEY',
    u'TIMESTAMPGET',
    u'EPOCHREFGET',
    u'EPOCHREFSET',
    u'EPOCHREFADJ1D',
    u'PASSINGGET',
    u'PASSINGINFOGET',
    u'BEACONGET',
    u'PREWARN',
]
# Documented configuration parameter. If default is not None, the
# value will be set in _sane().
CONFINFO = {
    u'01': {
        u'label': u'Push Pre-Warn',
        u'default': None,
        u'00': u'disabled',
        u'01': u'enabled'
    },
    u'02': {
        u'label': u'Blink/beep on repeated passing',
        u'default': None,
        u'00': u'disabled',
        u'01': u'enabled'
    },
    u'03': {
        u'label': u'Impulse input or beep output',
        u'default': None,
        u'00': u'impulse-in',
        u'01': u'beep-out'
    },
    u'04': {
        u'label': u'Auto-shutdown on power loss',
        # force disabled since USB cable connection often fails
        u'default': u'00',
        u'00': u'disabled',
        u'01': u'enabled'
    },
    u'05': {
        u'label': u'Operation Mode',
        # assume usb-timing is required unless explicity configured otherwise
        u'default': u'06',
        u'05': u'usb-kiosk',
        u'06': u'usb-timing',
        u'07': u'usb-store&copy'
    },
    u'06': {
        u'label': u'Channel ID',
        u'default': None,
        u'00': u'1',
        u'01': u'2',
        u'02': u'3',
        u'03': u'4',
        u'04': u'5',
        u'05': u'6',
        u'06': u'7',
        u'07': u'8'
    },
    u'07': {
        u'label': u'Loop ID',
        u'default': None,
        u'00': u'1',
        u'01': u'2',
        u'02': u'3',
        u'03': u'4',
        u'04': u'5',
        u'05': u'6',
        u'06': u'7',
        u'07': u'8'
    },
    u'08': {
        u'label': u'Loop Power',
        u'default': None
    },
    u'09': {
        u'label': u'Blink dead-time',
        u'default': None
    },
    u'0a': {
        u'label': u'Charging via USB',
        u'default': None,
        u'00': u'disabled',
        u'01': u'enabled'
    },
    u'0b': {
        u'label': u'Use DTR',
        # always use DTR, unless configured otherwise
        u'default': u'01',
        u'00': u'disabled',
        u'01': u'enabled'
    },
    u'0c': {
        u'label': u'Alternate Channel Switching',
        u'default': None,
        u'00': u'disabled',
        u'01': u'automatic',
        u'02': u'force'
    },
    u'0d': {
        u'label': u'Box Mode',
        u'default': None,
        u'31': u'check',
        u'32': u'deep-sleep',
        u'33': u'health-check',
        u'34': u'tracking',
        u'41': u'usb-timing',
    },
    u'a0': {
        u'label': u'Tray Scan Power',
        u'default': None,
    },
    u'a1': {
        u'label': u'Tray Scan interval',
        u'default': None,
    },
    u'a2': {
        u'label': u'Tray Scan ramp up delay',
        u'default': None,
    },
    u'a3': {
        u'label': u'Tray Scan row to column delay',
        u'default': None,
    },
    u'a4': {
        u'label': u'Repeat Row and Column cycle for another N repetition',
        u'default': None,
    },
    u'b1': {
        u'label': u'CheckSum',
        u'default': None,
        u'0': u'disabled',
        u'1': u'enabled',
    },
    u'b2': {
        u'label': u'Push Passings',
        u'default': None,
        u'0': u'disabled',
        u'1': u'enabled',
    }
}
# Labels for decoder information options
INFOLBLS = {
    u'01': u'Decoder ID',
    u'02': u'Firmware Major Version',
    u'03': u'Hardware Version',
    u'04': u'Box Type',
    u'05': u'Battery Voltage',
    u'07': u'Battery State',
    u'08': u'Battery Level',
    u'09': u'Internal Temperature',
    u'0a': u'Supply Voltage',
    u'0b': u'Loop Status',
    u'0c': u'Firmware Minor Version',
}
BOX_TYPES = {
    u'0a': u'active-ext',
    u'1e': u'management-box',
    u'28': u'usb-timing-box'
}
BATTERY_STATES = {
    u'00': u'Fault',
    u'01': u'Charging',
    u'02': u'Reduced Charging',
    u'03': u'Discharging'
}
LOOP_STATES = {
    u'00': u'OK',
    u'01': u'Fault',
    u'02': u'Limit',
    u'03': u'Overvoltage Error'
}


class rru(decoder):
    """Race Result USB Active thread object class."""

    def __init__(self):
        decoder.__init__(self)
        self._sitenoise = {
            1: 100,
            2: 100,
            3: 100,
            4: 100,
            5: 100,
            6: 100,
            7: 100,
            8: 100
        }
        self._config = {}
        self._boxname = None
        self._rrustamp = None
        self._rruht = None
        self._error = False
        self._io = None
        self._rdbuf = b''
        self._curreply = None  # current multi-line response mode
        self._lastpassing = 0
        self._lastrequest = None
        self._request_pending = False
        self._allowstored = False
        self._refcount = 0

    # API overrides
    def setport(self, device=None):
        """Request new device address."""
        self._request_pending = False
        self._rrustamp = None
        self._rruht = None
        self._flush()
        self._cqueue.put_nowait((u'_port', device))

    def status(self):
        """Request status info from decoder."""
        for c in sorted(CONFINFO):
            self.write(u''.join([u'CONFGET;', c]))
        for c in sorted(INFOLBLS):
            self.write(u''.join([u'INFOGET;', c]))
        self.write(u'BEACONGET')
        self.write(u'PASSINGINFOGET')
        self.write(u'TIMESTAMPGET')

    def connected(self):
        return self._boxname is not None and self._io is not None

    def clear(self, data=None):
        """Clear internal passing memory."""
        self._cqueue.put_nowait((u'_reset', data))

    # Device-specific functions
    def _close(self):
        self._boxname = None
        if self._io is not None:
            LOG.debug(u'Close connection')
            cp = self._io
            self._io = None
            cp.close()

    def _port(self, port):
        """Re-establish connection to supplied device port."""
        self._close()
        self._rdbuf = b''
        LOG.debug(u'Connecting to %r', port)
        s = serial.Serial(baudrate=RRU_BAUD,
                          rtscts=False,
                          timeout=RRU_IOTIMEOUT)
        s.dtr = 0  # This must be set _before_ open()
        s.port = port
        s.open()
        self._io = s
        self._sane()

    def _sane(self, data=None):
        """Load system config and then check decoder is properly configured"""
        setconfs = {}
        if sysconf.has_option(u'rru', u'allowstored'):
            self._allowstored = strops.confopt_bool(
                sysconf.get(u'rru', u'allowstored'))
            LOG.info(u'Allow stored passings: %r', self._allowstored)
        if sysconf.has_option(u'rru', u'decoderconfig'):
            setconfs = sysconf.get(u'rru', u'decoderconfig')
            LOG.debug(u'Loaded %r config options from sysconf', len(setconfs))
        self._config = {}
        for opt in CONFINFO:
            ko = CONFINFO[opt]
            kn = ko[u'label']
            if kn in setconfs and setconfs[kn] is not None:
                self._config[opt] = setconfs[kn]
            elif ko[u'default'] is not None:
                self._config[opt] = ko[u'default']

        # Set protocol
        self.write(u'ASCII')
        # Fetch decoder ID
        self.write(u'INFOGET;01')
        # Set options, ordering ensures Box mode is set before impulse in
        for opt in [
                u'05', u'01', u'02', u'04', u'07', u'08', u'09', u'0a', u'0b',
                u'0c', u'0d', u'a0', u'a1', u'a2', u'a3', u'a4', u'b1', u'b2',
                u'03'
        ]:
            if opt in self._config and self._config[opt] is not None:
                self.write(u';'.join([u'CONFSET', opt, self._config[opt]]))
        # Check if site survey is required
        if u'06' in self._config and self._config[u'06'] is not None:
            if self._config[u'06'].lower() == u'auto':
                LOG.debug(u'Requesting site survey')
                self.write(u'SITESURVEY')
            else:
                self.write(u';'.join([u'CONFSET', u'06', self._config[u'06']]))
        # Request current epoch ref setting
        self.write(u'EPOCHREFGET')
        # Request current number of ticks
        self.write(u'TIMESTAMPGET')
        # Request current memory status
        self.write(u'PASSINGINFOGET')

    def _sync(self, data=None):
        LOG.debug(u'Performing blocking DTR sync')
        # for the purpose of sync, the "epoch" is considered to be
        # midnight localtime of the current day
        self._rrustamp = None
        self._rruht = None
        # Determine the 'reference' epoch
        nt = tod.now()
        ntt = nt.truncate(0)
        ntd = (nt - ntt).timeval
        if ntd < 0.1 or ntd > 0.9:
            LOG.debug(u'Sleeping 0.3s')
            time.sleep(0.3)
            nt = tod.now()
        ntt = nt.truncate(0)
        ett = ntt + tod.ONE
        LOG.debug(u'Host reference time: %s', ett.rawtime())
        es = u'EPOCHREFSET;{0:08x}'.format(int(ett.timeval))
        self._write(es)
        LOG.debug(u'Waiting for top of second')
        acceptval = tod.tod(u'0.001')
        diff = ett - nt
        while diff > acceptval and diff < tod.ONE:
            time.sleep(0.0005)
            nt = tod.now()
            diff = ett - nt
        LOG.debug(u'Set DTR')
        self._io.dtr = 1
        time.sleep(0.2)
        LOG.debug(u'Clear DTR')
        self._io.dtr = 0

    def _reset(self, data=None):
        LOG.debug(u'Performing box reset')
        self._write(u'RESET')
        while True:
            m = self._readline()
            LOG.debug(u'RECV: %r', m)
            if m == u'AUTOBOOT':
                break
        self._rrustamp = None
        self._lastrequest = 0
        self._lastpassing = 0
        # Queue 'sane' config options before sync request
        self._sane()
        self.sync()

    def _write(self, msg):
        if self._io is not None:
            ob = (msg + RRU_EOL)
            self._io.write(ob.encode(RRU_ENCODING))
            LOG.debug(u'SEND: %r', ob)

    def _tstotod(self, ts):
        """Convert a race result timestamp to time of day."""
        ret = None
        try:
            ti = int(ts, 16) - self._rrustamp
            tsec = decimal.Decimal(ti // 256) + decimal.Decimal(ti % 256) / 256
            nsec = (self._rruht.timeval + tsec) % 86400
            if nsec < 0:
                LOG.debug(u'Negative timestamp: %r', nsec)
                nsec = 86400 + nsec
            ret = tod.tod(nsec)
        except Exception as e:
            LOG.error(u'%s converting timeval %r: %s', e.__class__.__name__,
                      ts, e)
        return ret

    def _confmsg(self, cid, val):
        """Handle config response."""
        lbl = cid
        vbl = val
        if cid in CONFINFO:
            option = CONFINFO[cid]
            if u'label' in option:
                lbl = option[u'label']
            if val in option:
                vbl = option[val]
        if cid in self._config:
            # check decoder has the desired value
            if val != self._config[cid] and self._config[cid] != u'auto':
                if self._curreply == u'CONFSET':
                    LOG.error(u'Error setting config %r, desired:%r actual:%r',
                              lbl, self._config[cid], val)
                else:
                    LOG.info(u'Updating config %r: %r => %r', lbl, val,
                             self._config[cid])
                    self.write(u';'.join([u'CONFSET', cid, self._config[cid]]))
            else:
                LOG.debug(u'Config %r: %r', lbl, vbl)
        else:
            LOG.debug(u'Config %r: %r', lbl, vbl)

    def _infomsg(self, pid, val):
        """Show and save decoder info message."""
        lbl = pid
        vbl = val
        if pid in INFOLBLS:
            lbl = INFOLBLS[pid]
            if pid == u'01':  # ID
                vbl = u'A-{0:d}'.format(int(val, 16))
                self._boxname = vbl
            elif pid == u'02':  # Firmware
                vbl = u'v{0:0.1f}'.format(int(val, 16) / 10)
            elif pid == u'03':  # Hardware
                vbl = u'v{0:0.1f}'.format(int(val, 16) / 10)
            elif pid == u'04':  # Box Type
                if val in BOX_TYPES:
                    vbl = BOX_TYPES[val]
            elif pid == u'05':  # Batt Voltage
                vbl = u'{0:0.1f}V'.format(int(val, 16) / 10)
            elif pid == u'07':  # Battery State
                if val in BATTERY_STATES:
                    vbl = BATTERY_STATES[val]
            elif pid == u'08':  # Battery Level
                vbl = u'{0:d}%'.format(int(val, 16))
            elif pid == u'09':  # Int Temp
                vbl = u'{0:d}\xb0C'.format(int(val, 16))
            elif pid == u'0a':  # Supply Voltage
                vbl = u'{0:0.1f}V'.format(int(val, 16) / 10)
            elif pid == u'0b':  # Loop Status
                if val in LOOP_STATES:
                    vbl = LOOP_STATES[val]
            LOG.info(u'Info %s: %s', lbl, vbl)
        else:
            LOG.info(u'Info [undocumented] %s: %s', lbl, vbl)

    def _refgetmsg(self, epoch, stime):
        """Collect the epoch ref and system tick message."""
        self._rruht = tod.mkagg(int(epoch, 16))
        self._rrustamp = int(stime, 16)
        LOG.debug(u'Reference ticks: %r @ %r', self._rrustamp,
                  self._rruht.rawtime())

    def _timestampchk(self, ticks):
        """Receive the number of ticks on the decoder."""
        tcnt = int(ticks, 16)
        LOG.info(u'Box tick count: %r', tcnt)
        if tcnt > RRU_REFTHRESH:
            LOG.info(u'Tick threshold exceeded, adjusting ref')
            self.write(u'EPOCHREFADJ1D')

    def _passinginfomsg(self, mv):
        """Receive info about internal passing memory."""
        if len(mv) == 5:
            pcount = int(mv[0], 16)
            if pcount > 0:
                pfirst = int(mv[1], 16)
                pftime = self._tstotod(mv[2])
                plast = int(mv[3], 16)
                pltime = self._tstotod(mv[4])
                LOG.info(u'Info %r Passings, %r@%s - %r@%s', pcount, pfirst,
                         pftime.rawtime(2), plast, pltime.rawtime(2))
                if plast + 1 < self._lastpassing:
                    LOG.warning(
                        'Decoder memory/ID mismatch last=%r, req=%r, clear/sync required.',
                        plast, self._lastpassing)
                    self._lastpassing = plast + 1
            else:
                LOG.info(u'Info No Passings')
        else:
            LOG.debug(u'Non-passinginfo message: %r', mv)

    def _passingmsg(self, mv):
        """Receive a passing from the decoder."""
        if len(mv) == RRU_PASSLEN:
            # USB decoder doesn't return passing ID, use internal count
            istr = unicode(self._lastpassing)
            tagid = mv[0]  # [TranspCode:string]
            wuc = mv[1]  # [WakeupCounter:4]
            timestr = mv[2]  # [Time:8]
            hits = mv[3]  # [Hits:2]
            rssi = mv[4]  # [RSSI:2]
            battery = mv[5]  # [Battery:2]
            loopid = mv[8]  # [LoopId:1]
            adata = mv[10]  # [InternalActiveData:2]

            # Check values
            if not loopid:
                loopid = u'C1'  # add faked id for passives
            else:
                loopid = strops.id2chan(int(loopid, 16) + 1)
            activestore = False
            if adata:
                aval = int(adata, 16)
                activestore = (int(adata, 16) & 0x40) == 0x40
            if tagid == RRU_MARKER:
                tagid = u''

            if battery and tagid:
                try:
                    bv = int(battery, 16) / 10
                    if bv < RRU_LOWBATT:
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
            LOG.log(DECODER_LOG_LEVEL, u';'.join(mv))

            # accept valid passings and trigger callback
            t = self._tstotod(timestr)
            if t is not None:
                t.index = istr
                t.chan = loopid
                t.refid = tagid
                t.source = self._boxname
                if not activestore or self._allowstored:
                    self._trig(t)
                else:
                    pass
            self._lastpassing += 1
        elif len(mv) == 2:
            resp = int(mv[0], 16)
            rcount = int(mv[1], 16)
            if resp != self._lastrequest:
                LOG.error(u'Sequence mismatch request: %r, response: %r',
                          self._lastrequest, resp)
                self._lastpassing = 0
            elif rcount > 0:
                LOG.debug(u'Receiving %r passings', rcount)
        else:
            LOG.debug(u'Non-passing message: %r', mv)

    def _beaconmsg(self, mv):
        """Receive a beacon from the decoder."""
        if len(mv) == RRU_BEACONLEN:
            # noise/transponder averages
            chid = int(mv[5], 16) + 1
            chnoise = 10.0 * int(mv[12], 16)
            tlqi = int(mv[13], 16) / 2.56
            trssi = -90 + int(mv[14], 16)
            LOG.info(u'Info Ch {0} Noise: {1:0.0f}%'.format(chid, chnoise))
            LOG.info(u'Info Avg LQI: {0:0.0f}%'.format(tlqi))
            LOG.info(u'Info Avg RSSI: {}dBm'.format(trssi))
        elif len(mv) == 1:
            bcount = int(mv[0], 16)
            LOG.debug(u'Receiving %r beacons', bcount)
        else:
            LOG.debug(u'Non-beacon message: %r', mv)

    def _idupdate(self, reqid, minid):
        """Handle an empty PASSINGGET response."""
        resp = int(reqid, 16)
        newid = int(minid, 16)
        if resp != self._lastrequest:
            LOG.error(
                u'ID mismatch: request=%r, response=%r:%r, reset to first passing',
                self._lastrequest, resp, newid)
            self._lastpassing = newid
        else:
            if resp < newid:
                LOG.warning(u'Data loss: %r passings not received',
                            newid - resp)
                self._lastpassing = newid
            # otherwise no missed passings, ignore error

    def _surveymsg(self, chan, noise):
        """Receive a site survey update."""
        channo = int(chan, 16) + 1
        if channo in self._sitenoise:
            self._sitenoise[channo] = 10 * int(noise, 16)
        else:
            LOG.debug(u'Unknown channel in site survey: %r', channo)

    def _chansurf(self):
        """Examine survey for a better channel and hop if needed."""
        ch = None
        cv = 55  # don't accept any channel with noise over 50%
        lv = []
        for c in sorted(self._sitenoise, key=strops.rand_key):
            nv = self._sitenoise[c]
            lv.append(u'{}:{:d}%'.format(c, nv))
            if nv < cv:
                ch = c
                cv = nv
        LOG.debug(u'Site survey: %s', u' '.join(lv))
        if ch is not None:
            LOG.info(u'Selected channel %r (%d%%)', ch, cv)
            self.loopchannel = ch
            m = u'CONFSET;06;{0:02x}'.format(self.loopchannel - 1)
            self.write(m)
        else:
            LOG.warning(u'Unable to find a suitable channel')

    def _handlereply(self, mv):
        """Process the body of a decoder response."""
        if self._curreply == u'PASSINGGET':
            self._passingmsg(mv)
        elif self._curreply == u'PASSINGINFOGET':
            self._passinginfomsg(mv)
        elif self._curreply == u'PASSINGIDERROR':
            if len(mv) == 2:
                self._idupdate(mv[0], mv[1])
        elif self._curreply == u'INFOGET':
            if len(mv) == 2:
                self._infomsg(mv[0], mv[1])
        elif self._curreply in [
                u'EPOCHREFGET', u'EPOCHREFSET', u'EPOCHREFADJ1D'
        ]:
            if len(mv) == 2:
                self._refgetmsg(mv[0], mv[1])
        elif self._curreply in [u'CONFGET', u'CONFSET']:
            if len(mv) == 2:
                self._confmsg(mv[0], mv[1])
        elif self._curreply == u'TIMESTAMPGET':
            if len(mv) == 1:
                self._timestampchk(mv[0])
        elif self._curreply == u'SITESURVEY':
            if len(mv) == 2:
                self._surveymsg(mv[0], mv[1])
        elif self._curreply == u'BEACONGET':
            self._beaconmsg(mv)
        else:
            LOG.debug(u'%r : %r', self._curreply, mv)

    def _procline(self, l):
        """Handle the next line of response from decoder."""
        mv = l.split(u';')
        if len(mv) > 0:
            if mv[0] == u'#P':
                # pushed passing overrides cmd/reply logic
                self._passingmsg(mv[1:])
            elif mv[0] == u'PREWARN':
                self._curreply = mv[0]
            elif mv[0] == u'EOR':
                if self._curreply in [u'PASSINGGET', u'PASSINGIDERROR']:
                    self._request_pending = False
                if self._curreply == u'SITESURVEY':
                    self._chansurf()
                self._curreply = None
            elif mv[0] in REPLIES:
                if self._curreply is not None:
                    LOG.debug(u'Protocol error: %r not terminated',
                              self._curreply)
                self._curreply = mv[0]
                if mv[1] != u'00':
                    if self._curreply == u'PASSINGGET' and mv[1] == u'10':
                        LOG.debug(u'ID error: %r', self._curreply)
                        self._curreply = u'PASSINGIDERROR'
                    else:
                        LOG.debug(u'%r error: %r', self._curreply, mv[1])
            else:
                if self._curreply is not None:
                    self._handlereply(mv)

    def _readline(self):
        """Read from the decoder until end of line or timeout condition."""
        ret = None
        ch = self._io.read(1)
        while ch != b'':
            if ch == b'\n':
                if len(self._rdbuf) > 0:
                    # linefeed ends the current 'message'
                    ret = self._rdbuf.lstrip(b'\0').decode(RRU_ENCODING)
                    self._rdbuf = b''
                else:
                    ret = u'EOR'  # Flag end of response
                break
            else:
                self._rdbuf += ch
            ch = self._io.read(1)
        return ret

    def _request_next(self):
        """Queue a passingget request if the reftime is set."""
        if self._rrustamp is not None:
            if not self._request_pending:
                self._request_pending = True
                es = u'PASSINGGET;{0:08x}'.format(self._lastpassing)
                self._lastrequest = self._lastpassing
                self.write(es)
                self._refcount += 1
                if self._refcount > RRU_REFCHECK:
                    self.write(u'TIMESTAMPGET')
                    self._refcount = 0

    def run(self):
        """Decoder main loop."""
        LOG.debug(u'Starting')
        self._running = True
        self._boxname = None
        while self._running:
            try:
                m = None  # next commmand
                if self._io is not None:
                    # Fetch all responses from unit
                    refetch = False
                    while True:
                        l = self._readline()
                        if l is None:
                            # wait for sitesurvey
                            if self._curreply != u'SITESURVEY':
                                # on time out, request passings
                                refetch = True
                                break
                        else:
                            LOG.debug(u'RECV: %r', l)
                            self._procline(l)
                            if self._curreply == u'PREWARN':
                                # Note: this does not work
                                refetch = True
                            if l == u'EOR':
                                break
                    if refetch:
                        self._request_next()
                    m = self._cqueue.get_nowait()
                else:
                    m = self._cqueue.get()
                self._cqueue.task_done()
                self._proccmd(m)
            except Queue.Empty:
                pass
            except serial.SerialException as e:
                self._close()
                LOG.error(u'%s: %s', e.__class__.__name__, e)
            except Exception as e:
                LOG.critical(u'%s: %s', e.__class__.__name__, e)
                self._running = False
        self.setcb()
        LOG.debug(u'Exiting')
