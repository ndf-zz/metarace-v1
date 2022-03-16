"""Generic timed event handler for time trial and pursuit track events."""

# Refer: UCI Regulations Part 3 "Track Races" 3.2.051 - 3.2.075
#        and 3.2.101 - 3.2.112

from __future__ import division

import gtk
import glib
import gobject
import os
import logging
from math import ceil

import metarace
from metarace import tod
from metarace import timy
from metarace import scbwin
from metarace import loghandler
from metarace import uiutil
from metarace import strops
from metarace import timerpane
from metarace import report
from metarace import jsonconfig

LOG = logging.getLogger(u'metarace.ittt')
LOG.setLevel(logging.DEBUG)

# config version string
EVENT_ID = u'ittt-2.0'

# startlist model columns
COL_BIB = 0
COL_FIRSTNAME = 1
COL_LASTNAME = 2
COL_CLUB = 3
COL_COMMENT = 4
COL_SEED = 5
COL_PLACE = 6
COL_START = 7
COL_FINISH = 8
COL_LASTLAP = 9
COL_SPLITS = 10

# scb function key mappings
key_reannounce = u'F4'  # (+CTRL) calls into delayed announce
key_startlist = u'F6'  # re-display running time (startlist)
key_results = u'F4'  # recalc/show result window

# timing function key mappings
key_armstart = u'F5'  # arm for start impulse
key_armlap_A = u'F7'  # arm for lap 'Front'
key_armlap_B = u'F8'  # arm for lap 'Back'
key_armfinish_A = u'F9'  # arm for finish impulse 'Front'
key_armfinish_B = u'F10'  # arm for finish impulse 'Back'
key_catch_A = u'F11'  # A rider catches B
key_catch_B = u'F12'  # B rider catches A

# extended function key mappings
key_abort = u'F5'  # + ctrl for clear/abort
key_falsestart = u'F6'  # + ctrl for false start
key_abort_A = u'F7'  # + ctrl abort A
key_abort_B = u'F8'  # + ctrl abort B


class ittt(object):

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or u'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort:  # override ctrl+f5
                    self.toidle()
                    return True
                elif key == key_reannounce:  # run delayed announce
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_falsestart:  # false start both lanes
                    self.falsestart()
                    return True
                elif key == key_abort_A:  # abort front straight rider
                    self.abortrider(self.fs)
                    return True
                elif key == key_abort_B:
                    self.abortrider(self.bs)
                    return True
            elif key[0] == u'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_armlap_A:
                    self.armlap(self.fs, self.chan_A)
                    return True
                elif key == key_armlap_B:
                    self.armlap(self.bs, self.chan_B)
                    return True
                elif key == key_armfinish_A:
                    self.armfinish(self.fs, self.chan_A)
                    return True
                elif key == key_armfinish_B:
                    self.armfinish(self.bs, self.chan_B)
                    return True
                elif key == key_catch_A:
                    self.catchrider(self.fs)
                    return True
                elif key == key_catch_B:
                    self.catchrider(self.bs)
                    return True
                elif key == key_startlist:
                    self.showtimerwin()
                    return True
                elif key == key_results:
                    self.do_places()
                    return True
        return False

    def do_places(self):
        """Show race result on scoreboard."""
        self.meet.scbwin = None
        self.timerwin = False  # TODO: bib width enhancement
        fmtplaces = []
        name_w = self.meet.scb.linelen - 11
        fmt = [(3, u'l'), (3, u'r'), u' ', (name_w, u'l'), (4, u'r')]
        if self.teamnames:
            name_w = self.meet.scb.linelen - 8
            fmt = [(3, u'l'), u' ', (name_w, u'l'), (4, u'r')]
        rcount = 0
        pcount = 0
        for r in self.riders:
            rcount += 1
            if r[COL_PLACE] is not None and r[COL_PLACE] != u'':
                pcount += 1
                plstr = r[COL_PLACE].decode(u'utf-8')
                if plstr.isdigit():
                    plstr = plstr + u'.'
                if not self.teamnames:
                    name_w = self.meet.scb.linelen - 11
                    name = strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                          r[COL_LASTNAME].decode(u'utf-8'),
                                          name_w,
                                          trunc=True)
                    club = r[COL_CLUB].decode(u'utf-8')
                    bib = r[COL_BIB].decode(u'utf-8')
                    fmtplaces.append([plstr, bib, name, club])
                else:
                    name = r[COL_FIRSTNAME].decode(u'utf-8')
                    club = r[COL_CLUB].decode(u'utf-8')
                    fmtplaces.append([plstr, name, club])
        evtstatus = u'Standings'
        if rcount > 0 and pcount == rcount:
            evtstatus = u'Result'

        self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                           head=self.meet.racenamecat(
                                               self.event),
                                           subhead=evtstatus.upper(),
                                           coldesc=fmt,
                                           rows=fmtplaces)
        self.meet.scbwin.reset()

    def todstr(self, col, cr, model, iter, data=None):
        """Format tod into text for listview."""
        ft = model.get_value(iter, COL_FINISH)
        if ft is not None:
            sp = model.get_value(iter, COL_LASTLAP)
            st = model.get_value(iter, COL_START)
            if st is None:
                st = tod.ZERO
            mstr = (ft - st).rawtime(self.precision)
            sstr = u''
            if sp is not None:
                sstr = u'/' + (ft - sp).rawtime(self.precision)
            cr.set_property('text', mstr + sstr)
        else:
            cr.set_property('text', u'')

    def setup_splits(self):
        """Prepare split data for the event based on distance."""
        track_n = None
        track_d = None
        track_l = None
        event_d = None
        self.splitlist = []
        self.splitmap = {}
        try:
            # note: this partially replicates get_distance from trackmeet
            track_n = float(self.meet.tracklen_n)
            track_d = float(self.meet.tracklen_d)
            track_l = track_n / track_d
            if self.units in [u'metres', u'meters']:
                event_d = float(self.distance)
            elif self.units == u'laps':
                event_d = track_n * float(self.distance) / track_d
        except Exception as e:
            LOG.warning(u'Unable to setup split points: %s', e)
        if event_d is not None and track_l is not None:
            LOG.debug(u'Track lap=%0.1f, Event dist=%0.1f', track_l, event_d)
            # add a dummy entry for the finish passing
            splitid = u'{0:0.0f}m'.format(event_d)
            self.splitlist.insert(0, splitid)
            # work backward from finish by half-laps, adding data holders
            count = 1
            while True:
                splitdist = event_d - (count * 0.5 * track_n / track_d)
                if splitdist > 15.0:  # token minimum first inter
                    splitid = u'{0:0.0f}m'.format(splitdist)
                    self.splitlist.insert(0, splitid)
                    self.splitmap[splitid] = {
                        u'dist': splitdist,
                        u'data': tod.todlist(splitid),
                    }
                else:
                    break
                count += 1
            LOG.debug(u'Configured %r splits: %r', len(self.splitlist),
                      self.splitlist)
            self.fs.splitlbls = self.splitlist
            self.bs.splitlbls = self.splitlist
        else:
            LOG.debug(u'Split points not available')

    def loadconfig(self):
        """Load race config from disk."""
        self.riders.clear()
        self.results.clear()

        # failsafe defaults -> dual timer, C0 start, PA/PB
        deftimetype = u'dual'
        defdistance = u''
        defdistunits = u'metres'
        defprecision = 3
        defchans = timy.CHAN_START
        defchana = timy.CHAN_PA
        defchanb = timy.CHAN_PB
        defautotime = False  # without splits, this is not reliable
        self.seedsrc = 1  # fetch seed from the rank col

        # type specific overrides
        if self.evtype in [u'pursuit race', u'team pursuit race']:
            self.difftime = True  # NOT CONFIGURABLE

        if self.evtype in [u'team pursuit', u'team pursuit race']:
            self.teampursuit = True
            defprecision = 2
        else:
            self.teampursuit = False

        cr = jsonconfig.config({
            u'event': {
                u'startlist': u'',
                u'id': EVENT_ID,
                u'start': None,
                u'lstart': None,
                u'fsbib': None,
                u'fsstat': u'idle',
                u'bsbib': None,
                u'bsstat': u'idle',
                u'showinfo': True,
                u'showcats': False,
                u'comments': [],
                u'distance': defdistance,
                u'distunits': defdistunits,
                u'precision': defprecision,
                u'chan_S': defchans,
                u'chan_A': defchana,
                u'chan_B': defchanb,
                u'autotime': defautotime,
                u'autospec': u'',
                u'inomnium': False,
                u'timetype': deftimetype,
            }
        })
        cr.add_section(u'event')
        cr.add_section(u'riders')
        cr.add_section(u'splits')
        cr.add_section(u'traces')
        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                LOG.error(u'Unable to read config: %s', e)
        else:
            LOG.info(u'%r not found, loading defaults', self.configfile)

        self.chan_S = strops.confopt_chan(cr.get(u'event', u'chan_S'),
                                          defchans)
        self.chan_A = strops.confopt_chan(cr.get(u'event', u'chan_A'),
                                          defchana)
        self.chan_B = strops.confopt_chan(cr.get(u'event', u'chan_B'),
                                          defchanb)
        self.comments = cr.get(u'event', u'comments')
        self.autospec = cr.get(u'event', u'autospec')
        self.distance = strops.confopt_dist(cr.get(u'event', u'distance'))
        self.units = strops.confopt_distunits(cr.get('event', 'distunits'))
        # override event configuration from program entry
        if self.event[u'laps']:
            self.units = u'laps'
            self.distance = strops.confopt_posint(self.event[u'laps'],
                                                  self.distance)
            LOG.debug(u'Event distance set by program entry: %r laps',
                      self.distance)
        # re-initialise split data for the event
        if not self.readonly:
            self.setup_splits()

        self.set_timetype(cr.get(u'event', u'timetype'))
        self.autotime = strops.confopt_bool(cr.get(u'event', u'autotime'))
        self.update_expander_lbl_cb()
        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get(u'event', u'showinfo')))
        self.showcats = strops.confopt_bool(cr.get(u'event', u'showcats'))
        self.inomnium = strops.confopt_bool(cr.get(u'event', u'inomnium'))
        if self.inomnium:
            self.seedsrc = 3  # read seeding from points standing
        self.precision = strops.confopt_posint(cr.get(u'event', u'precision'),
                                               3)

        # re-load starters and results
        self.onestart = False
        for r in cr.get(u'event', u'startlist').split():
            nr = [r, u'', u'', u'', u'', u'', u'', None, None, None, None]
            co = u''
            st = None
            ft = None
            lt = None
            sp = {}
            if cr.has_option(u'riders', r):
                ril = cr.get(u'riders', r)
                if len(ril) >= 1:  # save comment for stimes
                    co = ril[0]
                if len(ril) >= 2:  # write heat into rec
                    nr[COL_SEED] = ril[1]
                if len(ril) >= 4:  # Start ToD and others
                    st = tod.mktod(ril[3])
                    if st is not None:
                        self.onestart = True
                if len(ril) >= 5:  # Finish ToD
                    ft = tod.mktod(ril[4])
                if len(ril) >= 6:  # start of last lap ToD
                    lt = tod.mktod(ril[5])
            dbr = self.meet.rdb.getrider(r, self.series)
            if dbr is not None:
                for i in range(1, 4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)  # unicode
            nri = self.riders.append(nr)
            if not self.readonly:
                # skip fetching traces and split if opened readonly
                if cr.has_option(u'traces', r):
                    self.traces[r] = cr.get(u'traces', r)
                if cr.has_option(u'splits', r):
                    rsplit = cr.get(u'splits', r)
                    for sid in rsplit:
                        sp[sid] = tod.mktod(rsplit[sid])
            self.settimes(nri, st, ft, lt, sp, doplaces=False, comment=co)
        if self.series and u't' in self.series:
            self.teamnames = True
            if u'pursuit' in self.evtype:
                LOG.debug(u'Forced precision 2 for team pursuit')
                self.precision = 2
        self.placexfer()

        # re-join any existing timer state
        curstart = tod.mktod(cr.get(u'event', u'start'))
        lstart = tod.mktod(cr.get(u'event', u'lstart'))
        if lstart is None:
            lstart = curstart  # can still be None if start not set
        dorejoin = False
        # Front straight
        fsstat = cr.get(u'event', u'fsstat')
        if fsstat in [u'running', u'load']:  # running with no start gets load
            self.fs.setrider(cr.get(u'event', u'fsbib'))  # will set 'load'
            if fsstat == u'running' and curstart is not None:
                self.fs.start(curstart)  # overrides to 'running'
                dorejoin = True
        # Back straight
        bsstat = cr.get(u'event', u'bsstat')
        if bsstat in [u'running', u'load']:  # running with no start gets load
            self.bs.setrider(cr.get(u'event', u'bsbib'))  # will set 'load'
            if bsstat == u'running' and curstart is not None:
                self.bs.start(curstart)  # overrides to 'running'
                dorejoin = True

        if not self.onestart and self.autospec:
            self.meet.autostart_riders(self,
                                       self.autospec,
                                       infocol=self.seedsrc)
        if dorejoin:
            self.torunning(curstart, lstart)
        elif self.timerstat == u'idle':
            glib.idle_add(self.fs.grab_focus)

        # After load complete - check config and report.
        eid = cr.get(u'event', u'id')
        if eid and eid != EVENT_ID:
            LOG.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            LOG.error(u'Attempt to save readonly event')
            return
        cw = jsonconfig.config()
        cw.add_section(u'event')

        # save basic race properties
        cw.set(u'event', u'distance', self.distance)
        cw.set(u'event', u'distunits', self.units)
        cw.set(u'event', u'chan_S', self.chan_S)
        cw.set(u'event', u'chan_A', self.chan_A)
        cw.set(u'event', u'chan_B', self.chan_B)
        cw.set(u'event', u'autospec', self.autospec)
        cw.set(u'event', u'timetype', self.timetype)
        cw.set(u'event', u'autotime', self.autotime)
        cw.set(u'event', u'startlist', self.get_startlist())
        cw.set(u'event', u'inomnium', self.inomnium)
        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'showcats', self.showcats)
        cw.set(u'event', u'precision', self.precision)
        cw.set(u'event', u'comments', self.comments)

        # extract and save timerpane config for interrupted run
        if self.curstart is not None:
            cw.set(u'event', u'start', self.curstart.rawtime())
        if self.lstart is not None:
            cw.set(u'event', u'lstart', self.lstart.rawtime())
        cw.set(u'event', u'fsstat', self.fs.getstatus())
        cw.set(u'event', u'fsbib', self.fs.getrider())
        cw.set(u'event', u'bsstat', self.bs.getstatus())
        cw.set(u'event', u'bsbib', self.bs.getrider())

        cw.add_section(u'riders')
        cw.add_section(u'traces')
        cw.add_section(u'splits')

        # save out all starters
        for r in self.riders:
            rno = r[COL_BIB].decode(u'utf-8')
            # place is saved for info only
            slice = [
                r[COL_COMMENT].decode(u'utf-8'), r[COL_SEED].decode(u'utf-8'),
                r[COL_PLACE].decode(u'utf-8')
            ]
            tl = [r[COL_START], r[COL_FINISH], r[COL_LASTLAP]]
            for t in tl:
                if t is not None:
                    slice.append(t.rawtime())
                else:
                    slice.append(None)
            cw.set(u'riders', rno, slice)

            # save timing traces
            if rno in self.traces:
                cw.set(u'traces', rno, self.traces[rno])

            # save split times
            if r[COL_SPLITS]:
                rs = {}
                for sp in r[COL_SPLITS]:
                    if r[COL_SPLITS][sp] is not None:
                        st = r[COL_SPLITS][sp].rawtime()
                        rs[sp] = st
                cw.set(u'splits', rno, rs)

        cw.set(u'event', u'id', EVENT_ID)
        LOG.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        cnt = 0
        sec = report.dual_ittt_startlist()
        sec.showheats = True
        if self.timetype == u'single':
            sec.set_single()
            if u't' in self.series and self.series != u'tmsl':
                # TMSL is for madison support events
                sec.showheats = True

        headvec = [
            u'Event', self.evno, u':', self.event[u'pref'], self.event[u'info']
        ]
        if not program:
            headvec.append(u'- Start List')
        sec.heading = u' '.join(headvec)

        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        if self.event[u'reco']:
            sec.footer = self.event[u'reco']
        if self.event[u'plac']:
            sec.lines = self.get_heats(placeholders=self.event[u'plac'])
        else:
            sec.lines = self.get_heats()
        ret.append(sec)
        return ret

    def sort_startlist(self, x, y):
        """Comparison function for ttt seeding."""
        if x[1] == y[1]:  # same seed? revert to bib ascending
            return cmp(x[2], y[2])
        else:
            return cmp(x[1], y[1])

    def sort_heats(self, x, y):
        """Comparison function for ttt heats."""
        (xh, xl) = strops.heatsplit(x[0])
        (yh, yl) = strops.heatsplit(y[0])
        if xh == yh:
            return cmp(xl, yl)
        else:
            return cmp(xh, yh)

    def reorder_startlist(self):
        """Reorder model according to the seeding field."""
        if len(self.riders) > 1:
            auxmap = []
            cnt = 0
            for r in self.riders:
                auxmap.append([
                    cnt,
                    strops.riderno_key(r[COL_SEED]),
                    strops.riderno_key(r[COL_BIB])
                ])
                cnt += 1
            auxmap.sort(self.sort_startlist)
            self.riders.reorder([a[0] for a in auxmap])

    def get_heats(self, placeholders=0, cats=None):
        """Return a list of heats in the event."""
        ret = []

        # arrange riders by seeding
        self.reorder_startlist()

        # then build aux map of heats
        hlist = []
        emptyrows = False
        count = len(self.riders)
        if count < placeholders:
            count = placeholders
            miss = 2000
            while len(self.riders) < count:
                self.addrider(unicode(miss))  # WARNING!
                miss += 1
        blanknames = False
        teams = False
        if u't' in self.series:  # Team no hack
            teams = True
        if placeholders > 0:
            blanknames = True
        if self.timetype == u'single':
            for r in self.riders:
                rno = r[COL_BIB].decode(u'utf-8')
                info = None
                if cats and rno in cats:
                    info = cats[rno]
                rh = self.meet.newgetrider(rno, self.series)
                rname = u''
                heat = unicode(count) + u'.1'
                if rh is not None:
                    if not teams:
                        rname = rh[u'namestr']
                    else:
                        rname = rh[u'first']
                        if teams:
                            info = []
                            col = u'black'
                            for trno in strops.reformat_riderlist(
                                    rh[u'note']).split():
                                trh = self.meet.newgetrider(trno)  #!! SERIES?
                                if trh is not None:
                                    if self.series == u'tmsl':
                                        trno = col
                                        col = u'red'
                                    info.append([trno, trh[u'namestr'], None])
                    # consider partners here
                    if rh[u'cat'] and u'tandem' in rh[u'cat'].lower():
                        ph = self.meet.newgetrider(rh[u'note'], self.series)
                        if ph is not None:
                            info = [[
                                u' ', ph[u'namestr'] + u' - Pilot',
                                ph[u'ucicode']
                            ]]
                if teams:  # Team no hack
                    rno = u' '  # force name
                hlist.append([heat, rno, rname, info])
                # all heats are one up
                count -= 1
        else:
            hno = int(ceil(0.5 * count))
            lane = 1
            for r in self.riders:
                rno = r[COL_BIB].decode(u'utf-8')
                rh = self.meet.newgetrider(rno, self.series)
                rname = u''
                heat = unicode(hno) + u'.' + unicode(lane)
                info = None
                if cats and rno in cats:
                    info = cats[rno]
                if rh is not None:
                    if not teams:
                        rname = rh[u'namestr']
                    else:
                        rname = rh[u'first']
                        if teams:
                            info = []
                            for trno in strops.reformat_riderlist(
                                    rh[u'note']).split():
                                trh = self.meet.newgetrider(trno)  #!! SERIES?
                                if trh is not None:
                                    info.append([trno, trh[u'namestr'], None])
                    # consider partners here
                    if rh[u'cat'] and u'tandem' in rh[u'cat'].lower():
                        ph = self.meet.newgetrider(rh[u'note'], self.series)
                        if ph is not None:
                            info = [[
                                u' ', ph[u'namestr'] + u' - Pilot',
                                ph[u'ucicode']
                            ]]
                if u't' in self.series:  # Team no hack
                    rno = u' '  # force name
                hlist.append([heat, rno, rname, info])
                lane += 1
                if lane > 2:
                    hno -= 1
                    lane = 1

        # sort the heatlist into front/back heat 1, 2, 3 etc
        hlist.sort(self.sort_heats)

        lh = None
        lcnt = 0
        rec = []
        for r in hlist:
            (h, l) = strops.heatsplit(r[0])
            if lh is not None and (h != lh or lcnt > 1):
                lcnt = 0
                ret.append(rec)
                rec = []
            heat = str(h)
            if self.difftime:  # override heat if 'final'
                heat = u'-'
            if blanknames and len(r[1]) > 3:  # HACK for miss
                r[1] = u''
                r[2] = u''
                r[3] = None
            rec.extend([heat, r[1], r[2], r[3]])
            lcnt += 1
            lh = h
        if len(rec) > 0:
            ret.append(rec)
        return ret

    def get_startlist(self):
        """Return a list of bibs in the rider model."""
        ret = []
        for r in self.riders:
            ret.append(r[COL_BIB])
        return u' '.join(ret)

    def delayed_announce(self):
        """Initialise the announcer's screen after a delay."""
        if self.winopen:
            # clear page
            self.meet.txt_clear()
            self.meet.txt_title(u' '.join([
                u'Event', self.evno, u':', self.event[u'pref'],
                self.event[u'info']
            ]))
            self.meet.txt_line(1)
            self.meet.txt_line(7)

            # fill in front straight
            fbib = self.fs.getrider()
            if fbib:
                r = self.getrider(fbib)
                if r is not None:
                    clubstr = u''
                    tcs = r[COL_CLUB].decode(u'utf-8')
                    if tcs and tcs <= 3:
                        clubstr = u'(' + tcs + u')'
                    namestr = strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                             r[COL_LASTNAME].decode(u'utf-8'),
                                             24,
                                             trunc=True)
                    placestr = u'   '  # 3 ch
                    if r[COL_PLACE]:
                        placestr = strops.truncpad(
                            r[COL_PLACE].decode(u'utf-8') + u'.', 3)
                    bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3,
                                             u'r')
                    tmstr = u''
                    if r[COL_START] is not None and r[COL_FINISH] is not None:
                        tmstr = (r[COL_FINISH] - r[COL_START]).rawtime(3)
                    cmtstr = u''
                    if r[COL_COMMENT]:
                        cmtstr = strops.truncpad(
                            u'[' + r[COL_COMMENT].decode(u'utf-8').strip() +
                            u']', 38, u'r')
                    self.meet.txt_postxt(3, 0, u'        Front Straight')
                    self.meet.txt_postxt(
                        4, 0, u' '.join([placestr, bibstr, namestr, clubstr]))
                    self.meet.txt_postxt(5, 26,
                                         strops.truncpad(tmstr, 12, u'r'))
                    self.meet.txt_postxt(6, 0, cmtstr)

            # fill in back straight
            bbib = self.bs.getrider()
            if bbib:
                r = self.getrider(bbib)
                if r is not None:
                    clubstr = u''
                    tcs = r[COL_CLUB].decode(u'utf-8')
                    if tcs and tcs <= 3:
                        clubstr = u'(' + tcs + u')'
                    namestr = strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                             r[COL_LASTNAME].decode(u'utf-8'),
                                             24,
                                             trunc=True)
                    placestr = u'   '  # 3 ch
                    if r[COL_PLACE]:
                        placestr = strops.truncpad(
                            r[COL_PLACE].decode(u'utf-8') + u'.', 3)
                    bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3,
                                             u'r')
                    tmstr = u''
                    if r[COL_START] is not None and r[COL_FINISH] is not None:
                        tmstr = (r[COL_FINISH] - r[COL_START]).rawtime(3)
                    cmtstr = u''
                    if r[COL_COMMENT]:
                        cmtstr = strops.truncpad(
                            u'[' + r[COL_COMMENT].decode(u'utf-8').strip() +
                            u']', 38, u'r')
                    self.meet.txt_postxt(3, 42, u'        Back Straight')
                    self.meet.txt_postxt(
                        4, 42, u' '.join([placestr, bibstr, namestr, clubstr]))
                    self.meet.txt_postxt(5, 68,
                                         strops.truncpad(tmstr, 12, u'r'))
                    self.meet.txt_postxt(6, 42, cmtstr)

            # fill in leaderboard/startlist
            count = 0
            curline = 9
            posoft = 0
            for r in self.riders:
                count += 1
                if count == 19:
                    curline = 9
                    posoft = 42

                clubstr = u''
                tcs = r[COL_CLUB].decode(u'utf-8')
                if tcs and tcs <= 3:
                    clubstr = u' (' + tcs + u')'

                namestr = u''
                if not self.teamnames:
                    namestr = strops.truncpad(
                        strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                       r[COL_LASTNAME].decode(u'utf-8'),
                                       20 - len(clubstr)) + clubstr, 20)
                else:
                    namestr = strops.truncpad(
                        r[COL_FIRSTNAME].decode(u'utf-8'), 20)
                placestr = u'   '  # 3 ch
                if r[COL_PLACE]:
                    placestr = strops.truncpad(
                        r[COL_PLACE].decode(u'utf-8') + u'.', 3)
                bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3, u'r')
                tmstr = u'         '  # 9 ch
                if r[COL_START] is not None and r[COL_FINISH] is not None:
                    tmstr = strops.truncpad(
                        (r[COL_FINISH] - r[COL_START]).rawtime(3), 9, u'r')
                self.meet.txt_postxt(
                    curline, posoft,
                    u' '.join([placestr, bibstr, namestr, tmstr]))
                curline += 1

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
        prfile = os.path.join(metarace.UI_PATH, u'ittt_properties.ui')
        LOG.debug(u'Building event properties from %r', prfile)
        b = gtk.Builder()
        b.add_from_file(prfile)
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.meet.window)

        tt = b.get_object(u'race_score_type')
        if self.timetype == u'dual':
            tt.set_active(0)
        else:
            tt.set_active(1)
        di = b.get_object(u'race_dist_entry')
        if self.distance is not None:
            di.set_text(str(self.distance))
        else:
            di.set_text(u'')
        du = b.get_object(u'race_dist_type')
        if self.units == u'laps':
            du.set_active(1)
        else:
            du.set_active(0)
        chs = b.get_object(u'race_stchan_combo')
        chs.set_active(self.chan_S)
        cha = b.get_object(u'race_achan_combo')
        cha.set_active(self.chan_A)
        chb = b.get_object(u'race_bchan_combo')
        chb.set_active(self.chan_B)
        aa = b.get_object(u'race_autoarm_toggle')
        aa.set_active(self.autotime)
        se = b.get_object(u'race_series_entry')
        se.set_text(self.series)
        as_e = b.get_object(u'auto_starters_entry')
        as_e.set_text(self.autospec)
        olddistance = self.meet.get_distance(self.distance, self.units)

        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            if tt.get_active() == 1:
                self.set_timetype(u'single')
            else:
                self.set_timetype(u'dual')
            dval = di.get_text().decode(u'utf-8')
            if dval.isdigit():
                self.distance = int(dval)
            else:
                self.distance = None
            if du.get_active() == 0:
                self.units = u'metres'
            else:
                self.units = u'laps'

            # if distance has changed, re-initialise split data
            newdistance = self.meet.get_distance(self.distance, self.units)
            if newdistance != olddistance:
                LOG.debug(u'Event distance changed from %r to %r', olddistance,
                          newdistance)
                self.setup_splits()

            # disable autotime if splits are not known
            self.autotime = aa.get_active()
            if not self.splitlist:
                self.autotime = False
                LOG.info(u'No splits configured, autotime disabled.')

            self.chan_S = chs.get_active()
            self.chan_A = cha.get_active()
            self.chan_B = chb.get_active()

            # update series
            ns = se.get_text().decode(u'utf-8')
            if ns != self.series:
                self.series = ns
                self.event[u'seri'] = ns

            # update auto startlist spec
            nspec = as_e.get_text().decode(u'utf-8')
            if nspec != self.autospec:
                self.autospec = nspec
                if self.autospec:
                    self.meet.autostart_riders(self,
                                               self.autospec,
                                               infocol=self.seedsrc)

            # xfer starters if not empty
            slist = strops.riderlist_split(
                b.get_object(u'race_starters_entry').get_text().decode(
                    u'utf-8'), self.meet.rdb, self.series)

            # if no starters yet - automatically seed by order entered
            if len(self.riders) == 0:
                cnt = 1
                for s in slist:
                    self.addrider(s, cnt)
                    cnt += 1
            else:
                for s in slist:
                    self.addrider(s)
            glib.idle_add(self.delayed_announce)
        else:
            LOG.debug(u'Edit event properties cancelled')

        # if prefix is empty, grab input focus
        if not self.prefix_ent.get_text():
            self.prefix_ent.grab_focus()
        dlg.destroy()

    def result_gen(self):
        """Generator function to export a final result."""
        for r in self.riders:
            bib = r[COL_BIB].decode(u'utf-8')
            rank = None
            time = None
            info = None
            cmts = r[COL_COMMENT].decode(u'utf-8')
            if cmts in [u'caught', u'rel', u'w/o']:
                info = cmts
            if self.onestart:
                pls = r[COL_PLACE].decode(u'utf-8')
                if pls:
                    if pls.isdigit():
                        rank = int(pls)
                    else:
                        rank = pls
                if r[COL_FINISH] is not None:
                    time = (r[COL_FINISH] - r[COL_START]).truncate(
                        self.precision)

            yield [bib, rank, time, info]

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        self.placexfer()
        ret = []
        sec = report.section()
        sec.heading = u'Event ' + self.evno + u': ' + u' '.join(
            [self.event[u'pref'], self.event[u'info']]).strip()
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        sec.lines = []
        ftime = None
        downprec = min(self.precision, 2)
        rcount = 0
        pcount = 0
        for r in self.riders:
            rcount += 1
            rno = r[COL_BIB].decode(u'utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            if rh is None:
                LOG.warning(u'Rider info not found %r', rno)
                continue

            rank = None
            rname = u''
            plink = u''
            if not self.teamnames:
                rname = rh[u'namestr']
            else:  # Team no hack
                rno = u' '  # force name
                rname = rh[u'first']
            rtime = None
            rcat = None
            # consider partners here
            if rh[u'cat'] and u'tandem' in rh[u'cat'].lower():
                ph = self.meet.newgetrider(rh[u'note'], self.series)
                if ph is not None:
                    plink = [
                        u'', u'', ph[u'namestr'] + u' - Pilot', ph[u'ucicode'],
                        u'', u'', u''
                    ]
            if self.event[u'cate']:
                if rh[u'cat']:
                    rcat = rh[u'cat']
            if rh[u'ucicode']:
                rcat = rh[u'ucicode']  # overwrite by force
            info = None
            dtime = None
            if self.onestart:
                pls = r[COL_PLACE].decode(u'utf-8')
                if pls:
                    if pls.isdigit():
                        rank = pls + u'.'
                    else:
                        rank = pls
                    pcount += 1
                if r[COL_FINISH] is not None:
                    time = (r[COL_FINISH] - r[COL_START]).truncate(
                        self.precision)
                    if ftime is None:
                        ftime = time
                    else:
                        dtime = '+' + (time - ftime).rawtime(downprec)
                    if r[COL_START] != tod.ZERO or self.precision != 3:
                        rtime = time.rawtime(self.precision)
                    else:
                        rtime = time.rawtime(2) + u'\u2007'
                elif r[COL_COMMENT]:
                    rtime = str(r[COL_COMMENT])

            sec.lines.append([rank, rno, rname, rcat, rtime, dtime, plink])
            # then add team members if relevant
            if u't' in self.series:
                for trno in strops.reformat_riderlist(rh[u'note']).split():
                    trh = self.meet.newgetrider(trno)  #!! SERIES?
                    if trh is not None:
                        trname = trh[u'namestr']
                        trinf = trh[u'ucicode']
                        sec.lines.append(
                            [None, trno, trname, trinf, None, None, None])
        sv = []
        if substr:
            sv.append(sv)
        if self.onestart:
            if rcount > 0 and pcount < rcount:
                sv.append(u'STANDINGS')
            else:
                sv.append(u'Result')
        sec.subheading = u' - '.join(sv)

        ret.append(sec)

        # comment
        if self.comments:
            sec = report.bullet_text()
            sec.heading = u'Decisions of the commissaires panel'
            for c in self.comments:
                sec.lines.append([None, self.comment])
            ret.append(sec)

        return ret

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode(u'utf-8')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode(u'utf-8')
        self.update_expander_lbl_cb()

    def update_expander_lbl_cb(self):
        """Update race info expander label."""
        self.info_expand.set_label(self.meet.infoline(self.event))

    def clear_rank(self, cb):
        """Run callback once in main loop idle handler."""
        cb(u'')
        return False

    def lap_trig(self, sp, t):
        """Register manual lap trigger."""
        # fetch cur split and sid from sp, making sure on a whole lap
        if sp.on_halflap():
            sp.lap_up()
        sid = sp.get_sid()  # might be None
        rank = self.insert_split(sid, t - self.curstart, sp.getrider())
        prev = None
        if sp.split > 1:
            prev = sp.getsplit(sp.split - 2)
        self.log_lap(sp.getrider(), sid, self.curstart, t, prev)
        # save inter time to split cache in timer, and advance split pointer
        sp.intermed(t)
        sp.lap_up()

        if self.difftime:
            if self.diffstart is None or self.difflane is sp:
                self.diffstart = t
                self.difflane = sp
            else:
                # 'other' lane has previously completed this lap
                so = self.t_other(sp)
                if so.split == sp.split and self.diffstart is not None:
                    dt = t - self.diffstart
                    if dt < 4:
                        sp.difftime(dt)
                    self.difflane = None
                    self.diffstart = None
        if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
            if rank is not None:
                rlbl = u'({}) {}:'.format(rank + 1, sid)
            else:
                rlbl = u'{}:'.format(sid)
            if sp is self.fs:
                self.meet.scbwin.setr1(rlbl)
                glib.timeout_add_seconds(4, self.clear_rank,
                                         self.meet.scbwin.setr1)
                self.meet.txt_postxt(
                    5, 8,
                    strops.truncpad(rlbl, 17) + u' ' + self.fs.get_time())
            else:
                self.meet.scbwin.setr2(rlbl)
                glib.timeout_add_seconds(4, self.clear_rank,
                                         self.meet.scbwin.setr2)
                self.meet.txt_postxt(
                    5, 50,
                    strops.truncpad(rlbl, 17) + u' ' + self.bs.get_time())

    def fin_trig(self, sp, t):
        """Register a manual finish trigger."""
        sp.finish(t)
        if self.difftime:
            if self.diffstart is None or self.difflane is sp:
                self.diffstart = t
                self.difflane = sp
            else:
                so = self.t_other(sp)
                if so.split == sp.split and self.diffstart is not None:
                    dt = t - self.diffstart
                    if dt < 4:
                        sp.difftime(dt)
                    self.difflane = None
                    self.diffstart = None
        # fetch start of last lap if possible
        prev = None
        if sp.split > 1 and not sp.on_halflap():
            # only take prev time for a whole lap at finish
            if self.splitlist and sp.split == len(self.splitlist) - 1:
                prev = sp.getsplit(sp.split - 2)
            else:
                LOG.warning(u'Rider %r manual finish with incorrect splits',
                            sp.getrider())
        if prev is None:
            LOG.warning(u'Last lap data not available for %r', sp.getrider())

        # update model with result
        ri = self.getiter(sp.getrider())
        if ri is not None:
            self.settimes(ri, self.curstart, t, prev, sp.splits)
        else:
            LOG.warning(u'Rider not in model, finish time not stored')
        self.log_elapsed(sp.getrider(), self.curstart, t, sp.get_sid(), prev)

        # then report to scb, announce and result
        if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
            place = self.riders.get_value(ri, COL_PLACE)
            if sp is self.fs:
                elap = t - self.curstart
                self.meet.scbwin.setr1(u'(' + place + u')')
                self.meet.scbwin.sett1(self.fs.get_time())
                self.meet.gemini.set_time(self.fs.get_time(), 0)
                if self.timetype == u'single':  # Speed/TTB is hack mode
                    dist = self.meet.get_distance(self.distance, self.units)
                    if dist is not None:
                        spstr = elap.speedstr(dist).strip()
                        glib.timeout_add_seconds(1, self.clear_200_ttb,
                                                 self.meet.scbwin, u'Avg:',
                                                 spstr.rjust(12))
                    else:
                        glib.timeout_add_seconds(2, self.clear_200_ttb,
                                                 self.meet.scbwin)
            else:
                self.meet.scbwin.setr2(u'(' + place + u')')
                self.meet.scbwin.sett2(self.bs.get_time())
                self.meet.gemini.set_time(self.bs.get_time(), 1)
            self.meet.gemini.show_dual()
        # call for a delayed announce...
        glib.idle_add(self.delayed_announce)
        # AND THEN, if other lane not armed, export result
        if self.t_other(sp).getstatus() != 'armfin':
            self.meet.delayed_export()

    def timercb(self, e):
        """Handle a timer event."""
        chan = strops.chan2id(e.chan)
        if self.timerstat == u'armstart':
            if chan == self.chan_S:
                self.torunning(e)
        elif self.timerstat == u'autotime':
            LOG.warning(u'AUTOTIMER CALLBACK')
        elif self.timerstat == u'running':
            if chan == self.chan_A or (self.timetype == u'single'
                                       and self.chan_B):
                stat = self.fs.getstatus()
                if stat == u'armint':
                    self.lap_trig(self.fs, e)
                elif stat == u'armfin':
                    self.fin_trig(self.fs, e)
            elif chan == self.chan_B:
                stat = self.bs.getstatus()
                if stat == u'armint':
                    self.lap_trig(self.bs, e)
                elif stat == u'armfin':
                    self.fin_trig(self.bs, e)
        return False

    def timeout(self):
        """Update running time and emit to scoreboards."""
        if not self.winopen:
            return False
        now = tod.now()
        if self.fs.status in [u'running', u'armint', u'armfin']:
            self.fs.runtime(now - self.lstart)
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                elapstr = self.fs.get_time()
                self.meet.scbwin.sett1(elapstr)
                self.meet.gemini.set_time(elapstr[0:12], lane=0)
        if self.bs.status in [u'running', u'armint', u'armfin']:
            self.bs.runtime(now - self.lstart)
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                elapstr = self.bs.get_time()
                self.meet.scbwin.sett2(elapstr)
                self.meet.gemini.set_time(elapstr[0:12], lane=1)
        self.meet.gemini.show_dual()
        return True

    def show_200_ttb(self, scb):
        """Display time to beat."""
        if len(self.results) > 0:
            scb.setr2(u'Fastest:')
            scb.sett2(self.results[0].timestr(3))
        return False

    def clear_200_ttb(self, scb, r2=u'', t2=u''):
        """Clear time to beat."""
        scb.setr2(r2)
        scb.sett2(t2)
        return False

    def torunning(self, st, lst=None):
        """Set timer running."""
        if self.fs.status == u'armstart':
            self.fs.start(st)
        if self.bs.status == u'armstart':
            self.bs.start(st)
        self.curstart = st
        if lst is None:
            lst = tod.now()
        self.lstart = lst
        self.diffstart = None
        self.difflane = None
        if self.autotime:
            self.timerstat = u'autotime'
        else:
            self.timerstat = u'running'
        self.onestart = True
        if self.timetype == u'single':
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                glib.timeout_add_seconds(3, self.show_200_ttb,
                                         self.meet.scbwin)

    def clearplaces(self):
        """Clear rider places."""
        for r in self.riders:
            r[COL_PLACE] = u''

    def getrider(self, bib):
        """Return temporary reference to model row."""
        ret = None
        for r in self.riders:
            if r[COL_BIB].decode(u'utf-8') == bib:
                ret = r
                break
        return ret

    def addrider(self, bib=u'', info=None):
        """Add specified rider to race model."""
        istr = u''
        if info is not None:
            istr = unicode(info)
        nr = [bib, u'', u'', u'', u'', istr, u'', None, None, None, None]
        ri = self.getrider(bib)
        if ri is None:  # adding a new record
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                for i in range(1, 4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)
            self.riders.append(nr)
        else:
            # rider exists in model, just update the seed value
            ri[COL_SEED] = istr

    def editcol_db(self, cell, path, new_text, col):
        """Cell update with writeback to meet."""
        new_text = new_text.decode(u'utf-8').strip()
        self.riders[path][col] = new_text
        rno = self.riders[path][COL_BIB].decode(u'utf-8')
        glib.idle_add(self.meet.rider_edit, rno, self.series, col, new_text)

    def placexfer(self):
        """Transfer places into model."""
        self.clearplaces()
        count = 0
        place = 1
        for t in self.results:
            bib = t[0].refid
            if t[0] > tod.FAKETIMES[u'max']:
                if t[0] == tod.FAKETIMES[u'dsq']:
                    place = u'dsq'
                elif t[0] == tod.FAKETIMES[u'ntr']:
                    place = u'ntr'
                elif t[0] == tod.FAKETIMES[u'rel']:
                    place = u'rel'
                elif t[0] == tod.FAKETIMES[u'dns']:
                    place = u'dns'
                elif t[0] == tod.FAKETIMES[u'dnf']:
                    place = u'dnf'
            else:
                place = self.results.rank(bib) + 1
            i = self.getiter(bib)
            if i is not None:
                if place == u'comment':  # superfluous but ok
                    place = self.riders.get_value(i, COL_COMMENT)
                self.riders.set_value(i, COL_PLACE, unicode(place))
                self.riders.swap(self.riders.get_iter(count), i)
                count += 1
            else:
                LOG.warning(u'Rider %r not found in model, check places', bib)

    def getiter(self, bib):
        """Return temporary iterator to model row."""
        i = self.riders.get_iter_first()
        while i is not None:
            if self.riders.get_value(i, COL_BIB).decode(u'utf-8') == bib:
                break
            i = self.riders.iter_next(i)
        return i

    def settimes(self,
                 iter,
                 st=None,
                 ft=None,
                 lt=None,
                 splits=None,
                 doplaces=True,
                 comment=None):
        """Transfer race times into rider model."""
        bib = self.riders.get_value(iter, COL_BIB)
        # clear result for this bib
        self.results.remove(bib)
        # assign tods
        self.riders.set_value(iter, COL_START, st)
        self.riders.set_value(iter, COL_FINISH, ft)
        self.riders.set_value(iter, COL_LASTLAP, lt)
        # save result
        if st is None:
            st = tod.ZERO
        if ft is not None:
            lastlap = None
            if lt is not None:
                lastlap = ft - lt
            self.results.insert(ft - st, lastlap, bib, prec=self.precision)
        else:  # DNF/Catch/etc
            self.results.insert(comment, None, bib, prec=self.precision)
        if splits is not None:
            # save reference to rider model
            self.riders.set_value(iter, COL_SPLITS, splits)
            for sid in splits:
                # and transfer into inter-ranks
                if sid in self.splitmap:
                    self.splitmap[sid][u'data'].remove(bib)
                    if splits[sid] is not None:
                        splitval = splits[sid] - st
                        self.splitmap[sid][u'data'].insert(splitval,
                                                           None,
                                                           bib,
                                                           prec=self.precision)
                else:
                    LOG.info(u'Unknown split %r for rider %r', sid, bib)

        # copy annotation into model if provided, or clear
        if comment:
            self.riders.set_value(iter, COL_COMMENT, comment)
        else:
            self.riders.set_value(iter, COL_COMMENT, u'')
        # if reqd, do places
        if doplaces:
            self.placexfer()

    def insert_split(self, sid, st, bib):
        """Insert a rider split into correct lap."""
        ret = None
        if sid in self.splitmap:
            self.splitmap[sid][u'data'].insert(st, None, bib)
            ret = self.splitmap[sid][u'data'].rank(bib)
        else:
            LOG.debug(u'No ranking for rider %r at unknown split %r', bib, sid)
        return ret

    def armstart(self):
        """Arm timer for start trigger."""
        if self.timerstat == u'armstart':
            self.toload()
        elif self.timerstat in [u'load', u'idle']:
            self.toarmstart()

    def disable_autotime(self):
        """Cancel a running autotime for manual intervention."""
        if self.timerstat == u'autotime':
            LOG.error(u'DISABLE AUTOTIMER')
            self.timerstat = u'running'

    def armlap(self, sp, cid):
        """Arm timer for a manual lap split."""
        if self.timerstat == u'autotime':
            LOG.info(u'Autotime disabled by manual intervention.')
            self.disable_autotime()
        if self.timerstat == u'running':
            if sp.getstatus() in [u'caught', u'running']:
                if sp.on_halflap():
                    sp.lap_up()
                if sp.split < len(self.splitlist) - 1:
                    sp.toarmint()
                else:
                    LOG.info(u'Rider %r approaching last lap, armfinish',
                             sp.getrider())
                    sp.toarmfin()
                self.meet.main_timer.arm(cid)
            elif sp.getstatus() == u'armint':
                sp.torunning()
                self.meet.main_timer.dearm(cid)

    def lanestr(self, sp):
        """Return f for front and b for back straight."""
        ret = u'f'
        if sp is self.bs:
            ret = u'b'
        return ret

    def abortrider(self, sp):
        """Abort the selected lane."""
        if sp.getstatus() not in [u'idle', u'caught', u'finish']:
            bib = sp.getrider()
            ri = self.getiter(bib)
            if ri is not None:
                self.settimes(ri,
                              st=self.curstart,
                              splits=sp.splits,
                              comment=u'abort')
            sp.tofinish()
            self.meet.timer_log_msg(bib, u'- Abort -')
            glib.idle_add(self.delayed_announce)

    def catchrider(self, sp):
        """Selected lane has caught other rider."""
        if not self.difftime:
            # heat is not terminated by catch of rider, just log details
            LOG.info(u'Rider %r catch ignored', sp.getrider())
        elif self.timetype != u'single':
            op = self.t_other(sp)
            if op.getstatus() not in [u'idle', u'finish']:
                bib = op.getrider()
                ri = self.getiter(bib)

                if ri is not None:
                    self.settimes(ri,
                                  st=self.curstart,
                                  splits=op.splits,
                                  comment=u'caught')
                op.tofinish(u'caught')
                self.meet.timer_log_msg(bib, u'- Caught -')
                if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                    if op is self.fs:
                        self.meet.scbwin.sett1(u' [caught]     ')
                        self.meet.gemini.set_time(u'    -:--.-  ', 0)
                    else:
                        self.meet.scbwin.sett2(u' [caught]     ')
                        self.meet.gemini.set_time(u'    -:--.-  ', 1)
            if sp.getstatus() not in [u'idle', u'finish']:
                bib = sp.getrider()
                ri = self.getiter(bib)
                if ri is not None:
                    self.settimes(ri,
                                  st=self.curstart,
                                  splits=sp.splits,
                                  comment=u'catch')
                self.meet.timer_log_msg(bib, u'- Catch -')
                # but continue by default - manual abort to override.
            glib.idle_add(self.delayed_announce)
        else:
            LOG.warning(u'Unable to catch with single rider')

    def falsestart(self):
        """Register false start."""
        if self.timerstat == u'autotime':
            self.disable_autotime()
        if self.timerstat == u'running':
            if self.fs.getstatus() not in [u'idle', u'caught', u'finish']:
                self.fs.toload()
                self.meet.timer_log_msg(self.fs.getrider(), u'- False start -')
                if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                    self.meet.scbwin.setr1(u'False')
                    self.meet.scbwin.sett1(u'Start')
            if self.bs.getstatus() not in [u'idle', u'caught', u'finish']:
                self.bs.toload()
                self.meet.timer_log_msg(self.bs.getrider(), u'- False start -')
                if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                    self.meet.scbwin.setr2(u'False')
                    self.meet.scbwin.sett2(u'Start')
            self.toidle(idletimers=False)
        elif self.timerstat == u'armstart':
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                self.meet.gemini.clear()
                self.meet.scbwin.sett1(u'            ')
                self.meet.scbwin.sett2(u'            ')
            self.toload()

    def armfinish(self, sp, cid):
        """Arm timer for finish trigger."""
        if self.timerstat == u'autotime':
            LOG.info(u'Autotime disabled by manual intervention.')
            self.disable_autotime()
        if self.timerstat == u'running':
            if sp.getstatus() in [u'running', u'caught', u'finish']:
                if sp.getstatus() == u'finish':
                    self.meet.timer_log_msg(sp.getrider(), u'- False finish -')
                    self.meet.scbwin.setr1(u'')
                    self.meet.scbwin.setr2(u'')
                sp.toarmfin()
                self.meet.main_timer.arm(cid)
            elif sp.getstatus() == u'armfin':
                sp.torunning()
                self.meet.main_timer.dearm(cid)

    def toload(self):
        """Set timer status to load."""
        if self.fs.status == u'armstart':
            self.fs.toload()
        if self.bs.status == u'armstart':
            self.bs.toload()
        self.toidle(idletimers=False)

    def fmtridername(self, tp):
        """Prepare rider name for display on scoreboard."""
        bib = tp.getrider().strip()
        if bib != u'':
            name = u'[New Rider]'
            r = self.getrider(bib)
            ret = name
            if r is not None and r[COL_BIB].decode(u'utf-8') != u'':
                if self.teamnames:
                    club = r[COL_CLUB].decode(u'utf-8')
                    name = r[COL_FIRSTNAME].decode(u'utf-8')
                    name_w = self.meet.scb.linelen - 5  # w=4 + space
                    ret = u' '.join([
                        strops.truncpad(name, name_w, u'l'),
                        strops.truncpad(club, 4, u'r')
                    ])
                    tp.namevec = [bib, ret, club]
                else:
                    name_w = self.meet.scb.linelen - 9
                    first = r[COL_FIRSTNAME].decode(u'utf-8')
                    last = r[COL_LASTNAME].decode(u'utf-8')
                    club = r[COL_CLUB].decode(u'utf-8')
                    name = strops.fitname(first, last, name_w)
                    tp.namevec = [bib, strops.resname(first, last, club), u'']
                    ret = u' '.join([
                        strops.truncpad(r[COL_BIB], 3, u'r'),
                        strops.truncpad(name, name_w),
                        strops.truncpad(club, 4, 'r')
                    ])
            return ret
        else:
            tp.namevec = None
            return u''

    def showtimerwin(self):
        """Show timer window on scoreboard."""
        self.meet.scbwin = None
        self.meet.scbwin = scbwin.scbtt(self.meet.scb,
                                        self.meet.racenamecat(self.event),
                                        self.fmtridername(self.fs),
                                        self.fmtridername(self.bs))
        if self.timetype == u'single':
            self.meet.scbwin.set_single()
        self.meet.gemini.reset_fields()
        self.meet.gemini.set_bib(self.fs.getrider(), 0)
        self.meet.gemini.set_bib(self.bs.getrider(), 1)
        self.timerwin = True
        self.meet.scbwin.reset()

    def toarmstart(self):
        """Set timer to arm start."""
        doarm = False
        if self.fs.status == u'load':
            self.fs.toarmstart()
            doarm = True
        if self.bs.status == u'load' and self.timetype != u'single':
            self.bs.toarmstart()
            doarm = True
        if doarm:
            self.meet.timer_log_event(self.event)
            self.timerstat = u'armstart'
            self.curstart = None
            self.lstart = None
            self.meet.main_timer.arm(self.chan_S)
            self.showtimerwin()
            self.meet.delayimp(u'0.01')
            if self.fs.status == u'armstart':
                bib = self.fs.getrider()
                if bib not in self.traces:
                    self.traces[bib] = []
                self.fslog = loghandler.traceHandler(self.traces[bib])
                logging.getLogger().addHandler(self.fslog)
                self.meet.scbwin.sett1(u'       0.0     ')
                nstr = self.fs.biblbl.get_text()
                self.meet.timer_log_msg(bib, nstr)
            if self.bs.status == u'armstart':
                bib = self.bs.getrider()
                if bib not in self.traces:
                    self.traces[bib] = []
                self.bslog = loghandler.traceHandler(self.traces[bib])
                logging.getLogger().addHandler(self.bslog)
                self.meet.scbwin.sett2(u'       0.0     ')
                nstr = self.bs.biblbl.get_text()
                self.meet.timer_log_msg(bib, nstr)
            if self.timetype == u'single':
                self.bs.toidle()
                self.bs.disable()
            glib.idle_add(self.delayed_announce)

    def toidle(self, idletimers=True):
        """Set timer to idle state."""
        if self.timerstat == u'autotime':
            self.disable_autotime()
        if self.fslog is not None:
            logging.getLogger().removeHandler(self.fslog)
            self.fslog = None
        if self.bslog is not None:
            logging.getLogger().removeHandler(self.bslog)
            self.bslog = None
        if idletimers:
            self.fs.toidle()
            self.bs.toidle()
        self.timerstat = u'idle'
        self.meet.delayimp(u'2.00')
        self.curstart = None
        self.lstart = None
        self.diffstart = None
        for i in range(0, 8):
            self.meet.main_timer.dearm(i)
        if not self.onestart:
            pass
        self.fs.grab_focus()

    def t_other(self, tp=None):
        """Return reference to 'other' timer."""
        if tp is self.fs:
            return self.bs
        else:
            return self.fs

    def lanelookup(self, bib=None):
        """Prepare name string for timer lane."""
        r = self.getrider(bib)
        if r is None:
            if self.meet.get_clubmode():
                LOG.info(u'Adding non-starter: %r', bib)
                self.addrider(bib)
                r = self.getrider(bib)
            else:  # 'champs' mode
                return None
        rtxt = u'[New Rider]'
        if r is not None:
            rtxt = strops.listname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                   r[COL_LASTNAME].decode(u'utf-8'),
                                   r[COL_CLUB].decode(u'utf-8'))
        return rtxt

    def bibent_cb(self, entry, tp):
        """Bib entry callback."""
        bib = entry.get_text().decode(u'utf-8').strip()
        if bib != u'' and bib.isalnum():
            nstr = self.lanelookup(bib)
            if nstr is not None:
                tp.biblbl.set_text(nstr)
                if tp.status == u'idle':
                    tp.toload()
                if self.timerstat == u'autotime':
                    LOG.info(u'HANDLE JOIN OF TIMER AFTER AUTOTIME START')
                    tp.start(self.curstart)
                if self.timerstat == u'running':
                    tp.start(self.curstart)
                if self.timetype != u'single':
                    self.t_other(tp).grab_focus()
            else:
                LOG.warning(u'Ignoring non-starter: %r', bib)
                tp.toidle()
        else:
            tp.toidle()

    def treeview_button_press(self, treeview, event):
        """Set callback for mouse press on model view."""
        if event.button == 3:
            pathinfo = treeview.get_path_at_pos(int(event.x), int(event.y))
            if pathinfo is not None:
                path, col, cellx, celly = pathinfo
                treeview.grab_focus()
                treeview.set_cursor(path, col, 0)
                self.context_menu.popup(None, None, None, event.button,
                                        event.time)
                return True
        return False

    def tod_context_clear_activate_cb(self, menuitem, data=None):
        """Clear times for selected rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.settimes(sel[1])
            self.log_clear(
                self.riders.get_value(sel[1], COL_BIB).decode(u'utf-8'))
            glib.idle_add(self.delayed_announce)

    def tod_context_rel_activate_cb(self, menuitem, data=None):
        """Relegate rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.settimes(sel[1], comment=u'rel')
            glib.idle_add(self.delayed_announce)

    def tod_context_ntr_activate_cb(self, menuitem, data=None):
        """No time recorded for rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.settimes(sel[1], comment=u'ntr')
            glib.idle_add(self.delayed_announce)

    def tod_context_dsq_activate_cb(self, menuitem, data=None):
        """Disqualify rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.settimes(sel[1], comment=u'dsq')
            glib.idle_add(self.delayed_announce)

    def tod_context_dns_activate_cb(self, menuitem, data=None):
        """Rider did not start."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.settimes(sel[1], comment=u'dns')
            glib.idle_add(self.delayed_announce)

    def tod_context_print_activate_cb(self, menuitem, data=None):
        """Print Rider trace"""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            bib = self.riders.get_value(sel[1], COL_BIB).decode(u'utf-8')
            if bib in self.traces:
                # TODO: replace timy reprint with report
                LOG.info(u'CREATE AND PRINT TRACE REPORT')

    def now_button_clicked_cb(self, button, entry=None):
        """Set specified entry to the current time."""
        if entry is not None:
            entry.set_text(tod.now().timestr())

    def tod_context_edit_activate_cb(self, menuitem, data=None):
        """Run edit time dialog."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]  # grab off row iter
            stod = self.riders.get_value(i, COL_START)
            st = u''
            if stod is not None:
                st = stod.timestr()
            else:
                st = u'0.0'
            ftod = self.riders.get_value(i, COL_FINISH)
            ft = u''
            if ftod is not None:
                ft = ftod.timestr()
            tvec = uiutil.edit_times_dlg(self.meet.window, st, ft)
            if tvec[0] == 1:
                stod = tod.mktod(tvec[1])
                ftod = tod.mktod(tvec[2])
                bib = self.riders.get_value(i, COL_BIB)
                if stod is not None and ftod is not None:
                    self.settimes(i, stod, ftod)  # set times
                    self.log_elapsed(bib, stod, ftod, manual=True)
                else:
                    self.settimes(i)  # clear times
                    self.log_clear(bib)
                LOG.info(u'Race times manually adjusted for rider %r', bib)
            else:
                LOG.debug(u'Edit race times cancelled')
            glib.idle_add(self.delayed_announce)

    def tod_context_del_activate_cb(self, menuitem, data=None):
        """Delete selected row from race model."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]  # grab off row iter
            if self.riders.remove(i):
                pass  # re-select?
            glib.idle_add(self.delayed_announce)

    def log_clear(self, bib):
        """Print clear time log."""
        self.meet.timer_log_msg(bib, u'- Time Cleared -')

    def log_lap(self, bib, sid, start, split, prev=None):
        """Print a split log."""
        if prev is None:
            prev = start
        self.meet.timer_log_straight(bib, sid, split - prev, 3)
        if prev != start:
            self.meet.timer_log_straight(bib, u'time', split - start, 3)

    def log_elapsed(self,
                    bib,
                    start,
                    finish,
                    sid=None,
                    prev=None,
                    manual=False):
        """Print elapsed log info."""
        if manual:
            self.meet.timer_log_msg(bib, u'- Manual Adjust -')
        if prev is not None and prev != start:
            self.meet.timer_log_straight(bib, sid, finish - prev, 3)
        self.meet.timer_log_straight(bib, u'ST', start)
        self.meet.timer_log_straight(bib, u'FIN', finish)
        self.meet.timer_log_straight(bib, u'TIME', finish - start, 3)

    def set_timetype(self, data=None):
        """Update timer panes to match timetype or data if provided."""
        if data is not None:
            self.timetype = strops.confopt_pair(data, 'single', 'dual')
        if self.timetype == u'single':
            self.bs.frame.hide()
            self.bs.hide_splits()
            self.fs.frame.set_label(u'Timer')
            self.fs.show_splits()
        else:
            self.bs.frame.show()
            self.bs.show_splits()
            self.fs.frame.set_label(u'Front Straight')
            self.fs.show_splits()

    def destroy(self):
        """Signal race shutdown."""
        if self.context_menu is not None:
            self.context_menu.destroy()
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
        self.readonly = not ui
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        LOG.debug(u'Init %sevent %s', rstr, self.evno)

        # properties
        self.timetype = u'dual'
        self.distance = None
        self.units = u'laps'
        self.autotime = False
        self.comments = []
        self.difftime = False
        self.precision = 3
        self.teampursuit = False
        self.teamnames = False  # team names only shown
        self.chan_A = timy.CHAN_PA  # default is ITT/Pursuit
        self.chan_B = timy.CHAN_PB
        self.chan_S = timy.CHAN_START
        self.fsvec = None
        self.bsvec = None
        self.fslog = None
        self.bslog = None
        self.traces = {}

        # race run time attributes
        self.autospec = u''
        self.inomnium = False
        self.seedsrc = 1  # default seeding is by rank in last round
        self.onestart = False
        self.winopen = ui
        self.timerwin = False
        self.showcats = False
        self.timerstat = u'idle'
        self.curstart = None
        self.lstart = None
        self.diffstart = None  # for diff time in pursuit race
        self.difflane = None  # for diff time in pursuit race
        self.splitlist = []  # ordered list of split ids
        self.splitmap = {}  # map of split ids and rank data
        self.results = tod.todlist(u'FIN')  # FIX FOR LAST LAP

        self.riders = gtk.ListStore(
            gobject.TYPE_STRING,  # 0 bib
            gobject.TYPE_STRING,  # 1 firstname
            gobject.TYPE_STRING,  # 2 lastname
            gobject.TYPE_STRING,  # 3 club
            gobject.TYPE_STRING,  # 4 Comment
            gobject.TYPE_STRING,  # 5 seeding
            gobject.TYPE_STRING,  # 6 place
            gobject.TYPE_PYOBJECT,  # 7 Start
            gobject.TYPE_PYOBJECT,  # 8 Finish
            gobject.TYPE_PYOBJECT,  # 9 Last Lap
            gobject.TYPE_PYOBJECT)  # 10 Splits

        uifile = os.path.join(metarace.UI_PATH, u'ittt.ui')
        LOG.debug(u'Building event interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)

        self.frame = b.get_object(u'race_vbox')
        self.frame.connect(u'destroy', self.shutdown)

        # meta info pane
        self.info_expand = b.get_object(u'info_expand')
        b.get_object(u'race_info_evno').set_text(self.evno)
        self.showev = b.get_object(u'race_info_evno_show')
        self.prefix_ent = b.get_object(u'race_info_prefix')
        self.prefix_ent.connect(u'changed', self.editent_cb, u'pref')
        self.prefix_ent.set_text(self.event[u'pref'])
        self.info_ent = b.get_object(u'race_info_title')
        self.info_ent.connect(u'changed', self.editent_cb, u'info')
        self.info_ent.set_text(self.event[u'info'])

        # Timer Panes
        mf = b.get_object(u'race_timer_pane')
        self.fs = timerpane.timerpane(u'Front Straight')
        self.fs.bibent.connect(u'activate', self.bibent_cb, self.fs)
        self.bs = timerpane.timerpane(u'Back Straight')
        self.bs.urow = 6  # scb row for timer messages
        self.bs.bibent.connect(u'activate', self.bibent_cb, self.bs)
        mf.pack_start(self.fs.frame)
        mf.pack_start(self.bs.frame)
        mf.set_focus_chain([self.fs.frame, self.bs.frame, self.fs.frame])

        # Result Pane
        t = gtk.TreeView(self.riders)
        self.view = t
        t.set_reorderable(True)
        t.set_rules_hint(True)

        # show window
        self.context_menu = None
        if ui:
            t.connect(u'button_press_event', self.treeview_button_press)
            # TODO: show team name & club but pop up for rider list
            uiutil.mkviewcoltxt(t, u'No.', COL_BIB, calign=1.0)
            uiutil.mkviewcoltxt(t,
                                u'First Name',
                                COL_FIRSTNAME,
                                self.editcol_db,
                                expand=True)
            uiutil.mkviewcoltxt(t,
                                u'Last Name',
                                COL_LASTNAME,
                                self.editcol_db,
                                expand=True)
            uiutil.mkviewcoltxt(t, u'Club', COL_CLUB, self.editcol_db)
            uiutil.mkviewcoltxt(t, u'Seed', COL_SEED, self.editcol_db)
            uiutil.mkviewcoltod(t, u'Time/Last Lap', cb=self.todstr)
            uiutil.mkviewcoltxt(t, u'Rank', COL_PLACE, halign=0.5, calign=0.5)
            t.show()
            b.get_object(u'race_result_win').add(t)

            b.connect_signals(self)
            b = gtk.Builder()
            b.add_from_file(os.path.join(metarace.UI_PATH, u'tod_context.ui'))
            self.context_menu = b.get_object(u'tod_context')
            b.connect_signals(self)
