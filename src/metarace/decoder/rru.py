
"""Race Result USB Decoder interface."""

from __future__ import division

import threading
import Queue
import decimal
import logging
import serial
import time

from . import decoder
from metarace import tod

LOG = logging.getLogger(u'metarace.decoder.rru')
LOG.setLevel(logging.DEBUG)

RRU_BAUD = 19200
RRU_PASSLEN = 12
RRU_BEACONLEN = 17
RRU_LOWBATT = 2.0       # Warn if battery voltage is below this many volts
RRU_REFTHRESH = 0x2a30000	# 2 x 86400 x 256
RRU_ENCODING = u'iso8859-1'
RRU_MARKER = u'_____127'	# trigger marker
RRU_EOL = u'\n'
RRU_IOTIMEOUT = 1.0	# Serial port I/O read timeout in seconds
REPLIES = [
	u'ASCII', u'CONFSET', u'CONFGET', u'INFOGET', u'SITESURVEY',
        u'TIMESTAMPGET', u'EPOCHREFGET', u'EPOCHREFSET',
        u'EPOCHREFADJ1D', u'PASSINGGET', u'BEACONGET',
]
CONFINFO = {
	u'01': {u'label': u'Push Pre-Warn',
		u'00': u'disabled',
		u'01': u'enabled' },
	u'02': {u'label': u'Blink/beep on repeated passing',
		u'00': u'disabled',
		u'01': u'enabled' },
	u'03': {u'label': u'Impulse input or beep output',
		u'00': u'impulse-in',
		u'01': u'beep-out' },
	u'04': {u'label': u'Auto-shutdown on power loss',
		u'00': u'disabled',
		u'01': u'enabled' },
	u'05': {u'label': u'Operation Mode',
		u'05': u'usb-kiosk',
		u'06': u'usb-timing',
                u'07': u'usb-store&copy' },
	u'06': {u'label': u'Channel ID',
		u'00':u'1', u'01':u'2', u'02':u'3', u'03':u'4',
		u'04':u'5', u'05':u'6', u'06':u'7', u'07':u'8'},
	u'07': {u'label': u'Loop ID',
		u'00':u'1', u'01':u'2', u'02':u'3', u'03':u'4',
		u'04':u'5', u'05':u'6', u'06':u'7', u'07':u'8'},
	u'08': {u'label': u'Loop Power'},
        u'0a': {u'label': u'Charging via USB',
		u'00': u'disabled',
		u'01': u'enabled' },
        u'0b': {u'label': u'Use DTR',
		u'00': u'disabled',
		u'01': u'enabled' },
}
INFOLBLS = {
	u'01': u'Decoder ID',
	u'02': u'Firmware Version',
	u'03': u'Hardware Version',
	u'04': u'Box Type',
	u'05': u'Battery Voltage',
	u'07': u'Battery State',
	u'08': u'Battery Level',
	u'09': u'Internal Temperature',
	u'0a': u'Supply Voltage',
	u'0b': u'Loop Status',
}
BOX_TYPES = { u'0a': u'active-ext', u'1e': u'management-box',
              u'28': u'usb-timing-box' }
BATTERY_STATES = { u'00':u'Fault', u'01':u'Charging',
                   u'02':u'Reduced Charging', u'03':u'Discharging'}
LOOP_STATES = { u'00':u'OK', u'01':u'Fault',
                u'02':u'Limit', u'03':u'Overvoltage Error' }

class rru(decoder):
    """Race Result USB Active thread object class."""
    def __init__(self):
        decoder.__init__(self)
        self.looppower = 37	# percentage ish
        self.loopid = 1		# id number 1-8
        self.loopchannel = 1	# channel id 1-8
        self._sitenoise = {1:100, 2:100, 3:100, 4:100,
                            5:100, 6:100, 7:100, 8:100 }
        self._boxname = u'rru'
        self._rrustamp = None
        self._rruht = None
        self._error = False
        self._io = None
        self._rdbuf = b''
        self._curreply = None	# current multi-line response mode
        self._lastpassing = 0
        self._request_pending = False

    # API overrides
    def setport(self, device=None):
        """Request new device address."""
        self._request_pending = False
        self._rrustamp = None
        self._rruht = None
        self._flush()
        self._cqueue.put_nowait((u'_port', device))

    def sane(self):
        for m in [ u'ASCII',		# Decoder protocol
                   u'INFOGET;01',	# Decoder ID
                   u'INFOGET;02',	# Firmware Version
                   u'INFOGET;03',	# Hardware Version
                   u'INFOGET;04',	# Box Type
                   u'CONFSET;01;00',	# Push Pre-Warn disabled
                   u'CONFSET;02;00',	# Blink on repeated... disabled
                   u'CONFSET;03;00',	# Impulse input (usb decoder problem)
                   u'CONFSET;04;00',	# Auto-shutdown ... disabled
                   u'CONFSET;05;06',	# Operation Mode usb-timing
                   u'CONFSET;06;{0:02x}'.format(self.loopchannel-1),
                   u'CONFSET;07;{0:02x}'.format(self.loopid-1),
                   u'CONFSET;08;{0:02x}'.format(self.looppower),
                   u'CONFSET;0a;00',	# Charging via USB disabled
                   u'CONFSET;0b;01',	# Use DTR enabled
                   u'EPOCHREFGET',	# Request current epoch ref setting
                   u'TIMESTAMPGET',	# Request number of ticks on decoder
                 ]:
            self.write(m)

    def status(self):
        """Request status from decoder."""
        for m in [ u'INFOGET;01',       # Decoder ID
                   u'INFOGET;02',	# Firmware Version
                   u'INFOGET;03',	# Hardware Version
                   u'INFOGET;04',	# Box Type
                   u'INFOGET;07',	# Battery State
                   u'INFOGET;08',	# Battery Level
                   u'INFOGET;0b',	# Loop Status
                   u'TIMESTAMPGET',	# Box timestamp
                 ]:
            self.write(m)

    # Device-specific functions
    def _close(self):
        if self._io is not None:
            LOG.debug(u'Close connection')
            cp = self._io
            self._io = None
            cp.close()

    def _port(self, port):
        """Re-establish connection to supplied device port."""
        self._close()
        LOG.debug(u'Connecting to %r', port)
        s = serial.Serial(baudrate=RRU_BAUD, rtscts=False,
                          timeout=RRU_IOTIMEOUT)
        s.dtr = 0	# This must be set _before_ open()
        s.port = port
        s.open()
        self._io = s
        self.sane()

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
            tsec = decimal.Decimal(ti//256)+decimal.Decimal(ti%256)/256
            nsec = (self._rruht.timeval + tsec)%86400
            if nsec < 0:
                LOG.debug(u'Negative timestamp: %r', nsec)
                nsec = 86400+nsec
            ret = tod.tod(nsec).truncate(3)
        except Exception as e:
            LOG.error(u'%s converting timeval %r: %s',
                      e.__class__.__name__, ts, e)
        return ret

    def _confmsg(self, cid, val):
        """Display config response."""
        lbl = cid
        vbl = val
        if cid in CONFINFO:
            option = CONFINFO[cid]
            if u'label' in option:
                lbl = option[u'label']
            if val in option:
                vbl = option[val]
        LOG.debug(u'Config %r: %r', lbl, vbl)

    def _infomsg(self, pid, val):
        """Show and save decoder info message."""
        lbl = pid
        vbl = val
        if pid in INFOLBLS:
            lbl = INFOLBLS[pid]
            if pid == u'01':	# ID
                vbl = u'A-{0:d}'.format(int(val, 16))
                self._boxname = vbl
            elif pid == u'02':	# Firmware 
                vbl = u'v{0:0.1f}'.format(int(val, 16)/10)
            elif pid == u'03':	# Hardware
                vbl = u'v{0:0.1f}'.format(int(val, 16)/10)
            elif pid == u'04':	# Box Type
                if val in BOX_TYPES:
                    vbl = BOX_TYPES[val]
            elif pid == u'05':	# Batt Voltage
                vbl = u'{0:0.1f}V'.format(int(val, 16)/10)
            elif pid == u'07':	# Battery State
                if val in BATTERY_STATES:
                    vbl = BATTERY_STATES[val]
            elif pid == u'08':      # Battery Level
                vbl = u'{0:d}%'.format(int(val, 16))
            elif pid == u'09':	# Int Temp
                vbl = u'{0:d}\xb0C'.format(int(val, 16))
            elif pid == u'0a':	# Supply Voltage
                vbl = u'{0:0.1f}V'.format(int(val, 16)/10)
            elif pid == u'0b':      # Loop Status
                if val in LOOP_STATES:
                    vbl = LOOP_STATES[val]
            LOG.info(u'Info %s: %s', lbl, vbl)
        else:
            LOG.debug(u'Unknown Info %r: %r', lbl, val)

    def _refgetmsg(self, epoch, stime):
        """Collect the epoch ref and system tick message."""
        self._rruht = tod.mkagg(int(epoch, 16))
        self._rrustamp = int(stime, 16)
        LOG.debug(u'Reference ticks: %r @ %r', self._rrustamp,
                    self._rruht.rawtime())

    def _timestampchk(self, ticks):
        """Receive the number of ticks on the decoder."""
        tcnt = int(ticks, 16)
        LOG.debug(u'Box tick count: %r', tcnt)
        if tcnt > RRU_REFTHRESH:
            LOG.debug(u'Tick threshold exceeded, queue reref')
            self.write(u'EPOCHREFADJ1D')

    def _passingmsg(self, mv):
        """Receive a passing from the decoder."""
        if len(mv) == RRU_PASSLEN:
            istr = unicode(self._lastpassing)
            tagid = mv[0]		# [TranspCode:string]
            wuc = mv[1]			# [WakeupCounter:4]
            timestr = mv[2]		# [Time:8]
            battery = mv[5]		# [Battery:2]
            loopid = mv[8]		# [LoopId:1]
            adata = mv[10]		# [InternalActiveData:2]

            # Check values
            if not loopid:
                loopid = u'1'	# add fake id for passives
            else:
                loopid = unicode(int(loopid,16)+1)
            activestore = False
            if adata:
                aval = int(adata, 16)
                activestore = (int(adata, 16)&0x40) == 0x40

            if tagid == RRU_MARKER:
                tagid = u''

            if battery and tagid:
                try:
                    bv = int(battery, 16)/10
                    if bv < RRU_LOWBATT:
                        LOG.warning(u'Low battery on %r: %0.1fV', tagid, bv)
                except Exception as e:
                    LOG.debug(u'%s reading battery voltage: %s',
                              e.__class__.__name__, e)

            # accept valid passings and trigger callback
            if not activestore:
                t = self._tstotod(timestr)
                if t is not None:
                    t.index = istr
                    t.chan = loopid
                    t.refid = tagid
                    t.source = self._boxname
                    self._trig(t)
            else:
                LOG.info(u'Stored passing: %r',
                         u';'.join([istr, tagid, u'0000-00-00', timestr,
                                    self._boxname, loopid]))
            self._lastpassing += 1
        else:
            LOG.debug(u'Non-passing message: %r', mv)

    def _beaconmsg(self, mv):
        """Receive a beacon from the decoder."""
        if len(mv) == RRU_BEACONLEN:
            LOG.debug(u'Beacon: %r', mv)
        else:
            LOG.debug(u'Non-beacon message: %r', mv)

    def _idupdate(self, minid):
        """Handle a passing request ID error."""
        newid = int(minid, 16)
        LOG.debug(u'First available passing %r', newid)
        self._lastpassing = newid

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
        cv = 55		# don't accept any channel with noise over 50%
        lv = []
        for c in sorted(self._sitenoise):
            nv = self._sitenoise[c]
            lv.append(u'{}:{:d}%'.format(c, nv))
            if nv < cv:
                ch = c
                cv = nv
        LOG.debug(u'Site survey: %s', u' '.join(lv))
        if ch is not None:
            LOG.info(u'Selected loop channel %r (%d%%)', ch, cv)
            self.loopchannel = ch
            m = u'CONFSET;06;{0:02x}'.format(self.loopchannel-1)
            self.write(m)

    def _handlereply(self, mv):
        """Process the body of a decoder response."""
        if self._curreply == u'PASSINGGET':
            self._passingmsg(mv)
        elif self._curreply == u'PASSINGIDERROR':
            if len(mv) == 2:
                # this is problematic - check column
                self._idupdate(mv[0])
        elif self._curreply == u'INFOGET':
            if len(mv) == 2:
                self._infomsg(mv[0], mv[1])
        elif self._curreply in [u'EPOCHREFGET',
                                 u'EPOCHREFSET', u'EPOCHREFADJ1D']:
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
            if mv[0] == u'#P':	# pushed passing overrides cmd/reply logic
                self._passingmsg(mv[1:])
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
                    ret = u'EOR'	# Flag end of response
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
                self.write(es)
            self.write(u'BEACONGET')

    def run(self):
        """Decoder main loop."""
        LOG.debug(u'Starting')
        self._running = True
        while self._running:
            try:
                m = None	# next commmand
                if self._io is not None:
                    # Fetch all responses from unit
                    refetch = False
                    while True:
                         l = self._readline()
                         if l is None:
                             # When read times out, request passings
                             refetch = True
                             break
                         LOG.debug(u'RECV: %r', l)
                         self._procline(l)
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
                LOG.error(u'%s: %s', e.__class__.__name__, e)
        self.setcb()	# make sure callback is unrefed
        LOG.debug(u'Exiting')
