"""Transponder 'decoder' interface."""

from __future__ import division

import threading
import Queue
import logging

from metarace import tod
from metarace import sysconf

LOG = logging.getLogger(u'metarace.decoder')
LOG.setLevel(logging.DEBUG)
DECODER_LOG_LEVEL = 15
logging.addLevelName(DECODER_LOG_LEVEL, u'DECODER')
PHOTOTHRESH = tod.tod(u'0.03')
DEFAULT_HANDLER = u'null'


def mkdevice(portstr=u'', curdev=None):
    """Return a decoder handle for the provided port specification."""
    # Note: If possible, returns the current device
    ret = curdev
    devtype = DEFAULT_HANDLER
    if sysconf.has_option(u'decoder',u'default'):
        devtype = sysconf.get(u'decoder',u'default')
        LOG.debug(u'Default type set to %r from sysconf', devtype)
    (a, b, c) = portstr.partition(u':')
    if b:
        a = a.lower()
        if a in HANDLERS:
            devtype = a
        a = c  # shift port into a
    devport = a
    if curdev is None:
        curdev = HANDLERS[devtype]()
        curdev.setport(devport)
    elif type(curdev) is HANDLERS[devtype]:
        LOG.debug(u'Requested decoder is %s', curdev.__class__.__name__)
        curdev.setport(devport)
    else:
        LOG.debug(u'Changing decoder type from %s to %s',
                  curdev.__class__.__name__, devtype)
        curdev.setcb(None)
        wasalive = curdev.running()
        if wasalive:
            curdev.exit(u'Change decoder type')
            LOG.debug(u'Waiting for %s decoder to exit',
                      curdev.__class__.__name__)
            curdev.join()
        curdev = None
        curdev = HANDLERS[devtype]()
        curdev.setport(devport)
        LOG.debug(u'Starting %s decoder', curdev.__class__.__name__)
        if wasalive:
            curdev.start()
    return curdev


class decoder(threading.Thread):
    """Idealised transponder decoder interface."""

    # API Methods
    def getcb(self):
        """Return the current callback function."""
        return self._cb

    def setcb(self, func=None):
        """Set or clear the event callback."""
        if func is not None:
            self._cb = func
        else:
            self._cb = self._defcallback

    def running(self):
        """Return state of running flag."""
        return self._running

    def exit(self, msg=None):
        """Request thread termination."""
        self._running = False
        self._cqueue.put_nowait((u'_exit', msg))

    def setport(self, device=None):
        """Request new device address."""
        self._flush()
        self._cqueue.put_nowait((u'_port', device))

    def sane(self, data=None):
        """Reset decoder to sane state."""
        self._cqueue.put_nowait((u'_sane', data))

    def sync(self, data=None):
        """Synchronise decoder to host PC time."""
        self._cqueue.put_nowait((u'_sync', data))

    def start_session(self, data=None):
        """Request decoder to start current timing session."""
        self._cqueue.put_nowait((u'_start_session', data))

    def stop_session(self, data=None):
        """Request decoder to stop current timing session."""
        self._cqueue.put_nowait((u'_stop_session', data))

    def status(self, data=None):
        """Request status message from decoder."""
        self._cqueue.put_nowait((u'_status', data))

    def clear(self, data=None):
        """Clear passings in decoder memory."""
        self._cqueue.put_nowait((u'_clear', data))

    def trig(self, impulse=None):
        """Queue a fake timing impulse through decoder interface."""
        self._cqueue.put_nowait((u'_trig', impulse))

    def replay(self, file=None):
        """Request replay of passings from the provided file indicator."""
        self._cqueue.put_nowait((u'_replay', file))

    def wait(self):
        """Suspend calling thread until the command queue is empty."""
        self._cqueue.join()

    def write(self, msg=None):
        """Queue a raw device command string."""
        self._cqueue.put_nowait((u'_write', msg))

    def photothresh(self):
        """Return the relevant photo finish threshold."""
        return PHOTOTHRESH

    # Private Methods
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self._cqueue = Queue.Queue()
        self._running = False
        self._cb = self._defcallback

    def _defcallback(self, evt=None):
        """Default callback is a debug log entry."""
        LOG.debug(unicode(evt))

    def _close(self):
        """Close hardware connection to decoder."""
        raise NotImplementedError(u'decoder._close()')

    def _flush(self):
        """Clear out the command queue."""
        try:
            while True:
                self._cqueue.get_nowait()
                self._cqueue.task_done()
        except Queue.Empty:
            pass

    def _exit(self, msg):
        """Handle request to exit."""
        LOG.debug(u'Request to exit: %r', msg)
        self._close()
        self._flush()
        self._running = False

    def _port(self, port):
        """Re-connect decoder hardware."""
        raise NotImplementedError(u'decoder._port()')

    def _sane(self, data=None):
        """Return decoder to known initial state."""
        raise NotImplementedError(u'decoder._sane()')

    def _sync(self, data=None):
        """Roughly align decoder timebase with host PC clock."""
        raise NotImplementedError(u'decoder._sync()')

    def _start_session(self, data=None):
        """Start decoder timing session."""
        raise NotImplementedError(u'decoder._start_session()')

    def _stop_session(self, data=None):
        """Stop decoder timing session."""
        raise NotImplementedError(u'decoder._stop_session()')

    def _status(self, data=None):
        """Request status from decoder."""
        raise NotImplementedError(u'decoder._status()')

    def _clear(self, data=None):
        """Request clear of memory on decoder."""
        raise NotImplementedError(u'decoder._clear()')

    def _trig(self, impulse):
        """Return a timing impulse to the host application."""
        self._cb(impulse)

    def _replay(self, file):
        """Request a replay of passings from file."""
        raise NotImplementedError(u'decoder._replay()')

    def _write(self, msg):
        """Write the supplied msg to the decoder."""
        raise NotImplementedError(u'decoder._write()')

    def _proccmd(self, cmd):
        """Process a command tuple from the queue."""
        method = getattr(self, cmd[0], None)
        if method is not None:
            method(cmd[1])
        else:
            LOG.debug(u'Unknown command: %r', cmd)

    def run(self):
        """Decoder main loop."""
        LOG.debug(u'Starting')
        self._running = True
        while self._running:
            try:
                c = self._cqueue.get()
                self._cqueue.task_done()
                self._proccmd(c)
            except Exception as e:
                # errors in dummy decoder should not appear in UI
                LOG.debug(u'%s: %s', e.__class__.__name__, e)
        self.setcb()  # make sure callback is unrefed
        LOG.debug(u'Exiting')


from rrs import rrs
from rru import rru
from thbc import thbc

HANDLERS = {
    u'null': decoder,
    u'thbc': thbc,
    u'rrs': rrs,
    u'rru': rru,
}
