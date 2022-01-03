
"""Flying 200m and lap time trial module for trackmeet."""

# Refer: UCI Regulations Part 3 "Track Races" 3.2.022 - 3.2.028
# 	 and 3.2.253 - 3.2.258

import gtk
import glib
import gobject
import os
import logging

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

LOG = logging.getLogger(u'metarace.f200')
LOG.setLevel(logging.DEBUG)

# config version string
EVENT_ID = u'f200-2.0'

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
COL_100 = 9

# scb function key mappings (also trig announce)
key_reannounce = u'F4'    # (+CTRL) calls into delayed announce
key_startlist = u'F3'     # startlist
key_results = u'F4'       # recalc/show result window
key_timerwin = u'F6'      # re-show timing window

# timing function key mappings
key_armstart = u'F5'      # arm for start impulse
key_armsplit = u'F7'      # de/arm intermed (manual override)
key_armfinish = u'F9'     # de/arm finish (manual override)

# extended function key mappings
key_reset = u'F5'         # + ctrl for clear/abort
key_falsestart = u'F6'    # + ctrl for false start
key_abort = u'F7'         # + ctrl abort A

class f200(object):
    """Flying 200 time trial."""

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or u'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_reset:    # override ctrl+f5
                    self.toidle()
                    return True
                elif key == key_reannounce:
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_falsestart:
                    self.falsestart()
                    return True
                elif key == key_abort:
                    self.abortrider(self.fs)
                    return True
            elif key[0] == u'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_armsplit:
                    self.armsplit(self.fs)
                    return True
                elif key == key_armfinish:
                    self.armfinish(self.fs)
                    return True
                elif key == key_startlist:
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_timerwin:
                    self.showtimerwin()
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_results:
                    self.do_places()
                    glib.idle_add(self.delayed_announce)
                    return True
        return False

    def do_places(self):
        """Show race result on scoreboard."""
        self.meet.scbwin = None
        self.timerwin = False
        fmtplaces = []
        name_w = self.meet.scb.linelen - 11
        pcount = 0
        rcount = 0
        for r in self.riders:
            rcount += 1
            if r[COL_PLACE] is not None and r[COL_PLACE] != u'':
                pcount += 1
                plstr = r[COL_PLACE].decode(u'utf-8')
                if plstr.isdigit():
                    plstr += u'.'
                first = r[COL_FIRSTNAME].decode('utf-8')
                last = r[COL_LASTNAME].decode('utf-8')
                club = r[COL_CLUB].decode('utf-8')
                bib = r[COL_BIB].decode('utf-8')
                fmtplaces.append([plstr, bib,
                            strops.fitname(first, last, name_w),
                                  club])
        FMT = [(3,u'l'), (3,u'r'), u' ',(name_w, u'l'), (4,u'r')]

        evtstatus = u'Standings'
        if rcount > 0 and pcount == rcount:
            evtstatus = u'Result'

        self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                               head=self.meet.racenamecat(self.event),
                               subhead=evtstatus.upper(),
                               coldesc=FMT,
                               rows=fmtplaces)
        self.meet.scbwin.reset()

    def todstr(self, col, cr, model, iter, data=None):
        """Format tod into text for listview."""
        ft = model.get_value(iter, COL_FINISH)
        if ft is not None:
            sp = model.get_value(iter, COL_100)
            st = model.get_value(iter, COL_START)
            if st is None:
                st = tod.tod(0)
            mstr = (ft - st).rawtime(3)
            sstr = u''
            if sp is not None:
                sstr = u'/' + (ft - sp).rawtime(3)
            cr.set_property(u'text', mstr+sstr)
        else:
            cr.set_property(u'text', u'')

    def loadconfig(self):
        """Load race config from disk."""
        self.riders.clear()
        self.results.clear()
        self.splits.clear()

        defautoarm = True
        defdistance = 200
        defunits = u'metres'
        defchans = timy.CHAN_200
        defchani = timy.CHAN_100
        defchanf = timy.CHAN_FINISH
        if self.evtype == u'flying lap':
            # override defaults for flying lap type
            defdistance = 1
            defunits = u'laps'
            defchans = timy.CHAN_FINISH
            defchani = timy.CHAN_200
            defchanf = timy.CHAN_FINISH

        self.seedsrc = 1 # for autospec loads, fetch seed from the rank col

        cr = jsonconfig.config({u'event':{
				u'startlist':'',
				u'id':EVENT_ID,
				u'start':None,
      				u'lstart':None,
				u'fsbib':None,
				u'fsstat':u'idle',
				u'showinfo':True,
				u'autospec':u'',
				u'comments':[],
                                u'chan_S': defchans,
                                u'chan_I': defchani,
                                u'chan_F': defchanf,
				u'inomnium':False,
				u'distance':defdistance,
				u'distunits':defunits,
				u'autoarm':defautoarm}})
        cr.add_section(u'event')
        cr.add_section(u'riders')
        cr.add_section(u'traces')
        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                LOG.error(u'Unable to read config: %s', e)
        else:
            LOG.info(u'%r not found, loading defaults', self.configfile)

        self.chan_S = strops.confopt_chan(cr.get(u'event', u'chan_S'), defchans)
        self.chan_I = strops.confopt_chan(cr.get(u'event', u'chan_I'), defchans)
        self.chan_F = strops.confopt_chan(cr.get(u'event', u'chan_F'), defchans)
        self.comments = cr.get(u'event', u'comments')
        self.autospec = cr.get(u'event', u'autospec')
        self.distance = strops.confopt_dist(cr.get(u'event', u'distance'))
        self.units = strops.confopt_distunits(cr.get(u'event', u'distunits'))
        self.autoarm = strops.confopt_bool(cr.get(u'event', u'autoarm'))
        self.update_expander_lbl_cb()
        self.info_expand.set_expanded(strops.confopt_bool(
                                       cr.get(u'event', u'showinfo')))
        self.inomnium = strops.confopt_bool(cr.get(u'event', u'inomnium'))
        if self.inomnium:
            self.seedsrc = 3    # read seeding from points standinds

        # re-load starters/results and traces
        self.onestart = False
        rlist = cr.get(u'event', u'startlist').split()
        for r in rlist:
            nr=[r, u'', u'', u'', u'', u'', u'', None, None, None]
            co = u''
            st = None
            ft = None
            sp = None
            if cr.has_option(u'riders', r):
                ril = cr.get(u'riders', r)
                if len(ril) >= 1:	# save comment for stimes
                    co = ril[0]
                if len(ril) >= 2:	# write heat into rec
                    nr[COL_SEED] = ril[1]
                if len(ril) >= 4:	# Start ToD and others
                    st = tod.mktod(ril[3])
                    if st is not None:		# assigned in settimes
                        self.onestart = True
                if len(ril) >= 5:	# Finish ToD
                    ft = tod.mktod(ril[4])
                if len(ril) >= 6:		# 100m ToD
                    sp = tod.mktod(ril[5])
            dbr = self.meet.rdb.getrider(r, self.series)
            if dbr is not None:
                for i in range(1,4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i) # unicode
            nri = self.riders.append(nr)
            self.settimes(nri, st, ft, sp, doplaces=False, comment=co)
            if cr.has_option(u'traces', r):
                self.traces[r] = cr.get(u'traces', r)
        self.placexfer()

        # re-join an existing timer state
        curstart = tod.mktod(cr.get(u'event', u'start'))
        lstart = tod.mktod(cr.get(u'event', u'lstart'))
        if lstart is None:
            lstart = curstart	# can still be None if start not set
        dorejoin = False
        fsstat = cr.get(u'event', u'fsstat')
        if fsstat not in [u'idle', u'finish']:
            self.fs.setrider(cr.get(u'event', u'fsbib')) # load rider
            if fsstat in [u'running', u'armfin', u'armint'] and curstart is not None:     
                self.fs.start(curstart)  # overrides to 'running'
                dorejoin = True

        if not self.onestart and self.autospec:
            self.meet.autostart_riders(self, self.autospec,
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
        cw.set(u'event', u'chan_I', self.chan_I)
        cw.set(u'event', u'chan_F', self.chan_F)
        cw.set(u'event', u'autospec', self.autospec)
        cw.set(u'event', u'autoarm', self.autoarm)
        cw.set(u'event', u'startlist', self.get_startlist())
        cw.set(u'event', u'inomnium', self.inomnium)
        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'comments', self.comments)

        # extract and save timerpane config for interrupted run
        if self.curstart is not None:
            cw.set(u'event', u'start', self.curstart.rawtime())
        if self.lstart is not None:
            cw.set(u'event', u'lstart', self.lstart.rawtime())
        cw.set(u'event', u'fsstat', self.fs.getstatus())
        cw.set(u'event', u'fsbib', self.fs.getrider())

        cw.add_section(u'traces')
        for rider in self.traces:
            cw.set(u'traces', rider, self.traces[rider])

        cw.add_section(u'riders')

        # save out all starters
        for r in self.riders:
            # place is saved for info only
            slice = [r[COL_COMMENT].decode(u'utf-8'),
                     r[COL_SEED].decode(u'utf-8'),
                     r[COL_PLACE].decode(u'utf-8')]
            tl = [r[COL_START], r[COL_FINISH], r[COL_100]]
            for t in tl:
                if t is not None:
                    slice.append(t.rawtime())
                else:
                    slice.append(None)
            cw.set(u'riders', r[COL_BIB].decode(u'utf-8'), slice)
        cw.set(u'event', u'id', EVENT_ID)
        LOG.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def sort_startlist(self, x, y):
        """Comparison function for seeding."""
        if x[1] == y[1]:        # same seed? revert to bib ascending
            return cmp(x[2], y[2])
        else:
            return cmp(x[1], y[1])

    def sort_heats(self, x, y):
        """Comparison function for heats."""
        (xh, xl) = strops.heatsplit(x[0])
        (yh, yl) = strops.heatsplit(y[0])
        if xh == yh:
            return cmp(xl, yl)
        else:
            return cmp(xh, yh)

    def reorder_startlist(self):
        """Re-order model according to the seeding field."""
        if len(self.riders) > 1:
            auxmap = []
            cnt = 0
            for r in self.riders:
                auxmap.append([cnt,
                            strops.riderno_key(r[COL_SEED].decode(u'utf-8')),
                            strops.riderno_key(r[COL_BIB].decode(u'utf-8'))])
                cnt += 1
            auxmap.sort(self.sort_startlist)
            self.riders.reorder([a[0] for a in auxmap])

    def get_heats(self, placeholders=0):
        """Return a list of heats in the event."""
        ret = []

        # arrange riders by seeding
        self.reorder_startlist()

        # then build aux map of heats
        hlist = []
        count = len(self.riders)
        if count < placeholders:
            count = placeholders
        if placeholders == 0:
            for r in self.riders:
                rno = r[COL_BIB].decode(u'utf-8')
                rh = self.meet.newgetrider(rno, self.series)
                info = None
                rname = u''
                if rh is not None:
                    rname = rh[u'namestr']
                    if rh[u'cat'] and u'tandem' in rh[u'cat'].lower():
                        ph = self.meet.newgetrider(rh[u'note'], self.series)
                        if ph is not None:
                            info = [[u' ',ph[u'namestr'] + u' - Pilot', ph[u'ucicode']]]
                hlist.append([str(count)+'.1', rno, rname, info])
                                 # all f200 heats are one up
                count -= 1
        else:
            for r in range(0, placeholders):
                rno = u''
                rname = u''
                hlist.append([str(count)+'.1', rno, rname, None])
                count -= 1

        # sort the heatlist
        hlist.sort(self.sort_heats)

        lh = None
        lcnt = 0
        rec = []
        for r in hlist:
            (h,l) = strops.heatsplit(r[0])
            if lh is not None and (h != lh or lcnt > 1):
                lcnt = 0
                ret.append(rec)
                rec = []
            heat = str(h)
            rec.extend([heat, r[1], r[2], r[3]])
            lcnt += 1
            lh = h
        if len(rec) > 0:
            ret.append(rec)
        return ret

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        cnt = 0
        sec = report.dual_ittt_startlist()
        sec.set_single()	# 200s are one-up

        headvec = [u'Event', self.evno, u':', self.event[u'pref'],
                         self.event[u'info']]
        if not program:
            headvec.append(u'- Start List')
        sec.heading = u' '.join(headvec)
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join([lapstring, self.event[u'dist'],
                             self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr
        if self.event[u'reco']:
            sec.footer = self.event[u'reco']
        sec.lines = self.get_heats()
        ret.append(sec)
        return ret

    def get_startlist(self):
        """Return a list of bibs in the rider model."""
        ret = []
        for r in self.riders:
            ret.append(r[COL_BIB].decode(u'utf-8'))
        return u' '.join(ret)

    def delayed_announce(self):
        """Initialise the announcer's screen after a delay."""
        if self.winopen:
            # clear page
            self.meet.txt_clear()
            self.meet.txt_title(u' '.join([
                  u'Event', self.evno, u':', self.event[u'pref'],
                   self.event[u'info']]))
            self.meet.txt_line(1)
            self.meet.txt_line(7)

            # fill in front straight (only one?)
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
                                             24, trunc=True)
                    placestr = u'   ' # 3 ch
                    if r[COL_PLACE]:
                        placestr = strops.truncpad(r[COL_PLACE].decode(u'utf-8') + u'.', 3)
                    bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3, u'r')
                    tmstr = u''
                    et = None
                    if r[COL_START] is not None and r[COL_FINISH] is not None:
                        et = (r[COL_FINISH] - r[COL_START]).truncate(3)
                        tmstr = u'200m: ' + et.rawtime(3).rjust(12)
                    cmtstr = u''
                    if et is not None:
                        cmtstr = strops.truncpad(u'Average: '
                                                + et.speedstr(200), 38, u'r')
                    elif r[COL_COMMENT]:
                        cmtstr = strops.truncpad(
                             u'[' + r[COL_COMMENT].decode(u'utf-8').strip() + u']', 38, u'r')
                    self.meet.txt_postxt(3,0,u'        Current Rider')
                    self.meet.txt_postxt(4,0,u' '.join([placestr, bibstr,
                                                         namestr, clubstr]))
                    self.meet.txt_postxt(5,0,strops.truncpad(tmstr, 38, u'r'))
                    self.meet.txt_postxt(6,0,cmtstr)

            # fill in leaderboard/startlist
            count = 0
            curline = 9
            posoft = 0
            for r in self.riders:
                count += 1
                if count == 19:
                    curline = 9
                    posoft += 42

                clubstr = u''
                tcs = r[COL_CLUB].decode(u'utf-8')
                if tcs and tcs <= 3:
                    clubstr = u' (' + tcs + u')'
 
                namestr = strops.truncpad(strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                              r[COL_LASTNAME].decode(u'utf-8'), 22-len(clubstr),
                                  trunc=True)+clubstr, 22)
                placestr = u'   ' # 3 ch
                if r[COL_PLACE]:
                    placestr = strops.truncpad(r[COL_PLACE].decode(u'utf-8') + u'.', 3)
                bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3, u'r')
                tmstr = u'       ' # 7 ch
                if r[COL_START] is not None and r[COL_FINISH] is not None:
                    tmstr = strops.truncpad(
                           (r[COL_FINISH] - r[COL_START]).rawtime(3), 7, u'r')
                self.meet.txt_postxt(curline, posoft, u' '.join([
                      placestr, bibstr, namestr, tmstr]))
                curline += 1

    def shutdown(self, win=None, msg=u'Exiting'):
        """Terminate event object."""
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        LOG.debug(u'Shutdown %sevent %s: %s', rstr, self.evno, msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def do_properties(self):
        """Run event properties dialog."""
        prfile = os.path.join(metarace.UI_PATH, u'ittt_properties.ui')
        LOG.debug(u'Building event properties from %r', prfile)
        b = gtk.Builder()
        b.add_from_file(prfile)
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.meet.window)

        # customise dialog for flying 200/flying lap
        b.get_object(u'race_score_type').hide()
        b.get_object(u'race_timing_label').hide()
        intlbltxt = u'100m Channel:'
        inthint = u'Select timing channel for 100m split.'
        if self.evtype == u'flying lap':
            intlbltxt = u'200m Channel:'
            inthint = u'Select timing channel for 200m start.'
        intlbl = b.get_object(u'race_achan_label')
        intlbl.set_text(intlbltxt)
        intcombo = b.get_object(u'race_achan_combo')
        intcombo.set_property(u'tooltip_text', inthint)
        intcombo.set_active(self.chan_I)
        finlbl = b.get_object(u'race_bchan_label')
        finlbl.set_text(u'Finish Channel:')
        fincombo = b.get_object(u'race_bchan_combo')
        fincombo.set_property(u'tooltip_text',
                        u'Select timing channel for finish.')
        fincombo.set_active(self.chan_F)
        stcombo = b.get_object(u'race_stchan_combo')
        stcombo.set_active(self.chan_S)
        aa = b.get_object(u'race_autoarm_toggle')
        aa.set_active(self.autoarm)

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
        se = b.get_object(u'race_series_entry')
        se.set_text(self.series)
        as_e = b.get_object(u'auto_starters_entry')
        as_e.set_text(self.autospec)

        response = dlg.run()
        if response == 1:       # id 1 set in glade for "Apply"
            dval = di.get_text().decode(u'utf-8')
            if dval.isdigit():
                self.distance = int(dval)
            else:
                self.distance = None
            if du.get_active() == 0:
                self.units = u'metres'
            else:
                self.units = u'laps'
            self.autoarm = aa.get_active()
            self.chan_S = stcombo.get_active()
            self.chan_I = intcombo.get_active()
            self.chan_F = fincombo.get_active()

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
                    self.meet.autostart_riders(self, self.autospec,
                                             infocol=self.seedsrc)

            # xfer starters if not empty
            slist = strops.riderlist_split(
              b.get_object(u'race_starters_entry').get_text().decode(u'utf-8'),
              self.meet.rdb, self.series)

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
        """Generator function to export rankings."""
        for r in self.riders:
            bib = r[COL_BIB].decode(u'utf-8')
            rank = None
            time = None
            info = None
            cmts = r[COL_COMMENT].decode(u'utf-8')
            if cmts in [u'rel']:
                info = cmts
            if self.onestart:
                pls = r[COL_PLACE].decode(u'utf-8')
                if pls:
                    if pls.isdigit():
                        rank = int(pls)
                    else:
                        rank = pls
                if r[COL_FINISH] is not None:
                    time = (r[COL_FINISH]-r[COL_START]).truncate(3)

            yield [bib, rank, time, info]

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        self.placexfer()
        ret = []
        sec = report.section()
        sec.heading = u'Event ' + self.evno + u': ' + u' '.join([
                       self.event[u'pref'], self.event[u'info']
                              ]).strip()
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join([lapstring, self.event[u'dist'],
                             self.event[u'prog']]).strip()
        sec.lines = []
        ftime = None
        rcount = 0
        pcount = 0
        for r in self.riders:
            rcount += 1
            rno = r[COL_BIB].decode(u'utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            if rh is None:
                LOG.warning(u'Rider not found %r', rno)
                continue

            rcat = None		# should check ev[u'cate']
            plink = u''
            rank = None
            
            rname = rh[u'namestr']
            # consider partners here
            if rh[u'cat'] and u'tandem' in rh[u'cat'].lower():
                ph = self.meet.newgetrider(rh[u'note'], self.series)
                if ph is not None:
                    plink = [u'', u'', ph[u'namestr'] + u' - Pilot', ph[u'ucicode'], u'', u'',u'']
            if self.event[u'cate']:
                if rh[u'cat']:
                    rcat = rh[u'cat']
            if rh[u'ucicode']:
                rcat = rh[u'ucicode']   # overwrite by force
            rtime = None
            info = None
            dtime = None
            if self.onestart:
                pls = r[COL_PLACE].decode(u'utf-8')
                if pls:
                    if pls.isdigit():
                        rank = pls + u'.'
                    else:
                        rank = pls
                    plcount += 1
                if r[COL_FINISH] is not None:
                    time = (r[COL_FINISH]-r[COL_START]).truncate(3)
                    if ftime is None:
                        ftime = time
                    else:
                        dtime = u'+' + (time - ftime).rawtime(2)
                    if r[COL_START] != tod.ZERO:
                        rtime = time.rawtime(3)
                    else:
                        rtime = time.rawtime(2) + u'\u2007'

            sec.lines.append([rank, rno, rname, rcat, rtime, dtime, plink])
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
        return ret

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode(u'utf-8')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode(u'utf-8')
        self.update_expander_lbl_cb()

    def update_expander_lbl_cb(self):
        """Update event info expander label."""
        self.info_expand.set_label(self.meet.infoline(self.event))

    def clear_rank(self, cb):
        """Run callback once in main loop idle handler."""
        cb(u'')
        return False

    def split_trig(self, sp, t):
        """Register lap trigger."""
        bib = sp.getrider()
        self.splits.insert(t-self.curstart, None, bib)
        rank = self.splits.rank(bib)
        self.log_split(sp.getrider(), self.curstart, t)
        sp.intermed(t, 2)	# show split... delay ~2 sec
        if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
            intstr = u' at 100m'
            if self.evtype == u'flying lap':
                intstr = u' at 200 start'
            lapstr = strops.num2ord(unicode(rank + 1)) + intstr
            self.meet.scbwin.setr1(u'(' + unicode(rank + 1) + u')')
            glib.timeout_add_seconds(2, self.clear_rank,
                                        self.meet.scbwin.setr1)
            # announce lap and rank to txt announcer 
            self.meet.txt_postxt(5, 8, strops.truncpad(lapstr,17)
                                      + u' ' + sp.get_time())
        if self.autoarm:
            self.armfinish(sp)

    def fin_trig(self, sp, t):
        """Register finish trigger."""
        sp.finish(t)
        ri = self.getiter(sp.getrider())
        split = None
        if len(sp.splits) > 0:
            split = sp.splits[0]
        if ri is not None:
            self.settimes(ri, self.curstart, t, split)
        else:
            LOG.warning(u'Rider %r not in model, finish time not stored',
                         sp.getrider())
        self.log_elapsed(sp.getrider(), self.curstart, t, split)
        if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
            place = self.riders.get_value(ri, COL_PLACE)
            self.meet.scbwin.setr1(u'(' + place + u')')
            self.meet.scbwin.sett1(sp.get_time())
            dist = self.meet.get_distance(self.distance, self.units)
            if dist is not None:
                elap = t - self.curstart
                spstr = elap.speedstr(dist).strip()
                glib.timeout_add_seconds(1, self.clear_200_ttb,
                                            self.meet.scbwin,
                                            u'Avg:',
                                            spstr.rjust(12))
            else:
                glib.timeout_add_seconds(2, self.clear_200_ttb,
                                            self.meet.scbwin)
            self.meet.gemini.set_rank(place)
            self.meet.gemini.set_time((t - self.curstart).rawtime(2))
            self.meet.gemini.show_brt()

        # call for a delayed announce...
        glib.idle_add(self.delayed_announce)
        self.meet.delayed_export()

    def timercb(self, e):
        """Handle a timer event."""
        chan = timy.chan2id(e.chan)
        if self.timerstat == u'armstart':
            if chan == self.chan_S:	# Start trig
                self.torunning(e)
                glib.timeout_add_seconds(2, self.armfinish, self.fs, True)
        elif self.timerstat == u'running':
            if chan == self.chan_I:	# Intermediate
                stat = self.fs.getstatus()
                if stat == u'armint':
                    self.split_trig(self.fs, e)
                # else ignore spurious intermediate
            elif chan == self.chan_F:	# Finish
                stat = self.fs.getstatus()
                if stat in [u'armfin', u'armint']:
                    self.fin_trig(self.fs, e)
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        if self.fs.status in [u'running', u'armint', u'armfin']:
            now = tod.now()
            self.fs.runtime(now - self.lstart)
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                self.meet.scbwin.sett1(self.fs.get_time())
                elapstr = (now - self.lstart).rawtime(1).rjust(4) + ' '
                self.meet.gemini.set_time(elapstr)
                self.meet.gemini.show_brt()
        return True

    def show_200_ttb(self, scb):
        """Display time to beat."""
        if len(self.results) > 0:
            scb.setr2(u'Fastest:')
            scb.sett2(self.results[0][0].timestr(3))
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
        self.curstart = st
        if lst is None:
            lst = tod.now()
        self.lstart = lst
        self.timerstat = u'running'
        self.onestart = True
        if self.autoarm:
            self.armsplit(self.fs)
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
        nr=[bib, u'', u'', u'', u'', istr, u'', None, None, None]
        ri = self.getrider(bib)
        if ri is None:  # adding a new record
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                for i in range(1,4):
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
                place = self.results.rank(bib)+1
            i = self.getiter(bib)
            if i is not None:
                if place == u'comment':	# superfluous but ok
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

    def settimes(self, iter, st=None, ft=None, split=None,
                             doplaces=True, comment=None):
        """Transfer race times into rider model."""
        bib = self.riders.get_value(iter, COL_BIB).decode(u'utf-8')
        # clear result for this bib
        self.results.remove(bib)
        self.splits.remove(bib)
        # assign tods
        self.riders.set_value(iter, COL_START, st)
        self.riders.set_value(iter, COL_FINISH, ft)
        self.riders.set_value(iter, COL_100, split)
        # save result
        if st is None:
            st = tod.ZERO
        if ft is not None:
            last100 = None
            if split is not None:
                self.splits.insert(split - st, None, bib) # save first 100 split
                last100 = ft - split	# and prepare to record second 100
            self.results.insert(ft - st, last100, bib)
        else:	# DNF/etc
            self.results.insert(comment, None, bib)
        # copy annotation into model if provided, or clear
        if comment:
            self.riders.set_value(iter, COL_COMMENT, comment)
        else:
            self.riders.set_value(iter, COL_COMMENT, u'')
        # if reqd, do places
        if doplaces:
            self.placexfer()

    def armstart(self):
        """Arm timer for start trigger."""
        if self.timerstat == u'armstart':
            self.toload()
        elif self.timerstat in [u'load', u'idle']:
            self.toarmstart()

    def armsplit(self, sp):
        """Arm timer for a split."""
        if self.timerstat == u'running':
            if sp.getstatus() == u'running':
                sp.toarmint()
                self.meet.main_timer.arm(self.chan_I)
            elif sp.getstatus() == u'armint':
                sp.torunning()
                self.meet.main_timer.dearm(self.chan_I)
                self.meet.main_timer.dearm(self.chan_F)
        return False

    def abortrider(self, sp):
        """Abort the current heat."""
        if sp.getstatus() not in [u'idle', u'finish']:
            bib = sp.getrider()
            ri = self.getiter(bib)
            if ri is not None:
                self.settimes(ri, st=self.curstart, comment=u'dnf')
            sp.tofinish()
            self.meet.timer_log_msg(bib, u'- Abort -')
            glib.idle_add(self.delayed_announce)

    def falsestart(self):
        """Register false start."""
        if self.timerstat == u'running':
            if self.fs.getstatus() not in [u'idle', u'finish']:
                self.fs.toload()
                bib = fs.getrider()
                self.meet.timer_log_msg(self.fs.getrider(), u'- False start -')
                if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                    self.meet.scbwin.setr1(u'False')
                    self.meet.scbwin.sett1(u'Start')
            self.toidle(idletimers=False)
        elif self.timerstat == u'armstart':
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtt:
                self.meet.scbwin.sett1(u'            ')
                self.meet.scbwin.sett2(u'            ')
            self.toload()

    def armfinish(self, sp, force=False):
        """Arm timer for finish trigger."""
        if self.timerstat == u'running':
            if sp.getstatus() in [u'running', u'finish']:
                if sp.getstatus() == u'finish':
                     self.meet.timer_log_msg(sp.getrider(), u'- False Finish -')
                     self.meet.scbwin.setr1(u'')
                     self.meet.scbwin.setr2(u'')
                sp.toarmfin()
                self.meet.main_timer.arm(self.chan_F)
            elif sp.getstatus() == u'armfin' and not force:
                sp.torunning()
                self.meet.main_timer.dearm(self.chan_F)
            else:
                # request to arm finish before intermediate
                self.meet.main_timer.arm(self.chan_F)
        return False

    def toload(self):
        """Set timer status to load."""
        if self.fs.status == u'armstart':
            self.fs.toload()
        self.toidle(idletimers=False)

    def fmtridername(self, tp):
        """Prepare rider name for display on scoreboard."""
        name_w = self.meet.scb.linelen - 9
        bib = tp.getrider().strip()
        if bib != u'':
            name = u'[New Rider]'
            r = self.getrider(bib)
            if r is not None and r[COL_BIB]:
                first = r[COL_FIRSTNAME].decode(u'utf-8')
                last = r[COL_LASTNAME].decode(u'utf-8')
                club = r[COL_CLUB].decode(u'utf-8')
                name = strops.fitname(first,
                                      last,
                                      name_w)
            return u' '.join([strops.truncpad(r[COL_BIB].decode(u'utf-8'),
                                              3, u'r'),
                             strops.truncpad(name, name_w),
                             strops.truncpad(club, 4, u'r')])
        else:
            return u''
        
    def showtimerwin(self):
        """Show timer window on scoreboard."""
        self.meet.scbwin = None
        self.meet.scbwin = scbwin.scbtt(self.meet.scb,
                                self.meet.racenamecat(self.event),
                                self.fmtridername(self.fs))
        self.meet.gemini.reset_fields()
        self.meet.gemini.set_bib(self.fs.getrider())
        self.meet.gemini.show_brt()
        self.timerwin = True
        self.meet.scbwin.reset()

    def toarmstart(self):
        """Set timer to arm start."""
        if self.fs.status == u'load':
            self.meet.timer_log_event(self.event)
            self.fs.toarmstart()
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
                self.meet.gemini.set_bib(bib)
                self.meet.gemini.set_time(u' 0.0 ')
                self.meet.gemini.set_rank(u'')
                self.meet.gemini.show_brt()
            glib.idle_add(self.delayed_announce)

    def toidle(self, idletimers=True):
        """Set timer to idle state."""
        if self.fslog is not None:
            logging.getLogger().removeHandler(self.fslog)
            self.fslog = None
        if idletimers:
            self.fs.toidle()
        self.timerstat = u'idle'
        self.meet.delayimp(u'2.00')
        self.curstart = None
        self.lstart = None
        for i in range(0, 8):
            self.meet.main_timer.dearm(i)
        if not self.onestart:
            pass
        self.fs.grab_focus()

    def lanelookup(self, bib=None):
        """Prepare name string for timer lane."""
        r = self.getrider(bib)
        if r is None:
            if self.meet.get_clubmode():        # fill in starters
                LOG.warning(u'Adding non-starter %r', bib)
                self.addrider(bib)
                r = self.getrider(bib)
            else:
                LOG.warning(u'Rider %r not in event', bib)
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
        if bib and bib.isalnum():
            nstr = self.lanelookup(bib)
            if nstr is not None:
                tp.biblbl.set_text(nstr)
                if tp.status == u'idle':
                    tp.toload()
                if self.timerstat == u'running':
                    tp.start(self.curstart)
            else:
                LOG.warning(u'Rider %r not in event.', bib)
                tp.toidle()
        else:
            tp.toidle()
    
    def time_context_menu(self, widget, event, data=None):
        """Popup menu for result list."""
        self.context_menu.popup(None, None, None, event.button,
                                event.time, selpath)

    def treeview_button_press(self, treeview, event):
        """Set callback for mouse press on model view."""
        if event.button == 3:
            pathinfo = treeview.get_path_at_pos(int(event.x), int(event.y))
            if pathinfo is not None:
                path, col, cellx, celly = pathinfo
                treeview.grab_focus()
                treeview.set_cursor(path, col, 0)
                self.context_menu.popup(None, None, None,
                                        event.button, event.time)
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
            i = sel[1]	# grab off row iter
            stod = self.riders.get_value(i, COL_START)
            st = u''
            if stod is not None:
                st = stod.timestr()
            ftod = self.riders.get_value(i, COL_FINISH)
            ft = u''
            if ftod is not None:
                ft = ftod.timestr()
            ret = uiutil.edit_times_dlg(self.meet.window,st,ft)
            if ret[0] == 1:
                stod = tod.mktod(ret[1])
                ftod = tod.mktod(ret[2])
                bib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
                if stod is not None and ftod is not None:
                    self.settimes(i, stod, ftod)	# set times
                    self.log_elapsed(bib, stod, ftod, manual=True)
                else:
                    self.settimes(i)			# clear times
                    self.log_clear(bib)
                LOG.info(u'Race times manually adjusted for rider %r', bib)
            else:
                LOG.debug('Edit race times cancelled')
            glib.idle_add(self.delayed_announce)

    def tod_context_del_activate_cb(self, menuitem, data=None):
        """Delete selected row from race model."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]	# grab off row iter
            self.riders.remove(i)
            glib.idle_add(self.delayed_announce)

    def log_clear(self, bib):
        """Print clear time log."""
        self.meet.timer_log_msg(bib, u'- Time Cleared -')

    def log_split(self, bib, start, split):
        """Print split log."""
        slbl = u'100'
        if self.evtype == u'flying lap':
            slbl = u'int'
        self.meet.timer_log_straight(bib, slbl, split-start, 3)
        
    def log_elapsed(self, bib, start, finish, split=None, manual=False):
        """Print elapsed log info."""
        if manual:
            self.meet.timer_log_msg(bib, u'- Manual Adjust -')
        self.meet.timer_log_straight(bib, u'ST', start)
        self.meet.timer_log_straight(bib, u'FIN', finish)
        if split is not None:
            slbl = u'L100'
            if self.evtype == u'flying lap':
                slbl = u'L200'
            self.meet.timer_log_straight(bib, slbl, finish - split, 3)
        self.meet.timer_log_straight(bib, u'TIME', finish - start, 3)

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
        self.event = event      # Note: now a treerowref
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
        self.distance = 200
        self.units = u'metres'
        self.autoarm = True
        self.comments = []
        self.chan_S = timy.CHAN_200
        self.chan_I = timy.CHAN_100
        self.chan_F = timy.CHAN_FINISH

        # race run time attributes
        self.onestart = False
        self.winopen = ui
        self.timerwin = False
        self.timerstat = u'idle'
        self.curstart = None
        self.lstart = None
        self.results = tod.todlist(u'FIN')
        self.splits = tod.todlist(u'100')
        self.autospec = u''
        self.inomnium = False
        self.seedsrc = 1        # default seeding is by rank in last round

        self.riders = gtk.ListStore(gobject.TYPE_STRING,   # 0 bib
                                    gobject.TYPE_STRING,   # 1 firstname
                                    gobject.TYPE_STRING,   # 2 lastname
                                    gobject.TYPE_STRING,   # 3 club
                                    gobject.TYPE_STRING,   # 4 Comment
                                    gobject.TYPE_STRING,   # 5 seed
                                    gobject.TYPE_STRING,   # 6 place
                                    gobject.TYPE_PYOBJECT, # 7 Start
                                    gobject.TYPE_PYOBJECT, # 8 Finish
                                    gobject.TYPE_PYOBJECT) # 9 100m 

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
        self.prefix_ent.connect(u'changed', self.editent_cb,u'pref')
        self.prefix_ent.set_text(self.event[u'pref'])
        self.info_ent = b.get_object(u'race_info_title')
        self.info_ent.connect(u'changed', self.editent_cb,u'info')
        self.info_ent.set_text(self.event[u'info'])
        self.traces = {}

        # Timer Pane
        mf = b.get_object(u'race_timer_pane')
        self.fs = timerpane.timerpane(u'Timer')
        self.fs.bibent.connect(u'activate', self.bibent_cb, self.fs)
        self.fs.hide_splits()
        self.fslog = None
        mf.pack_start(self.fs.frame)

        # show window
        self.context_menu = None
        if ui:
            LOG.debug(u'Connecting event ui handlers')
            # Result Pane
            t = gtk.TreeView(self.riders)
            self.view = t
            t.set_reorderable(True)
            t.set_rules_hint(True)
            t.connect(u'button_press_event', self.treeview_button_press)
     
            # TODO: show team name & club but pop up for rider list
            tmlbl = u'200m/Last 100m'
            if self.evtype == u'flying lap':
                tmlbl = u'Time/Last 200m'
            uiutil.mkviewcoltxt(t, u'No.', COL_BIB, calign=1.0)
            uiutil.mkviewcoltxt(t, u'First Name', COL_FIRSTNAME,
                                   self.editcol_db, expand=True)
            uiutil.mkviewcoltxt(t, u'Last Name', COL_LASTNAME,
                                   self.editcol_db, expand=True)
            uiutil.mkviewcoltxt(t, u'Club', COL_CLUB, self.editcol_db)
            uiutil.mkviewcoltxt(t, u'Seed', COL_SEED, self.editcol_db)
            uiutil.mkviewcoltod(t, tmlbl, cb=self.todstr)
            uiutil.mkviewcoltxt(t, u'Rank', COL_PLACE, halign=0.5, calign=0.5)
            t.show()
            b.get_object(u'race_result_win').add(t)

            b.connect_signals(self)
            b = gtk.Builder()
            b.add_from_file(os.path.join(metarace.UI_PATH, u'tod_context.ui'))
            self.context_menu = b.get_object(u'tod_context')
            b.connect_signals(self)
