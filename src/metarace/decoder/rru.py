# SPDX-License-Identifier: MIT
"""Race Result USB Decoder interface."""

from __future__ import division

import threading
import Queue
import decimal
import logging
import serial
from time import sleep

from . import (decoder, DECODER_LOG_LEVEL)
from metarace import tod
from metarace import sysconf
from metarace import strops

_log = logging.getLogger(u'metarace.decoder.rru')
_log.setLevel(logging.DEBUG)

_RRU_BAUD = 19200
_RRU_PASSLEN = 12
_RRU_BEACONLEN = 17
_RRU_LOWBATT = 2.1  # Warn if battery voltage is below this many volts
_RRU_REFCHECK = 1800  # check ref after this many timeouts
_RRU_REFTHRESH = 0x2a30000  # 2 x 86400 x 256
_RRU_ENCODING = u'iso8859-1'
_RRU_MARKER = u'_____127'  # trigger marker
_RRU_EOL = u'\n'
# Serial port I/O read timeout in seconds
_RRU_IOTIMEOUT = 1.0
# List of handled responses from decoder
_REPLIES = [
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
_CONFINFO = {
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
_INFOLBLS = {
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
_BOX_TYPES = {
    u'0a': u'active-ext',
    u'1e': u'management-box',
    u'28': u'usb-timing-box'
}
_BATTERY_STATES = {
    u'00': u'Fault',
    u'01': u'Charging',
    u'02': u'Reduced Charging',
    u'03': u'Discharging'
}
_LOOP_STATES = {
    u'00': u'OK',
    u'01': u'Fault',
    u'02': u'Limit',
    u'03': u'Overvoltage Error'
}


class rru(decoder):
    """Race Result USB Active Decoder"""

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
        self._autochannelid = None
        self._refcount = 0

    # API overrides
    def status(self):
        """Request status info from decoder."""
        for c in sorted(_CONFINFO):
            self.write(u''.join([u'CONFGET;', c]))
        for c in sorted(_INFOLBLS):
            self.write(u''.join([u'INFOGET;', c]))
        self.write(u'BEACONGET')
        self.write(u'PASSINGINFOGET')
        self.write(u'TIMESTAMPGET')

    def connected(self):
        return self._boxname is not None and self._io is not None

    # Device-specific functions
    def _close(self):
        self._boxname = None
        if self._io is not None:
            _log.debug(u'Close connection')
            cp = self._io
            self._io = None
            cp.close()

    def _port(self, port):
        """Re-establish connection to supplied device port."""
        self._request_pending = False
        self._rrustamp = None
        self._rruht = None
        self._close()
        self._rdbuf = b''
        _log.debug(u'Connecting to %r', port)
        s = serial.Serial(baudrate=_RRU_BAUD,
                          rtscts=False,
                          timeout=_RRU_IOTIMEOUT)
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
            _log.debug(u'Allow stored passings: %r', self._allowstored)
        if sysconf.has_option(u'rru', u'decoderconfig'):
            setconfs = sysconf.get(u'rru', u'decoderconfig')
            _log.debug(u'Loaded %r config options from sysconf', len(setconfs))
        self._config = {}
        for opt in _CONFINFO:
            ko = _CONFINFO[opt]
            kn = ko[u'label']
            if kn in setconfs and setconfs[kn] is not None:
                self._config[opt] = setconfs[kn]
                sv = setconfs[kn]
                if sv in ko:
                    sv = ko[sv]
                _log.debug(u'Set %s: %s', kn, sv)
            elif ko[u'default'] is not None:
                self._config[opt] = ko[u'default']

        # if channel id set already by site survey, keep it
        if self._autochannelid is not None:
            self._config[u'06'] = self._autochannelid

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
                _log.info(u'Performing site survey to set Channel ID')
                self.write(u'SITESURVEY')
            else:
                self.write(u';'.join([u'CONFSET', u'06', self._config[u'06']]))
        # Request current epoch ref setting
        self.write(u'EPOCHREFGET')
        # Request current number of ticks
        self.write(u'TIMESTAMPGET')
        # Request loop ID
        self.write(u'CONFGET;07')
        # Request current memory status
        self.write(u'PASSINGINFOGET')

    def _sync(self, data=None):
        _log.debug(u'Performing blocking DTR sync')
        # for the purpose of sync, the "epoch" is considered to be
        # midnight localtime of the current day
        self._rrustamp = None
        self._rruht = None
        # Determine the 'reference' epoch
        nt = tod.now()
        ntt = nt.truncate(0)
        ntd = (nt - ntt).timeval
        if ntd < 0.1 or ntd > 0.9:
            _log.debug(u'Sleeping 0.3s')
            sleep(0.3)
            nt = tod.now()
        ntt = nt.truncate(0)
        ett = ntt + tod.ONE
        _log.debug(u'Host reference time: %s', ett.rawtime())
        es = u'EPOCHREFSET;{0:08x}'.format(int(ett.timeval))
        self._write(es)
        _log.debug(u'Waiting for top of second')
        acceptval = tod.tod(u'0.001')
        diff = ett - nt
        while diff > acceptval and diff < tod.ONE:
            sleep(0.0005)
            nt = tod.now()
            diff = ett - nt
        _log.debug(u'Set DTR')
        self._io.dtr = 1
        sleep(0.2)
        _log.debug(u'Clear DTR')
        self._io.dtr = 0

    def _start_session(self, data=None):
        """Reset decoder to start a new session"""
        # On USB decoder, session is always active, this
        # method performs a hard reset/clear/sync
        self._clear(data)
        _log.info(u'Start session')

    def _stop_session(self, data=None):
        """Stop the current timing session"""
        # USB decoder will continue collecting passings, but
        # they are not received by handler - the passings can
        # be fetched by restarting connection without reset
        self._rrustamp = None
        _log.info(u'Stop session')

    def _clear(self, data=None):
        _log.debug(u'Performing box reset')
        self._boxname = None
        self._write(u'RESET')
        while True:
            m = self._readline()
            #_log.debug(u'RECV: %r', m)
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
            ob = (msg + _RRU_EOL)
            self._io.write(ob.encode(_RRU_ENCODING))
            #_log.debug(u'SEND: %r', ob)

    def _tstotod(self, ts):
        """Convert a race result timestamp to time of day."""
        ret = None
        try:
            ti = int(ts, 16) - self._rrustamp
            tsec = decimal.Decimal(ti // 256) + decimal.Decimal(ti % 256) / 256
            nsec = (self._rruht.timeval + tsec) % 86400
            if nsec < 0:
                _log.debug(u'Negative timestamp: %r', nsec)
                nsec = 86400 + nsec
            ret = tod.tod(nsec)
        except Exception as e:
            _log.error(u'%s converting timeval %r: %s', e.__class__.__name__,
                       ts, e)
        return ret

    def _confmsg(self, cid, val):
        """Handle config response."""
        lbl = cid
        vbl = val
        if cid in _CONFINFO:
            option = _CONFINFO[cid]
            if u'label' in option:
                lbl = option[u'label']
            if cid == u'08':
                vbl = '{0:d}%'.format(int(val, 16))
            elif cid == u'07':
                vbl = '{0:d}'.format(int(val, 16) + 1)
            else:
                if val in option:
                    vbl = option[val]
        if cid in self._config:
            # check decoder has the desired value
            if val != self._config[cid] and self._config[cid] != u'auto':
                if self._curreply == u'CONFSET':
                    _log.error(
                        u'Error setting config %s, desired:%r actual:%r', lbl,
                        self._config[cid], val)
                else:
                    _log.info(u'Updating config %s: %s => %s', lbl, val,
                              self._config[cid])
                    self.write(u';'.join([u'CONFSET', cid, self._config[cid]]))
            else:
                _log.info(u'Config %s: %s', lbl, vbl)
        else:
            if cid in [u'07', u'08']:
                _log.info(u'Config %s: %s', lbl, vbl)
            else:
                _log.debug(u'Config %s: %s', lbl, vbl)

    def _infomsg(self, pid, val):
        """Show and save decoder info message."""
        lbl = pid
        vbl = val
        if pid in _INFOLBLS:
            lbl = _INFOLBLS[pid]
            if pid == u'01':  # ID
                vbl = u'A-{0:d}'.format(int(val, 16))
                self._boxname = vbl
            elif pid == u'02':  # Firmware
                vbl = u'v{0:0.1f}'.format(int(val, 16) / 10)
            elif pid == u'03':  # Hardware
                vbl = u'v{0:0.1f}'.format(int(val, 16) / 10)
            elif pid == u'04':  # Box Type
                if val in _BOX_TYPES:
                    vbl = _BOX_TYPES[val]
            elif pid == u'05':  # Batt Voltage
                vbl = u'{0:0.1f}V'.format(int(val, 16) / 10)
            elif pid == u'07':  # Battery State
                if val in _BATTERY_STATES:
                    vbl = _BATTERY_STATES[val]
            elif pid == u'08':  # Battery Level
                vbl = u'{0:d}%'.format(int(val, 16))
            elif pid == u'09':  # Int Temp
                vbl = u'{0:d}\xb0C'.format(int(val, 16))
            elif pid == u'0a':  # Supply Voltage
                vbl = u'{0:0.1f}V'.format(int(val, 16) / 10)
            elif pid == u'0b':  # Loop Status
                if val in _LOOP_STATES:
                    vbl = _LOOP_STATES[val]
            _log.info(u'Info %s: %s', lbl, vbl)
        else:
            _log.info(u'Info [undocumented] %s: %s', lbl, vbl)

    def _refgetmsg(self, epoch, stime):
        """Collect the epoch ref and system tick message."""
        self._rruht = tod.mkagg(int(epoch, 16))
        self._rrustamp = int(stime, 16)
        _log.debug(u'Reference ticks: %r @ %r', self._rrustamp,
                   self._rruht.rawtime())

    def _timestampchk(self, ticks):
        """Receive the number of ticks on the decoder."""
        tcnt = int(ticks, 16)
        _log.info(u'Box tick count: %r', tcnt)
        if tcnt > _RRU_REFTHRESH:
            _log.info(u'Tick threshold exceeded, adjusting ref')
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
                _log.info(u'Info %r Passings, %r@%s - %r@%s', pcount, pfirst,
                          pftime.rawtime(2), plast, pltime.rawtime(2))
                if plast + 1 < self._lastpassing:
                    _log.warning(
                        'Decoder memory/ID mismatch last=%r, req=%r, clear/sync required.',
                        plast, self._lastpassing)
                    self._lastpassing = plast + 1
            else:
                _log.info(u'Info No Passings')
        else:
            _log.debug(u'Non-passinginfo message: %r', mv)

    def _passingmsg(self, mv):
        """Receive a passing from the decoder."""
        if len(mv) == _RRU_PASSLEN:
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
            if tagid == _RRU_MARKER:
                tagid = u''

            if battery and tagid:
                try:
                    bv = int(battery, 16) / 10
                    if bv < _RRU_LOWBATT:
                        _log.warning(u'Low battery %s: %0.1fV', tagid, bv)
                except Exception as e:
                    _log.debug(u'%s reading battery voltage: %s',
                               e.__class__.__name__, e)

            if hits and rssi and tagid:
                try:
                    hitcount = int(hits, 16)
                    rssival = int(rssi, 16)
                    twofour = -90 + ((rssival & 0x70) >> 2)
                    lstrength = 1 + (rssival & 0x0f)
                    if lstrength < 5 or twofour < -82 or hitcount < 4:
                        _log.warning(
                            u'Poor read %s: Hits:%d RSSI:%ddBm Loop:%ddB',
                            tagid, hitcount, twofour, lstrength)
                except Exception as e:
                    _log.debug(u'%s reading hits/RSSI: %s',
                               e.__class__.__name__, e)

            # emit a decoder log line TBD
            _log.log(DECODER_LOG_LEVEL, u';'.join(mv))

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
                _log.error(u'Sequence mismatch request: %r, response: %r',
                           self._lastrequest, resp)
                self._lastpassing = 0
            elif rcount > 0:
                _log.debug(u'Receiving %r passings', rcount)
        else:
            _log.debug(u'Non-passing message: %r', mv)

    def _beaconmsg(self, mv):
        """Receive a beacon from the decoder."""
        if len(mv) == _RRU_BEACONLEN:
            # noise/transponder averages
            chid = int(mv[5], 16) + 1
            chnoise = 10.0 * int(mv[12], 16)
            tlqi = int(mv[13], 16) / 2.56
            trssi = -90 + int(mv[14], 16)
            _log.info(u'Info Ch {0} Noise: {1:0.0f}%'.format(chid, chnoise))
            _log.info(u'Info Avg LQI: {0:0.0f}%'.format(tlqi))
            _log.info(u'Info Avg RSSI: {}dBm'.format(trssi))
        elif len(mv) == 1:
            bcount = int(mv[0], 16)
            _log.debug(u'Receiving %r beacons', bcount)
        else:
            _log.debug(u'Non-beacon message: %r', mv)

    def _idupdate(self, reqid, minid):
        """Handle an empty PASSINGGET response."""
        resp = int(reqid, 16)
        newid = int(minid, 16)
        if resp != self._lastrequest:
            _log.error(
                u'ID mismatch: request=%r, response=%r:%r, reset to first passing',
                self._lastrequest, resp, newid)
            self._lastpassing = newid
        else:
            if resp < newid:
                _log.warning(u'Data loss: %r passings not received',
                             newid - resp)
                self._lastpassing = newid
            # otherwise no missed passings, ignore error

    def _surveymsg(self, chan, noise):
        """Receive a site survey update."""
        channo = int(chan, 16) + 1
        if channo in self._sitenoise:
            self._sitenoise[channo] = 10 * int(noise, 16)
        else:
            _log.debug(u'Unknown channel in site survey: %r', channo)

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
        _log.debug(u'Site survey: %s', u' '.join(lv))
        if ch is not None:
            _log.info(u'Selected channel %r (%d%%)', ch, cv)
            sv = u'{0:02x}'.format(ch - 1)
            self._autochannelid = sv
            m = u'CONFSET;06;{}'.format(sv)
            self.write(m)
        else:
            _log.warning(u'Unable to find a suitable channel')

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
            _log.debug(u'%r : %r', self._curreply, mv)

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
            elif mv[0] in _REPLIES:
                if self._curreply is not None:
                    _log.debug(u'Protocol error: %r not terminated',
                               self._curreply)
                self._curreply = mv[0]
                if mv[1] != u'00':
                    if self._curreply == u'PASSINGGET' and mv[1] == u'10':
                        self._curreply = u'PASSINGIDERROR'
                    else:
                        _log.debug(u'%r error: %r', self._curreply, mv[1])
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
                    ret = self._rdbuf.lstrip(b'\0').decode(_RRU_ENCODING)
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
                if self._refcount > _RRU_REFCHECK:
                    self.write(u'TIMESTAMPGET')
                    self._refcount = 0

    def run(self):
        """Decoder main loop."""
        _log.debug(u'Starting')
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
                            #_log.debug(u'RECV: %r', l)
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
                _log.error(u'%s: %s', e.__class__.__name__, e)
            except Exception as e:
                _log.critical(u'%s: %s', e.__class__.__name__, e)
                self._running = False
        self.setcb()
        _log.debug(u'Exiting')
