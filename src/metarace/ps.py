"""Point score, madison and omnium handler for trackmeet."""

import gtk
import glib
import gobject
import pango
import os
import logging
import csv
import ConfigParser

import metarace
from metarace import timy
from metarace import scbwin
from metarace import tod
from metarace import unt4
from metarace import uiutil
from metarace import riderdb
from metarace import strops
from metarace import report

# config version string
EVENT_ID = 'trackpsmad-1.3'

# Model columns
SPRINT_COL_ID = 0
SPRINT_COL_LABEL = 1
SPRINT_COL_200 = 2
SPRINT_COL_SPLIT = 3
SPRINT_COL_PLACES = 4
SPRINT_COL_POINTS = 5

RES_COL_BIB = 0
RES_COL_FIRST = 1
RES_COL_LAST = 2
RES_COL_CLUB = 3
RES_COL_INRACE = 4
RES_COL_POINTS = 5
RES_COL_LAPS = 6
RES_COL_TOTAL = 7
RES_COL_PLACE = 8
RES_COL_FINAL = 9
RES_COL_INFO = 10
RES_COL_STPTS = 11

# scb consts

SPRINT_PLACE_DELAY = 3  # 3 seconds per place
SPRINT_PLACE_DELAY_MAX = 11  # to a maximum of 11

# scb function key mappings
key_startlist = 'F3'
key_results = 'F4'

# timing function key mappings
key_armstart = 'F5'
key_showtimer = 'F6'
key_armfinish = 'F9'
key_lapdown = 'F11'

# extended function key mappings
key_abort = 'F5'
key_falsestart = 'F6'


class ps(object):
    """Data handling for point score and Madison races."""

    def loadconfig(self):
        """Load race config from disk."""
        self.riders.clear()
        self.sprints.clear()
        self.sprintlaps = u''
        self.sperintpoints = {}
        defscoretype = u'points'
        defmasterslaps = u'No'  # for teams omit bibs?
        if self.evtype == u'madison':
            defscoretype = u'madison'
            defmasterslaps = u'No'
        cr = ConfigParser.ConfigParser({
            'startlist': '',
            'id': EVENT_ID,
            'start': '',
            'lstart': '',
            'finish': '',
            'comments': '',
            'sprintlaps': '',
            'distance': '',
            'runlap': '',
            'distunits': 'laps',
            'masterslaps': defmasterslaps,
            'inomnium': 'No',
            'showinfo': 'No',
            'autospec': '',
            'scoring': defscoretype
        })
        cr.add_section('event')
        cr.add_section('sprintplaces')
        cr.add_section('sprintpoints')
        cr.add_section('sprintsource')
        cr.add_section('laplabels')
        cr.add_section('points')

        if os.path.isfile(self.configpath):
            self.log.debug('Attempting to read points config from path=' +
                           repr(self.configpath))
            cr.read(self.configpath)

        self.inomnium = strops.confopt_bool(cr.get('event', 'inomnium'))
        if self.inomnium:
            self.seedsrc = 1  # fetch start list seeding from omnium

        for r in cr.get('event', 'startlist').split():
            nr = [r, '', '', '', True, 0, 0, 0, '', -1, '', 0]
            if cr.has_option('points', r):
                ril = csv.reader([cr.get('points', r)]).next()
                if len(ril) >= 1:
                    nr[RES_COL_INRACE] = strops.confopt_bool(ril[0])
                if len(ril) >= 3:
                    try:
                        nr[RES_COL_LAPS] = int(ril[2])
                    except ValueError:
                        pass
                if len(ril) >= 4:
                    nr[RES_COL_INFO] = ril[3]
                if len(ril) >= 5:
                    spts = ril[4]
                    if spts.isdigit():
                        nr[RES_COL_STPTS] = int(spts)

            dbr = self.meet.rdb.getrider(r, self.series)
            if dbr is not None:
                for i in range(1, 4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)
            self.riders.append(nr)
        if cr.get('event', 'scoring').lower() == 'madison':
            self.scoring = 'madison'
        else:
            self.scoring = 'points'
        self.type_lbl.set_text(self.scoring.capitalize())

        # race infos
        self.comments = []
        nc = cr.get('event', 'comments')
        if nc:
            self.comments.append(nc)

        self.autospec = cr.get('event', 'autospec')
        self.distance = strops.confopt_dist(cr.get('event', 'distance'))
        self.units = strops.confopt_distunits(cr.get('event', 'distunits'))
        self.runlap = strops.confopt_posint(cr.get('event', 'runlap'))
        self.masterslaps = strops.confopt_bool(cr.get('event', 'masterslaps'))
        # override laps from event listings
        if not self.onestart and self.event[u'laps']:
            self.units = 'laps'
            self.distance = strops.confopt_posint(self.event[u'laps'],
                                                  self.distance)

        self.reset_lappoints()
        slt = unicode(cr.get('event', 'sprintlaps'))
        self.sprintlaps = strops.reformat_biblist(slt)

        # load any special purpose sprint points
        for (sid, spstr) in cr.items('sprintpoints'):
            self.sprintpoints[sid] = spstr  # validation in sprint model

        # load lap labels
        for (sid, spstr) in cr.items('laplabels'):
            self.laplabels[sid] = spstr  # just plain text

        # load any autospec'd sprint results
        for (sid, spstr) in cr.items('sprintsource'):
            self.sprintsource[sid] = spstr  # just plain text

        self.sprint_model_init()

        oft = 0
        for s in self.sprints:
            places = ''
            if cr.has_option('sprintplaces', s[SPRINT_COL_ID]):
                places = strops.reformat_placelist(
                    cr.get('sprintplaces', s[SPRINT_COL_ID]))
                if len(places) > 0:
                    oft += 1
            s[SPRINT_COL_PLACES] = places
            if cr.has_option('sprintplaces', s[SPRINT_COL_ID] + '_200'):
                s[SPRINT_COL_200] = tod.mktod(
                    cr.get('sprintplaces', s[SPRINT_COL_ID] + '_200'))
            if cr.has_option('sprintplaces', s[SPRINT_COL_ID] + '_split'):
                s[SPRINT_COL_SPLIT] = tod.mktod(
                    cr.get('sprintplaces', s[SPRINT_COL_ID] + '_split'))
        if oft > 0:
            if oft >= len(self.sprints):
                oft = len(self.sprints) - 1
            self.ctrl_place_combo.set_active(oft)
            self.onestart = True

        ## for omnium - look up the places from event links if present
        if self.inomnium:
            for s in self.sprints:
                sid = s[SPRINT_COL_ID]
                if sid in self.sprintsource:
                    ###
                    # what is the SID
                    splac = self.meet.autoplace_riders(self,
                                                       self.sprintsource[sid])
                    self.log.debug(u'Loaded ' + sid + u' places from event ' +
                                   self.sprintsource[sid] + u' : ' + splac)
                    if splac:
                        s[SPRINT_COL_PLACES] = splac
        self.recalculate()

        self.info_expand.set_expanded(
            strops.confopt_bool(cr.get('event', 'showinfo')))
        self.set_start(cr.get('event', 'start'), cr.get('event', 'lstart'))
        self.set_finish(cr.get('event', 'finish'))
        self.set_elapsed()

        # after load, add auto if required
        if not self.onestart and self.autospec:
            self.meet.autostart_riders(self, self.autospec, self.seedsrc)

        # After load complete - check config and report. This ensures
        # an error message is left on top of status stack. This is not
        # always a hard fail and the user should be left to determine
        # an appropriate outcome.
        eid = cr.get('event', 'id')
        if eid and eid != EVENT_ID:
            self.log.error('Event configuration mismatch: ' + repr(eid) +
                           ' != ' + repr(EVENT_ID))
            #self.readonly = True

    def get_startlist(self):
        """Return a list of bibs in the rider model."""
        ret = []
        for r in self.riders:
            ret.append(r[RES_COL_BIB])
        return ' '.join(ret)

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            self.log.error('Attempt to save readonly ob.')
            return
        cw = ConfigParser.ConfigParser()
        cw.add_section('event')
        if self.start is not None:
            cw.set('event', 'start', self.start.rawtime())
        if self.lstart is not None:
            cw.set('event', 'lstart', self.lstart.rawtime())
        if self.finish is not None:
            cw.set('event', 'finish', self.finish.rawtime())
        cw.set('event', 'startlist', self.get_startlist())
        if self.info_expand.get_expanded():
            cw.set('event', 'showinfo', 'Yes')
        else:
            cw.set('event', 'showinfo', 'No')
        cw.set('event', 'distance', self.distance)
        cw.set('event', 'distunits', self.units)
        cw.set('event', 'scoring', self.scoring)
        if self.runlap is not None:
            cw.set('event', 'runlap', self.runlap)
        if self.masterslaps:
            cw.set('event', 'masterslaps', 'Yes')
        else:
            cw.set('event', 'masterslaps', 'No')
        cw.set('event', 'autospec', self.autospec)
        cw.set('event', 'inomnium', self.inomnium)
        cw.set('event', 'sprintlaps', self.sprintlaps)

        thecom = u''
        if len(self.comments) > 0:
            thecom = self.comments[0]
        cw.set('event', 'comments', thecom)

        cw.add_section('sprintplaces')
        cw.add_section('sprintpoints')
        cw.add_section('sprintsource')
        cw.add_section('laplabels')
        for s in self.sprints:
            sid = s[SPRINT_COL_ID]
            cw.set('sprintplaces', s[SPRINT_COL_ID], s[SPRINT_COL_PLACES])
            if s[SPRINT_COL_200] is not None:
                cw.set('sprintplaces', s[SPRINT_COL_ID] + '_200',
                       s[SPRINT_COL_200].rawtime())
            if s[SPRINT_COL_SPLIT] is not None:
                cw.set('sprintplaces', s[SPRINT_COL_ID] + '_split',
                       s[SPRINT_COL_SPLIT].rawtime())
            if s[SPRINT_COL_POINTS] is not None:
                cw.set('sprintpoints', s[SPRINT_COL_ID],
                       ' '.join(map(str, s[SPRINT_COL_POINTS])))
            if s[SPRINT_COL_ID] in self.laplabels:
                cw.set('laplabels', sid, self.laplabels[sid])
            if s[SPRINT_COL_ID] in self.sprintsource:
                cw.set(u'sprintsource', sid, self.sprintsource[sid])

        cw.add_section('points')
        for r in self.riders:
            bf = 'No'
            if r[RES_COL_INRACE]:
                bf = 'Yes'
            slice = [
                bf,
                str(r[RES_COL_POINTS]),
                str(r[RES_COL_LAPS]),
                str(r[RES_COL_INFO]),
                str(r[RES_COL_STPTS])
            ]
            cw.set('points', r[RES_COL_BIB],
                   ','.join(map(lambda i: str(i).replace(',', '\\,'), slice)))
        cw.set('event', 'id', EVENT_ID)
        self.log.debug('Saving points config to: ' + self.configpath)
        with open(self.configpath, 'wb') as f:
            cw.write(f)

    def result_gen(self):
        """Generator function to export a final result."""
        fl = None
        ll = None
        for r in self.riders:
            bib = r[RES_COL_BIB]
            rank = None
            info = None
            if r[RES_COL_TOTAL] == 0:
                info = '-'
            else:
                info = str(r[RES_COL_TOTAL])
            if self.onestart:
                if r[RES_COL_INRACE]:
                    if r[RES_COL_PLACE] is not None and r[RES_COL_PLACE] != '':
                        rank = int(r[RES_COL_PLACE])
                else:
                    if r[RES_COL_INFO] in ['dns', 'dsq']:
                        rank = r[RES_COL_INFO]
                    else:
                        rank = 'dnf'  # ps only handle did not finish

            if self.scoring == 'madison':
                laps = r[RES_COL_LAPS]
                if fl is None:
                    fl = laps  # anddetermine laps down
                if ll is not None:
                    down = fl - laps
                    if ll != laps:
                        yield [
                            '', ' ',
                            str(down) + ' Lap' + strops.plural(down) +
                            ' Behind', ''
                        ]
                ll = laps

            time = None
            yield [bib, rank, time, info]

    def result_report(self, recurse=False):
        """Return a list of report sections containing the race result."""
        self.recalculate()
        ret = []
        sec = report.section()
        sec.heading = 'Event ' + self.evno + ': ' + ' '.join(
            [self.event[u'pref'], self.event[u'info']]).strip()
        lapstring = strops.lapstring(self.event[u'laps'])
        substr = u' '.join(
            [lapstring, self.event[u'dist'], self.event[u'prog']]).strip()
        sec.units = 'Pts'
        fs = ''
        if self.finish is not None:
            fs = self.time_lbl.get_text().strip()
        fl = None
        ll = None
        for r in self.riders:
            rno = r[RES_COL_BIB].decode('utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            rname = u''
            if rh is not None:
                rname = rh[u'namestr']
            plstr = ''
            rcat = None
            if self.event[u'cate']:
                if rname is not None and rh[u'cat']:
                    rcat = rh[u'cat']
            if self.inomnium:
                rcat = None
            if rh is not None and rh[u'ucicode']:
                rcat = rh[u'ucicode']  # overwrite by force
            if self.onestart and r[RES_COL_PLACE] is not None:
                plstr = r[RES_COL_PLACE]
                if r[RES_COL_PLACE].isdigit():
                    plstr += '.'
                elif r[RES_COL_INFO] in [u'dns', u'dsq']:
                    plstr = r[RES_COL_INFO]
                ptstr = ''
                if r[RES_COL_TOTAL] != 0:
                    ptstr = str(r[RES_COL_TOTAL])
                finplace = ''
                if r[RES_COL_FINAL] >= 0:
                    finplace = str(r[RES_COL_FINAL] + 1)

                if self.scoring == 'madison':
                    laps = r[RES_COL_LAPS]
                    if fl is None:
                        fl = laps  # anddetermine laps down
                    if ll is not None:
                        down = fl - laps
                        if ll != laps:
                            sec.lines.append([
                                None, None,
                                unicode(down) + u' Lap' + strops.plural(down) +
                                u' Behind', None, None, None
                            ])
                    ll = laps
                if plstr or finplace or ptstr:  # only output those with points
                    # dnf  or
                    # placed in final sprint
                    sec.lines.append([plstr, rno, rname, rcat, fs, ptstr])
                    ## TEAM HACKS
                    if u't' in self.series and rh is not None:
                        for trno in rh[u'note'].split():
                            trh = self.meet.newgetrider(trno)  #!! SERIES?
                            if trh is not None:
                                trname = trh[u'namestr']
                                trinf = trh[u'ucicode']
                                sec.lines.append([
                                    None, u'', trname, trinf, None, None, None
                                ])
                fs = ''

        if self.onestart:
            sec.subheading = substr + u' - ' + self.standingstr()
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

        # output intermediate sprints?

        return ret

    def getrider(self, bib):
        """Return temporary reference to model row."""
        ret = None
        for r in self.riders:
            if r[RES_COL_BIB] == bib:
                ret = r
                break
        return ret

    def getiter(self, bib):
        """Return temporary iterator to model row."""
        i = self.riders.get_iter_first()
        while i is not None:
            if self.riders.get_value(i, RES_COL_BIB) == bib:
                break
            i = self.riders.iter_next(i)
        return i

    def addrider(self, bib='', info=None):
        """Add specified rider to race model."""
        nr = [bib, '', '', '', True, 0, 0, 0, '', -1, '', 0]
        er = self.getrider(bib)
        if bib == '' or er is None:
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                for i in range(1, 4):
                    nr[i] = self.meet.rdb.getvalue(dbr, i)
                if self.inomnium:
                    if info:
                        nr[RES_COL_INFO] = info
            return self.riders.append(nr)
        else:
            if er is not None:
                self.log.debug('onestart is: ' + repr(self.onestart))
                if self.inomnium and not self.onestart:
                    er[RES_COL_INFO] = info
            return None

    def delrider(self, bib):
        """Remove the specified rider from the model."""
        i = self.getiter(bib)
        if i is not None:
            self.riders.remove(i)

    def resettimer(self):
        """Reset race timer."""
        self.set_finish()
        self.set_start()
        self.timerstat = 'idle'
        self.meet.timer.dearm(timy.CHAN_START)
        self.meet.timer.dearm(timy.CHAN_FINISH)
        uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Idle')
        self.stat_but.set_sensitive(True)
        self.set_elapsed()

    def armstart(self):
        """Toggle timer arm start state."""
        if self.timerstat == 'idle':
            self.timerstat = 'armstart'
            uiutil.buttonchg(self.stat_but, uiutil.bg_armstart, 'Arm Start')
            self.meet.timer.arm(timy.CHAN_START)
        elif self.timerstat == 'armstart':
            self.timerstat = 'idle'
            uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Idle')
            self.meet.timer.dearm(timy.CHAN_START)
            self.curtimerstr = ''
        elif self.timerstat == 'running':
            self.timerstat = 'armsprintstart'
            uiutil.buttonchg(self.stat_but, uiutil.bg_armstart, 'Arm Sprint')
            self.meet.timer.arm(timy.CHAN_START)
        elif self.timerstat == 'armsprintstart':
            self.timerstat = 'running'
            uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Running')
            self.meet.timer.dearm(timy.CHAN_START)

    def armfinish(self):
        """Toggle timer arm finish state."""
        if self.timerstat in ['running', 'armsprint', 'armsprintstart']:
            self.timerstat = 'armfinish'
            uiutil.buttonchg(self.stat_but, uiutil.bg_armfin, 'Arm Finish')
            self.meet.timer.arm(timy.CHAN_FINISH)
        elif self.timerstat == 'armfinish':
            self.timerstat = 'running'
            uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Running')
            self.meet.timer.dearm(timy.CHAN_FINISH)

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
            if x[1].isdigit() and y[1].isdigit():
                return cmp(int(x[1]), int(y[1]))
            else:
                return cmp(x[1], y[1])

    def reorder_riderno(self):
        """Sort the rider list by rider number."""
        if len(self.riders) > 1:
            auxmap = []
            cnt = 0
            intmark = 0
            for r in self.riders:
                if self.inomnium:
                    # now os is omnium final, so sort on current rank
                    if r[RES_COL_PLACE].isdigit() or r[RES_COL_PLACE] == u'':
                        # but only add riders currently ranked, or unplaced
                        intmark = strops.mark2int(r[RES_COL_PLACE].decode(
                            'utf-8', 'replace'))
                    else:
                        intmark = 999
                    # old omnium had ps in series
                    #intmark = strops.mark2int(r[RES_COL_INFO].decode('utf-8','replace'))
                auxmap.append(
                    [cnt, r[RES_COL_BIB].decode('utf-8', 'replace'), intmark])
                cnt += 1
            auxmap.sort(self.sort_handicap)
            self.riders.reorder([a[0] for a in auxmap])

    def startlist_report(self, program=False):
        """Return a startlist report."""
        ret = []
        sec = report.twocol_startlist()
        if self.evtype == 'madison':
            # use the team singlecol method
            sec = report.section()
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
        self.reorder_riderno()
        sec.lines = []
        col2 = []
        cnt = 0
        if self.inomnium and len(self.riders) > 0:
            sec.lines.append([u' ', u' ', u'The Fence', None, None, None])
            col2.append([u' ', u' ', u'Sprinters Lane', None, None, None])
        for r in self.riders:
            rno = r[RES_COL_BIB].decode('utf-8')
            rh = self.meet.newgetrider(rno, self.series)
            inf = r[RES_COL_INFO].decode('utf-8', 'replace')
            if self.inomnium:
                # inf holds seed, ignore for now
                inf = None
            # layout needs adhjustment
            #if rh[u'ucicode']:
            #inf = rh[u'ucicode']   # overwrite by force
            rname = u''
            if rh is not None:
                rname = rh[u'namestr']

            if self.inomnium:
                if r[RES_COL_PLACE].isdigit() or r[RES_COL_PLACE] == u'':
                    cnt += 1
                    if cnt % 2 == 1:
                        sec.lines.append([None, rno, rname, inf, None, None])
                    else:
                        col2.append([None, rno, rname, inf, None, None])
            else:
                cnt += 1
                sec.lines.append([None, rno, rname, inf, None, None])
                if self.evtype == 'madison':
                    # add the black/red entry
                    if rh is not None:
                        tvec = rh[u'note'].split()
                        if len(tvec) == 2:
                            trname = u''
                            trinf = u''
                            trh = self.meet.newgetrider(tvec[0])
                            if trh is not None:
                                trname = trh[u'namestr']
                                trinf = trh[u'ucicode']
                            sec.lines.append([
                                None, u'Red', trname, trinf, None, None, None
                            ])
                            trname = u''
                            trinf = u''
                            trh = self.meet.newgetrider(tvec[1])
                            if trh is not None:
                                trname = trh[u'namestr']
                                trinf = trh[u'ucicode']
                            sec.lines.append([
                                None, u'Black', trname, trinf, None, None, None
                            ])
                            #sec.lines.append([None, None, None, None,
                            #None, None, None])

        for i in col2:
            sec.lines.append(i)
        ret.append(sec)

        # placeholders - why was this suppressed?
        if self.event[u'plac']:
            while cnt < self.event[u'plac']:
                sec.lines.append([None, None, None, None, None, None])
                cnt += 1

        if cnt > 0 and not program:
            sec = report.bullet_text()
            if self.evtype == 'madison':
                sec.lines.append([None, 'Total teams: ' + str(cnt)])
            else:
                sec.lines.append([None, 'Total riders: ' + str(cnt)])
            ret.append(sec)
        return ret

    def do_startlist(self):
        """Show startlist on scoreboard."""
        self.reorder_riderno()
        self.meet.scbwin = None
        self.timerwin = False
        startlist = []
        self.meet.announce.gfx_overlay(1)
        self.meet.announce.gfx_set_title(u'Startlist: ' + self.event[u'pref'] +
                                         u' ' + self.event[u'info'])

        name_w = self.meet.scb.linelen - 8
        for r in self.riders:
            self.meet.announce.gfx_add_row([
                r[0],
                strops.resname(r[RES_COL_FIRST].decode('utf-8'),
                               r[RES_COL_LAST].decode('utf-8'),
                               r[RES_COL_CLUB].decode('utf-8')), ''
            ])

            if r[RES_COL_INRACE]:
                club = r[RES_COL_CLUB].decode('utf-8', 'replace')
                if len(club) > 3:
                    # look it up?
                    if self.series in self.meet.ridermap:
                        rh = self.meet.ridermap[self.series][r[RES_COL_BIB]]
                        if rh is not None:
                            club = rh['note']
                startlist.append([
                    r[RES_COL_BIB],
                    strops.fitname(r[RES_COL_FIRST].decode('utf-8'),
                                   r[RES_COL_LAST].decode('utf-8'), name_w),
                    club
                ])
        FMT = [(3, u'r'), u' ', (name_w, u'l'), u' ', (3, u'r')]
        self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                           head=self.meet.racenamecat(
                                               self.event),
                                           subhead=u'STARTLIST',
                                           coldesc=FMT,
                                           rows=startlist)
        self.meet.scbwin.reset()

    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort:  # override ctrl+f5
                    self.resettimer()
                    return True
            if key[0] == 'F':
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
                    self.recalculate()
                    self.do_places()
                    glib.idle_add(self.delayed_announce)
                    return True
                elif key == key_lapdown:
                    if self.runlap is not None and self.runlap > 0:
                        self.runlap -= 1
                    return True
        return False

    def delayed_announce(self):
        """Initialise the announcer's screen after a delay."""
        if self.winopen:
            self.meet.announce.clrall()
            self.meet.ann_title(' '.join([
                self.meet.event_string(self.evno), ':', self.event[u'pref'],
                self.event[u'info']
            ]))

            self.meet.announce.linefill(1, '_')
            self.meet.announce.linefill(8, '_')

            # fill in a sprint if not empty
            sid = None
            i = self.ctrl_place_combo.get_active_iter()
            if i is not None:
                pl = self.sprints.get_value(i, SPRINT_COL_PLACES)
                if pl is not None and pl != '':
                    sinfo = self.sprints.get_value(i, SPRINT_COL_LABEL)
                    self.meet.announce.setline(3, sinfo + ':')
                    sid = int(self.sprints.get_string_from_iter(i))
                    cnt = 0
                    unitshown = False
                    for r in self.sprintresults[sid]:
                        pstr = ''
                        if r[3] != '':
                            pstr = r[3]
                            if not unitshown:
                                pstr += 'pts'
                                unitshown = True
                        self.meet.announce.postxt(
                            4 + cnt, 0, ' '.join([
                                strops.truncpad(r[0], 3),
                                strops.truncpad(r[1], 3, 'r'),
                                strops.truncpad(r[2], 20), pstr
                            ]))
                        cnt += 1
                        if cnt > 3:  # is this required?
                            break
                else:
                    sid = int(self.sprints.get_string_from_iter(i)) - 1

            tp = ''
            if self.start is not None and self.finish is not None:
                et = self.finish - self.start
                tp = 'Time: ' + et.timestr(2) + '    '
                dist = self.meet.get_distance(self.distance, self.units)
                if dist:
                    tp += 'Avg: ' + et.speedstr(dist)
            self.meet.announce.postxt(4, 40, tp)

            # do result standing
            mscount = len(self.sprints)
            if sid is not None:
                mscount = sid
            sidstart = mscount - 8
            if sidstart < 0:
                sidstart = 0
            elif sidstart > len(self.sprints) - 10:
                sidstart = len(self.sprints) - 10
            if self.scoring == 'madison':
                leaderboard = []
                rtype = 'Team '
                if self.evtype != 'madison':
                    rtype = 'Rider'
                hdr = '     # ' + rtype + '                 Lap Pt '
                nopts = ''
                scnt = 0
                for s in self.sprints:
                    if scnt >= sidstart and scnt < sidstart + 10:
                        hdr += strops.truncpad(s[SPRINT_COL_ID], 4, 'r')
                        nopts += '    '
                    scnt += 1
                hdr += ' Fin'
                self.meet.announce.setline(10, hdr)
                curline = 11
                ldrlap = None
                curlap = None
                for r in self.riders:
                    if ldrlap is None:
                        ldrlap = r[RES_COL_LAPS]
                        curlap = ldrlap
                    lapdwn = r[RES_COL_LAPS] - ldrlap
                    lapstr = '  '
                    if lapdwn != 0:
                        lapstr = strops.truncpad(str(lapdwn), 2, 'r')

                    psrc = '-'
                    if r[RES_COL_TOTAL] != 0:
                        psrc = str(r[RES_COL_TOTAL])
                    ptstr = strops.truncpad(psrc, 2, 'r')

                    placestr = '   '
                    if self.onestart and r[RES_COL_PLACE] != '':
                        placestr = strops.truncpad(r[RES_COL_PLACE] + '.', 3)
                    elif not r[RES_COL_INRACE]:
                        placestr = 'dnf'

                    spstr = ''
                    if r[RES_COL_BIB] in self.auxmap:
                        scnt = 0
                        for s in self.auxmap[r[RES_COL_BIB]]:
                            if scnt >= sidstart and scnt < sidstart + 10:
                                spstr += str(s).rjust(4)
                            scnt += 1
                    else:
                        spstr = nopts

                    finstr = 'u/p'
                    if r[RES_COL_FINAL] >= 0:
                        finstr = strops.truncpad(str(r[RES_COL_FINAL] + 1), 3,
                                                 'r')

                    bibstr = strops.truncpad(r[RES_COL_BIB], 2, 'r')

                    clubstr = ''
                    if r[RES_COL_CLUB] != '' and len(r[RES_COL_CLUB]) <= 3:
                        clubstr = ' (' + r[RES_COL_CLUB] + ')'
                    namestr = strops.truncpad(strops.fitname(r[RES_COL_FIRST],
                                                             r[RES_COL_LAST],
                                                             22 - len(clubstr),
                                                             trunc=True) +
                                              clubstr,
                                              22,
                                              elipsis=False)

                    self.meet.announce.postxt(
                        curline, 0, ' '.join([
                            placestr, bibstr, namestr, lapstr, ptstr, spstr,
                            finstr
                        ]))
                    curline += 1
                    if r[RES_COL_INRACE]:
                        if curlap > r[RES_COL_LAPS]:
                            while curlap != r[RES_COL_LAPS]:
                                curlap -= 1
                                if curlap < -15:
                                    break
                                leaderboard.append(u'-')
                        leaderboard.append(r[RES_COL_BIB].rjust(2) +
                                           psrc.rjust(3))
                self.meet.announce.publish_cmd(
                    u'leaderboard',
                    unichr(unt4.US).join(leaderboard))
            else:
                # use scratch race style layout for up to 26 riders
                count = 0
                curline = 11
                posoft = 0
                leaderboard = []
                for r in self.riders:
                    count += 1
                    if count == 14:
                        curline = 11
                        posoft = 41

                    psrc = '-'
                    if r[RES_COL_TOTAL] != 0:
                        psrc = str(r[RES_COL_TOTAL])

                    ptstr = strops.truncpad(psrc, 3, 'r')
                    clubstr = ''
                    if r[RES_COL_CLUB] != '' and len(r[RES_COL_CLUB]) <= 3:
                        clubstr = ' (' + r[RES_COL_CLUB] + ')'
                    namestr = strops.truncpad(strops.fitname(r[RES_COL_FIRST],
                                                             r[RES_COL_LAST],
                                                             27 - len(clubstr),
                                                             trunc=True) +
                                              clubstr,
                                              27,
                                              elipsis=False)
                    placestr = '   '
                    if self.onestart and r[RES_COL_PLACE] != '':
                        placestr = strops.truncpad(r[RES_COL_PLACE] + '.', 3)
                    elif not r[RES_COL_INRACE]:
                        placestr = 'dnf'
                    bibstr = strops.truncpad(r[RES_COL_BIB], 3, 'r')
                    self.meet.announce.postxt(
                        curline, posoft,
                        ' '.join([placestr, bibstr, namestr, ptstr]))
                    curline += 1

                    if self.inomnium and r[RES_COL_INRACE]:
                        leaderboard.append(r[RES_COL_BIB].rjust(2) +
                                           psrc.rjust(3))
                if posoft > 0:
                    self.meet.announce.postxt(
                        10, 0,
                        '      # Rider                       Pts        # Rider                       Pts'
                    )
                else:
                    self.meet.announce.postxt(
                        10, 0, '      # Rider                       Pts')

                self.meet.announce.publish_cmd(
                    u'leaderboard',
                    unichr(unt4.US).join(leaderboard))
        return False

    def do_places(self):
        """Show race result on scoreboard."""

        thesec = self.result_report()
        placestype = u'Standings: '
        if len(thesec) > 0:
            if self.finished:
                placestype = u'Result: '
            self.meet.announce.gfx_overlay(1)
            self.meet.announce.gfx_set_title(placestype + self.event[u'pref'] +
                                             u' ' + self.event[u'info'])
            for l in thesec[0].lines:
                pts = '-'
                if l[5]:
                    pts = l[5]
                self.meet.announce.gfx_add_row([l[0], l[2], pts])

        resvec = []
        fmt = ''
        hdr = ''
        if self.scoring == 'madison':
            name_w = self.meet.scb.linelen - 8
            fmt = [(2, u'r'), u' ', (name_w, u'l'), (2, u'r'), (3, u'r')]
            # does this require special consideration?
            leaderboard = []
            hdr = u' # team' + (
                (self.meet.scb.linelen - 13) * u' ') + u'lap pt'
            llap = None  # leader's lap
            for r in self.riders:
                if r[RES_COL_INRACE]:
                    bstr = r[RES_COL_BIB].decode('utf-8', 'ignore')
                    if llap is None:
                        llap = r[RES_COL_LAPS]
                    lstr = str(r[RES_COL_LAPS] - llap)
                    if lstr == '0': lstr = ''
                    pstr = str(r[RES_COL_TOTAL])
                    if pstr == '0': pstr = '-'
                    resvec.append([
                        bstr,
                        strops.fitname(
                            '', r[RES_COL_LAST].decode('utf-8',
                                                       'ignore').upper(),
                            name_w), lstr, pstr
                    ])
        else:
            name_w = self.meet.scb.linelen - 10
            fmt = [(3, u'l'), (3, u'r'), u' ', (name_w, u'l'), (3, u'r')]
            #self.meet.scb.linelen - 3) + ' pt'
            if self.inomnium:
                fmt = [(3, u'l'), (3, u'r'), u' ', (name_w - 1, u'l'),
                       (4, u'r')]
                #self.meet.scb.linelen - 3) + ' pt'
            #ldr = None
            for r in self.riders:
                if r[RES_COL_INRACE]:
                    plstr = r[RES_COL_PLACE] + u'.'
                    bstr = r[RES_COL_BIB]
                    #if ldr is None and r[RES_COL_TOTAL] > 0:
                    #ldr = r[RES_COL_TOTAL]	# current leader
                    pstr = str(r[RES_COL_TOTAL])
                    #if self.inomnium and ldr is not None:
                    #pstr = '-' + str(ldr - r[RES_COL_TOTAL])
                    if pstr == '0': pstr = '-'
                    resvec.append([
                        plstr, bstr,
                        strops.fitname(r[RES_COL_FIRST], r[RES_COL_LAST],
                                       name_w), pstr
                    ])
            # cols are: rank, bib, name, pts
        hdr = self.meet.racenamecat(self.event)
        self.meet.scbwin = None
        self.timerwin = False
        evtstatus = self.standingstr(width=self.meet.scb.linelen).upper()
        #evtstatus=u'STANDINGS'
        #if self.finished:
        #evtstatus=u'RESULT'
        self.meet.scbwin = scbwin.scbtable(scb=self.meet.scb,
                                           head=hdr,
                                           subhead=evtstatus,
                                           coldesc=fmt,
                                           rows=resvec)
        self.meet.scbwin.reset()
        return False

    def dnfriders(self, biblist=''):
        """Remove listed bibs from the race."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[RES_COL_INRACE] = False
                recalc = True
                self.log.info('Rider ' + str(bib) + ' withdrawn')
            else:
                self.log.warn('Did not withdraw no. = ' + str(bib))
        if recalc:
            self.recalculate()
            self.meet.delayed_export()
        return False

    def announce_packet(self, line, pos, txt):
        self.meet.announce.postxt(line, pos, txt)
        return False

    def gainlap(self, biblist=''):
        """Credit each rider listed in biblist with a lap on the field."""
        recalc = False
        rlines = []
        srlines = []
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[RES_COL_LAPS] += 1
                recalc = True
                self.log.info('Rider ' + str(bib) + ' gain lap')
                rlines.append(' '.join([
                    bib.rjust(3),
                    strops.fitname(r[RES_COL_FIRST],
                                   r[RES_COL_LAST],
                                   26,
                                   trunc=True)
                ]))
                srlines.append([
                    bib,
                    strops.fitname(r[RES_COL_FIRST], r[RES_COL_LAST], 20)
                ])
            else:
                self.log.warn('Did not gain lap for no. = ' + str(bib))
        if recalc:
            self.oktochangecombo = False
            self.recalculate()
            glib.timeout_add_seconds(2, self.announce_packet, 3, 50,
                                     'Gaining a lap:')
            cnt = 1
            for line in rlines:
                glib.timeout_add_seconds(2, self.announce_packet, 3 + cnt, 50,
                                         line)
                cnt += 1
                if cnt > 4:
                    break
            # and do it on the scoreboard too ?!
            self.meet.scbwin = scbwin.scbintsprint(
                self.meet.scb, self.meet.racenamecat(self.event),
                u'Gaining a Lap'.upper(), [(3, u'r'), u' ',
                                           (self.meet.scb.linelen - 4, u'l')],
                srlines)
            self.meet.scbwin.reset()
            self.next_sprint_counter += 1
            delay = SPRINT_PLACE_DELAY * len(rlines) or 1
            if delay > SPRINT_PLACE_DELAY_MAX:
                delay = SPRINT_PLACE_DELAY_MAX
            glib.timeout_add_seconds(delay, self.delayed_result)
            self.meet.delayed_export()
        return False

    def loselap(self, biblist=''):
        """Deduct a lap from each rider listed in biblist."""
        recalc = False
        rlines = []
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[RES_COL_LAPS] -= 1
                recalc = True
                self.log.info('Rider ' + str(bib) + ' lose lap')
                rlines.append(u' '.join([
                    bib.rjust(3),
                    strops.fitname(r[RES_COL_FIRST],
                                   r[RES_COL_LAST],
                                   26,
                                   trunc=True)
                ]))
            else:
                self.log.warn('Did not lose lap for no. = ' + str(bib))
        if recalc:
            self.oktochangecombo = False
            self.recalculate()
            glib.timeout_add_seconds(2, self.announce_packet, 3, 50,
                                     'Losing a lap:')
            cnt = 1
            for line in rlines:
                glib.timeout_add_seconds(2, self.announce_packet, 3 + cnt, 50,
                                         line)
                cnt += 1
                if cnt > 4:
                    break
            self.meet.delayed_export()
        return False

    def showtimer(self):
        """Show race timer on scoreboard."""
        tp = 'Time:'
        self.meet.scbwin = scbwin.scbtimer(scb=self.meet.scb,
                                           line1=self.meet.racenamecat(
                                               self.event),
                                           line2=u'',
                                           timepfx=tp)
        wastimer = self.timerwin
        self.timerwin = True
        if self.timerstat == 'finished':
            if not wastimer:
                self.meet.scbwin.reset()
            elap = self.finish - self.start
            self.meet.scbwin.settime(elap.timestr(2))
            dist = self.meet.get_distance(self.distance, self.units)
            if dist:
                self.meet.scbwin.setavg(elap.speedstr(dist))
                self.meet.scbwin.update()
        else:
            self.meet.scbwin.reset()

    def shutdown(self, win=None, msg='Exiting'):
        """Terminate race object."""
        self.log.debug('Race shutdown: ' + msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def starttrig(self, e):
        """React to start trigger."""
        if self.timerstat == 'armstart':
            if self.distance and self.units == u'laps':
                self.runlap = self.distance - 1
                self.log.debug(u'SET RUNLAP: ' + repr(self.runlap))
            self.set_start(e, tod.now())
        elif self.timerstat == 'armsprintstart':
            uiutil.buttonchg(self.stat_but, uiutil.bg_armfin, 'Arm Sprint')
            self.meet.timer.arm(timy.CHAN_FINISH)
            self.timerstat = 'armsprint'
            self.sprintstart = e
            self.sprintlstart = tod.now()

    def fintrig(self, e):
        """React to finish trigger."""
        if self.timerstat == 'armfinish':
            self.set_finish(e)
            self.set_elapsed()
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                self.showtimer()
            self.log_elapsed()
            glib.idle_add(self.delayed_announce)
        elif self.timerstat == 'armsprint':
            uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Running')
            self.timerstat = 'running'
            if self.sprintstart is not None:
                elap = (e - self.sprintstart).timestr(2)
                self.log.info('200m: ' + elap)
                if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                    self.meet.scbwin.avgpfx = '200m:'
                    self.meet.scbwin.setavg(elap)
            self.sprintstart = None

    def rftimercb(self, e):
        """Handle rftimer event."""
        if e.refid == '' or e.chan == u'STS':  # got a trigger
            #return self.starttrig(e)
            return False

        # else assume this is a passing
        r = self.meet.rdb.getrefid(e.refid)
        if r is None:
            self.log.info('Unknown tag: ' + e.refid + '@' + e.rawtime(2))
            return False

        bib = self.meet.rdb.getvalue(r, riderdb.COL_BIB)
        ser = self.meet.rdb.getvalue(r, riderdb.COL_SERIES)
        if ser != self.series:
            self.log.error(u'Ignored non-series rider: ' + repr(bib))
            return False
        r = self.getrider(bib)
        if r is not None:
            self.log.info(u'SAW ' + repr(bib))
        return False

    def timercb(self, e):
        """Handle a timer event."""
        chan = timy.chan2id(e.chan)
        if chan == timy.CHAN_START:
            self.log.debug('Got a start impulse.')
            self.starttrig(e)
        elif chan == timy.CHAN_FINISH:
            self.log.debug('Got a finish impulse.')
            self.fintrig(e)
        return False

    def timeout(self):
        """Update scoreboard and respond to timing events."""
        if not self.winopen:
            return False
        if self.finish is None and self.start is not None:
            self.set_elapsed()
            if self.timerwin and type(self.meet.scbwin) is scbwin.scbtimer:
                self.meet.scbwin.settime(self.time_lbl.get_text())
        return True

    def do_properties(self):
        """Run race properties dialog."""
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'ps_properties.ui'))
        dlg = b.get_object('properties')
        dlg.set_transient_for(self.meet.window)
        rle = b.get_object('race_laps_entry')
        rle.set_text(self.sprintlaps)
        if self.onestart:
            rle.set_sensitive(False)
        rsb = b.get_object('race_showbib_toggle')
        rsb.set_active(self.masterslaps)
        rt = b.get_object('race_score_type')
        if self.scoring == 'madison':
            rt.set_active(0)
        else:
            rt.set_active(1)
        di = b.get_object('race_dist_entry')
        if self.distance is not None:
            di.set_text(str(self.distance))
        else:
            di.set_text('')
        du = b.get_object('race_dist_type')
        if self.units == 'metres':
            du.set_active(0)
        else:
            du.set_active(1)
        se = b.get_object('race_series_entry')
        se.set_text(self.series)
        as_e = b.get_object('auto_starters_entry')
        as_e.set_text(self.autospec)

        response = dlg.run()
        if response == 1:  # id 1 set in glade for "Apply"
            self.log.debug('Updating race properties.')
            if not self.onestart:
                newlaps = strops.reformat_biblist(rle.get_text())
                if self.sprintlaps != newlaps:
                    self.sprintlaps = newlaps
                    self.log.info('Reset sprint model.')
                    self.sprint_model_init()
            self.masterslaps = rsb.get_active()
            if rt.get_active() == 0:
                self.scoring = 'madison'
            else:
                self.scoring = 'points'
            self.type_lbl.set_text(self.scoring.capitalize())
            dval = di.get_text()
            if dval.isdigit():
                self.distance = int(dval)
            else:
                self.distance = None
            if du.get_active() == 0:
                self.units = 'metres'
            else:
                self.units = 'laps'

            # update series
            ns = se.get_text()
            if ns != self.series:
                self.series = ns
                self.event[u'seri'] = ns

            # update auto startlist spec
            nspec = as_e.get_text()
            if nspec != self.autospec:
                self.autospec = nspec
                if not self.onestart:
                    if self.autospec:
                        self.meet.autostart_riders(self, self.autospec,
                                                   self.seedsrc)

            # xfer starters if not empty
            slist = strops.reformat_riderlist(
                b.get_object('race_starters_entry').get_text(), self.meet.rdb,
                self.series).split()
            for s in slist:
                self.addrider(s)

            # recalculate
            self.reset_lappoints()
            self.recalculate()
            glib.idle_add(self.delayed_announce)
        else:
            self.log.debug('Edit race properties cancelled.')

        # if prefix is empty, grab input focus
        if self.prefix_ent.get_text() == '':
            self.prefix_ent.grab_focus()
        dlg.destroy()

    ## Race timing manipulations
    def set_start(self, start='', lstart=None):
        """Set the race start time."""
        if type(start) is tod.tod:
            self.start = start
            if lstart is not None:
                self.lstart = lstart
            else:
                self.lstart = self.start
        else:
            self.start = tod.mktod(start)
            if lstart is not None:
                self.lstart = tod.mktod(lstart)
            else:
                self.lstart = self.start
        if self.start is None:
            #self.start_lbl.set_text('')
            pass
        else:
            #self.start_lbl.set_text(self.start.timestr(4))
            if self.finish is None:
                self.set_running()

    def set_finish(self, finish=''):
        """Set the race finish time."""
        if type(finish) is tod.tod:
            self.finish = finish
        else:
            self.finish = tod.mktod(finish)
        if self.finish is None:
            #self.finish_lbl.set_text('')
            if self.start is not None:
                self.set_running()
        else:
            if self.start is None:
                self.set_start('0')
            #self.finish_lbl.set_text(self.finish.timestr(4))
            self.set_finished()

    def set_elapsed(self):
        """Update elapsed race time."""
        if self.start is not None and self.finish is not None:
            et = self.finish - self.start
            self.time_lbl.set_text(et.timestr(2))
        elif self.start is not None:  # Note: uses 'local start' for RT
            runtm = (tod.now() - self.lstart).timestr(1)
            self.time_lbl.set_text(runtm)

            if self.runlap is not None:
                if self.runlap != self.lastrunlap:
                    self.log.debug(u'Runlap: ' + repr(self.runlap))
                    self.lastrunlap = self.runlap

        elif self.timerstat == 'armstart':
            self.time_lbl.set_text(tod.tod(0).timestr(1))
            if self.runlap and self.runlap != self.lastrunlap:
                self.log.debug(u'Runlap: ' + repr(self.runlap))
                self.lastrunlap = self.runlap
        else:
            self.time_lbl.set_text('')

    def log_elapsed(self):
        """Log elapsed time on timy receipt."""
        self.meet.timer.printline(self.meet.racenamecat(self.event))
        self.meet.timer.printline('      ST: ' + self.start.timestr(4))
        self.meet.timer.printline('     FIN: ' + self.finish.timestr(4))
        self.meet.timer.printline('    TIME: ' +
                                  (self.finish - self.start).timestr(2))

    ## State manipulation
    def set_running(self):
        """Set timer to running."""
        self.timerstat = 'running'
        uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Running')

    def set_finished(self):
        """Set timer to finished."""
        self.timerstat = 'finished'
        uiutil.buttonchg(self.stat_but, uiutil.bg_none, 'Finished')
        self.stat_but.set_sensitive(False)
        self.ctrl_places.grab_focus()

    def update_expander_lbl_cb(self):
        """Update the expander label."""
        self.info_expand.set_label('Race Info : ' +
                                   self.meet.racenamecat(self.event, 64))

    def ps_info_time_edit_clicked_cb(self, button, data=None):
        """Run the edit times dialog."""
        ostx = ''
        oftx = ''
        if self.start is not None:
            ostx = self.start.rawtime(4)
        if self.finish is not None:
            oftx = self.finish.rawtime(4)
        ret = uiutil.edit_times_dlg(self.meet.window, ostx, oftx)
        if ret[0] == 1:  # id 1 set in glade for "Apply"
            try:
                stod = None
                if ret[1]:
                    stod = tod.tod(ret[1], 'MANU', 'C0i')
                    self.meet.timer.printline(' ' + str(stod))
                ftod = None
                if ret[2]:
                    ftod = tod.tod(ret[2], 'MANU', 'C1i')
                    self.meet.timer.printline(' ' + str(ftod))
                self.set_start(stod)
                self.set_finish(ftod)
                self.set_elapsed()
                if self.start is not None and self.finish is not None:
                    self.log_elapsed()
                self.log.info('Updated race times.')
            except Exception as v:
                self.log.error('Error updating times: ' + str(v))
        else:
            self.log.info('Edit race times cancelled.')

    def ps_ctrl_place_combo_changed_cb(self, combo, data=None):
        """Handle sprint combo change."""
        self.oktochangecombo = False  # cancel delayed combo changer
        i = self.ctrl_place_combo.get_active_iter()
        if i is not None:
            self.ctrl_places.set_text(
                self.sprints.get_value(i, SPRINT_COL_PLACES) or '')
        else:
            self.ctrl_places.set_text('')
        self.ctrl_places.grab_focus()

    def standingstr(self, width=None):
        """Return an event status string for reports and scb."""
        ret = u'Standings'
        totsprints = 0
        lastsprint = None
        sprintid = None
        cur = 1
        for s in self.sprints:
            self.log.debug(u'cur: ' + repr(cur) + u' val: ' +
                           repr(s[SPRINT_COL_PLACES]))
            if s[SPRINT_COL_PLACES]:
                lastsprint = cur
                sprintid = s[SPRINT_COL_ID]
            if s[SPRINT_COL_ID] not in self.laplabels:
                totsprints += 1
                cur += 1
        if lastsprint >= totsprints:
            ret = u'Result'
            # check for all places in final sprint?
            for r in self.riders:
                if r[RES_COL_INRACE] and r[RES_COL_FINAL] < 0:
                    # not placed at finish
                    ret = u'Provisional Result'
                    break
        else:
            ret = u'Standings'
            if lastsprint:
                if sprintid in self.laplabels:
                    ret += ' After ' + self.laplabels[sprintid]
                elif totsprints > 0:
                    if width is not None and width < 25:
                        ret += u' - Sprint {0}/{1}'.format(
                            lastsprint, totsprints)
                    else:
                        ret += u' After Sprint {0} of {1}'.format(
                            lastsprint, totsprints)
                else:
                    self.log.debug(u'Total sprints was 0: ' +
                                   repr(lastsprint) + u' / ' +
                                   repr(totsprints))
        return ret

    def delayed_result(self):
        """Roll the places entry on to the next sprint."""
        ## TODO : Find correct re-entrant method for handling delayed
        ##        execution
        if self.next_sprint_counter > 1:
            self.next_sprint_counter -= 1
        elif self.next_sprint_counter == 1:
            self.next_sprint_counter = 0
            self.do_places()
            if self.ctrl_places.get_property('has-focus'):
                if self.oktochangecombo:
                    i = self.ctrl_place_combo.get_active_iter()
                    i = self.ctrl_place_combo.get_model().iter_next(i)
                    if i is not None:
                        self.ctrl_place_combo.set_active_iter(i)
            else:
                # input widget lost focus, don't auto advance
                self.oktochangecombo = False
        else:
            self.next_sprint_counter = 0  # clamp negatives
        return False

    def checkplaces(self, places=''):
        """Check the proposed places against current race model."""
        ret = True
        placeset = set()
        for no in strops.reformat_biblist(places).split():
            # repetition? - already in place set?
            if no in placeset:
                self.log.error('Duplicate no in places: ' + repr(no))
                ret = False
            placeset.add(no)
            # rider in the model?
            lr = self.getrider(no)
            if lr is None:
                if not self.meet.get_clubmode():
                    self.log.error('Non-starter in places: ' + repr(no))
                    ret = False
                # otherwise club mode allows non-starter in places
            else:
                # rider still in the race?
                if not lr[RES_COL_INRACE]:
                    self.log.error('DNF rider in places: ' + repr(no))
                    ret = False
        return ret

    def ps_ctrl_places_activate_cb(self, entry, data=None):
        """Handle places entry."""
        places = strops.reformat_placelist(entry.get_text())
        if self.checkplaces(places):
            self.oktochangecombo = False  # cancel existing delayed change
            entry.set_text(places)

            i = self.ctrl_place_combo.get_active_iter()
            prevplaces = self.sprints.get_value(i, SPRINT_COL_PLACES)
            self.sprints.set_value(i, SPRINT_COL_PLACES, places)
            sid = int(self.sprints.get_string_from_iter(i))
            sinfo = self.sprints.get_value(i, SPRINT_COL_LABEL)
            self.recalculate()
            self.timerwin = False
            self.log.info(sinfo + ': ' + places)
            if prevplaces == '':
                FMT = [(2, u'l'), (3, u'r'), u' ',
                       (self.meet.scb.linelen - 8, u'l'), u' ', (1, u'r')]
                self.meet.scbwin = scbwin.scbintsprint(
                    self.meet.scb, self.meet.racenamecat(self.event),
                    sinfo.upper(), FMT, self.sprintresults[sid][0:4])
                self.meet.scbwin.reset()
                self.next_sprint_counter += 1
                delay = SPRINT_PLACE_DELAY * len(self.sprintresults[sid]) or 1
                if delay > SPRINT_PLACE_DELAY_MAX:
                    delay = SPRINT_PLACE_DELAY_MAX
                self.oktochangecombo = True

                self.meet.announce.gfx_overlay(1)
                self.meet.announce.gfx_set_title(self.event[u'pref'] + u' ' +
                                                 self.event[u'info'] + u' ' +
                                                 sinfo)
                for r in self.sprintresults[sid]:
                    self.meet.announce.gfx_add_row([r[0], r[4], r[3]])

                glib.timeout_add_seconds(delay, self.delayed_result)
            elif type(self.meet.scbwin) is scbwin.scbtable:
                self.do_places()  # overwrite result table?
            glib.timeout_add_seconds(1, self.delayed_announce)
            self.meet.delayed_export()
        else:
            self.log.error('Places not updated.')

    def ps_ctrl_action_combo_changed_cb(self, combo, data=None):
        """Handle change on action combo."""
        self.ctrl_action.set_text('')
        self.ctrl_action.grab_focus()

    def ps_ctrl_action_activate_cb(self, entry, data=None):
        """Perform current action on bibs listed."""
        rlist = entry.get_text()
        acode = self.action_model.get_value(
            self.ctrl_action_combo.get_active_iter(), 1)
        if acode == 'gain':
            self.gainlap(strops.reformat_biblist(rlist))
            entry.set_text('')
        elif acode == 'lost':
            self.loselap(strops.reformat_biblist(rlist))
            entry.set_text('')
        elif acode == 'dnf':
            self.dnfriders(strops.reformat_biblist(rlist))
            entry.set_text('')
        elif acode == 'add':
            rlist = strops.reformat_riderlist(rlist, self.meet.rdb,
                                              self.series)
            for bib in rlist.split():
                self.addrider(bib)
            entry.set_text('')
        elif acode == 'del':
            rlist = strops.reformat_riderlist(rlist, self.meet.rdb,
                                              self.series)
            for bib in rlist.split():
                self.delrider(bib)
            entry.set_text('')
        elif acode == 'lap':
            self.runlap = strops.confopt_posint(rlist)
        else:
            self.log.error('Ignoring invalid action.')
        glib.idle_add(self.delayed_announce)

    def ps_sprint_cr_label_edited_cb(self, cr, path, new_text, data=None):
        """Sprint column edit."""
        self.sprints[path][SPRINT_COL_LABEL] = new_text

    def ps_sprint_cr_places_edited_cb(self, cr, path, new_text, data=None):
        """Sprint place edit."""
        new_text = strops.reformat_placelist(new_text)
        self.sprints[path][SPRINT_COL_PLACES] = new_text
        opath = self.sprints.get_string_from_iter(
            self.ctrl_place_combo.get_active_iter())
        if opath == path:
            self.ctrl_places.set_text(new_text)
        self.recalculate()
        # edit places outside control - no auto trigger of export

    def editcol_db(self, cell, path, new_text, col):
        """Cell update with writeback to meet."""
        new_text = new_text.strip()
        self.riders[path][col] = new_text.strip()
        glib.idle_add(self.meet.rider_edit, self.riders[path][RES_COL_BIB],
                      self.series, col, new_text)

    def editcol_cb(self, cell, path, new_text, col):
        self.riders[path][col] = new_text.strip()

    def ps_result_cr_inrace_toggled_cb(self, cr, path, data=None):
        self.riders[path][RES_COL_INRACE] = not (
            self.riders[path][RES_COL_INRACE])
        self.recalculate()

    def ps_result_cr_laps_edited_cb(self, cr, path, new_text, data=None):
        try:
            laps = int(new_text)
            self.riders[path][RES_COL_LAPS] = laps
            self.recalculate()
        except ValueError:
            self.log.warn('Ignoring non-numeric lap count')

    def zeropoints(self):
        for r in self.riders:
            r[RES_COL_POINTS] = 0
            r[RES_COL_TOTAL] = 0
            r[RES_COL_PLACE] = ''
            r[RES_COL_FINAL] = -1  # Negative => Unplaced in final sprint

    def pointsxfer(self, placestr, final=False, index=0, points=None):
        """Transfer points from sprint placings to aggregate."""
        placeset = set()
        if points is None:
            points = [5, 3, 2, 1]  # Default is four places
        self.sprintresults[index] = []
        place = 0
        count = 0
        name_w = self.meet.scb.linelen - 8
        for placegroup in placestr.split():
            for bib in placegroup.split('-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is None:  # ensure rider exists at this point
                        self.log.info('Adding non-starter: ' + repr(bib))
                        self.addrider(bib)
                        r = self.getrider(bib)
                    ptsstr = ''
                    if place < len(points):
                        ptsstr = str(points[place])
                        r[RES_COL_POINTS] += points[place]
                        if bib not in self.auxmap:
                            self.auxmap[bib] = self.nopts[0:]
                        self.auxmap[bib][index] = str(points[place])
                    if final:
                        r[RES_COL_FINAL] = place
                        self.finished = True
                    plstr = unicode(place + 1) + u'.'
                    fname = r[RES_COL_FIRST].decode('utf-8')
                    lname = r[RES_COL_LAST].decode('utf-8')
                    club = r[RES_COL_CLUB].decode('utf-8')
                    if len(club) > 3:
                        # look it up?
                        if self.series in self.meet.ridermap:
                            rh = self.meet.ridermap[self.series][bib]
                            if rh is not None:
                                club = rh['note']
                    self.sprintresults[index].append([
                        plstr, r[RES_COL_BIB],
                        strops.fitname(fname, lname, name_w), ptsstr,
                        strops.resname(fname, lname, club)
                    ])
                    count += 1
                else:
                    self.log.error('Ignoring duplicate no: ' + repr(bib))
            place = count
        if count > 0:
            self.onestart = True

    def retotal(self, r):
        """Update totals."""
        if self.scoring == 'madison':
            r[RES_COL_TOTAL] = r[RES_COL_STPTS] + r[RES_COL_POINTS]
        else:
            r[RES_COL_TOTAL] = r[RES_COL_STPTS] + r[RES_COL_POINTS] + (
                self.lappoints * r[RES_COL_LAPS])

    # Sorting performed in-place on aux table with cols:
    #  0 INDEX		Index in main model
    #  1 BIB		Rider's bib
    #  2 INRACE		Bool rider still in race?
    #  3 LAPS		Rider's laps up/down
    #  4 TOTAL		Total points scored
    #  5 FINAL		Rider's place in final sprint (-1 for unplaced)

    # Point score sorting:
    # inrace / points / final sprint
    def sortpoints(self, x, y):
        if x[2] != y[2]:  # compare inrace
            if x[2]:
                return -1
            else:
                return 1
        else:  # defer to points
            return self.sortpointsonly(x, y)

    def sortpointsonly(self, x, y):
        if x[4] > y[4]:
            return -1
        elif x[4] < y[4]:
            return 1
        else:  # defer to last sprint
            if x[5] == y[5]:
                #self.log.warn('Sort could not split riders.')
                return 0  # places same - or both unplaced
            else:
                xp = x[5]
                if xp < 0: xp = 9999
                yp = y[5]
                if yp < 0: yp = 9999
                return cmp(xp, yp)
        self.log.error('Sort comparison did not match any paths.')

    # Madison score sorting:
    # inrace / laps / points / final sprint
    def sortmadison(self, x, y):
        if x[2] != y[2]:  # compare inrace
            if x[2]:
                return -1
            else:
                return 1
        else:  # defer to distance (laps)
            if x[3] > y[3]:
                return -1
            elif x[3] < y[3]:
                return 1
            else:  # defer to points / final sprint
                return self.sortpointsonly(x, y)

    def sort_riderno(self, x, y):
        return cmp(strops.riderno_key(x[1]), strops.riderno_key(y[1]))

    # result recalculation
    def recalculate(self):
        self.zeropoints()
        self.finished = False
        self.auxmap = {}
        idx = 0
        for s in self.sprints:
            self.pointsxfer(s[SPRINT_COL_PLACES], s[SPRINT_COL_ID] == '0', idx,
                            s[SPRINT_COL_POINTS])
            idx += 1

        if len(self.riders) == 0:
            return

        auxtbl = []
        idx = 0
        for r in self.riders:
            self.retotal(r)
            auxtbl.append([
                idx, r[RES_COL_BIB], r[RES_COL_INRACE], r[RES_COL_LAPS],
                r[RES_COL_TOTAL], r[RES_COL_FINAL]
            ])
            idx += 1
        if self.scoring == 'madison':
            auxtbl.sort(self.sortmadison)
        else:
            auxtbl.sort(self.sortpoints)
        self.riders.reorder([a[0] for a in auxtbl])
        place = 0
        idx = 0
        for r in self.riders:
            if r[RES_COL_INRACE]:
                if idx == 0:
                    place = 1
                else:
                    if self.scoring == 'madison':
                        if self.sortmadison(auxtbl[idx - 1], auxtbl[idx]) != 0:
                            place = idx + 1
                    else:
                        if self.sortpoints(auxtbl[idx - 1], auxtbl[idx]) != 0:
                            place = idx + 1
                r[RES_COL_PLACE] = str(place)
                idx += 1
            else:
                r[RES_COL_PLACE] = 'dnf'

    def sprint_model_init(self):
        """Initialise the sprint places model."""
        self.ctrl_place_combo.set_active(-1)
        self.ctrl_places.set_sensitive(False)
        self.sprints.clear()
        self.auxmap = {}
        self.nopts = []
        isone = False
        self.sprintresults = []
        for sl in self.sprintlaps.split():
            isone = True
            lt = sl
            if sl.isdigit():
                if int(sl) == 0:
                    lt = 'Final sprint'
                else:
                    lt = 'Sprint at ' + sl + ' to go'
            sp = None
            if sl in self.sprintpoints:
                nextp = []
                for nv in self.sprintpoints[sl].split():
                    if nv.isdigit():
                        nextp.append(int(nv))
                    else:
                        nextp = None
                        break
                sp = nextp
            nr = [sl, lt, None, None, '', sp]
            self.sprints.append(nr)
            self.sprintresults.append([])
            self.nopts.append('')
        if isone:
            self.ctrl_place_combo.set_active(0)
            self.ctrl_places.set_sensitive(True)

    def spptsedit(self, cr, path, new_text, data=None):
        """Sprint points edit."""
        new_text = strops.reformat_biblist(new_text)
        op = None
        nextp = []
        for nv in new_text.split():
            if nv.isdigit():
                nextp.append(int(nv))
            else:
                nextp = None
                break
        sid = self.sprints[path][SPRINT_COL_ID]
        if nextp is not None and len(nextp) > 0:
            self.sprintpoints[sid] = u' '.join(map(unicode, nextp))
            op = nextp
        else:
            if sid in self.sprintpoints:
                del self.sprintpoints[sid]
        self.sprints[path][SPRINT_COL_POINTS] = op
        self.recalculate()

    def spptsstr(self, col, cr, model, iter, data=None):
        """Format tod into text for listview."""
        pv = model.get_value(iter, SPRINT_COL_POINTS)
        if pv is not None and len(pv) > 0:
            cr.set_property('text', u', '.join(map(unicode, pv)))
        else:
            cr.set_property('text', '')

    def todstr(self, col, cr, model, iter, data=None):
        """Format tod into text for listview."""
        st = model.get_value(iter, SPRINT_COL_200)
        ft = model.get_value(iter, SPRINT_COL_SPLIT)
        if st is not None and ft is not None:
            cr.set_property('text', (ft - st).timestr(2))
        else:
            cr.set_property('text', '')

    def reset_lappoints(self):
        """Update lap points allocations."""
        if self.masterslaps:
            self.lappoints = 10
            ## OLD WAY WAS AUTO -> NOW JUST MANUALLY
            #dist = self.meet.get_distance(self.distance, self.units)
            #if dist is not None and dist < 20000:
            #self.lappoints = 10
            #else:
            #self.lappoints = 20
        else:
            self.lappoints = 20

    def destroy(self):
        """Signal race shutdown."""
        self.frame.destroy()

    def show(self):
        """Show race window."""
        self.frame.show()

    def hide(self):
        """Hide race window."""
        self.frame.hide()

    def editent_cb(self, entry, col):
        """Shared event entry update callback."""
        if col == u'pref':
            self.event[u'pref'] = entry.get_text().decode('utf-8', 'replace')
        elif col == u'info':
            self.event[u'info'] = entry.get_text().decode('utf-8', 'replace')
        self.update_expander_lbl_cb()

    def __init__(self, meet, event, ui=True):
        """Constructor."""
        self.meet = meet
        self.event = event  # Note: now a treerowref
        self.evno = event[u'evid']
        self.evtype = event[u'type']
        self.series = event[u'seri']
        self.configpath = meet.event_configfile(self.evno)
        self.comments = []

        self.log = logging.getLogger('points')
        self.log.setLevel(logging.DEBUG)
        self.log.debug('opening event: ' + str(self.evno))

        # race property attributes
        self.masterslaps = True
        self.lappoints = 20
        self.scoring = 'points'
        self.distance = None
        self.units = 'laps'
        self.sprintlaps = ''
        self.sprintpoints = {}
        self.nopts = []
        self.sprintresults = []
        self.laplabels = {}
        self.sprintsource = {}
        self.auxmap = {}

        # race run time attributes
        self.onestart = False
        self.runlap = None
        self.lastrunlap = None
        self.readonly = not ui
        self.start = None
        self.lstart = None
        self.finish = None
        self.winopen = ui
        self.timerwin = False
        self.timerstat = 'idle'
        self.curtimerstr = ''
        self.sprintstart = None
        self.sprintlstart = None
        self.next_sprint_counter = 0
        self.oktochangecombo = False
        self.autospec = ''
        self.finished = False
        self.inomnium = False
        self.seedsrc = None

        # data models
        self.sprints = gtk.ListStore(
            gobject.TYPE_STRING,  # ID = 0
            gobject.TYPE_STRING,  # LABEL = 1
            gobject.TYPE_PYOBJECT,  # 200 = 2
            gobject.TYPE_PYOBJECT,  # SPLITS = 3
            gobject.TYPE_STRING,  # PLACES = 4
            gobject.TYPE_PYOBJECT)  # POINTS = 5

        self.riders = gtk.ListStore(
            gobject.TYPE_STRING,  # BIB = 0
            gobject.TYPE_STRING,  # FIRST = 1
            gobject.TYPE_STRING,  # LAST = 2
            gobject.TYPE_STRING,  # CLUB = 3
            gobject.TYPE_BOOLEAN,  # INRACE = 4
            gobject.TYPE_INT,  # POINTS = 5
            gobject.TYPE_INT,  # LAPS = 6
            gobject.TYPE_INT,  # TOTAL = 7
            gobject.TYPE_STRING,  # PLACE = 8
            gobject.TYPE_INT,  # FINAL = 9
            gobject.TYPE_STRING,  # INFO = 10
            gobject.TYPE_INT)  # STPTS = 11

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'ps.ui'))

        self.frame = b.get_object('ps_vbox')
        self.frame.connect('destroy', self.shutdown)

        # info pane
        self.info_expand = b.get_object('info_expand')
        b.get_object('ps_info_evno').set_text(self.evno)
        self.showev = b.get_object('ps_info_evno_show')
        self.prefix_ent = b.get_object('ps_info_prefix')
        self.prefix_ent.connect('changed', self.editent_cb, u'pref')
        self.prefix_ent.set_text(self.event[u'pref'])
        self.info_ent = b.get_object('ps_info_title')
        self.info_ent.connect('changed', self.editent_cb, u'info')
        self.info_ent.set_text(self.event[u'info'])

        self.time_lbl = b.get_object('ps_info_time')
        self.time_lbl.modify_font(pango.FontDescription("monospace bold"))
        self.update_expander_lbl_cb()  # signals get connected later...
        self.type_lbl = b.get_object('race_type')
        self.type_lbl.set_text(self.scoring.capitalize())

        # ctrl pane
        self.stat_but = b.get_object('ps_ctrl_stat_but')
        self.ctrl_place_combo = b.get_object('ps_ctrl_place_combo')
        self.ctrl_place_combo.set_model(self.sprints)
        self.ctrl_places = b.get_object('ps_ctrl_places')
        self.ctrl_action_combo = b.get_object('ps_ctrl_action_combo')
        self.ctrl_action = b.get_object('ps_ctrl_action')
        self.action_model = b.get_object('ps_action_model')

        # sprints pane
        t = gtk.TreeView(self.sprints)
        t.set_reorderable(True)
        t.set_enable_search(False)
        t.set_rules_hint(True)
        t.show()
        uiutil.mkviewcoltxt(t,
                            'Sprint',
                            SPRINT_COL_LABEL,
                            self.ps_sprint_cr_label_edited_cb,
                            expand=True)
        #uiutil.mkviewcoltod(t, '200m', cb=self.todstr)
        uiutil.mkviewcoltxt(t,
                            'Places',
                            SPRINT_COL_PLACES,
                            self.ps_sprint_cr_places_edited_cb,
                            expand=True)
        uiutil.mkviewcoltod(t,
                            'Points',
                            cb=self.spptsstr,
                            editcb=self.spptsedit)
        b.get_object('ps_sprint_win').add(t)

        # results pane
        t = gtk.TreeView(self.riders)
        t.set_reorderable(True)
        t.set_enable_search(False)
        t.set_rules_hint(True)
        t.show()
        uiutil.mkviewcoltxt(t, 'No.', RES_COL_BIB, calign=1.0)
        uiutil.mkviewcoltxt(t,
                            'First Name',
                            RES_COL_FIRST,
                            self.editcol_db,
                            expand=True)
        uiutil.mkviewcoltxt(t,
                            'Last Name',
                            RES_COL_LAST,
                            self.editcol_db,
                            expand=True)
        uiutil.mkviewcoltxt(t, 'Club', RES_COL_CLUB, self.editcol_db)
        uiutil.mkviewcoltxt(t, 'Info', RES_COL_INFO, self.editcol_cb)
        uiutil.mkviewcolbool(t,
                             'In',
                             RES_COL_INRACE,
                             self.ps_result_cr_inrace_toggled_cb,
                             width=50)
        uiutil.mkviewcoltxt(t, 'Pts', RES_COL_POINTS, calign=1.0, width=50)
        uiutil.mkviewcoltxt(t,
                            'Laps',
                            RES_COL_LAPS,
                            calign=1.0,
                            width=50,
                            cb=self.ps_result_cr_laps_edited_cb)
        uiutil.mkviewcoltxt(t, 'Total', RES_COL_TOTAL, calign=1.0, width=50)
        uiutil.mkviewcoltxt(t, 'L/S', RES_COL_FINAL, calign=0.5, width=50)
        uiutil.mkviewcoltxt(t, 'Rank', RES_COL_PLACE, calign=0.5, width=50)
        b.get_object('ps_result_win').add(t)

        if ui:
            # connect signal handlers
            b.connect_signals(self)
