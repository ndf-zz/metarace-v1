"""UCI Hour Record handler for trackmeet."""

import gtk
import glib
import gobject
import logging
import os

import metarace
from metarace import timy
from metarace import scbwin
from metarace import tod
from metarace import unt4
from metarace import uiutil
from metarace import jsonconfig
from metarace import strops
from metarace import report

LOG = logging.getLogger(u'metarace.hour')
LOG.setLevel(logging.DEBUG)

# config version string
EVENT_ID = 'hourrec'

# run clock up to this long after expiry of hour
MAX_AFTER = tod.tod(u'10:00')

# scb function key mappings (also trig announce)
key_timerwin = 'F6'  # re-show timing window
key_results = u'F4'

# timing function key mappings
key_armstart = 'F5'  # arm start -> countdown
key_armfinish = 'F9'  # override finish trigger

# extended function key mappings
key_reset = 'F5'  # + ctrl for clear/abort
key_falsestart = 'F6'  # + ctrl for false start
key_abort = 'F7'  # + ctrl abort A


class hourrec(object):
    """Data handling for Hour record."""

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_reset:  # override ctrl+f5
                    self.toidle()
                    return True
                elif key == key_results:
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_falsestart:
                    self.falsestart()
                    return True
                elif key == key_abort:
                    self.abortrider()
                    return True
            elif key[0] == 'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_armfinish:
                    self.armfinish()
                    return True
                elif key == key_timerwin or key == key_results:
                    self.showtimerwin()
                    glib.idle_add(self.delayed_announce)
                    return True
        return False

    def recalc(self):
        """Recalc runtime from current state."""
        self.statusline.set_text(u'')
        self.curspeed = None
        elap = None
        if self.finish is None and self.elapsed == u'' and self.start is not None and len(
                self.splitlist) > 0:
            # elapsed time at last completed lap
            self.elapsed = (self.splitlist[-1] - self.start).rawtime(0)
        if self.lstart is not None:
            elap = tod.now() - self.lstart  # use ltime for roll-over
        if self.finish is not None:  # rider has crossed line after time
            self.TC = self.lapcount - 1
            self.TTC = None
            if len(self.splitlist) > 2:
                belltime = self.splitlist[-2]
                laststart = self.splitlist[-3]
                # time of the last complete lap
                self.TTC = belltime - laststart
                # time remaining to ride at the beginning of the last lap
                self.TRC = self.reclen - (belltime - self.start)
                # additional distance
                self.DiC = self.LPi * float(self.TRC.timeval) / float(
                    self.TTC.timeval)
                # distance covered in the hour
                self.D = int(self.DiC + self.TC * self.LPi)
                self.compute = u'D={0}\u2006m, LPi\u00d7TC={1:0.1f}\u2006m, LPi={2:0.1f}\u2006m, TC={3}, DiC={4:0.1f}\u2006m, TTC={5}, TRC={6}'.format(
                    self.D, self.LPi * self.TC, self.LPi, self.TC, self.DiC,
                    self.TTC.rawtime(3), self.TRC.rawtime(3))
                self.infoline.set_text(
                    u'D: {0:0.3f}\u2006km, DiC: {1:0.1f}\u2006m'.format(
                        self.D / 1000.0, self.DiC))
                LOG.debug(self.compute)
            self.projection = None
            self.statusline.set_text(u'{} Laps'.format(self.TC))
        else:
            self.TC = self.lapcount
            self.statusline.set_text(u'{} Laps'.format(self.TC))
            if elap > self.reclen:  # time has expired
                self.TTC = None
                if len(self.splitlist) > 1:
                    belltime = self.splitlist[-1]
                    laststart = self.splitlist[-2]
                    # time of the last complete lap
                    self.TTC = belltime - laststart
                    # time remaining to ride at the beginning of the last lap
                    self.TRC = self.reclen - (belltime - self.start)
                    # additional distance
                    self.DiC = self.LPi * float(self.TRC.timeval) / float(
                        self.TTC.timeval)
                    # distance covered in the hour
                    self.D = int(self.DiC + self.TC * self.LPi)
                    self.compute = u'D={0}\u2006m, LPi\u00d7TC={1:0.1f}\u2006m, LPi={2:0.1f}\u2006m, TC={3}, DiC={4:0.1f}\u2006m, TTC={5}, TRC={6}'.format(
                        self.D, self.LPi * self.TC, self.LPi, self.TC,
                        self.DiC, self.TTC.rawtime(3), self.TRC.rawtime(3))
                    self.infoline.set_text(
                        u'D: {0:0.3f}\u2006km, DiC: {1:0.1f}\u2006m'.format(
                            self.D / 1000.0, self.DiC))
                    LOG.debug(self.compute)
                self.projection = None
            else:
                # update projection (use 3 lap average speed)
                if len(self.splitlist) > 3:
                    et = self.splitlist[-1] - self.start
                    rt = self.reclen - et
                    di = int(self.LPi * self.lapcount)
                    pi = self.splitlist[-4]
                    pd = self.splitlist[-1]
                    pr = (self.LPi * 3) / (float(pd.timeval) -
                                           float(pi.timeval))
                    self.curspeed = 3.6 * pr
                    de = di + int(float(rt.timeval) * pr)
                    LOG.debug(u'Projected final dist: %r', de)
                    self.infoline.set_text(
                        u'Speed: {0:0.1f}\u2006km/h, Est: {1:0.2f}\u2006km'.
                        format(self.curspeed, de / 1000.0))
                    if de < self.maxproj and de > self.minproj:
                        self.projection = de
                    else:
                        self.projection = None
        # update telegraph outputs
        if self.D is not None and self.D > 0:
            self.meet.cmd_announce(u'distance', str(self.D))
        else:
            self.meet.cmd_announce(u'distance', None)
        if self.projection is not None:
            self.meet.cmd_announce(u'projection', unicode(self.projection))
        else:
            self.meet.cmd_announce(u'projection', None)
        if self.curspeed is not None:
            self.meet.cmd_announce(u'curspeed',
                                   '{0:0.1f}'.format(self.curspeed))
        else:
            self.meet.cmd_announce(u'curspeed', None)

    def update_expander_lbl_cb(self):
        """Update race info expander label."""
        self.info_expand.set_label(self.meet.infoline(self.event))

    def loadconfig(self):
        """Load race config from disk."""
        self.model.clear()
        cr = jsonconfig.config({
            u'event': {
                u'id': EVENT_ID,
                u'comments': [],
                u'riderstr': u'',
                u'wallstart': None,
                u'start': None,
                u'showinfo': True,
                u'finish': None,
                u'lstart': None,
                u'target': None,
                u'reclen': '1h00:00',
                u'minlap': '14.0',
                u'record': None,
                u'projlap': 12,
                u'lpi': None,
                u'minproj': 30000,
                u'maxproj': 60000,
                u'lapcount': 0,
                u'splitlist': []
            }
        })
        cr.add_section(u'event')
        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                LOG.error(u'Unable to read config: %s', e)
        else:
            LOG.info(u'%r not found, loading defaults', self.configfile)

        # race infos
        self.comments = cr.get(u'event', u'comments')
        self.update_expander_lbl_cb()
        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get(u'event', u'showinfo')))

        self.riderstr = cr.get(u'event', u'riderstr')

        self.reclen = tod.mktod(cr.get(u'event', u'reclen'))
        if self.reclen is None:
            LOG.error(u'Invalid record length: Reset to 1h00:00')
            self.reclen = tod.tod(u'1h00:00')
        self.minlap = tod.mktod(cr.get(u'event', u'minlap'))
        if self.minlap is None:
            LOG.error(u'Invalid min lap time: Reset to 14.0s')
            self.minlap = tod.tod(u'14.0')
        self.start = tod.mktod(cr.get(u'event', u'start'))
        self.finish = tod.mktod(cr.get(u'event', u'finish'))
        self.lstart = tod.mktod(cr.get(u'event', u'lstart'))
        self.wallstart = tod.mktod(cr.get(u'event', u'wallstart'))

        self.target = strops.confopt_posint(cr.get(u'event', u'target'))
        self.record = strops.confopt_posint(cr.get(u'event', u'record'))
        chklap = float(self.meet.tracklen_n) / float(self.meet.tracklen_d)
        self.LPi = strops.confopt_float(cr.get(u'event', u'lpi'), chklap)
        self.projlap = strops.confopt_posint(cr.get(u'event', u'projlap'), 12)
        self.minproj = strops.confopt_posint(cr.get(u'event', u'minproj'),
                                             30000)
        self.maxproj = strops.confopt_posint(cr.get(u'event', u'maxproj'),
                                             60000)
        self.lapcount = strops.confopt_posint(cr.get(u'event', u'lapcount'))
        self.splitlist = []
        lc = 0
        pt = self.start
        for lt in cr.get(u'event', u'splitlist'):
            nlt = tod.mktod(lt)
            if nlt is not None:
                lc += 1
                self.splitlist.append(nlt)
                if self.start is not None:
                    self.addlapline(nlt, pt, lc, False)
                pt = nlt
        # check count of laps agains splits
        if len(self.splitlist) != self.lapcount:
            LOG.error(u'SPLIT LIST != LAPCOUNT')

        # arm the front straight
        if self.winopen:
            self.meet.main_timer.arm(2)
            self.meet.main_timer.armlock(True)
            self.meet.scbwin = scbwin.scbtt(scb=self.meet.scb,
                                            header=self.event[u'pref'],
                                            subheader=self.event[u'info'])
            self.meet.scbwin.reset()
        # recalc
        self.recalc()

        # then set UI
        if self.start is None:
            self.toidle()
        else:
            if self.D is not None and self.D > 0:
                self.tofinish()
            else:
                self.torunning()

    def addlapline(self, split, lastsplit, count, scroll=True):
        """Insert a lap line on the ui view."""
        ctxt = unicode(count)
        selap = split - self.start
        dtxt = u'[final lap]'
        if selap < self.reclen:
            dtxt = u'{0:0.3f}\u2006km'.format(count * self.LPi / 1000.0)
        stxt = (split - self.start).rawtime(3)
        ltxt = (split - lastsplit).rawtime(3)
        self.model.insert(0, [ctxt, dtxt, stxt, ltxt])
        if scroll:
            self.view.scroll_to_cell(0)

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            LOG.error('Attempt to save readonly ob.')
            return
        cw = jsonconfig.config()
        cw.add_section(u'event')

        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'riderstr', self.riderstr)
        cw.set(u'event', u'reclen', self.reclen.rawtime())
        cw.set(u'event', u'minlap', self.minlap.rawtime())
        if self.start is not None:
            cw.set(u'event', u'start', self.start.rawtime())
        if self.lstart is not None:
            cw.set(u'event', u'lstart', self.lstart.rawtime())
        if self.wallstart is not None:
            cw.set(u'event', u'wallstart', self.wallstart.rawtime())
        if self.finish is not None:
            cw.set(u'event', u'finish', self.finish.rawtime())

        cw.set(u'event', u'lpi', self.LPi)
        cw.set(u'event', u'target', self.target)
        cw.set(u'event', u'record', self.record)
        cw.set(u'event', u'projlap', self.projlap)
        cw.set(u'event', u'minproj', self.minproj)
        cw.set(u'event', u'maxproj', self.maxproj)
        cw.set(u'event', u'lapcount', self.lapcount)
        cw.set(u'event', u'comments', self.comments)

        slout = []
        for lt in self.splitlist:
            slout.append(lt.rawtime())
        cw.set(u'event', u'splitlist', slout)

        cw.set(u'event', u'id', EVENT_ID)
        LOG.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        cnt = 0
        sec = report.bullet_text()
        sec.heading = u' '.join([self.event[u'pref'],
                                 self.event[u'info']]).strip()
        substr = u' '.join([self.event[u'dist'], self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        if self.event[u'reco']:
            sec.footer = self.event[u'reco']
        # the rider free-form string
        if self.riderstr:
            sec.lines.append([u'', self.riderstr])
        if self.record is not None:
            tstr = u'{0:0.3f}\u2006km'.format(self.record / 1000.0)
            sec.lines.append([u'', u'Record: ' + tstr])
        if self.target is not None:
            tstr = u'{0:0.3f}\u2006km'.format(self.target / 1000.0)
            sec.lines.append([u'', u'Target: ' + tstr])
        if self.wallstart is not None:
            sec.lines.append(
                [u'', u'Start Time: ' + self.wallstart.meridiem(secs=False)])
        ret.append(sec)
        return ret

    def get_startlist(self):
        """Return a list of bibs in the rider model."""
        ret = []
        for r in self.riders:
            ret.append(r[COL_BIB])
        return ' '.join(ret)

    def delayed_announce(self):
        """Initialise the announcer's screen after a delay."""
        if self.winopen:
            LOG.debug(u'TODO delayed_announce')

    def shutdown(self, win=None, msg=u'Exiting'):
        """Terminate race object."""
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        LOG.debug(u'Shutdown %sevent %s: %s', rstr, self.evno, msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def do_properties(self):
        """Run race properties dialog."""
        prfile = os.path.join(metarace.UI_PATH, u'hour_properties.ui')
        LOG.debug(u'Building event properties from %r', prfile)
        b = gtk.Builder()
        b.add_from_file(prfile)
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.meet.window)
        rl = b.get_object(u'recordlen_entry')
        rl.set_text(self.reclen.rawtime(0))
        ri = b.get_object(u'ridername_entry')
        ri.set_text(self.riderstr)
        st = b.get_object(u'start_entry')
        if self.wallstart is not None:
            st.set_text(self.wallstart.rawtime(0))
        rc = b.get_object(u'record_entry')
        if self.record is not None:
            rc.set_text(unicode(self.record))
        tg = b.get_object(u'target_entry')
        if self.target is not None:
            tg.set_text(unicode(self.target))
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            LOG.debug(u'Updating event properties')
            # update setting
            self.riderstr = ri.get_text().decode(u'utf-8')
            checklen = tod.mktod(rl.get_text().decode(u'utf-8'))
            if checklen is not None:
                self.reclen = checklen
            else:
                LOG.error(u'Invalid record len %r, using default: 1h00:00',
                          rl.get_text().decode(u'utf-8'))
                self.reclen = tod.mktod(u'1h00:00')
            self.wallstart = tod.mktod(st.get_text().decode(u'utf-8'))
            self.target = strops.confopt_posint(tg.get_text().decode(u'utf-8'),
                                                None)
            self.record = strops.confopt_posint(rc.get_text().decode(u'utf-8'),
                                                None)
            glib.idle_add(self.delayed_announce)
        else:
            LOG.debug(u'Edit event properties cancelled')

        # if prefix is empty, grab input focus
        if not self.prefix_ent.get_text():
            self.prefix_ent.grab_focus()
        dlg.destroy()

    def result_gen(self):
        """Generator function to export a final result."""
        yield [u'', u'', u'', u'']

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        ret = []
        sec = report.bullet_text()
        sec.heading = u' '.join([self.event[u'pref'],
                                 self.event[u'info']]).strip()
        substr = u' '.join([self.event[u'dist'], self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        if self.event[u'reco']:
            sec.footer = self.event[u'reco']

        sec.lines.append([u'', self.riderstr])
        # Distance measure - result if D available
        #if self.finish is not None:
        if self.D is not None and self.D > 0:
            dstr = u'{0:0.3f}\u2006km'.format(self.D / 1000.0)
            sec.lines.append([u'', u'Final distance: ' + dstr])
            if self.record is not None:
                if self.D > self.record:
                    sec.lines.append([
                        '', u'New record by {} metre{}'.format(
                            self.D - self.record,
                            strops.plural(self.D - self.record))
                    ])
                else:
                    tstr = u'{0:0.3f}\u2006km'.format(self.record / 1000.0)
                    sec.lines.append([
                        u'', u'{} metre{} short of existing record: '.format(
                            self.record - self.D,
                            strops.plural(self.record - self.D)) + tstr
                    ])
            sec.lines.append([u'', u'Complete laps: ' + unicode(self.TC)])
            if self.compute:
                sec.lines.append(
                    [u'', u'Additional distance: {0:0.1f} m'.format(self.DiC)])
                sec.lines.append([u'', self.compute])
        else:
            if self.record is not None:
                tstr = u'{0:0.3f}\u2006km'.format(self.record / 1000.0)
                sec.lines.append([u'', u'Record: ' + tstr])
            if self.target is not None:
                tstr = u'{0:0.3f}\u2006km'.format(self.target / 1000.0)
                sec.lines.append([u'', u'Target: ' + tstr])
            if self.wallstart is not None:
                sec.lines.append([
                    u'', u'Start Time: ' + self.wallstart.meridiem(secs=False)
                ])
            if self.projection is not None:
                tstr = u'{0:0.3f}\u2006km'.format(self.projection / 1000.0)
                sec.lines.append([u'', u'Projection: ' + tstr])
            if self.lapcount > 0:
                sec.lines.append([u'', u'Elapsed: ' + self.elapsed])
                sec.lines.append([u'', u'Laps: ' + unicode(self.lapcount)])
        ret.append(sec)
        if self.start is not None and self.lapcount > 0:
            sec = report.threecol_section()
            sec.subheading = u'Lap Times'
            sec.lines = []
            lt = self.start
            count = 1
            ld = 0
            for st in self.splitlist:
                laptime = st - lt
                split = st - self.start
                lstr = u'{}'.format(count)
                nd = int(0.010 + self.LPi * count / 1000.0)
                if nd != ld:
                    lstr += u'  / {}\u2006km'.format(nd)
                    ld = nd
                sec.lines.append([
                    u'', u'', lstr, u'',
                    laptime.rawtime(3),
                    split.rawtime(3)
                ])
                lt = st
                count += 1
            ret.append(sec)
        if len(self.comments) > 0:
            sec = report.bullet_text()
            sec.subheading = u'Decisions of the commisaires panel'
            for c in self.comments:
                sec.lines.append([None, c])
            ret.append(sec)
        return ret

    def addrider(self, bib='', info=None):
        return None

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode(u'utf-8')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode(u'utf-8')
        self.update_expander_lbl_cb()

    def split_trig(self, t):
        """Register lap trigger."""
        if self.start is not None:
            lastlap = None
            lasttwo = None
            if len(self.splitlist) > 0:
                lastlap = self.splitlist[-1]
            else:
                lastlap = self.start
            if len(self.splitlist) > 2:
                lasttwo = t - self.splitlist[-2]
            laptime = t - lastlap
            if laptime > self.minlap:
                self.laptimedirty = True
                elap = t - self.start
                if elap <= self.reclen:
                    self.lapcount += 1
                    self.splitlist.append(t)
                    self.addlapline(t, lastlap, self.lapcount)
                    self.recalc()
                    self.curlap = laptime
                    glib.idle_add(self.scblap)
                    LOG.info(u'Lap %r: %s @ %s', self.lapcount,
                             laptime.rawtime(2), t.rawtime(2))
                    if laptime < 60:
                        self.lastlapstr = laptime.rawtime(1)
                    else:
                        self.lastlapstr = laptime.rawtime(0)
                    if (elap + laptime) > self.reclen:
                        self.tofinal()
                    elif lasttwo is not None:
                        if (elap + lasttwo) > self.reclen:
                            self.tobell()
                else:
                    if self.finish is None:
                        self.lapcount += 1
                        self.splitlist.append(t)
                        self.addlapline(t, lastlap, self.lapcount)
                        self.finish = t
                        self.curlap = laptime
                        self.recalc()
                        self.tofinish()
                        LOG.info(u'Final Lap Completed.')
                    else:
                        LOG.info(u'Duplicate finish pass.')
                # and ask meet for an export
                self.meet.delayed_export()
            else:
                LOG.info(u'Ignored short lap.')
        else:
            LOG.info(u'Ignored trig without start.')

    def scblap(self):
        # output to main scoreboard
        if type(self.meet.scbwin) is scbwin.scbtt:
            self.meet.scbwin.setline1(
                strops.truncpad(
                    u'Elapsed: ', self.meet.scb.linelen - 12, align='r') +
                strops.truncpad(self.elapsed, 12))

            if self.lapcount > 0:
                ## display kilometre updates elsewhere?
                if self.lapcount > 3:
                    pass
                    #kilo = self.lapcount // 4
                    #self.meet.scbwin.setr1(u'Lap {0}, {1}\u2006km:'.format(
                    #self.lapcount,kilo))
                #else:
                self.meet.scbwin.setr1(u'Lap {0}:'.format(self.lapcount))
                self.meet.scbwin.sett1(self.lastlapstr)
            else:
                self.meet.scbwin.setline2(u'')
                self.meet.scbwin.setr1(u'')
                self.meet.scbwin.sett1(u'')
            if self.record is not None:
                self.meet.scbwin.setline2(
                    strops.truncpad(
                        u'Record: ', self.meet.scb.linelen - 12, align='r') +
                    strops.truncpad(
                        u'{0:0.3f}\u2006km'.format(self.record / 1000.0), 12))
            elif self.target is not None:
                self.meet.scbwin.setline2(
                    strops.truncpad(
                        u'Target: ', self.meet.scb.linelen - 12, align='r') +
                    strops.truncpad(
                        u'{0:0.3f}\u2006km'.format(self.target / 1000.0), 12))
            if self.lapcount > self.projlap and self.projection is not None:
                self.meet.scbwin.setr2(u'Projection:')
                self.meet.scbwin.sett2(u'{0:0.2f}\u2006km'.format(
                    self.projection / 1000.0))
            else:
                self.meet.scbwin.setr2(u'')
                self.meet.scbwin.sett2(u'')
            self.meet.scbwin.update()

        # telegraph outputs
        self.meet.cmd_announce(u'lapcount', unicode(self.lapcount))
        if self.laptimedirty:
            self.meet.cmd_announce(u'laptime', self.lastlapstr)
            self.laptimedirty = False
        self.meet.cmd_announce(u'elapsed', self.elapsed)

        # on the gemini - use B/T dual timer mode
        self.meet.gemini.set_bib(str(self.lapcount), 0)
        self.meet.gemini.set_time(self.lastlapstr, 0)
        self.meet.gemini.set_time(self.elapsed, 1)
        self.meet.gemini.show_dual()

        return False

    def rftimercb(self, e):
        """Handle rftimer event."""
        if e.refid == '':  # got a trigger
            #return self.starttrig(e)
            return False
        return False

    def timercb(self, e):
        """Handle a timer event."""
        chan = timy.chan2id(e.chan)
        if chan == 0:
            if self.start is None:
                self.lstart = tod.now()
                self.start = e
                self.torunning()
                LOG.info(u'Set Start: %s', e.rawtime())
            else:
                LOG.info(u'Spurious start trig: %s', e.rawtime())
        elif chan == 2:
            self.split_trig(e)
        else:
            LOG.info(u'Trigger: %r @ %s', chan, e.rawtime())
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        now = tod.now()
        nelap = self.elapsed
        dofinishtxt = False
        dostarttxt = False
        setarmfinish = False
        # determine the new elapsed time in secs
        if self.lstart is not None and self.finish is None:
            tot = now - self.lstart
            #self.elaptod = tot
            if tot >= (self.reclen + MAX_AFTER):
                nelap = u'--:--'
            else:
                nelap = tot.rawtime(0)
            if tot > self.reclen:  # time has expired
                setarmfinish = True

        elif self.finish is not None:
            # the hour is over
            #nelap = u'60:00'
            nelap = u'--:--'
            dofinishtxt = True
        else:
            # Before Start
            dostarttxt = True
            if self.wallstart is not None:
                #self.elaptod = tod.tod(u'0')
                if now < self.wallstart:
                    nelap = (self.wallstart - now).rawtime(0)

        if nelap != self.elapsed or dofinishtxt:
            self.elapsed = nelap
            if setarmfinish:
                self.tofinish()
                self.recalc()
                dofinishtxt = True
            if dofinishtxt:
                if type(self.meet.scbwin) is scbwin.scbtt:
                    self.meet.scbwin.setline1(u'')
                    self.meet.scbwin.setr1(u'Result:')
                    #self.meet.scbwin.setr1(u'Final Distance:')
                    if self.D is not None and self.D > 0:
                        self.meet.scbwin.sett1(u'{0:0.3f}\u2006km'.format(
                            self.D / 1000.0))
                    if self.record is not None:
                        if self.D > self.record:
                            self.meet.scbwin.setline2(u'NEW RECORD'.center(
                                self.meet.scb.linelen))
                        else:
                            self.meet.scbwin.setline2(u'')
                    else:
                        self.meet.scbwin.setline2(u'')
                    self.meet.scbwin.setr2(u'')
                    self.meet.scbwin.sett2(u'')
                    self.meet.scbwin.update()
            elif dostarttxt:
                if type(self.meet.scbwin) is scbwin.scbtt:
                    if self.record is not None:
                        self.meet.scbwin.setr1(u'Record:')
                        self.meet.scbwin.sett1(u'{0:0.3f}\u2006km'.format(
                            self.record / 1000.0))
                    elif self.target is not None:
                        self.meet.scbwin.setr1(u'Target:')
                        self.meet.scbwin.sett1(u'{0:0.3f}\u2006km'.format(
                            self.target / 1000.0))
                    if self.wallstart is not None:
                        line1 = strops.truncpad(
                            u'Start Time: ',
                            self.meet.scb.linelen - 12,
                            align='r') + strops.truncpad(
                                self.wallstart.meridiem(secs=False),
                                12,
                                align='l')
                        self.meet.scbwin.setline2(line1)

                    if nelap != u'0':
                        self.meet.scbwin.setr2(u'Countdown:')
                        self.meet.scbwin.sett2(nelap)
                    else:
                        self.meet.scbwin.setr2(u'')
                        self.meet.scbwin.sett2(u'')
                    self.meet.scbwin.update()
            else:
                self.scblap()

        # update elapsed button
        if not dostarttxt:
            self.time_lbl.set_text(self.elapsed)
        else:
            self.time_lbl.set_text(u'-' + self.elapsed)

        ## updates and running lap


# hl board,
        return True

    def armstart(self):
        """Arm timer for start trigger."""
        if self.start is None:
            LOG.info(u'Arm Start.')
            self.stat_but.buttonchg(uiutil.bg_armint, u'Arm Start')
            self.meet.main_timer.arm(0)
        else:
            LOG.info(u'Event already started.')

    def armsplit(self, sp, cid=4):
        """Arm timer for a 50m/200m split."""
        self.DiC = 0.0  # additional distance
        LOG.info(u'armsplit')

    def abortrider(self):
        """Abort the attempt."""
        LOG.debug(u'Ignored abort rider in hour event')

    def falsestart(self):
        """Register false start."""
        LOG.debug(u'Ignored false start in hour event')

    def armfinish(self):
        """Arm timer for finish trigger."""
        LOG.debug(u'Ignored arm finish in hour event')

    def showtimerwin(self):
        """Show timer window on scoreboard."""
        self.meet.scbwin = scbwin.scbtt(scb=self.meet.scb,
                                        header=self.event[u'pref'],
                                        subheader=self.event[u'info'])
        self.meet.scbwin.reset()
        self.recalc()

    def torunning(self):
        """Set timer running."""
        self.stat_but.buttonchg(uiutil.bg_armstart, u'Running')

    def tofinish(self):
        """Set timer finished."""
        self.stat_but.buttonchg(uiutil.bg_none, u'Finished')

    def tobell(self):
        """Set timer bell lap."""
        self.stat_but.buttonchg(uiutil.bg_armint, u'Bell')

    def tofinal(self):
        """Set timer final lap."""
        self.stat_but.buttonchg(uiutil.bg_armfin, u'Final Lap')

    def toidle(self):
        """Set timer to idle state."""
        self.finish = None
        self.start = None
        self.lstart = None
        self.lapcount = 0  # current lap counter
        self.elapsed = u''  # current elapsed time str
        #self.elaptod = tod.tod(u'0')
        self.lastlapstr = u'     '  # last lap as string
        self.laptimedirty = False
        self.splitlist = []
        self.stat_but.buttonchg(uiutil.bg_none, u'Idle')
        self.model.clear()
        sline = u'Ready'
        if self.record is not None:
            sline = u'Record: {0:0.3f}\u2006km - Ready'.format(self.record /
                                                               1000.0)
        self.infoline.set_text(sline)
        self.statusline.set_text(u'')
        LOG.info(u'Reset event state to idle')

    def destroy(self):
        """Signal race shutdown."""
        self.frame.destroy()

    def show(self):
        """Show race window."""
        self.frame.show()

    def hide(self):
        """Hide race window."""
        self.frame.hide()

    def __init__(self, meet, event, ui=True):
        """Constructor."""
        self.meet = meet
        self.event = event  # Note: now a treerowref
        self.evno = event[u'evid']
        self.evtype = event[u'type']
        self.series = event[u'seri']
        self.configfile = meet.event_configfile(self.evno)
        self.autospec = ''

        self.readonly = not ui
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        self.winopen = ui
        LOG.debug(u'Init %shour event %s', rstr, self.evno)
        self.comments = []

        # model
        self.reclen = tod.tod(u'1h00:00')  # record duration
        self.minlap = tod.tod(u'14.0')  # min lap time
        self.compute = u''
        self.riderstr = u''  # rider name string
        self.wallstart = None  # advertised start time
        self.finish = None  # timer finish tod
        self.start = None  # timer start tod
        self.lstart = None  # local start tod
        self.target = None  # current target in m
        self.record = None  # current record in m
        self.projlap = 12  # start showing projection after this many laps
        self.minproj = 30000  # minimum possible projection
        self.maxproj = 60000  # maximum possible projection
        self.projection = None  # current projection in m
        self.avglap = None  # current lap avg
        self.lapcount = 0  # current lap counter
        self.elapsed = u''  # current elapsed time str
        self.onestart = True  #
        self.lastlapstr = u'     '  # last lap as string
        self.laptimedirty = True
        self.splitlist = []

        # computes
        self.D = 0  # dist in m	(trunc to m)
        self.LPi = 250.0  # len of track	(should be int)
        self.TC = 0  # number of complete laps before last lap
        self.DiC = 0.0  # additional distance
        self.TTC = None  # time of last complete lap
        self.TRC = None  # time remaining to ride at beginning of
        # last lap

        # lap view model
        self.model = gtk.ListStore(gobject.TYPE_STRING, gobject.TYPE_STRING,
                                   gobject.TYPE_STRING, gobject.TYPE_STRING)

        uifile = os.path.join(metarace.UI_PATH, u'hour.ui')
        LOG.debug(u'Building event interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)

        self.frame = b.get_object(u'race_vbox')
        self.frame.connect(u'destroy', self.shutdown)

        # info pane
        self.info_expand = b.get_object(u'info_expand')
        b.get_object(u'race_info_evno').set_text(self.evno)
        self.showev = b.get_object(u'race_info_evno_show')
        self.prefix_ent = b.get_object(u'race_info_prefix')
        self.prefix_ent.connect(u'changed', self.editent_cb, u'pref')
        self.prefix_ent.set_text(self.event[u'pref'])
        self.info_ent = b.get_object(u'race_info_title')
        self.info_ent.connect(u'changed', self.editent_cb, u'info')
        self.info_ent.set_text(self.event[u'info'])

        self.time_lbl = b.get_object(u'race_info_time')
        self.time_lbl.modify_font(uiutil.DIGITFONT)

        # ctrl pane
        self.stat_but = uiutil.statbut(b.get_object(u'race_ctrl_stat_but'))
        self.stat_but.set_sensitive(True)
        self.infoline = b.get_object(u'race_ctrl_info')
        self.statusline = b.get_object(u'race_ctrl_status')
        self.statusline.modify_font(uiutil.DIGITFONT)

        # start timer and show window
        if ui:
            LOG.debug(u'Connecting event ui handlers')
            # riders pane
            t = gtk.TreeView(self.model)
            self.view = t
            t.set_reorderable(False)
            t.set_enable_search(False)
            t.set_rules_hint(True)

            # riders columns
            uiutil.mkviewcoltxt(t, u'Lap  ', 0, calign=0.0)
            uiutil.mkviewcoltxt(t, u'', 1, expand=True, calign=0.0)
            uiutil.mkviewcoltxt(t, u'Split', 2, calign=1.0)
            uiutil.mkviewcoltxt(t, u'Time', 3, calign=1.0)
            t.show()
            b.get_object(u'race_result_win').add(t)
            b.connect_signals(self)
