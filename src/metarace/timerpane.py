"""Lane timer module."""

import gtk
import gobject
import logging

from metarace import tod
from metarace import unt4
from metarace import uiutil

LOG = logging.getLogger(u'metarace.timerpane')
LOG.setLevel(logging.DEBUG)

FIELDWIDTH = u'00h00:00.0000'
ARMTEXT = u'       0.0   '


class timerpane(object):

    def setrider(self, bib=None, ser=None):
        """Set bib for timer."""
        if bib is not None:
            self.bibent.set_text(bib)
            if ser is not None:
                self.serent.set_text(ser)
            self.bibent.activate()  # and chain events

    def grab_focus(self, data=None):
        """Steal focus into bib entry."""
        self.bibent.grab_focus()
        return False  # allow addition to idle_add or delay

    def getrider(self):
        """Return bib loaded into timer."""
        return self.bibent.get_text().decode(u'utf-8')

    def getstatus(self):
        """Return timer status.

        Timer status may be one of:

          'idle'	-- lane empty or ready for new rider
          'load'	-- rider loaded into lane
          'armstart'	-- armed for start trigger
          'running'	-- timer running
          'armint'	-- armed for intermediate split
          'armfin'	-- armed for finish trigger
          'finish'	-- timer finished

        """
        return self.status

    def set_time(self, tstr=u''):
        """Set timer string."""
        self.ck.set_text(tstr)

    def get_time(self):
        """Return current timer string."""
        return self.ck.get_text().decode(u'utf-8')

    def show_splits(self):
        """Show the split button and label."""
        self.ls.show()
        self.lb.show()

    def hide_splits(self):
        """Hide the split button and label."""
        self.ls.hide()
        self.lb.hide()

    def set_split(self, split=None):
        """Set the split pointer and update label."""
        # update split index from supplied argument
        if isinstance(split, int):
            if split >= 0 and split < len(self.splitlbls):
                self.split = split
            else:
                LOG.warning(u'Requested split %r not in range %r', split,
                            self.splitlbls)
        elif isinstance(split, basestring):
            if split in self.splitlbls:
                self.split = self.splitlbls.index(split)
            else:
                LOG.warning(u'Requested split %r not found %r', split,
                            self.splitlbls)
        else:
            self.split = -1  # disable label

        # update label to match current split
        if self.split >= 0 and self.split < len(self.splitlbls):
            self.ls.set_text(self.splitlbls[self.split])
        else:
            self.ls.set_text(u'')

    def on_halflap(self):
        """Return true is current split pointer is a half-lap."""
        return self.split % 2 == 0

    def lap_up(self):
        """Increment the split point to the next whole lap."""
        nsplit = self.split
        if self.on_halflap():
            nsplit += 1
        else:
            nsplit += 2
        self.set_split(nsplit)

    def lap_up_clicked_cb(self, button, data=None):
        """Respond to lap up button press."""
        if self.status in [u'running', u'armint', u'armfin']:
            self.missedlap()

    def runtime(self, runtod):
        """Update timer run time."""
        if runtod > self.recovtod:
            self.set_time(runtod.timestr(1))

    def missedlap(self):
        """Flag a missed lap to allow 'catchup'."""
        LOG.info(u'No time recorded for split %r', self.split)
        self.lap_up()

    def get_sid(self, inter=None):
        """Return the split id for the supplied, or current split."""
        if inter is None:
            inter = self.split
        ret = None
        if inter >= 0 and inter < len(self.splitlbls):
            ret = self.splitlbls[inter]
        return ret

    def intermed(self, inttod, recov=4):
        """Trigger an intermediate time."""
        nt = inttod - self.starttod
        if self.on_halflap():
            # reduce recover time on half laps
            recov = 2
        self.recovtod.timeval = nt.timeval + recov
        self.set_time(nt.timestr(3))
        self.torunning()

        # store intermedate split in local split cache
        sid = self.get_sid()
        self.splits[sid] = inttod

    def difftime(self, dt):
        """Overwrite split time with a difference time."""
        dstr = (u'+' + dt.rawtime(2) + u' ').rjust(12)
        self.set_time(dstr)

    def getsplit(self, inter):
        """Return split for specified passing."""
        ret = None
        sid = self.get_sid(inter)
        if sid in self.splits:
            ret = self.splits[sid]
        return ret

    def finish(self, fintod):
        """Trigger finish on timer."""
        # Note: split pointer is not updated, so after finish, if
        #       labels are loaded, the current split will point to
        #       a dummy sid for event distance
        self.finishtod = fintod
        self.ls.set_text(u'Finish')
        self.set_time((self.finishtod - self.starttod).timestr(3))
        self.tofinish()

    def tofinish(self, status=u'finish'):
        """Set timer to finished."""
        self.status = status
        self.b.buttonchg(uiutil.bg_none, u'Finished')

    def toarmfin(self):
        """Arm timer for finish."""
        self.status = u'armfin'
        self.b.buttonchg(uiutil.bg_armfin, u'Finish Armed')

    def toarmint(self):
        """Arm timer for intermediate."""
        self.status = u'armint'
        self.b.buttonchg(uiutil.bg_armint, u'Lap Armed')

    def torunning(self):
        """Update timer state to running."""
        self.bibent.set_sensitive(False)
        self.serent.set_sensitive(False)
        self.status = u'running'
        self.b.buttonchg(uiutil.bg_none, u'Running')

    def start(self, starttod):
        """Trigger start on timer."""
        self.starttod = starttod
        self.set_split(0)
        self.torunning()

    def toload(self, bib=None):
        """Load timer."""
        self.status = u'load'
        self.starttod = None
        self.recovtod = tod.tod(0)  # timeval is manipulated
        self.finishtod = None
        self.set_time()
        self.splits = {}
        self.set_split()
        if bib is not None:
            self.setrider(bib)
        self.b.buttonchg(uiutil.bg_none, u'Ready')

    def toarmstart(self):
        """Set state to armstart."""
        self.status = u'armstart'
        self.set_split()
        self.set_time(ARMTEXT)
        self.b.buttonchg(uiutil.bg_armstart, u'Start Armed')

    def disable(self):
        """Disable rider bib entry field."""
        self.bibent.set_sensitive(False)
        self.serent.set_sensitive(False)

    def enable(self):
        """Enable rider bib entry field."""
        self.bibent.set_sensitive(True)
        self.serent.set_sensitive(True)

    def toidle(self):
        """Set timer state to idle."""
        self.status = u'idle'
        self.bib = None
        self.bibent.set_text(u'')
        self.bibent.set_sensitive(True)
        self.serent.set_sensitive(True)
        self.biblbl.set_text(u'')
        self.starttod = None
        self.recovtod = tod.tod(0)
        self.finishtod = None
        self.split = -1  # next expected passing
        self.splits = {}  # map of split ids to split data
        self.set_split()
        self.set_time()
        self.b.buttonchg(uiutil.bg_none, u'Idle')

    def __init__(self, label=u'Timer', doser=False):
        """Constructor."""
        s = gtk.Frame(label)
        s.set_border_width(5)
        s.set_shadow_type(gtk.SHADOW_IN)
        s.show()
        self.doser = doser

        v = gtk.VBox(False, 5)
        v.set_border_width(5)

        # Bib and name label
        h = gtk.HBox(False, 5)
        l = gtk.Label(u'Rider #:')
        l.show()
        h.pack_start(l, False)
        self.bibent = gtk.Entry(6)
        self.bibent.set_width_chars(3)
        self.bibent.show()
        h.pack_start(self.bibent, False)
        self.serent = gtk.Entry(6)
        self.serent.set_width_chars(2)
        if self.doser:
            self.serent.show()
        h.pack_start(self.serent, False)
        self.biblbl = gtk.Label(u'')
        self.biblbl.show()
        h.pack_start(self.biblbl, True)
        h.show()
        v.pack_start(h, False)

        # Clock row 'HHhMM:SS.DCMZ'
        self.ck = gtk.Label(FIELDWIDTH)
        self.ck.set_alignment(0.5, 0.5)
        self.ck.modify_font(uiutil.DIGITFONT)
        self.ck.show()
        v.pack_start(self.ck, True)

        # Timer ctrl/status button
        h = gtk.HBox(False, 5)
        b = gtk.Button()
        b.show()
        b.set_property(u'can-focus', False)
        self.b = uiutil.statbut(b)
        self.b.buttonchg(uiutil.bg_none, u'Idle')
        h.pack_start(b, True)
        self.ls = gtk.Label(u'')
        h.pack_start(self.ls, False)
        self.lb = gtk.Button(u'+')
        self.lb.set_border_width(5)
        self.lb.set_property(u'can-focus', False)
        self.lb.connect(u'clicked', self.lap_up_clicked_cb)
        h.pack_start(self.lb, False)
        h.show()
        v.pack_start(h, False)
        v.show()
        s.add(v)
        self.frame = s
        self.splitlbls = []  # ordered set of split ids
        self.toidle()
