# SPDX-License-Identifier: MIT
"""Generic race handler for trackmeet."""

import gtk
import glib
import gobject
import logging
import os

import metarace
from metarace import tod
from metarace import timy
from metarace import riderdb
from metarace import scbwin
from metarace import uiutil
from metarace import strops
from metarace import report
from metarace import jsonconfig

_log = logging.getLogger(u'metarace.race')
_log.setLevel(logging.DEBUG)

# config version string
EVENT_ID = u'race-2.0'

# race model column constants
COL_BIB = 0
COL_FIRSTNAME = 1
COL_LASTNAME = 2
COL_CLUB = 3
COL_INFO = 4
COL_DNF = 5
COL_PLACE = 6

# scb function key mappings
key_startlist = u'F3'  # show starters in table
key_results = u'F4'  # recalc/show result window
key_lapdown = u'F11'  # decrement tv lap counter

# timing function key mappings
key_armstart = u'F5'  # arm for start/200m impulse
key_showtimer = u'F6'  # show timer
key_armfinish = u'F9'  # arm for finish impulse

# extended function key mappings
key_abort = u'F5'  # + ctrl for clear/abort
key_falsestart = u'F6'  # + ctrl for false start


class race(object):
    """Data handling for scratch, handicap, keirin, derby, etc races."""

    def clearplaces(self):
        """Clear places from data model."""
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

    def getiter(self, bib):
        """Return temporary iterator to model row."""
        i = self.riders.get_iter_first()
        while i is not None:
            if self.riders.get_value(i, COL_BIB).decode(u'utf-8') == bib:
                break
            i = self.riders.iter_next(i)
        return i

    def delayed_reorder(self):
        """Call reorder if the flag is one."""
        if self.reorderflag > 1:
            self.reorderflag -= 1
        elif self.reorderflag == 1:
            self.reorder_handicap()
            self.reorderflag = 0
        else:
            self.reorderflag = 0  # clamp negatives
        return False

    def addrider(self, bib=u'', info=None):
        """Add specified rider to race model."""
        nr = [bib, u'', u'', u'', u'', False, u'']
        er = self.getrider(bib)
        if not bib or er is None:
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                for i in range(1, 4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)  # unicode
                    if self.evtype == u'handicap':  # reqd?
                        if info:
                            nr[COL_INFO] = info
            if self.evtype in [u'handicap', u'keirin'] and not self.onestart:
                self.reorderflag += 1
                glib.timeout_add_seconds(1, self.delayed_reorder)
            self.riders.append(nr)
        else:
            if er is not None:
                # Rider already in the model, set the info if
                # event type is handicap or event is part of omnium
                if self.evtype == u'handicap' or self.inomnium:
                    if not er[COL_INFO] and info:  # don't overwrite if set
                        er[COL_INFO] = info

    def dnfriders(self, biblist=u''):
        """Remove listed bibs from the race."""
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[COL_DNF] = True
                _log.info(u'Rider %r withdrawn', bib)
            else:
                _log.warn(u'Did not withdraw rider %r', bib)
        return False

    def delrider(self, bib):
        """Remove the specified rider from the model."""
        i = self.getiter(bib)
        if i is not None:
            self.riders.remove(i)

    def placexfer(self, placestr):
        """Transfer places in placestr to model."""
        self.clearplaces()
        self.results = []
        placeset = set()
        resname_w = self.meet.scb.linelen - 11

        # move dnf riders to bottom of list and count inriders
        cnt = 0
        incnt = 0
        reorddnf = []
        if len(self.riders) > 0:
            for r in self.riders:
                if r[COL_DNF]:
                    reorddnf.append(cnt)
                else:
                    incnt += 1
                    reorddnf.insert(0, cnt)
                cnt += 1
            self.riders.reorder(reorddnf)

        # update eliminated rider ranks
        outriders = []
        for bib in self.eliminated:
            r = self.getrider(bib)
            rank = incnt + self.startplace
            r[COL_PLACE] = rank
            club = r[COL_CLUB].decode(u'utf-8')
            if len(club) > 3:
                # look it up?
                #if self.series in self.meet.ridermap:
                #rh = self.meet.ridermap[self.series][bib]
                #if rh is not None:
                #club = rh[u'note']
                club = u''
            outriders.insert(0, [
                unicode(rank) + u'.', bib,
                strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                               r[COL_LASTNAME].decode(u'utf-8'), resname_w),
                club
            ])
            i = self.getiter(bib)
            incnt -= 1
            self.riders.swap(self.riders.get_iter(incnt), i)

        # overwrite eliminations from placed riders - but warn on overlap
        place = 1
        count = 0
        clubmode = self.meet.get_clubmode()
        for placegroup in placestr.split():
            for bib in placegroup.split(u'-'):
                if bib not in placeset:
                    if count >= incnt and not clubmode:
                        _log.error(u'Error: More places than available')
                        break
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is None:  # ensure rider exists at this point
                        _log.warn(u'Adding non-starter: %r', bib)
                        self.addrider(bib)
                        r = self.getrider(bib)
                    rank = place + self.startplace
                    r[COL_PLACE] = rank
                    club = r[COL_CLUB].decode(u'utf-8')
                    if len(club) > 3:
                        # look it up?
                        #if self.series in self.meet.ridermap:
                        #rh = self.meet.ridermap[self.series][bib]
                        #if rh is not None:
                        #club = rh[u'note']
                        club = u''
                    self.results.append([
                        unicode(rank) + u'.', bib,
                        strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                       r[COL_LASTNAME].decode(u'utf-8'),
                                       resname_w), club
                    ])
                    i = self.getiter(bib)
                    self.riders.swap(self.riders.get_iter(count), i)
                    count += 1
                else:
                    _log.error(u'Ignoring duplicate no: %r', bib)
            place = count + 1
        self.results.extend(outriders)
        if count > 0 or len(outriders) > 0:
            self.onestart = True
        if count == incnt:
            self.resulttype = u'RESULT'
        elif count < incnt and len(outriders) > 0:
            self.resulttype = u'STANDING'
        else:
            self.resulttype = u'PROVISIONAL RESULT'

    def loadconfig(self):
        """Load race config from disk."""
        self.riders.clear()
        # set defaults timetype based on event type
        deftimetype = u'start/finish'
        defdistance = None
        defdistunits = u'laps'
        self.seedsrc = None  # default is no seed info
        if self.evtype == u'handicap':
            self.seedsrc = 3  # fetch handicap info from autospec
        if self.evtype in [u'sprint', u'keirin']:
            deftimetype = u'200m'
            defdistunits = u'metres'
            defdistance = u'200'
        if self.evtype == u'elimination':
            i = self.action_model.append([u'Eliminate', u'out'])
            self.action_model.append([u'Un-Eliminate', u'in'])
            if i is not None:
                self.ctrl_action_combo.set_active_iter(i)
        cr = jsonconfig.config({
            u'event': {
                u'startlist': u'',
                u'id': EVENT_ID,
                u'ctrl_places': u'',
                u'eliminated': [],
                u'start': None,
                u'lstart': None,
                u'comments': [],
                u'finish': None,
                u'runlap': None,
                u'distance': defdistance,
                u'distunits': defdistunits,
                u'showinfo': True,
                u'inomnium': False,
                u'startplace': 0,
                u'autospec': u'',
                u'timetype': deftimetype
            },
            u'riders': {}
        })
        cr.add_section(u'event')
        cr.add_section(u'riders')
        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                _log.error(u'Unable to read config: %s', e)
        else:
            _log.info(u'%r not found, loading defaults', self.configfile)
        self.inomnium = strops.confopt_bool(cr.get(u'event', u'inomnium'))
        if self.inomnium:
            self.seedsrc = 1  # fetch start list seeding from omnium
        self.autospec = cr.get(u'event', u'autospec')
        rlist = cr.get(u'event', u'startlist').split()
        for r in rlist:
            nr = [r, u'', u'', u'', u'', False, u'']
            if cr.has_option('riders', r):
                ril = cr.get(u'riders', r)
                for i in range(0, 3):
                    if len(ril) > i:
                        nr[i + 4] = ril[i]
            # Re-patch name
            dbr = self.meet.rdb.getrider(r, self.series)
            if dbr is not None:
                for i in range(1, 4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)  # unicode
            self.riders.append(nr)

        # race infos
        self.comments = cr.get(u'event', u'comments')
        self.startplace = strops.confopt_posint(cr.get(u'event',
                                                       u'startplace'))
        self.set_timetype(cr.get(u'event', u'timetype'))
        self.distance = strops.confopt_dist(cr.get(u'event', u'distance'))
        self.units = strops.confopt_distunits(cr.get(u'event', u'distunits'))
        self.runlap = strops.confopt_posint(cr.get(u'event', u'runlap'))
        if self.timetype != u'200m' and self.event[u'laps']:
            # use event program to override
            self.units = u'laps'
            self.distance = strops.confopt_posint(self.event[u'laps'],
                                                  self.distance)
        self.update_expander_lbl_cb()
        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get(u'event', u'showinfo')))
        self.set_start(cr.get(u'event', u'start'), cr.get(u'event', u'lstart'))
        self.set_finish(cr.get(u'event', u'finish'))
        self.set_elapsed()
        self.eliminated = cr.get(u'event', u'eliminated')
        places = strops.reformat_placelist(cr.get(u'event', u'ctrl_places'))
        self.ctrl_places.set_text(places)
        self.placexfer(places)
        if places:
            self.doscbplaces = False  # only show places on board if not set
            self.setfinished()
        else:
            if self.autospec:
                self.meet.autostart_riders(self,
                                           self.autospec,
                                           infocol=self.seedsrc)
            if self.evtype in [u'handicap', u'keirin'] or self.inomnium:
                self.reorder_handicap()

        # After load complete - check config and report.
        eid = cr.get(u'event', u'id')
        if eid and eid != EVENT_ID:
            _log.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)

    def sort_riderno(self, x, y):
        """Sort riders by rider no."""
        return cmp(strops.riderno_key(x[1]), strops.riderno_key(y[1]))

    def sort_handicap(self, x, y):
        """Sort function for handicap marks."""
        if x[2] != y[2]:
            if x[2] is None:  # y sorts first
                return 1
            elif y[2] is None:  # x sorts first
                return -1
            else:  # Both should be ints here
                return cmp(x[2], y[2])
        else:  # Defer to rider number
            return cmp(strops.riderno_key(x[1]), strops.riderno_key(y[1]))

    def reorder_handicap(self):
        """Reorder rider model according to the handicap marks."""
        if len(self.riders) > 1:
            auxmap = []
            cnt = 0
            for r in self.riders:
                auxmap.append([
                    cnt, r[COL_BIB].decode(u'utf-8'),
                    strops.mark2int(r[COL_INFO].decode(u'utf-8'))
                ])
                cnt += 1
            if self.inomnium or self.evtype == u'handicap':
                auxmap.sort(self.sort_handicap)
            else:
                auxmap.sort(self.sort_riderno)
            self.riders.reorder([a[0] for a in auxmap])

    def set_timetype(self, data=None):
        """Update state and ui to match timetype."""
        if data is not None:
            self.timetype = strops.confopt_pair(data, u'200m', u'start/finish')
            self.finchan = 1
            if self.timetype == u'200m':
                self.startchan = 4
            else:
                self.startchan = 0

    def set_start(self, start=None, lstart=None):
        """Set the race start."""
        self.start = tod.mktod(start)
        if lstart is not None:
            self.lstart = tod.mktod(lstart)
        else:
            self.lstart = self.start
        if self.start is None:
            pass
        else:
            if self.finish is None:
                self.setrunning()

    def set_finish(self, finish=None):
        """Set the race finish."""
        self.finish = tod.mktod(finish)
        if self.finish is None:
            if self.start is not None:
                self.setrunning()
        else:
            if self.start is None:
                self.set_start(0)  # TODO: Verify this path
            self.setfinished()

    def log_elapsed(self):
        """Log race elapsed time on Timy."""
        self.meet.main_timer.printline(self.meet.racenamecat(self.event))
        self.meet.main_timer.printline(u'      ST: ' + self.start.timestr(4))
        self.meet.main_timer.printline(u'     FIN: ' + self.finish.timestr(4))
        self.meet.main_timer.printline(u'    TIME: ' +
                                       (self.finish - self.start).timestr(2))

    def set_elapsed(self):
        """Update elapsed time in race ui and announcer."""
        if self.start is not None and self.finish is not None:
            et = self.finish - self.start
            self.time_lbl.set_text(et.timestr(2))
        elif self.start is not None:  # Note: uses 'local start' for RT
            runtm = (tod.now() - self.lstart).timestr(1)
            self.time_lbl.set_text(runtm)
            if self.runlap is not None:
                if self.runlap != self.lastrunlap:
                    _log.debug(u'Runlap: %r', self.runlap)
                    self.lastrunlap = self.runlap
        elif self.timerstat == u'armstart':
            self.time_lbl.set_text(u'       0.0   ')  # tod.ZERO.timestr(1)
            if self.runlap and self.runlap != self.lastrunlap:
                _log.debug(u'Runlap: %r', self.runlap)
                self.lastrunlap = self.runlap
        else:
            self.time_lbl.set_text(u'')

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
            self.meet.txt_line(19)

            # write out riders
            count = 0
            curline = 4
            posoft = 0
            for r in self.riders:
                count += 1
                if count == 14:
                    curline = 4
                    posoft = 41
                xtra = '    '
                if r[COL_INFO]:
                    inf = r[COL_INFO].decode(u'utf-8')
                    if self.evtype in [u'keirin', u'sprint']:  # encirc draw no
                        inf = strops.drawno_encirc(inf)
                    xtra = strops.truncpad(inf, 4, u'r')
                clubstr = u''
                tcs = r[COL_CLUB].decode(u'utf-8')
                if tcs and tcs <= 3:
                    clubstr = u' (' + tcs + u')'
                namestr = strops.truncpad(
                    strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                   r[COL_LASTNAME].decode(u'utf-8'),
                                   25 - len(clubstr),
                                   trunc=True) + clubstr,
                    25,
                    ellipsis=False)

                placestr = u'   '
                if r[COL_PLACE] != u'':
                    placestr = strops.truncpad(
                        r[COL_PLACE].decode(u'utf-8') + u'.', 3)
                elif r[COL_DNF]:
                    placestr = u'dnf'
                bibstr = strops.truncpad(r[COL_BIB].decode(u'utf-8'), 3, u'r')
                self.meet.txt_postxt(
                    curline, posoft,
                    u' '.join([placestr, bibstr, namestr, xtra]))
                curline += 1

            tp = u''
            if self.start is not None and self.finish is not None:
                et = self.finish - self.start
                if self.timetype == u'200m':
                    tp = u'200m: '
                else:
                    tp = u'Time: '
                tp += et.timestr(2) + u'    '
                dist = self.meet.get_distance(self.distance, self.units)
                if dist:
                    tp += u'Avg: ' + et.speedstr(dist)
            self.meet.txt_setline(21, tp)
        return False

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        sec = None
        etype = self.event[u'type']
        twocol = True
        if not self.inomnium and not program and etype in [u'axe', u'run']:
            sec = report.section()  # one column overrides
        else:
            sec = report.twocol_startlist()
        headvec = []
        if etype != u'break':
            headvec.extend([u'Event', self.evno, u':'])
        headvec.append(self.event[u'pref'])
        headvec.append(self.event[u'info'])
        if not program:
            headvec.append(u'- Start List')
        sec.heading = u' '.join(headvec)
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        if substr:
            sec.subheading = substr

        self.reorder_handicap()
        sec.lines = []
        cnt = 0
        col2 = []
        if self.inomnium and len(self.riders) > 0:
            sec.lines.append([u' ', u' ', u'The Fence', None, None, None])
            col2.append([u' ', u' ', u'Sprinters Lane', None, None, None])
        for r in self.riders:
            cnt += 1
            rno = r[COL_BIB].decode(u'utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            inf = r[COL_INFO].decode(u'utf-8')
            if self.evtype in [u'keirin', u'sprint']:  # encirc draw no
                inf = strops.drawno_encirc(inf)
            if self.inomnium:
                # inf holds seed, ignore for now
                inf = None
            rname = u''
            if rh is not None:
                rname = rh[u'namestr']
            if self.inomnium:
                if cnt % 2 == 1:
                    sec.lines.append([None, rno, rname, inf, None, None])
                else:
                    col2.append([None, rno, rname, inf, None, None])
            else:
                sec.lines.append([None, rno, rname, inf, None, None])
        for i in col2:
            sec.lines.append(i)
        if self.event[u'plac']:
            while cnt < self.event[u'plac']:
                sec.lines.append([None, None, None, None, None, None])
                cnt += 1
        ret.append(sec)

        ptype = u'Riders'
        if etype == u'run':
            ptype = u'Runners'
        elif self.evtype == u'axe':
            ptype = u'Axemen'
        if cnt > 0 and not program:
            sec = report.bullet_text()
            sec.lines.append([None, u'Total ' + ptype + u': ' + str(cnt)])
            ret.append(sec)
        return ret

    def get_startlist(self):
        """Return a list of bibs in the rider model."""
        ret = []
        for r in self.riders:
            ret.append(r[COL_BIB].decode(u'utf-8'))
        return u' '.join(ret)

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            _log.error(u'Attempt to save readonly event')
            return
        cw = jsonconfig.config()
        cw.add_section(u'event')
        if self.start is not None:
            cw.set(u'event', u'start', self.start.rawtime())
        if self.lstart is not None:
            cw.set(u'event', u'lstart', self.lstart.rawtime())
        if self.finish is not None:
            cw.set(u'event', u'finish', self.finish.rawtime())
        cw.set(u'event', u'ctrl_places',
               self.ctrl_places.get_text().decode(u'utf-8'))
        cw.set(u'event', u'eliminated', self.eliminated)
        cw.set(u'event', u'startlist', self.get_startlist())
        cw.set(u'event', u'showinfo', self.info_expand.get_expanded())
        cw.set(u'event', u'distance', self.distance)
        cw.set(u'event', u'distunits', self.units)
        cw.set(u'event', u'timetype', self.timetype)
        if self.runlap is not None:
            cw.set(u'event', u'runlap', self.runlap)
        cw.set(u'event', u'autospec', self.autospec)
        cw.set(u'event', u'inomnium', self.inomnium)
        cw.set(u'event', u'comments', self.comments)
        cw.set(u'event', u'startplace', self.startplace)

        cw.add_section(u'riders')
        for r in self.riders:
            cw.set(u'riders', r[COL_BIB].decode(u'utf-8'), [
                r[COL_INFO].decode(u'utf-8'), r[COL_DNF],
                r[COL_PLACE].decode(u'utf-8')
            ])
        cw.set(u'event', u'id', EVENT_ID)
        _log.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def shutdown(self, win=None, msg=u'Exit'):
        """Terminate event object."""
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        _log.debug(u'Shutdown %sevent %s: %s', rstr, self.evno, msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def do_properties(self):
        """Run event properties dialog."""
        prfile = os.path.join(metarace.UI_PATH, u'race_properties.ui')
        _log.debug(u'Building event properties from %r', prfile)
        b = gtk.Builder()
        b.add_from_file(prfile)
        dlg = b.get_object(u'properties')
        dlg.set_transient_for(self.meet.window)
        rt = b.get_object(u'race_score_type')
        if self.timetype != u'200m':
            rt.set_active(0)
        else:
            rt.set_active(1)
        di = b.get_object(u'race_dist_entry')
        if self.distance is not None:
            di.set_text(str(self.distance))
        else:
            di.set_text(u'')
        du = b.get_object(u'race_dist_type')
        if self.units == u'metres':
            du.set_active(0)
        else:
            du.set_active(1)
        se = b.get_object(u'race_series_entry')
        se.set_text(self.series)
        as_e = b.get_object(u'auto_starters_entry')
        as_e.set_text(self.autospec)
        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            _log.debug(u'Updating event properties')
            if rt.get_active() == 0:
                self.set_timetype(u'start/finish')
            else:
                self.set_timetype(u'200m')
            dval = di.get_text().decode(u'utf-8')
            if dval.isdigit():
                self.distance = int(dval)
            else:
                self.distance = None
            if du.get_active() == 0:
                self.units = u'metres'
            else:
                self.units = u'laps'

            # update series
            ns = se.get_text().decode(u'utf-8')
            if ns != self.series:
                self.series = ns
                self.event[u'seri'] = ns

            # update auto startlist spec
            nspec = as_e.get_text().decode(u'utf-8')
            if nspec != self.autospec:
                self.autospec = nspec
                if not self.ctrl_places.get_text():
                    if self.autospec:
                        self.meet.autostart_riders(self, self.autospec,
                                                   self.seedsrc)
                    if self.evtype == u'handicap':
                        self.reorder_handicap()

            # xfer starters if not empty
            slist = strops.riderlist_split(
                b.get_object(u'race_starters_entry').get_text().decode(
                    u'utf-8'), self.meet.rdb, self.series)
            for s in slist:
                self.addrider(s)
            glib.idle_add(self.delayed_announce)
        else:
            _log.debug(u'Edit event properties cancelled')

        # if prefix is empty, grab input focus
        if not self.prefix_ent.get_text():
            self.prefix_ent.grab_focus()
        dlg.destroy()

    def resettimer(self):
        """Reset race timer."""
        self.finish = None
        self.start = None
        self.lstart = None
        self.timerstat = u'idle'
        self.ctrl_places.set_text(u'')
        self.eliminated = []
        self.placexfer(u'')
        self.meet.main_timer.dearm(self.startchan)
        self.meet.main_timer.dearm(self.finchan)
        self.stat_but.buttonchg(uiutil.bg_none, u'Idle')
        self.stat_but.set_sensitive(True)
        self.set_elapsed()
        _log.info(u'Event reset - all places cleared.')

    def setrunning(self):
        """Set timer state to 'running'."""
        self.timerstat = u'running'
        self.stat_but.buttonchg(uiutil.bg_none, u'Running')

    def setfinished(self):
        """Set timer state to 'finished'."""
        self.timerstat = u'finished'
        self.stat_but.buttonchg(uiutil.bg_none, u'Finished')
        self.stat_but.set_sensitive(False)
        self.ctrl_places.grab_focus()

    def armstart(self):
        """Toggle timer arm start state."""
        if self.timerstat == u'idle':
            self.timerstat = u'armstart'
            self.stat_but.buttonchg(uiutil.bg_armstart, u'Arm Start')
            self.meet.main_timer.arm(self.startchan)
            if self.timetype == u'200m':
                # also accept C0 on sprint types
                self.meet.main_timer.arm(0)
        elif self.timerstat == u'armstart':
            self.timerstat = u'idle'
            self.time_lbl.set_text(u'')
            self.stat_but.buttonchg(uiutil.bg_none, u'Idle')
            self.meet.main_timer.dearm(self.startchan)
            if self.timetype == u'200m':
                # also accept C0 on sprint types
                self.meet.main_timer.dearm(0)

    def armfinish(self):
        """Toggle timer arm finish state."""
        if self.timerstat == u'running':
            self.timerstat = u'armfinish'
            self.stat_but.buttonchg(uiutil.bg_armfin, u'Arm Finish')
            self.meet.main_timer.arm(self.finchan)
        elif self.timerstat == u'armfinish':
            self.timerstat = u'running'
            self.stat_but.buttonchg(uiutil.bg_none, u'Running')
            self.meet.main_timer.dearm(self.finchan)
        return False  # for use in delayed callback

    def showtimer(self):
        """Display the running time on the scoreboard."""
        if self.timerstat == u'idle':
            self.armstart()
        tp = u'Time:'
        if self.timetype == u'200m':
            tp = u'200m:'
        self.meet.cmd_announce(u'eliminated', u'')
        self.meet.scbwin = scbwin.scbtimer(scb=self.meet.scb,
                                           line1=self.meet.racenamecat(
                                               self.event),
                                           line2=u'',
                                           timepfx=tp)
        wastimer = self.timerwin
        self.timerwin = True
        if self.timerstat == u'finished':
            if not wastimer:
                self.meet.scbwin.reset()
            if self.start is not None and self.finish is not None:
                elap = self.finish - self.start
                self.meet.scbwin.settime(elap.timestr(2))
                dist = self.meet.get_distance(self.distance, self.units)
                if dist:
                    self.meet.scbwin.setavg(elap.speedstr(dist))
            self.meet.scbwin.update()
        else:
            self.meet.scbwin.reset()

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or u'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort:  # override ctrl+f5
                    self.resettimer()
                    return True
            if key[0] == u'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_armfinish:
                    self.armfinish()
                    return True
                elif key == key_showtimer:
                    self.showtimer()
                    return True
                elif key == key_startlist:
                    self.do_startlist()
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_results:
                    self.doscbplaces = True  # override if already clear
                    self.do_places()
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_lapdown:
                    if self.runlap is not None and self.runlap > 0:
                        self.runlap -= 1
                    return True
        return False

    def do_places(self):
        """Update model and show race result on scoreboard."""
        secs = self.result_report()  # NOTE: calls into placexfer
        self.timerwin = False
        tp = u'Time:'
        if self.start is not None and self.finish is None:
            self.finish = tod.now()
            if self.lstart is not None:
                self.start = self.lstart  # override with localtime
            self.set_elapsed()
        if self.timetype == u'200m':
            tp = u'200m:'
            # previously, winner was displayed on gemini here
        ts = None
        if self.start is not None and self.finish is not None:
            ts = (self.finish - self.start).timestr(2)
        if self.doscbplaces:
            FMT = [(3, u'l'), (3, u'r'), u' ',
                   (self.meet.scb.linelen - 11, u'l'), (4, u'r')]
            self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                               head=self.meet.racenamecat(
                                                   self.event),
                                               subhead=self.resulttype,
                                               coldesc=FMT,
                                               rows=self.results,
                                               timepfx=tp,
                                               timestr=ts)
            self.meet.scbwin.reset()
            self.doscbplaces = False
        self.setfinished()

    def do_startlist(self):
        """Show start list on scoreboard."""

        self.reorder_handicap()
        self.meet.scbwin = None
        self.timerwin = False
        startlist = []
        ##self.meet.announce.gfx_overlay(1)
        ##self.meet.announce.gfx_set_title(u'Startlist: '
        ##+ self.event[u'pref'] + u' '
        ##+ self.event[u'info'])
        name_w = self.meet.scb.linelen - 8
        for r in self.riders:
            if not r[COL_DNF]:
                nfo = r[COL_INFO].decode(u'utf-8')
                if self.evtype in [u'sprint']:  # add asterisk
                    nfo = nfo + r[COL_CLUB].decode(u'utf-8').rjust(3)
                if not nfo:
                    nfo = r[COL_CLUB].decode(u'utf-8')
                    if len(nfo) > 3:
                        # look it up?
                        #if self.series in self.meet.ridermap:
                        #rh = self.meet.ridermap[self.series][r[0]]
                        #if rh is not None:
                        #nfo = rh[u'note']
                        nfo = u''
                startlist.append([
                    r[COL_BIB].decode(u'utf-8'),
                    strops.fitname(r[COL_FIRSTNAME].decode(u'utf-8'),
                                   r[COL_LASTNAME].decode(u'utf-8'), name_w),
                    nfo
                ])
                #inf = r[COL_INFO].decode(u'utf-8').strip()
                #if self.evtype in [u'keirin', u'sprint']:  # encirc draw no
                #inf = strops.drawno_encirc(inf)
                ##self.meet.announce.gfx_add_row([r[COL_BIB].decode(u'utf-8'),
                ##strops.resname(r[COL_FIRSTNAME].decode(u'utf-8'),
                ##r[COL_LASTNAME].decode(u'utf-8'),
                ##r[COL_CLUB].decode(u'utf-8')),
                ##inf])
        FMT = [(3, u'r'), u' ', (name_w, u'l'), (4, u'r')]
        self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                           head=self.meet.racenamecat(
                                               self.event),
                                           subhead=u'STARTLIST',
                                           coldesc=FMT,
                                           rows=startlist)
        self.meet.scbwin.reset()

    def stat_but_cb(self, button):
        """Race ctrl button callback."""
        if self.timerstat in (u'idle', u'armstart'):
            self.armstart()
        elif self.timerstat in (u'running', u'armfinish'):
            self.armfinish()

    def checkplaces(self, places=u''):
        """Check the proposed places against current race model."""
        ret = True
        placeset = set()
        for no in strops.reformat_biblist(places).split():
            # repetition? - already in place set?
            if no in placeset:
                _log.error(u'Duplicate no in places: %r', no)
                ret = False
            placeset.add(no)
            # rider in the model?
            lr = self.getrider(no)
            if lr is None:
                if not self.meet.get_clubmode():
                    _log.error(u'Non-starter in places: %r', no)
                    ret = False
                # otherwise club mode allows non-starter in places
            else:
                # rider still in the race?
                if lr[COL_DNF]:
                    _log.error(u'DNF rider in places: %r', no)
                    ret = False
        return ret

    def race_ctrl_places_activate_cb(self, entry, data=None):
        """Respond to activate on place entry."""
        places = strops.reformat_placelist(entry.get_text().decode(u'utf-8'))
        if self.checkplaces(places):
            entry.set_text(places)
            self.do_places()
            glib.idle_add(self.delayed_announce)
            self.meet.delayed_export()
        else:
            _log.error(u'Places not updated')

    def race_ctrl_action_activate_cb(self, entry, data=None):
        """Perform current action on bibs listed."""
        rlist = entry.get_text().decode(u'utf-8')
        acode = self.action_model.get_value(
            self.ctrl_action_combo.get_active_iter(), 1).decode(u'utf-8')
        if acode == u'dnf':
            self.dnfriders(strops.reformat_biblist(rlist))
            entry.set_text('')
        elif acode == u'add':
            rlist = strops.riderlist_split(rlist, self.meet.rdb, self.series)
            for bib in rlist:
                self.addrider(bib)
            entry.set_text(u'')
        elif acode == u'del':
            rlist = strops.riderlist_split(rlist, self.meet.rdb, self.series)
            for bib in rlist:
                self.delrider(bib)
            entry.set_text(u'')
        elif acode == u'lap':
            self.runlap = strops.confopt_posint(rlist)
            _log.debug(u'Manually set runlap to: %r', self.runlap)
        elif acode == u'out' and self.evtype == u'elimination':
            bib = rlist.strip()
            if self.eliminate(bib):
                entry.set_text(u'')
            # Short-circuit method to avoid re-announce
            return False
        elif acode == u'in' and self.evtype == u'elimination':
            bib = rlist.strip()
            if self.uneliminate(bib):
                entry.set_text(u'')
            # Short-circuit method to avoid re-announce
            return False
        else:
            _log.error(u'Ignoring invalid action')
            return False
        glib.idle_add(self.delayed_announce)

    def update_expander_lbl_cb(self):
        """Update race info expander label."""
        self.info_expand.set_label(self.meet.infoline(self.event))

    def uneliminate(self, bib):
        """Remove rider from the set of eliminated riders."""
        ret = False
        r = self.getrider(bib)
        if r is not None:
            if not r[COL_DNF]:
                if bib in self.eliminated:
                    self.eliminated.remove(bib)
                    self.placexfer(
                        self.ctrl_places.get_text().decode(u'utf-8'))
                    _log.info(u'Rider %r removed from eliminated riders.', bib)
                    glib.idle_add(self.delayed_announce)
                    ret = True
                else:
                    _log.error(u'Rider %r not eliminated', bib)
            else:
                _log.error(u'Cannot un-eliminate dnf rider: %r', bib)
        else:
            _log.error(u'Cannot un-eliminate non-starter: %r', bib)

        return ret

    def eliminate(self, bib):
        """Register rider as eliminated."""
        ret = False
        r = self.getrider(bib)
        if r is not None:
            if not r[COL_DNF]:
                if bib not in self.eliminated:
                    self.eliminated.append(bib)
                    self.placexfer(
                        self.ctrl_places.get_text().decode(u'utf-8'))
                    _log.error(u'Rider %r eliminated.', bib)
                    ret = True
                    fname = r[COL_FIRSTNAME].decode(u'utf-8')
                    lname = r[COL_LASTNAME].decode(u'utf-8')
                    club = r[COL_CLUB].decode(u'utf-8')
                    rno = r[COL_BIB].decode(u'utf-8')
                    rstr = (rno + u' ' + strops.fitname(
                        fname, lname, self.meet.scb.linelen - 3 - len(rno)))
                    self.meet.scbwin = scbwin.scbintsprint(
                        scb=self.meet.scb,
                        line1=self.meet.racenamecat(self.event),
                        line2=u'RIDER ELIMINATED',
                        coldesc=[u' ', (self.meet.scb.linelen - 1, u'l')],
                        rows=[[rstr]])
                    self.meet.scbwin.reset()
                    self.meet.gemini.reset_fields()
                    self.meet.gemini.set_bib(bib)
                    self.meet.gemini.show_brt()
                    self.meet.cmd_announce(u'eliminated', bib)
                    # announce it:
                    nrstr = strops.truncpad(
                        strops.resname_bib(r[COL_BIB].decode(u'utf-8'),
                                           r[COL_FIRSTNAME].decode(u'utf-8'),
                                           r[COL_LASTNAME].decode(u'utf-8'),
                                           r[COL_CLUB].decode(u'utf-8')), 60)
                    self.meet.txt_postxt(21, 0, u'Out: ' + nrstr)
                    glib.timeout_add_seconds(10, self.delayed_result)
                else:
                    _log.error(u'Rider %r already eliminated.', bib)
            else:
                _log.error(u'Cannot eliminate dnf rider: %r', bib)
        else:
            _log.error(u'Cannot eliminate non-starter: %r', bib)

        return ret

    def delayed_result(self):
        if self.ctrl_action.get_property('has-focus'):
            if isinstance(self.meet.scbwin, scbwin.scbintsprint):
                FMT = [(3, u'l'), (3, u'r'), u' ',
                       (self.meet.scb.linelen - 11, u'l'), (4, u'r')]
                self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                                   head=self.meet.racenamecat(
                                                       self.event),
                                                   subhead=self.resulttype,
                                                   coldesc=FMT,
                                                   rows=self.results)
                self.meet.scbwin.reset()
        self.meet.cmd_announce(u'eliminated', u'')
        self.meet.gemini.clear()
        glib.idle_add(self.delayed_announce)

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode(u'utf-8')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode(u'utf-8')
        self.update_expander_lbl_cb()

    def editcol_cb(self, cell, path, new_text, col):
        """Startlist cell update callback."""
        new_text = new_text.decode(u'utf-8').strip()
        if col == COL_BIB:
            if new_text.isalnum():
                if self.getrider(new_text) is None:
                    self.riders[path][COL_BIB] = new_text
                    dbr = self.meet.rdb.getrider(new_text, self.series)
                    if dbr is not None:
                        for i in range(1, 4):
                            self.riders[path][i] = self.meet.rdb.getvalue(
                                dbr, i)
        else:
            self.riders[path][col] = new_text.strip()

    def editcol_db(self, cell, path, new_text, col):
        """Cell update with writeback to meet."""
        new_text = new_text.decode(u'utf-8').strip()
        self.riders[path][col] = new_text
        glib.idle_add(self.meet.rider_edit,
                      self.riders[path][COL_BIB].decode(u'utf-8'), self.series,
                      col, new_text)

    def gotorow(self, i=None):
        """Select row for specified iterator."""
        if i is None:
            i = self.riders.get_iter_first()
        if i is not None:
            self.view.scroll_to_cell(self.riders.get_path(i))
            self.view.set_cursor_on_cell(self.riders.get_path(i))

    def dnf_cb(self, cell, path, col):
        """Toggle rider dnf flag."""
        self.riders[path][col] = not self.riders[path][col]

    def starttrig(self, e):
        """React to start trigger."""
        if self.timerstat == u'armstart':
            if self.distance and self.units == u'laps':
                self.runlap = self.distance - 1
                _log.debug(u'Set runlap: %r', self.runlap)
            self.start = e
            self.lstart = tod.now()
            self.setrunning()
            if self.timetype == u'200m':
                glib.timeout_add_seconds(4, self.armfinish)
                # delayed auto arm 200...

    def fintrig(self, e):
        """React to finish trigger."""
        if self.timerstat == u'armfinish':
            self.finish = e
            self.setfinished()
            self.set_elapsed()
            self.log_elapsed()
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                self.showtimer()
            glib.idle_add(self.delayed_announce)

    def timercb(self, e):
        """Handle a timer event."""
        chan = timy.chan2id(e.chan)
        if chan == self.startchan or chan == 0:
            _log.debug(u'Got a start impulse')
            self.starttrig(e)
        elif chan == self.finchan:
            _log.debug('Got a finish impulse')
            self.fintrig(e)
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        if self.finish is None:
            self.set_elapsed()
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                self.meet.scbwin.settime(self.time_lbl.get_text())
        return True

    def race_info_time_edit_activate_cb(self, button):
        """Display race timing edit dialog."""
        ostx = u''
        oftx = u''
        if self.start is not None:
            ostx = self.start.rawtime(4)
        else:
            ostx = u'0.0'
        if self.finish is not None:
            oftx = self.finish.rawtime(4)
        ret = uiutil.edit_times_dlg(self.meet.window, ostx, oftx)
        if ret[0] == 1:
            try:
                stod = None
                if ret[1]:
                    stod = tod.tod(ret[1], u'MANU', u'C0i')
                    self.meet.main_timer.printline(u' ' + str(stod))
                ftod = None
                if ret[2]:
                    ftod = tod.tod(ret[2], u'MANU', u'C1i')
                    self.meet.main_timer.printline(u' ' + str(ftod))
                _log.info(u'Updating race times st=%r, ft=%r', stod, ftod)
                self.set_start(stod)
                self.set_finish(ftod)
                self.set_elapsed()
                if self.start is not None and self.finish is not None:
                    self.log_elapsed()
            except Exception as v:
                _log.error(u'Error updating times %s: %s',
                           v.__class__.__name__, v)

            glib.idle_add(self.delayed_announce)
        else:
            _log.info('Edit race times cancelled')

    def result_gen(self):
        """Generator function to export a final result."""
        ft = None
        for r in self.riders:
            bib = r[COL_BIB].decode(u'utf-8')
            rank = None
            info = u''
            if self.evtype in [u'handicap', u'sprint']:
                # include handicap and previous win info
                info = r[COL_INFO].decode(u'utf-8').strip()
            if self.onestart:
                if not r[COL_DNF]:
                    if r[COL_PLACE]:
                        rank = int(r[COL_PLACE].decode(u'utf-8'))
                else:
                    inft = r[COL_INFO].decode(u'utf-8')
                    if inft in [u'dns', u'dsq']:
                        rank = inft
                    else:
                        rank = u'dnf'
            time = None
            if self.finish is not None and ft is None:
                time = (self.finish - self.start).rawtime(2)
                ft = True
            yield [bib, rank, time, info]

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        self.placexfer(self.ctrl_places.get_text().decode(u'utf-8'))
        ret = []
        sec = report.section()
        sec.heading = u'Event ' + self.evno + u': ' + u' '.join(
            [self.event[u'pref'], self.event[u'info']]).strip()
        sec.lines = []
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        first = True
        fs = u''
        if self.finish is not None:
            fs = self.time_lbl.get_text().decode(u'utf-8').strip()
        rcount = 0
        pcount = 0
        for r in self.riders:
            plstr = u''
            rcount += 1
            rno = r[COL_BIB].decode(u'utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            rname = u''
            if rh is not None:
                rname = rh[u'namestr']
            inf = r[COL_INFO].decode(u'utf-8').strip()
            if self.evtype in [u'keirin', u'sprint']:  # encirc draw no
                inf = strops.drawno_encirc(inf)
            if r[COL_DNF]:
                pcount += 1
                if r[COL_INFO] in [u'dns', u'dsq']:
                    plstr = r[COL_INFO].decode(u'utf-8')
                    inf = None
                else:
                    plstr = u'dnf'
            elif self.onestart and r[COL_PLACE] != u'':
                plstr = r[COL_PLACE].decode(u'utf-8') + u'.'
                pcount += 1
            # but suppress inf if within an omnium
            if self.inomnium:
                inf = None
            if self.evtype != u'handicap' and rh is not None and rh[u'ucicode']:
                inf = rh[u'ucicode']  # overwrite by force
            if plstr:  # don't emit a row for unplaced riders
                if not first:
                    sec.lines.append([plstr, rno, rname, inf, None, None])
                else:
                    sec.lines.append([plstr, rno, rname, inf, fs, None])
                    first = False
        if self.onestart:
            substr = substr.strip()
            shvec = []
            if substr:
                shvec.append(substr)
            shvec.append(self.standingstr())
            sec.subheading = u' - '.join(shvec)
        else:
            if substr:
                sec.subheading = substr

        ret.append(sec)

        if len(self.comments) > 0:
            sec = report.bullet_text()
            sec.subheading = u'Decisions of the commisaires panel'
            for c in self.comments:
                sec.lines.append([None, c])
            ret.append(sec)
        return ret

    def standingstr(self, width=None):
        """Return an event status string for reports and scb."""
        ret = u''
        if self.onestart:
            ret = u'Standings'
            rcount = 0
            pcount = 0
            winner = False
            for r in self.riders:
                if r[COL_DNF]:
                    pcount += 1
                elif r[COL_PLACE] != u'':
                    if r[COL_PLACE] == u'1':
                        winner = True
                    pcount += 1
                rcount += 1
            if winner:
                if rcount > 0 and pcount < rcount:
                    ret = u'Provisional Result'
                else:
                    ret = u'Result'
        return ret

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
        """Constructor.

        Parameters:

            meet -- handle to meet object
            event -- event object handle
            ui -- display user interface?

        """
        self.meet = meet
        self.event = event
        self.evno = event[u'evid']
        self.evtype = event[u'type']
        self.series = event[u'seri']
        self.configfile = meet.event_configfile(self.evno)
        self.results = []
        self.resulttype = u'RESULT'

        self.readonly = not ui
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        _log.debug(u'Init %sevent %s', rstr, self.evno)
        self.comments = []
        self.eliminated = []
        self.onestart = False
        self.runlap = None
        self.lastrunlap = None
        self.start = None
        self.lstart = None
        self.finish = None
        self.winopen = ui  # window 'open' on proper load- or consult edb
        self.timerwin = False
        self.timerstat = u'idle'
        self.distance = None
        self.units = u'laps'
        self.timetype = u'start/finish'
        self.startplace = 0  # offset to first place in this race (hack)
        self.autospec = u''  # automatic startlist
        self.inomnium = False
        self.seedsrc = None
        self.doscbplaces = True  # auto show result on scb
        self.reorderflag = 0
        self.startchan = 0
        self.finchan = 1

        self.riders = gtk.ListStore(
            gobject.TYPE_STRING,  # 0 bib
            gobject.TYPE_STRING,  # 1 first name
            gobject.TYPE_STRING,  # 2 last name
            gobject.TYPE_STRING,  # 3 club
            gobject.TYPE_STRING,  # 4 xtra info
            gobject.TYPE_BOOLEAN,  # 5 DNF/DNS
            gobject.TYPE_STRING)  # 6 placing

        uifile = os.path.join(metarace.UI_PATH, u'race.ui')
        _log.debug(u'Building event interface from %r', uifile)
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
        self.time_lbl.modify_font(uiutil.MONOFONT)

        # ctrl pane
        self.stat_but = uiutil.statbut(b.get_object(u'race_ctrl_stat_but'))
        self.ctrl_places = b.get_object(u'race_ctrl_places')
        self.ctrl_action_combo = b.get_object(u'race_ctrl_action_combo')
        self.ctrl_action = b.get_object(u'race_ctrl_action')
        self.action_model = b.get_object(u'race_action_model')

        # start timer and show window
        if ui:
            _log.debug(u'Connecting event ui handlers')
            # riders pane
            t = gtk.TreeView(self.riders)
            self.view = t
            t.set_reorderable(True)
            t.set_enable_search(False)
            t.set_rules_hint(True)

            # riders columns
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
            uiutil.mkviewcoltxt(t, u'Info', COL_INFO, self.editcol_cb)
            uiutil.mkviewcolbool(t, u'DNF', COL_DNF, self.dnf_cb)
            uiutil.mkviewcoltxt(t,
                                u'Place',
                                COL_PLACE,
                                self.editcol_cb,
                                halign=0.5,
                                calign=0.5)
            t.show()
            b.get_object(u'race_result_win').add(t)
            b.connect_signals(self)
