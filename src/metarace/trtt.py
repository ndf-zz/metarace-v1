# SPDX-License-Identifier: MIT
"""Road team time trial handler for roadmeet."""

import gtk
import glib
import gobject
import pango
import os
import logging
import threading
import bisect

import metarace
from metarace import tod
from metarace import eventdb
from metarace import riderdb
from metarace import strops
from metarace import countback
from metarace import uiutil
from metarace import report
from metarace import jsonconfig

_log = logging.getLogger(u'metarace.trtt')
_log.setLevel(logging.DEBUG)

# Model columns

# basic infos
COL_BIB = 0
COL_NAMESTR = 1
COL_SHORTNAME = 2
COL_CAT = 3
COL_COMMENT = 4
COL_INRACE = 5  # boolean in the race
COL_PLACE = 6  # unlike road events, this is the confirmed arrival at finish
COL_LAPS = 7  # Incremented if inrace and not finished

# timing infos
COL_RFTIME = 8  # one-off finish time by rfid
COL_CBUNCH = 9  # computed bunch time	-> derived from rftime
COL_MBUNCH = 10  # manual bunch time	-> manual overrive
COL_STOFT = 11  # start time 'offset', read from team start time
COL_BONUS = 12
COL_PENALTY = 13
COL_RFSEEN = 14  # list of tods this rider 'seen' by rfid
COL_TEAM = 15  # stored team ref for quick refs

# Nth wheel decides whose time is counted to the team
NTH_WHEEL = 3

# Minimum lap/elap time, should be at least the same as start gaps
MINLAP = tod.tod(u'2:00')

# Add a gap in the startlist when gap is larger than TEAMGAP
TEAMGAP = tod.tod(u'4:00')

# listview column nos
CATCOLUMN = 2
COMCOLUMN = 3
LAPCOLUMN = 4
STARTCOLUMN = 5

# rider commands
RIDER_COMMANDS_ORD = [
    u'add', u'del', u'que', u'dns', u'otl', u'dnf', u'dsq', u'com', u'ret',
    u'run', u'man', u'', u'fin'
]  # then intermediates...
RIDER_COMMANDS = {
    u'dns': u'Did not start',
    u'otl': u'Outside time limit',
    u'dnf': u'Did not finish',
    u'dsq': u'Disqualify',
    u'add': u'Add starters',
    u'del': u'Remove starters',
    u'que': u'Query riders',
    u'fin': u'Final places',
    u'com': u'Add comment',
    u'ret': u'Return to race',
    u'man': u'Manual passing',
    u'': u'',
    u'run': u'Show team time'
}

RESERVED_SOURCES = [
    u'fin',  # finished stage
    u'reg',  # registered to stage
    u'start'
]  # started stage
# additional cat finishes added in loadconfig

DNFCODES = [u'otl', u'dsq', u'dnf', u'dns']
GAPTHRESH = tod.tod(u'1.12')

# timing keys
key_announce = u'F4'
key_armstart = u'F5'
key_armlap = u'F6'
key_placesto = u'F7'  # fill places to selected rider
key_appendplace = u'F8'  # append sepected rider to places
key_armfinish = u'F9'
key_raceover = u'F10'

# extended fn keys	(ctrl + key)
key_abort = u'F5'
key_clearfrom = u'F7'  # clear places on selected rider and all following
key_clearplace = u'F8'  # clear rider from place list

# config version string
EVENT_ID = 'trtt-3.0'


class trtt(object):
    """Road time trial handler."""

    def hidecolumn(self, target, visible=False):
        tc = self.view.get_column(target)
        if tc:
            tc.set_visible(visible)

    def loadcats(self, cats=[]):
        self.cats = []  # clear old cat list
        if u'AUTO' in cats:  # ignore any others and re-load from rdb
            self.cats = self.meet.rdb.listcats()
            self.autocats = True
        else:
            self.autocats = False
            for cat in cats:
                if cat != u'':
                    cat = cat.upper()
                    if cat not in [u'CAT', u'SPARE', u'TEAM']:
                        self.cats.append(cat)
                    else:
                        _log.warning(u'Invalid result category: %r', cat)
        self.cats.append(u'')  # always include one empty cat
        _log.debug(u'Result category list updated: %r', self.cats)

    def team_start_times(self):
        """Scan riders and patch start times from team entry."""
        self.teamnames = {}
        self.teamuci = {}
        teamstarts = {}
        # pass 1: extract team times and names
        for r in self.riders:
            nt = r[COL_TEAM].decode('utf-8')
            if nt not in teamstarts:
                teamUci = u''
                teamName = nt
                st = tod.ZERO
                dbr = self.meet.rdb.getrider(nt, u'team')
                if dbr is not None:
                    st = tod.mktod(
                        self.meet.rdb.getvalue(dbr, riderdb.COL_REFID))
                    teamName = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
                    teamUci = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                else:
                    _log.warning(u'No team entry found for %r (rider: %s)', nt,
                                 r[COL_BIB].decode('utf-8'))
                self.teamnames[nt] = teamName
                self.teamuci[nt] = teamUci
                teamstarts[nt] = st

        # pass 2: patch start times if present
        cnt = 0
        for r in self.riders:
            nt = r[COL_TEAM].decode('utf-8')
            if nt in teamstarts and teamstarts[nt]:
                r[COL_STOFT] = teamstarts[nt]
                cnt += 1
            else:
                r[COL_STOFT] = tod.ZERO
                _log.warning(u'No start time for %s:%s',
                             r[COL_TEAM].decode(u'utf-8'),
                             r[COL_BIB].decode(u'utf-8'))
        _log.debug(u'Patched %r start times', cnt)

    def loadconfig(self):
        """Load event config from disk."""
        self.riders.clear()
        self.resettimer()
        self.cats = []

        cr = jsonconfig.config({
            u'trtt': {
                u'start': None,
                u'id': EVENT_ID,
                u'finished': False,
                u'showuciids': False,
                u'relativestart': False,
                u'showriders': True,
                u'places': u'',
                u'comment': [],
                u'categories': [],
                u'intermeds': [],
                u'contests': [],
                u'tallys': [],
                u'owntime': True,
                u'totlaps': None,
                u'defaultnth': NTH_WHEEL,
                u'minlap': None,
                u'nthwheel': {},
                u'startlist': u'',
                u'resultcats': False,
                u'autofinish': False,
                u'autoexport': False,
            }
        })
        cr.add_section(u'trtt')
        cr.add_section(u'riders')
        cr.add_section(u'stagebonus')
        cr.add_section(u'stagepenalty')
        cr.merge(metarace.sysconf, u'trtt')

        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                _log.error(u'Unable to read config: %s', e)
        else:
            _log.info(u'%r not found, loading defaults', self.configfile)

        # load result categories
        self.loadcats(cr.get(u'trtt', u'categories'))

        # read in default and category specific nth wheel values
        self.defaultnth = cr.get_posint(u'trtt', u'defaultnth')
        _log.debug(u'Default Nth Wheel: %r', self.defaultnth)
        self.nthwheel = cr.get(u'trtt', u'nthwheel')
        if len(self.nthwheel) > 0:
            _log.debug(u'Nth Wheel: %r', self.nthwheel)

        # amend reserved sources with any cats
        if len(self.cats) > 1:
            for cat in self.cats:
                if cat:
                    srcid = cat.lower() + u'fin'
                    RESERVED_SOURCES.append(srcid)
                    self.catplaces[srcid] = cat

        # restore intermediates
        for i in cr.get(u'trtt', u'intermeds'):
            if i in RESERVED_SOURCES:
                _log.info(u'Ignoring reserved inter: %r', i)
            else:
                crkey = u'intermed_' + i
                descr = u''
                places = u''
                km = None
                doshow = False
                abbr = u''
                if cr.has_option(crkey, u'descr'):
                    descr = cr.get(crkey, u'descr')
                if cr.has_option(crkey, u'dist'):
                    km = cr.get_float(crkey, u'dist', None)
                if cr.has_option(crkey, u'abbr'):
                    abbr = cr.get(crkey, u'abbr')
                if cr.has_option(crkey, u'show'):
                    doshow = cr.get_bool(crkey, u'show')
                if cr.has_option(crkey, u'places'):
                    places = strops.reformat_placelist(cr.get(
                        crkey, u'places'))
                if i not in self.intermeds:
                    _log.debug(u'Adding inter %r: %r %r', i, descr, places)
                    self.intermeds.append(i)
                    self.intermap[i] = {
                        u'descr': descr,
                        u'places': places,
                        u'abbr': abbr,
                        u'dist': km,
                        u'show': doshow
                    }
                else:
                    _log.info(u'Ignoring duplicate inter: %r', i)

        # load contest meta data
        tallyset = set()
        for i in cr.get(u'trtt', u'contests'):
            if i not in self.contests:
                self.contests.append(i)
                self.contestmap[i] = {}
                crkey = u'contest_' + i
                tally = u''
                if cr.has_option(crkey, u'tally'):
                    tally = cr.get(crkey, u'tally')
                    if tally:
                        tallyset.add(tally)
                self.contestmap[i][u'tally'] = tally
                descr = i
                if cr.has_option(crkey, u'descr'):
                    descr = cr.get(crkey, u'descr')
                    if descr == u'':
                        descr = i
                self.contestmap[i][u'descr'] = descr
                labels = []
                if cr.has_option(crkey, u'labels'):
                    labels = cr.get(crkey, u'labels').split()
                self.contestmap[i][u'labels'] = labels
                source = i
                if cr.has_option(crkey, u'source'):
                    source = cr.get(crkey, u'source')
                    if source == u'':
                        source = i
                self.contestmap[i][u'source'] = source
                bonuses = []
                if cr.has_option(crkey, u'bonuses'):
                    for bstr in cr.get(crkey, u'bonuses').split():
                        bt = tod.mktod(bstr)
                        if bt is None:
                            _log.info(u'Invalid bonus %r in contest %r', bstr,
                                      i)
                            bt = tod.ZERO
                        bonuses.append(bt)
                self.contestmap[i][u'bonuses'] = bonuses
                points = []
                if cr.has_option(crkey, u'points'):
                    pliststr = cr.get(crkey, u'points').strip()
                    if pliststr and tally == u'':
                        _log.error(u'No tally for points in contest %r', i)
                        tallyset.add(u'')  # add empty placeholder
                    for pstr in pliststr.split():
                        pt = 0
                        try:
                            pt = int(pstr)
                        except Exception:
                            _log.info(u'Invalid points %r in contest %r', pstr,
                                      i)
                        points.append(pt)
                self.contestmap[i][u'points'] = points
                allsrc = False  # all riders in source get same pts
                if cr.has_option(crkey, u'all_source'):
                    allsrc = cr.get_bool(crkey, u'all_source')
                self.contestmap[i][u'all_source'] = allsrc
                self.contestmap[i][u'category'] = 0
                if cr.has_option(crkey, u'category'):  # for climbs
                    self.contestmap[i][u'category'] = cr.get_posint(
                        crkey, u'category')
            else:
                _log.info(u'Ignoring duplicate contest %r', i)

            # check for invalid allsrc
            if self.contestmap[i][u'all_source']:
                if (len(self.contestmap[i][u'points']) > 1
                        or len(self.contestmap[i][u'bonuses']) > 1):
                    _log.info(u'Ignoring extra points/bonus for allsrc %r', i)

        # load points tally meta data
        tallylist = cr.get(u'trtt', u'tallys')
        # append any 'missing' tallys from points data errors
        for i in tallyset:
            if i not in tallylist:
                _log.debug(u'Adding missing tally to config %r', i)
                tallylist.append(i)
        # then scan for meta data
        for i in tallylist:
            if i not in self.tallys:
                self.tallys.append(i)
                self.tallymap[i] = {}
                self.points[i] = {}
                self.pointscb[i] = {}
                crkey = u'tally_' + i
                descr = u''
                if cr.has_option(crkey, u'descr'):
                    descr = cr.get(crkey, u'descr')
                self.tallymap[i][u'descr'] = descr
                keepdnf = False
                if cr.has_option(crkey, u'keepdnf'):
                    keepdnf = cr.get_bool(crkey, u'keepdnf')
                self.tallymap[i][u'keepdnf'] = keepdnf
            else:
                _log.info(u'Ignoring duplicate points tally %r', i)

        starters = cr.get(u'trtt', u'startlist').split()
        if len(starters) == 1 and starters[0] == u'all':
            starters = strops.riderlist_split(u'all', self.meet.rdb)
        for r in starters:
            self.addrider(r)
            if cr.has_option(u'riders', r):
                nr = self.getrider(r)
                # bib = comment,in,laps,rftod,mbunch,rfseen...
                ril = cr.get(u'riders', r)  # rider op is vec
                lr = len(ril)
                if lr > 0:
                    nr[COL_COMMENT] = ril[0]
                if lr > 1:
                    nr[COL_INRACE] = strops.confopt_bool(ril[1])
                if lr > 2:
                    nr[COL_LAPS] = strops.confopt_posint(ril[2])
                if lr > 3:
                    nr[COL_RFTIME] = tod.mktod(ril[3])
                if lr > 4:
                    nr[COL_MBUNCH] = tod.mktod(ril[4])
                if lr > 5:
                    nr[COL_STOFT] = tod.mktod(ril[5])
                if lr > 6:
                    for i in range(6, lr):
                        laptod = tod.mktod(ril[i])
                        if laptod is not None:
                            nr[COL_RFSEEN].append(laptod)
            # record any extra bonus/penalty to rider model
            if cr.has_option(u'stagebonus', r):
                nr[COL_BONUS] = tod.mktod(cr.get(u'stagebonus', r))
            if cr.has_option(u'stagepenalty', r):
                nr[COL_PENALTY] = tod.mktod(cr.get(u'stagepenalty', r))

        self.owntime = cr.get_bool(u'trtt', u'owntime')
        self.minlap = tod.mktod(cr.get(u'trtt', u'minlap'))
        if self.minlap is None:
            self.minlap = MINLAP
        _log.debug(u'Minimum lap time: %s', self.minlap.rawtime())

        self.set_start(cr.get(u'trtt', u'start'))
        self.totlaps = cr.get(u'trtt', u'totlaps')
        self.places = strops.reformat_placelist(cr.get(u'trtt', u'places'))
        self.comment = cr.get(u'trtt', u'comment')
        self.autoexport = cr.get_bool(u'trtt', u'autoexport')
        self.showuciids = cr.get_bool(u'trtt', u'showuciids')
        self.showriders = cr.get_bool(u'trtt', u'showriders')
        self.relativestart = cr.get_bool(u'trtt', u'relativestart')
        if strops.confopt_bool(cr.get(u'trtt', u'finished')):
            self.set_finished()
        self.recalculate()

        self.load_cat_data()

        if self.totlaps is not None:
            self.totlapentry.set_text(unicode(self.totlaps))

        # patch team start times from riderdb
        self.team_start_times()

        if cr.get_bool(u'trtt', u'autofinish'):
            # then override targetlaps if autofinish was set
            self.targetlaps = True

        # After load complete - check config and report.
        eid = cr.get(u'trtt', u'id')
        if eid and eid != EVENT_ID:
            _log.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)
            self.readonly = True

    def load_cat_data(self):
        self.catlaps = {}
        onetarget = False
        onemissing = False
        for c in self.cats:
            ls = None
            # fetch data on all but the uncat cat
            if c:
                dbr = self.meet.rdb.getrider(c, u'cat')
                if dbr is not None:
                    lt = strops.confopt_posint(
                        self.meet.rdb.getvalue(dbr, riderdb.COL_CAT))
                    if lt:
                        ls = lt
                        onetarget = True
                    else:
                        onemissing = True
            self.catlaps[c] = ls
        if onetarget:
            self.targetlaps = True
            if onemissing:
                # There's one or more cats without a target, issue warning
                missing = []
                for c in self.catlaps:
                    if self.catlaps[c] is None:
                        missing.append(repr(c))
                if missing:
                    _log.warning(u'Categories missing target lap count: %s',
                                 u', '.join(missing))
            _log.debug(u'Category laps: %r', self.catlaps)

    def get_ridercmdorder(self):
        """Return rider command list order."""
        ret = RIDER_COMMANDS_ORD[0:]
        for i in self.intermeds:
            ret.append(i)
        return ret

    def get_ridercmds(self):
        """Return a dict of rider bib commands for container ui."""
        ret = {}
        for k in RIDER_COMMANDS:
            ret[k] = RIDER_COMMANDS[k]
        for k in self.intermap:
            descr = k
            if self.intermap[k]['descr']:
                descr = k + u' : ' + self.intermap[k]['descr']
            ret[k] = descr
        return ret

    def get_startlist(self):
        """Return a list of all rider numbers registered to event."""
        ret = []
        for r in self.riders:
            ret.append(r[COL_BIB].decode(u'utf-8'))
        return u' '.join(ret)

    def get_starters(self):
        """Return a list of riders that 'started' the race."""
        ret = []
        for r in self.riders:
            if r[COL_COMMENT].decode(u'utf-8') != u'dns' or r[COL_INRACE]:
                ret.append(r[COL_BIB].decode(u'utf-8'))
        return u' '.join(ret)

    def get_catlist(self):
        """Return the ordered list of categories."""
        rvec = []
        for cat in self.cats:
            if cat != u'':
                rvec.append(cat)
        return rvec

    def ridercat(self, cat):
        """Return a result category for the provided rider cat."""
        ret = u''
        checka = cat.upper()
        if checka in self.cats:
            ret = checka
        return ret

    def saveconfig(self):
        """Save event config to disk."""
        if self.readonly:
            _log.error(u'Attempt to save readonly event')
            return
        cw = jsonconfig.config()
        cw.add_section(u'trtt')
        if self.start is not None:
            cw.set(u'trtt', u'start', self.start.rawtime())
        if self.minlap is not None:
            cw.set(u'trtt', u'minlap', self.minlap.rawtime())
        else:
            cw.set(u'trtt', u'minlap', None)
        cw.set(u'trtt', u'showuciids', self.showuciids)
        cw.set(u'trtt', u'showriders', self.showriders)
        cw.set(u'trtt', u'relativestart', self.relativestart)
        cw.set(u'trtt', u'finished', self.timerstat == 'finished')
        cw.set(u'trtt', u'places', self.places)
        cw.set(u'trtt', u'totlaps', self.totlaps)
        cw.set(u'trtt', u'defaultnth', self.defaultnth)
        cw.set(u'trtt', u'owntime', self.owntime)
        cw.set(u'trtt', u'autofinish', self.targetlaps)
        cw.set(u'trtt', u'autoexport', self.autoexport)
        cw.set(u'trtt', u'nthwheel', self.nthwheel)  # dict of cat keys

        # save intermediate data
        opinters = []
        for i in self.intermeds:
            crkey = u'intermed_' + i
            cw.add_section(crkey)
            cw.set(crkey, u'descr', self.intermap[i][u'descr'])
            cw.set(crkey, u'places', self.intermap[i][u'places'])
            cw.set(crkey, u'show', self.intermap[i][u'show'])
            if u'dist' in self.intermap[i]:
                cw.set(crkey, u'dist', self.intermap[i][u'dist'])
            if u'abbr' in self.intermap[i]:
                cw.set(crkey, u'abbr', self.intermap[i][u'abbr'])
            opinters.append(i)
        cw.set(u'trtt', u'intermeds', opinters)

        # save contest meta data
        cw.set(u'trtt', u'contests', self.contests)
        for i in self.contests:
            crkey = u'contest_' + i
            cw.add_section(crkey)
            cw.set(crkey, u'tally', self.contestmap[i][u'tally'])
            cw.set(crkey, u'source', self.contestmap[i][u'source'])
            cw.set(crkey, u'all_source', self.contestmap[i][u'all_source'])
            if u'category' in self.contestmap[i]:
                cw.set(crkey, u'category', self.contestmap[i][u'category'])
            blist = []
            for b in self.contestmap[i][u'bonuses']:
                blist.append(b.rawtime(0))
            cw.set(crkey, u'bonuses', u' '.join(blist))
            plist = []
            for p in self.contestmap[i][u'points']:
                plist.append(str(p))
            cw.set(crkey, u'points', u' '.join(plist))
        # save tally meta data
        cw.set(u'trtt', u'tallys', self.tallys)
        for i in self.tallys:
            crkey = u'tally_' + i
            cw.add_section(crkey)
            cw.set(crkey, u'descr', self.tallymap[i][u'descr'])
            cw.set(crkey, u'keepdnf', self.tallymap[i][u'keepdnf'])

        # save riders
        evtriders = self.get_startlist()
        if evtriders:
            cw.set(u'trtt', u'startlist', self.get_startlist())
        else:
            if self.autostartlist is not None:
                cw.set(u'trtt', u'startlist', self.autostartlist)
        if self.autocats:
            cw.set(u'trtt', u'categories', [u'AUTO'])
        else:
            cw.set(u'trtt', u'categories', self.get_catlist())
        cw.set(u'trtt', u'comment', self.comment)

        cw.add_section(u'riders')
        # sections for commissaire awarded bonus/penalty
        cw.add_section(u'stagebonus')
        cw.add_section(u'stagepenalty')
        for r in self.riders:
            rt = u''
            if r[COL_RFTIME] is not None:
                rt = r[COL_RFTIME].rawtime()
            mb = u''
            if r[COL_MBUNCH] is not None:
                mb = r[COL_MBUNCH].rawtime(1)
            sto = u''
            if r[COL_STOFT] is not None:
                sto = r[COL_STOFT].rawtime()
            # bib = comment,in,laps,rftod,mbunch,rfseen...
            bib = r[COL_BIB].decode('utf-8')
            slice = [
                r[COL_COMMENT].decode('utf-8'), r[COL_INRACE], r[COL_LAPS], rt,
                mb, sto
            ]
            for t in r[COL_RFSEEN]:
                if t is not None:
                    slice.append(t.rawtime())
            cw.set(u'riders', bib, slice)
            if r[COL_BONUS] is not None:
                cw.set(u'stagebonus', bib, r[COL_BONUS].rawtime())
            if r[COL_PENALTY] is not None:
                cw.set(u'stagepenalty', bib, r[COL_PENALTY].rawtime())
        cw.set(u'trtt', u'id', EVENT_ID)
        _log.debug(u'Saving event config %r', self.configfile)
        with metarace.savefile(self.configfile) as f:
            cw.write(f)

    def show(self):
        """Show event container."""
        self.frame.show()

    def hide(self):
        """Hide event container."""
        self.frame.hide()

    def title_close_clicked_cb(self, button, entry=None):
        """Close and save the race."""
        self.meet.close_event()

    def set_titlestr(self, titlestr=None):
        """Update the title string label."""
        if titlestr is None or titlestr == u'':
            titlestr = u'[Road Team Time Trial]'
        self.title_namestr.set_text(titlestr)

    def destroy(self):
        """Emit destroy signal to race handler."""
        if self.context_menu is not None:
            self.context_menu.destroy()
        self.frame.destroy()

    def points_report(self):
        """Return the points tally report."""
        self.recalculate()
        ret = []
        aux = []
        cnt = 0
        for tally in self.tallys:
            sec = report.section(u'points-' + tally)
            sec.heading = tally.upper() + u' ' + self.tallymap[tally][u'descr']
            sec.units = u'pt'
            tallytot = 0
            aux = []
            cnt = 0
            for bib in self.points[tally]:
                r = self.getrider(bib)
                tallytot += self.points[tally][bib]
                aux.append((self.points[tally][bib], self.pointscb[tally][bib],
                            -strops.riderno_key(bib), -cnt, [
                                None, r[COL_BIB].decode(u'utf-8'),
                                r[COL_NAMESTR].decode(u'utf-8'),
                                strops.truncpad(str(self.pointscb[tally][bib]),
                                                10,
                                                ellipsis=False), None,
                                str(self.points[tally][bib])
                            ], self.pointscb[tally][bib]))
                cnt += 1
            aux.sort(reverse=True)
            for r in aux:
                sec.lines.append(r[4])
            _log.debug(u'Total points for %r: %r', tally, tallytot)
            ret.append(sec)
        if len(self.bonuses) > 0:
            sec = report.section(u'bonus')
            sec.heading = u'Stage Bonuses'
            sec.units = u'sec'
            aux = []
            cnt = 0
            for bib in self.bonuses:
                r = self.getrider(bib)
                aux.append(
                    (self.bonuses[bib], -strops.riderno_key(bib), -cnt, [
                        None, r[COL_BIB], r[COL_NAMESTR], None, None,
                        str(int(self.bonuses[bib].truncate(0).timeval))
                    ]))
                cnt += 1
            aux.sort(reverse=True)
            for r in aux:
                sec.lines.append(r[3])
            ret.append(sec)
        if len(ret) == 0:
            _log.warning(u'No data available for points report')
        return ret

    def reorder_riderno(self):
        """Reorder riders by riderno."""
        self.calcset = False
        aux = []
        cnt = 0
        for r in self.riders:
            aux.append((strops.riderno_key(r[COL_BIB].decode('utf-8')), cnt))
            cnt += 1
        if len(aux) > 1:
            aux.sort()
            self.riders.reorder([a[1] for a in aux])
        return cnt

    def reorder_startlist(self):
        """Reorder riders for a startlist."""
        self.calcset = False
        aux = []
        cnt = 0
        for r in self.riders:
            aux.append((r[COL_STOFT],
                        strops.riderno_key(r[COL_BIB].decode('utf-8')), cnt))
            cnt += 1
        if len(aux) > 1:
            aux.sort()
            self.riders.reorder([a[2] for a in aux])
        return cnt

    def signon_report(self):
        """Return a signon report."""
        ret = []
        self.reorder_riderno()
        if len(self.cats) > 1:
            _log.debug(u'Preparing categorised signon for %r', self.cats)
            for c in self.cats:
                c = self.ridercat(c)
                _log.debug(u'Preparing signon cat %r', c)
                if True:
                    sec = report.signon_list(u'signon')
                    sec.heading = c
                    dbr = self.meet.rdb.getrider(c, u'cat')
                    if dbr is not None:
                        sec.heading = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_FIRST)
                        sec.subheading = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_LAST)
                    elif c == u'':
                        sec.heading = u'Uncategorised Riders'
                    for r in self.riders:
                        # primary cat is used for sign-on
                        cs = r[COL_CAT].decode(u'utf-8')
                        rcat = self.ridercat(riderdb.primary_cat(cs))
                        if rcat == c:
                            cmt = None
                            if not r[COL_INRACE]:
                                cmt = r[COL_COMMENT].decode(u'utf-8')
                            sec.lines.append([
                                cmt, r[COL_BIB].decode(u'utf-8'),
                                r[COL_NAMESTR].decode(u'utf-8')
                            ])
                    if len(sec.lines) > 0:
                        if c == u'':
                            _log.warning(u'%r uncategorised riders',
                                         len(sec.lines))
                        ret.append(sec)
                        ret.append(report.pagebreak(threshold=0.1))
                    else:
                        if c:
                            _log.warning(u'No starters for category %r', c)
        else:
            _log.debug(u'Preparing flat signon')
            sec = report.signon_list(u'signon')
            for r in self.riders:
                cmt = None
                if not r[COL_INRACE]:
                    cmt = r[COL_COMMENT].decode(u'utf-8')
                sec.lines.append([
                    cmt, r[COL_BIB].decode(u'utf-8'),
                    r[COL_NAMESTR].decode(u'utf-8')
                ])
            ret.append(sec)
        return ret

    def callup_report(self):
        """Callup is startlist in this case"""
        return self.startlist_report()

    def startlist_report(self):
        """Return a startlist report."""
        # This is time trial - so generate a time specific startlist
        ret = []
        cnt = self.reorder_startlist()
        tcount = 0
        rcount = 0
        sec = None
        lcat = None
        ltod = None
        lteam = None
        for r in self.riders:
            rcount += 1
            rno = r[COL_BIB].decode('utf-8')
            rname = r[COL_SHORTNAME].decode('utf-8')
            rteam = r[COL_TEAM].decode(u'utf-8')
            rstart = r[COL_STOFT]
            ruci = None
            tuci = None
            if rstart is None:
                rstart = tod.MAX
            if rteam != lteam:  # issue team time
                ltod = None
                cs = r[COL_CAT].decode(u'utf-8')
                tcat = self.ridercat(riderdb.primary_cat(cs))
                if self.showuciids:
                    dbr = self.meet.rdb.getrider(rteam, u'team')
                    if dbr is not None:
                        tuci = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                if not tuci and tcat == u'':
                    tuci = cs

                if lcat != tcat:
                    tcount = 0
                    catname = u''
                    if sec is not None:
                        ret.append(sec)
                        pb = report.pagebreak()
                        ##pb.set_threshold(0.60)
                        ret.append(pb)
                    sec = report.rttstartlist(u'startlist')
                    sec.heading = u'Startlist'
                    dbr = self.meet.rdb.getrider(tcat, u'cat')
                    if dbr is not None:
                        catname = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_FIRST)  # already decode
                        if catname:
                            sec.heading += u' - ' + catname
                        subhead = self.meet.rdb.getvalue(dbr, riderdb.COL_LAST)
                        if subhead:
                            sec.subheading = subhead
                        footer = self.meet.rdb.getvalue(dbr, riderdb.COL_NOTE)
                        if footer:
                            sec.footer = footer
                lcat = tcat

                tname = rteam  # use key and only replace if avail
                if rteam in self.teamnames:
                    tname = self.teamnames[rteam]
                if ltod is not None and rstart - ltod > TEAMGAP:
                    sec.lines.append([])
                ltod = rstart
                cstr = u''
                tcount += 1
                tcodestr = rteam.upper()
                if rteam.isdigit():
                    tcodestr = None
                startStr = rstart.meridiem()
                if self.relativestart:
                    startStr = rstart.rawtime(0)
                sec.lines.append(
                    [startStr, tcodestr, tname, tuci, u'___', cstr])
                lteam = rteam
            if self.showriders:
                if self.showuciids:
                    dbr = self.meet.rdb.getrider(rno, self.series)
                    if dbr is not None:
                        ruci = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                sec.lines.append([None, rno, rname, ruci, None, None, None])
        ret.append(sec)
        return ret

    def analysis_report(self):
        """Return an analysis report."""
        # temporary fall through to camera report
        return self.camera_report()

    def reorder_arrivals(self):
        """Re-order the rider list according to arrival at finish line"""
        self.calcset = False
        aux = []
        cnt = 0
        for r in self.riders:
            # in the race?
            inField = True
            if not r[COL_INRACE]:
                inField = False

            comStr = r[COL_COMMENT]
            if r[COL_PLACE]:
                comStr = r[COL_PLACE]

            # assigned ranking
            rank = strops.dnfcode_key(comStr)

            # arrival at finish line
            arrivalTime = tod.MAX
            if inField and r[COL_RFTIME] is not None:
                arrivalTime = r[COL_RFTIME]

            # count of non-finish passings (reversed)
            lapCount = 0
            if inField:
                lapCount = -r[COL_LAPS]

            # last seen passing
            lastSeen = tod.MAX
            if inField and len(r[COL_RFSEEN]) > 0:
                lastSeen = r[COL_RFSEEN][-1]

            # team start time
            teamStart = tod.MAX
            if inField and r[COL_STOFT] is not None:
                teamStart = r[COL_STOFT]

            # rider number key
            riderNo = strops.riderno_key(r[COL_BIB].decode('utf-8'))

            aux.append((rank, arrivalTime, lapCount, lastSeen, teamStart,
                        riderNo, cnt))
            cnt += 1
        if len(aux) > 1:
            aux.sort()
            self.riders.reorder([a[6] for a in aux])
        return cnt

    def camera_report(self):
        """Return the judges (camera) report."""
        # Note: camera report treats all riders as a single blob
        ret = []
        self.recalculate()  # fill places and bunch info, order by arrival
        pthresh = self.meet.timer.photothresh()
        totcount = 0
        dnscount = 0
        dnfcount = 0
        fincount = 0
        lcomment = u''
        insertgap = True
        teamCount = {}
        teamFirstWheel = {}
        if self.timerstat != u'idle':
            sec = report.judgerep(u'judging')
            sec.heading = u'Judges Report'
            sec.colheader = [
                u'hit', u'team', u'rider', u'lap', u'time', u'arrival',
                u'passings'
            ]
            repStart = tod.ZERO
            sec.start = repStart
            if self.start is not None:
                repStart = self.start
            sec.finish = tod.tod('5.0')
            laptimes = (tod.tod('0.5'), tod.tod('1.5'), tod.tod('2.5'),
                        tod.tod('3.5'), tod.tod('4.5'))
            sec.laptimes = laptimes
            first = True
            ft = None
            lt = None
            lrf = None
            lplaced = None
            ltimed = None
            for r in self.riders:
                totcount += 1
                marker = u' '
                es = u''
                bs = u''
                pset = False
                placed = False
                timed = False
                photo = False
                catstart = None
                catfinish = None
                chevron = None
                rteam = r[COL_TEAM].decode('utf-8')
                rname = r[COL_SHORTNAME].decode('utf-8')
                rbib = r[COL_BIB].decode(u'utf-8')
                rid = u' '.join((rbib, rname))
                rcat = r[COL_CAT].decode(u'utf-8')
                ecat = self.ridercat(riderdb.primary_cat(rcat))
                laplist = []
                if r[COL_RFTIME] is not None:
                    if rteam not in teamFirstWheel:
                        # first arrival for this team
                        teamFirstWheel[rteam] = r[COL_RFTIME] - tod.tod(u'0.5')
                        teamCount[rteam] = 0
                    teamCount[rteam] += 1
                    catstart = teamFirstWheel[rteam]
                    catfinish = catstart + tod.tod(u'5.0')
                    for lt in r[COL_RFSEEN]:
                        if lt <= r[COL_RFTIME]:
                            laplist.append(lt)
                else:
                    # include all captured laps
                    laplist = r[COL_RFSEEN]
                if r[COL_INRACE]:
                    if r[COL_RFTIME] is not None:
                        timed = True
                    comment = unicode(totcount)
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if timed or bt is not None:
                        fincount += 1
                        if r[COL_PLACE] != u'':
                            # in trtt this is arrival
                            #comment = r[COL_PLACE] + u'.'
                            placed = True
                            pset = True

                        # format arrival time
                        if r[COL_RFTIME] is not None:
                            if not pset and lrf is not None:
                                if r[COL_RFTIME] - lrf < pthresh:
                                    photo = True
                                    if not sec.lines[-1][7]:  # not placed
                                        sec.lines[-1][8] = True
                            rstart = r[COL_STOFT] + repStart
                            et = r[COL_RFTIME] - rstart
                            es = et.rawtime(1)
                            lrf = r[COL_RFTIME]
                        else:
                            lrf = None

                        # format 'finish' time
                        bs = u''
                        if bt is not None:
                            if bt != self.teamtimes[rteam]:
                                chevron = u'\u21CE'
                                bs = bt.rawtime(1)
                            elif teamCount[rteam] < 10:
                                chevron = str(teamCount[rteam]) + u'\u20DD'
                                if rteam in self.teamnth:
                                    if teamCount[rteam] == self.teamnth[rteam]:
                                        bs = bt.rawtime(1)
                        # sep placed and unplaced
                        insertgap = False
                        if lplaced and placed != lplaced:
                            sec.lines.append([None, None, None])
                            sec.lines.append(
                                [None, None, u'Riders not yet confirmed'])
                            insertgap = True
                        lplaced = placed
                    else:
                        if r[COL_COMMENT].decode(u'utf-8').strip() != u'':
                            comment = r[COL_COMMENT].decode(u'utf-8').strip()
                        else:
                            comment = u'___'

                    # sep timed and untimed
                    if not insertgap and ltimed and ltimed != timed:
                        sec.lines.append([None, None, None])
                        sec.lines.append(
                            [None, None, u'Riders not seen at finish.'])
                        insertgap = True
                    ltimed = timed
                    sec.lines.append([
                        comment, rteam, rid,
                        str(r[COL_LAPS]), bs, es, laplist, placed, photo,
                        catstart, rcat, catfinish, chevron
                    ])
                else:
                    comment = r[COL_COMMENT].decode(u'utf-8')
                    if comment == u'':
                        comment = u'dnf'
                    if comment != lcomment:
                        sec.lines.append([None, None, None])
                    lcomment = comment
                    if comment == u'dns':
                        dnscount += 1
                    else:
                        dnfcount += 1
                    # format 'elapsed' rftime
                    es = None
                    if r[COL_RFTIME] is not None:  # eg for OTL
                        if self.start is not None:
                            es = (r[COL_RFTIME] - self.start).rawtime(2)
                        else:
                            es = r[COL_RFTIME].rawtime(2)
                    sec.lines.append([
                        comment, rteam, rid,
                        unicode(r[COL_LAPS]), None, es, laplist, True, False,
                        catstart, rcat, catfinish
                    ])
                first = False

            ret.append(sec)
            sec = report.section(u'judgesummary')
            sec.lines.append(
                [None, None, u'Total riders: ' + unicode(totcount)])
            sec.lines.append(
                [None, None, u'Did not start: ' + unicode(dnscount)])
            sec.lines.append(
                [None, None, u'Did not finish: ' + unicode(dnfcount)])
            sec.lines.append([None, None, u'Finishers: ' + unicode(fincount)])
            residual = totcount - (fincount + dnfcount + dnscount)
            if residual > 0:
                sec.lines.append(
                    [None, None, u'Unaccounted for: ' + unicode(residual)])
            if len(sec.lines) > 0:
                ret.append(sec)
        else:
            _log.warning(u'Event is idle, report not available')
        return ret

    def catresult_report(self):
        """Return a categorised race result report."""
        self.recalculate()
        ret = []
        for cat in self.cats:
            ret.extend(self.single_catresult(cat))

        # show all intermediates here
        for i in self.intermeds:
            im = self.intermap[i]
            if im[u'places'] and im['show']:
                ret.extend(self.int_report(i))

        if len(self.comment) > 0:
            s = report.bullet_text()
            s.heading = u'Decisions of the commissaires panel'
            for comment in self.comment:
                s.lines.append([None, comment])
            ret.append(s)

        return ret

    def single_catresult(self, cat):
        _log.debug(u'Cat result for cat=%r', cat)
        ret = []
        allin = False
        catname = cat  # fallback emergency
        if cat == u'':
            if len(self.cats) > 1:
                catname = u'Uncategorised Riders'
            else:
                # There is only one cat - so all riders are in it
                allin = True
        subhead = u''
        footer = u''
        distance = self.meet.get_distance()
        laps = self.totlaps
        if self.catlaps[cat] is not None:
            laps = self.catlaps[cat]
        dbr = self.meet.rdb.getrider(cat, u'cat')
        if dbr is not None:
            catname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
            subhead = self.meet.rdb.getvalue(dbr, riderdb.COL_LAST)
            footer = self.meet.rdb.getvalue(dbr, riderdb.COL_NOTE)
            dist = self.meet.rdb.getvalue(dbr, riderdb.COL_REFID)
            if dist:
                try:
                    distance = float(dist)
                except Exception:
                    _log.warning(u'Invalid distance %r for cat %r', dist, cat)
        sec = report.section(u'result-' + cat)

        teamRes = {}
        teamAux = []
        teamCnt = 0
        finCnt = 0

        # find all teams and riders in the chosen cat
        for r in self.riders:
            rcat = r[COL_CAT].decode(u'utf-8').upper()
            rcats = [u'']
            if rcat.strip():
                rcats = rcat.split()
            incat = False
            if allin or (cat and cat in rcats):
                incat = True  # rider is in this category
            elif not cat:  # is the rider uncategorised?
                incat = rcats[0] not in self.cats  # backward logic
            if incat:
                rteam = r[COL_TEAM].decode(u'utf-8')
                if rteam not in teamRes:
                    teamCnt += 1
                    teamRes[rteam] = {}
                    teamRes[rteam][u'time'] = None
                    teamRes[rteam][u'rlines'] = []
                    if rteam in self.teamtimes:
                        # this team has a finish time
                        finCnt += 1
                        auxTime = self.teamtimes[rteam]
                        tUci = u''
                        if self.showuciids:
                            tUci = self.teamuci[rteam]
                        teamAux.append((auxTime, teamCnt, rteam))
                        teamRes[rteam][u'time'] = auxTime
                        teamRes[rteam][u'tline'] = [
                            None, rteam, self.teamnames[rteam], tUci,
                            auxTime.rawtime(1), u''
                        ]
                rTime = u''
                rName = r[COL_SHORTNAME].decode(u'utf-8')
                rBib = r[COL_BIB].decode(u'utf-8')
                rCom = u''
                if r[COL_INRACE]:
                    if teamRes[rteam][u'time'] is not None:
                        bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                        if bt is not None and bt != teamRes[rteam][u'time']:
                            rDown = bt - teamRes[rteam][u'time']
                            rTime = u'[+' + rDown.rawtime(1) + u']'
                else:
                    rCom = r[COL_COMMENT].decode(u'utf-8')
                rUci = u''
                if self.showuciids:
                    dbr = self.meet.rdb.getrider(rBib, self.series)
                    if dbr is not None:
                        rUci = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                teamRes[rteam][u'rlines'].append(
                    [rCom, rBib, rName, rUci, rTime, u''])

        # sort, patch ranks and append result section
        teamAux.sort()
        first = True
        wt = None
        lt = None
        curPlace = 1
        tcnt = 0
        for t in teamAux:
            tcnt += 1
            team = t[2]
            teamTime = t[0]
            if teamTime != lt:
                curPlace = tcnt
            teamRank = str(curPlace) + u'.'
            downStr = u''
            if wt is None:
                wt = teamTime
            else:
                downStr = u'+' + (teamTime - wt).rawtime(1)
            teamRes[team][u'tline'][0] = teamRank
            teamRes[team][u'tline'][5] = downStr

            if self.showriders:
                if not first:
                    sec.lines.append([])
            first = False
            sec.lines.append(teamRes[team][u'tline'])
            if self.showriders:
                sec.lines.extend(teamRes[team][u'rlines'])

            lt = teamTime

        if self.timerstat == u'finished':
            sec.heading = u'Result'
        elif self.timerstat in [u'idle', u'armstart']:
            sec.heading = u''
        elif self.timerstat in [u'running', u'armfinish']:
            if teamCnt == finCnt:
                sec.heading = u'Provisional Result'
            elif finCnt > 0:
                sec.heading = u'Virtual Standing'
            else:
                sec.heading = u'Event in Progress'
        else:
            sec.heading = u'Provisional Result'
        if footer:
            sec.footer = footer

        # Append all result categories and uncat if riders
        if cat or finCnt > 0:
            ret.append(sec)
            rsec = sec
            # Race metadata / UCI comments
            sec = report.bullet_text(u'uci-' + cat)
            if wt is not None:
                if distance is not None:
                    sec.lines.append([
                        None, u'Average speed of the winner: ' +
                        wt.speedstr(1000.0 * distance)
                    ])
            sec.lines.append([None, u'Number of teams: ' + unicode(teamCnt)])
            #if dnfcount > 0:
            #sec.lines.append([
            #None,
            #u'Teams not completing the event: ' + unicode(dnfcount)
            #])
            #residual = totcount - (fincount + dnfcount + dnscount + hdcount)
            #if residual > 0:
            #_log.info(u'%r teams unaccounted for: %r', cat, residual)
            ret.append(sec)

            # finish report title manipulation
            if catname:
                cv = []
                if rsec.heading:
                    cv.append(rsec.heading)
                cv.append(catname)
                rsec.heading = u': '.join(cv)
                rsec.subheading = subhead
            ret.append(report.pagebreak())
        return ret

    def result_report(self):
        """Return a race result report"""
        return self.catresult_report()

    def stat_but_clicked(self, button=None):
        """Deal with a status button click in the main container"""
        _log.debug(u'Stat button clicked')

    def ctrl_change(self, acode=u'', entry=None):
        """Notify change in action combo."""
        if acode == u'fin':
            if entry is not None:
                entry.set_text(self.places)
        elif acode in self.intermeds:
            if entry is not None:
                entry.set_text(self.intermap[acode][u'places'])
        else:
            if entry is not None:
                entry.set_text(u'')

    def race_ctrl(self, acode='', rlist=''):
        """Apply the selected action to the provided bib list."""
        if acode in self.intermeds:
            if acode == u'brk':
                rlist = u' '.join(strops.riderlist_split(rlist))
                self.intsprint(acode, rlist)
            else:
                rlist = strops.reformat_placelist(rlist)
                if self.checkplaces(rlist, dnf=False):
                    self.intermap[acode][u'places'] = rlist
                    self.recalculate()
                    self.intsprint(acode, rlist)
                    _log.info(u'Intermediate %r == %r', acode, rlist)
                else:
                    _log.error('Intermediate %r not updated', acode)
            return False
        elif acode == u'fin':
            rlist = strops.reformat_placelist(rlist)
            if self.checkplaces(rlist):
                self.places = rlist
                self.recalculate()
                self.finsprint(rlist)
                return False
            else:
                _log.error(u'Places not updated')
                return False
        elif acode == u'dnf':
            self.dnfriders(strops.reformat_biblist(rlist))
            return True
        elif acode == u'dsq':
            self.dnfriders(strops.reformat_biblist(rlist), u'dsq')
            return True
        elif acode == u'otl':
            self.dnfriders(strops.reformat_biblist(rlist), u'otl')
            return True
        elif acode == u'wd':
            self.dnfriders(strops.reformat_biblist(rlist), u'wd')
            return True
        elif acode == u'dns':
            self.dnfriders(strops.reformat_biblist(rlist), u'dns')
            return True
        elif acode == u'ret':
            self.retriders(strops.reformat_biblist(rlist))
            return True
        elif acode == u'man':
            # crude hack tool for now
            self.manpassing(strops.reformat_bibserlist(rlist))
            return True
        elif acode == u'del':
            rlist = strops.riderlist_split(rlist, self.meet.rdb, self.series)
            for bib in rlist:
                self.delrider(bib)
            self.team_start_times()
            return True
        elif acode == u'add':
            rlist = strops.riderlist_split(rlist, self.meet.rdb, self.series)
            for bib in rlist:
                self.addrider(bib)
            self.team_start_times()
            return True
        elif acode == u'que':
            rlist = strops.reformat_biblist(rlist)
            if rlist != u'':
                for bib in rlist.split():
                    self.query_rider(bib)
            return True
        elif acode == u'com':
            self.add_comment(rlist)
            return True
        elif acode == u'run':
            team = rlist.strip()
            if team:
                self.running_team = team
            else:
                self.running_team = None
                self.meet.cmd_announce(command=u'teamtime', msg=u'')
                self.elaplbl.set_text(u'')
        else:
            _log.error(u'Ignoring invalid action %r', acode)
        return False

    def add_comment(self, comment=u''):
        """Append a commissaires comment."""
        self.comment.append(comment.strip())
        _log.info(u'Added comment: %r', comment)

    def query_rider(self, bib=None):
        """List info on selected rider in the scratchpad."""
        _log.info('Query rider: %r', bib)
        r = self.getrider(bib)
        if r is not None:
            ns = strops.truncpad(
                r[COL_NAMESTR].decode(u'utf-8') + u' ' +
                r[COL_CAT].decode(u'utf-8'), 30)
            bs = u''
            bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
            if bt is not None:
                bs = bt.timestr(0)
            ps = r[COL_COMMENT].decode(u'utf-8')
            if r[COL_PLACE] != '':
                ps = strops.rank2ord(r[COL_PLACE])
            _log.info(u'%s %s %s %s', bib, ns, bs, ps)
            lt = None
            if len(r[COL_RFSEEN]) > 0:
                for rft in r[COL_RFSEEN]:
                    nt = rft.truncate(0)
                    ns = rft.timestr(1)
                    ls = u''
                    if lt is not None:
                        ls = (nt - lt).timestr(0)
                    _log.info(u'\t%s %s', ns, ls)
                    lt = nt
            if r[COL_RFTIME] is not None:
                _log.info(u'Finish: %s', r[COL_RFTIME].timestr(1))
        else:
            _log.info(u'%r not in startlist', bib)

    def startlist_gen(self, cat=u''):
        """Generator function to export a startlist."""
        mcat = self.ridercat(cat)
        self.reorder_startlist()
        eventStart = tod.ZERO
        if self.start is not None:
            eventStart = self.start
        for r in self.riders:
            cs = r[COL_CAT].decode(u'utf-8')
            rcat = self.ridercat(riderdb.primary_cat(cs))
            if mcat == rcat:
                start = u''
                if r[COL_STOFT] is not None and r[COL_STOFT] != tod.ZERO:
                    start = (eventStart + r[COL_STOFT]).rawtime(0)
                bib = r[COL_BIB].decode(u'utf-8')
                series = self.series
                name = r[COL_NAMESTR].decode(u'utf-8')
                cat = rcat
                firstxtra = u''
                lastxtra = u''
                clubxtra = u''
                dbr = self.meet.rdb.getrider(bib, self.series)
                if dbr is not None:
                    firstxtra = self.meet.rdb.getvalue(
                        dbr, riderdb.COL_FIRST).capitalize()
                    lastxtra = self.meet.rdb.getvalue(
                        dbr, riderdb.COL_LAST).upper()
                    clubxtra = self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB)
                yield [
                    start, bib, series, name, cat, firstxtra, lastxtra,
                    clubxtra
                ]

    def result_gen(self, cat=u''):
        """Generator function to export a final result."""
        # This is for stage race export, each rider gets a stage ranking and
        # time - based on configuration. Rankings will be incomplete until
        # all riders arrival order is confirmed
        self.recalculate()
        mcat = self.ridercat(cat)
        rcount = 0
        cnt = 0
        aux = []
        for r in self.riders:
            cnt += 1
            rcat = r[COL_CAT].decode(u'utf-8').upper()
            rcats = [u'']
            if rcat.strip():
                rcats = rcat.split()
            if mcat == u'' or mcat in rcats:
                if mcat:
                    rcat = mcat
                else:
                    rcat = rcats[0]
                rcount += 1
                # this rider is 'in' the cat
                bib = r[COL_BIB].decode(u'utf-8')
                bonus = None
                ft = None
                crank = u''
                if r[COL_INRACE]:
                    # start offset is already accounted for in recalc
                    ft = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if r[COL_PLACE] and r[COL_PLACE].isdigit():
                        crank = r[COL_PLACE]
                else:
                    crank = r[COL_COMMENT]
                if (bib in self.bonuses or r[COL_BONUS] is not None):
                    bonus = tod.ZERO
                    if bib in self.bonuses:
                        bonus += self.bonuses[bib]
                    if r[COL_BONUS] is not None:
                        bonus += r[COL_BONUS]
                penalty = None
                if r[COL_PENALTY] is not None:
                    penalty = r[COL_PENALTY]
                indRank = strops.dnfcode_key(crank)
                ftRank = tod.MAX
                if r[COL_INRACE] and ft is not None:
                    ftRank = ft
                yrec = [crank, bib, ft, bonus, penalty]
                aux.append((ftRank, indRank, cnt, yrec))
        aux.sort()
        lrank = None
        crank = None
        cnt = 0
        for r in aux:
            cnt += 1
            yrec = r[3]
            if yrec[0].isdigit():
                if yrec[2] is not None:
                    if r[1] != lrank:
                        crank = cnt
                        lrank = r[1]
                    yrec[0] = crank
                else:
                    yrec[0] = None
            yield yrec

    def clear_results(self):
        """Clear all data from event model."""
        self.resetplaces()
        self.places = ''
        _log.debug('Cleared event result')
        for r in self.riders:
            r[COL_COMMENT] = u''
            r[COL_INRACE] = True
            r[COL_PLACE] = u''
            r[COL_LAPS] = 0
            r[COL_RFSEEN] = []
            r[COL_RFTIME] = None
            r[COL_CBUNCH] = None
            r[COL_MBUNCH] = None
        _log.debug(u'Clear rider data')

    def getrider(self, bib):
        """Return reference to selected rider no."""
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
            if self.riders.get_value(i, COL_BIB) == bib:
                break
            i = self.riders.iter_next(i)
        return i

    def delrider(self, bib='', series=''):
        """Remove the specified rider from the model."""
        i = self.getiter(bib)
        if i is not None:
            self.riders.remove(i)
        self.clear_place(bib)

    def starttime(self, start=None, bib='', series=''):
        """Adjust start time for the rider."""
        if series == self.series:
            r = self.getrider(bib)
            if r is not None:
                r[COL_STOFT] = start

    def addrider(self, bib='', series=None):
        """Add specified rider to race model."""
        if series is not None and series != self.series:
            _log.debug(u'Ignoring non-series rider: %r',
                       strops.bibser2bibstr(bib, series))
            return None
        if bib == u'' or self.getrider(bib) is None:
            nr = [
                bib, u'', u'', u'', u'', True, u'', 0, None, None, None,
                tod.ZERO, None, None, [], u''
            ]
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                nr[COL_NAMESTR] = strops.listname(
                    self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                    self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                    self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB))
                nr[COL_SHORTNAME] = strops.listname(
                    self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                    self.meet.rdb.getvalue(dbr, riderdb.COL_LAST))
                nr[COL_CAT] = self.meet.rdb.getvalue(dbr, riderdb.COL_CAT)
                nr[COL_TEAM] = self.meet.rdb.getvalue(
                    dbr, riderdb.COL_CLUB).upper()
            return self.riders.append(nr)
        else:
            return None

    def resettimer(self):
        """Reset race timer."""
        _log.info(u'Reset event to idle')
        self.meet.alttimer.dearm(1)
        self.set_start()
        self.clear_results()
        self.timerstat = u'idle'
        self.meet.cmd_announce(u'timerstat', u'idle')
        self.meet.stat_but.buttonchg(uiutil.bg_none, u'Idle')
        self.meet.stat_but.set_sensitive(True)
        self.elaplbl.set_text(u'')
        self.live_announce = True

    def armstart(self):
        """Process an armstart request."""
        if self.timerstat == u'idle':
            self.timerstat = u'armstart'
            self.meet.cmd_announce(u'timerstat', u'armstart')
            self.meet.stat_but.buttonchg(uiutil.bg_armint, u'Arm Start')
        elif self.timerstat == u'armstart':
            self.timerstat = u'idle'
            self.meet.cmd_announce(u'timerstat', u'idle')
            self.meet.stat_but.buttonchg(uiutil.bg_none, u'Idle')

    def armfinish(self):
        """Process an armfinish request."""
        if self.timerstat in [u'running', u'finished']:
            self.armlap()
            self.timerstat = u'armfinish'
            self.meet.cmd_announce(u'timerstat', u'armfinish')
            self.meet.stat_but.buttonchg(uiutil.bg_armfin, u'Arm Finish')
            self.meet.stat_but.set_sensitive(True)
            self.meet.alttimer.armlock(True)
            self.meet.alttimer.arm(1)
        elif self.timerstat == u'armfinish':
            self.meet.alttimer.dearm(1)
            self.timerstat = u'running'
            self.meet.cmd_announce(u'timerstat', u'running')
            self.meet.stat_but.buttonchg(uiutil.bg_armstart, u'Running')

    def last_rftime(self):
        """Find the last rider with a RFID finish time set."""
        ret = None
        for r in self.riders:
            if r[COL_RFTIME] is not None:
                ret = r[COL_BIB].decode(u'utf-8')
        return ret

    def armlap(self):
        ## announce text handling...
        self.scratch_map = {}
        self.scratch_ord = []
        titlestr = self.title_namestr.get_text()
        if self.live_announce:
            self.meet.cmd_announce(u'clear', u'all')
            self.meet.cmd_announce(u'title', titlestr)
        self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
        self.running_team = None
        self.meet.cmd_announce(command=u'teamtime', msg=u'')
        self.elaplbl.set_text(u'')

    def key_event(self, widget, event):
        """Handle global key presses in event."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort:  # override ctrl+f5
                    if uiutil.questiondlg(
                            self.meet.window, u'Reset event to idle?',
                            u'Note: All result and timing data will be cleared.'
                    ):
                        self.resettimer()
                    return True
                elif key == key_announce:  # re-send current announce vars
                    self.reannounce_times()
                    return True
                elif key == key_clearfrom:  # clear all places from selected
                    self.clear_places_from_selection()
                    return True
                elif key == key_clearplace:  # clear selected rider from places
                    self.clear_selected_place()
                    return True
            if key[0] == 'F':
                if key == key_announce:
                    if self.places:
                        self.finsprint(self.places)
                    else:
                        self.reannounce_lap()
                    return True
                elif key == key_placesto:
                    self.fill_places_to_selected()
                    return True
                elif key == key_appendplace:
                    self.append_selected_place()
                    return True
        return False

    def append_selected_place(self):
        sel = self.view.get_selection()
        sv = sel.get_selected()
        if sv is not None:
            i = sv[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            selpath = self.riders.get_path(i)
            _log.info(u'Confirmed next place: %r/%r', selbib, selpath)
            nplaces = []
            # remove selected rider from places
            for placegroup in self.places.split():
                gv = placegroup.split(u'-')
                try:
                    gv.remove(selbib)
                except Exception:
                    pass
                if gv:
                    nplaces.append(u'-'.join(gv))
            # append selected rider to places and recalc
            nplaces.append(selbib)
            self.places = u' '.join(nplaces)
            self.recalculate()
            # advance selection
            j = self.riders.iter_next(i)
            if j is not None:
                # note: set by selection doesn't adjust focus
                self.view.set_cursor_on_cell(self.riders.get_path(j))

    def fill_places_to_selected(self):
        """Update places to match ordering up to selected rider."""
        if u'-' in self.places:
            _log.warning(u'Dead heat in result, places not updated')
            return False
        sel = self.view.get_selection()
        sv = sel.get_selected()
        if sv is not None:
            # fill places and recalculate
            i = sv[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            selpath = self.riders.get_path(i)
            _log.info(u'Confirm places to: %r/%r', selbib, selpath)
            oplaces = self.places.split()
            nplaces = []
            for r in self.riders:
                rbib = r[COL_BIB].decode(u'utf-8')
                if rbib in oplaces:
                    oplaces.remove(rbib)  # strip out DNFed riders
                if r[COL_INRACE]:
                    nplaces.append(rbib)  # add to new list
                if rbib == selbib:  # break after to get sel rider
                    break
            nplaces.extend(oplaces)
            self.places = u' '.join(nplaces)
            self.recalculate()
            # advance selection
            j = self.riders.iter_next(i)
            if j is not None:
                # note: set by selection doesn't adjust focus
                self.view.set_cursor_on_cell(self.riders.get_path(j))

    def clear_places_from_selection(self):
        """Clear all places from riders following the current selection."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            selpath = self.riders.get_path(i)
            _log.info(u'Clear places from: %r/%r', selbib, selpath)
            nplaces = []
            found = False
            for placegroup in self.places.split():
                newgroup = []
                for r in placegroup.split(u'-'):
                    if r == selbib:
                        found = True
                        break
                    newgroup.append(r)
                if newgroup:
                    nplaces.append(u'-'.join(newgroup))
                if found:
                    break
            self.places = u' '.join(nplaces)
            self.recalculate()

    def clear_place(self, bib):
        nplaces = []
        for placegroup in self.places.split():
            gv = placegroup.split(u'-')
            try:
                gv.remove(bib)
            except Exception:
                pass
            if gv:
                nplaces.append(u'-'.join(gv))
        self.places = u' '.join(nplaces)

    def clear_selected_place(self):
        """Remove the selected rider from places."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            selpath = self.riders.get_path(i)
            _log.info(u'Clear rider from places: %r/%r', selbib, selpath)
            self.clear_place(selbib)
            self.recalculate()

    def dnfriders(self, biblist='', code='dnf'):
        """Remove each rider from the event with supplied code."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                if code != u'wd':
                    r[COL_INRACE] = False
                r[COL_COMMENT] = code
                recalc = True
                _log.info(u'Rider %r did not finish with code: %r', bib, code)
            else:
                _log.warning(u'Unregistered Rider %r unchanged', bib)
        if recalc:
            self.recalculate()
        return False

    def manpassing(self, biblist=u''):
        """Register a manual passing, preserving order."""
        for bib in biblist.split():
            rno, rser = strops.bibstr2bibser(bib)
            if not rser:  # allow series manual override
                rser = self.series
            bibstr = strops.bibser2bibstr(rno, rser)
            t = tod.now()
            t.chan = u'MAN'
            t.refid = u'riderno:' + bibstr
            self.meet._timercb(t)
            _log.debug(u'Manual passing: %r', bibstr)

    def retriders(self, biblist=''):
        """Return all listed riders to the event."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[COL_INRACE] = True
                r[COL_COMMENT] = u''
                r[COL_LAPS] = len(r[COL_RFSEEN])
                recalc = True
                _log.info(u'Rider %r returned to event', bib)
            else:
                _log.warning(u'Unregistered rider %r unchanged', bib)
        if recalc:
            self.recalculate()
        return False

    def shutdown(self, win=None, msg='Race Sutdown'):
        """Close event."""
        _log.debug(u'Event shutdown: %r', msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def starttrig(self, e):
        """Process a 'start' trigger signal."""
        if self.timerstat == u'armstart':
            _log.info(u'Start trigger: %s@%s/%s', e.chan, e.rawtime(),
                      e.source)
            self.set_start(e)
        else:
            _log.info(u'Trigger: %s@%s/%s', e.chan, e.rawtime(), e.source)
        return False

    def alttimertrig(self, e):
        """Record timer message from alttimer."""
        # note: these impulses are sourced from alttimer device and keyboard
        #       transponder triggers are collected separately in timertrig()
        _log.debug(u'Alt timer: %s@%s/%s', e.chan, e.rawtime(), e.source)
        channo = strops.chan2id(e.chan)
        if channo == 1:
            # this is a finish impulse, treat as an n'th wheel indicator
            if self.timerstat == u'armfinish':
                _log.info(u'Team finish: %s', e.rawtime())
        else:
            # send through to catch-all trigger handler
            self.starttrig(e)

    def timertrig(self, e):
        """Process transponder passing event."""

        # all impulses from transponder timer are considered start triggers
        if e.refid in [u'', u'255']:
            return self.starttrig(e)

        # fetch rider data from riderdb using refid lookup
        r = self.meet.rdb.getrefid(e.refid)
        if r is None:
            _log.info(u'Unknown rider: %s:%s@%s/%s', e.refid, e.chan,
                      e.rawtime(2), e.source)
            return False

        bib = self.meet.rdb.getvalue(r, riderdb.COL_BIB)
        ser = self.meet.rdb.getvalue(r, riderdb.COL_SERIES)
        if ser != self.series:
            _log.info(u'Non-series rider: %s.%s', bib, ser)
            return False

        # fetch rider info from event
        lr = self.getrider(bib)
        if lr is None:
            _log.info(u'Non-starter: %s:%s@%s/%s', bib, e.chan, e.rawtime(2),
                      e.source)
            return False

        # log passing of rider before further processing
        if not lr[COL_INRACE]:
            _log.warning(u'Withdrawn rider: %s:%s@%s/%s', bib, e.chan,
                         e.rawtime(2), e.source)
            # but continue as if still in race
        else:
            _log.info(u'Saw: %s:%s@%s/%s', bib, e.chan, e.rawtime(2), e.source)

        # check run state
        if self.timerstat in [u'idle', u'armstart', u'finished']:
            return False

        # fetch primary category from event
        cs = lr[COL_CAT].decode(u'utf-8')
        rcat = self.ridercat(riderdb.primary_cat(cs))

        # check for start and minimum passing time
        st = tod.ZERO
        catstart = tod.ZERO
        if lr[COL_STOFT] is not None:
            # start offset in riders model overrides cat start
            catstart = lr[COL_STOFT]
        #elif rcat in self.catstarts and self.catstarts[rcat] is not None:
        #catstart = self.catstarts[rcat]
        if self.start is not None:
            st = self.start + catstart + self.minlap
        #_log.debug('%r: cat=%r, st=%s, catstart=%s, pass=%r', bib, rcat,
        #st.rawtime(), catstart.rawtime(), e.rawtime())
        # ignore all passings from before first allowed time
        if e <= st:
            _log.info(u'Ignored early passing: %s:%s@%s/%s < %s', bib, e.chan,
                      e.rawtime(2), e.source, st.rawtime(2))
            return False

        # check this passing against previous passing records
        # TODO: compare the source against the passing in question
        lastchk = None
        ipos = bisect.bisect_right(lr[COL_RFSEEN], e)
        if ipos == 0:  # first in-race passing, accept
            pass
        else:  # always one to the 'left' of e
            # check previous passing for min lap time
            lastseen = lr[COL_RFSEEN][ipos - 1]
            nthresh = lastseen + self.minlap
            if e <= nthresh:
                _log.info(u'Ignored short lap: %s:%s@%s/%s < %s', bib, e.chan,
                          e.rawtime(2), e.source, nthresh.rawtime(2))
                return False
            # check the following passing if it exists
            if len(lr[COL_RFSEEN]) > ipos:
                npass = lr[COL_RFSEEN][ipos]
                delta = npass - e
                if delta <= self.minlap:
                    _log.info(u'Spurious passing: %s:%s@%s/%s < %s', bib,
                              e.chan, e.rawtime(2), e.source, npass.rawtime(2))
                    return False

        # insert this passing in order
        lr[COL_RFSEEN].insert(ipos, e)

        # check if lap mode is target-based
        lapfinish = False
        targetlap = None
        if self.targetlaps:
            # category laps override event laps
            if rcat in self.catlaps and self.catlaps[rcat]:
                targetlap = self.catlaps[rcat]
            else:
                targetlap = self.totlaps
            if targetlap and lr[COL_LAPS] >= targetlap - 1:
                lapfinish = True  # arm just this rider

        # finishing rider path
        if self.timerstat == u'armfinish' or lapfinish:
            if lr[COL_RFTIME] is None:
                if lr[COL_COMMENT] != u'wd':
                    if lr[COL_PLACE] == u'':
                        lr[COL_RFTIME] = e
                        self._dorecalc = True
                    else:
                        _log.error(u'Placed rider seen at finish: %s:%s@%s/%s',
                                   bib, e.chan, e.rawtime(2), e.source)
                    if lr[COL_INRACE]:
                        lr[COL_LAPS] += 1
                        self.announce_rider(u'', bib,
                                            lr[COL_NAMESTR].decode(u'utf-8'),
                                            lr[COL_CAT].decode(u'utf-8'), e)
            else:
                _log.info(u'Duplicate finish rider %s:%s@%s/%s', bib, e.chan,
                          e.rawtime(2), e.source)
        # end finishing rider path

        # lapping rider path
        elif self.timerstat in [u'running']:  # not finished, not armed
            self._dorecalc = True
            if lr[COL_INRACE] and (lr[COL_PLACE] or lr[COL_CBUNCH] is None):
                # rider in the race, not yet finished: increment own lap count
                lr[COL_LAPS] += 1

                # announce all rider passings
                self.announce_rider(u'', bib, lr[COL_NAMESTR].decode(u'utf-8'),
                                    lr[COL_CAT].decode(u'utf-8'), e)
        return False

    def announce_rider(self, place, bib, namestr, cat, rftime):
        """Log a rider in the lap and emit to announce."""
        if bib not in self.scratch_map:
            self.scratch_map[bib] = rftime
            self.scratch_ord.append(bib)
        if self.live_announce:
            glib.idle_add(self.meet.rider_announce,
                          [place, bib, namestr, cat,
                           rftime.rawtime()])

    def finsprint(self, places):
        """Display a final sprint 'provisional' result."""

        self.live_announce = False
        self.meet.cmd_announce(u'clear', u'all')
        scrollmsg = u'Provisional - '
        self.meet.cmd_announce(u'title', u'Provisional Result')
        self.meet.cmd_announce(u'bunches', u'final')
        placeset = set()
        idx = 0
        st = tod.tod('0')
        if self.start is not None:
            st = self.start
        # result is sent in weird hybrid units TODO: fix the amb
        wt = None
        lb = None
        for placegroup in places.split():
            curplace = idx + 1
            for bib in placegroup.split('-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is not None:
                        ft = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                        fs = ''
                        if ft is not None:
                            # temp -> just use the no-blob style to correct
                            fs = ft.rawtime()
                            if wt is None:
                                wt = ft
                            lb = ft
                        scrollmsg += (u' ' + r[COL_PLACE] + u'. ' +
                                      r[COL_NAMESTR].decode('utf-8') + u' ')
                        glib.idle_add(self.meet.rider_announce, [
                            r[COL_PLACE] + '.', bib, r[COL_NAMESTR],
                            r[COL_CAT], fs
                        ])
                    idx += 1
        self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        if wt is not None:
            pass

    def int_report(self, acode):
        """Return report sections for the named intermed."""
        ret = []
        if acode not in self.intermeds:
            _log.debug(u'Attempt to read non-existent inter: %r', acode)
            return ret
        descr = acode
        if self.intermap[acode][u'descr']:
            descr = self.intermap[acode][u'descr']
        places = self.intermap[acode][u'places']
        lines = []
        placeset = set()
        idx = 0
        dotime = False
        if u'time' in self.intermap[acode][u'descr'].lower():
            dotime = True
        for placegroup in places.split():
            curplace = idx + 1
            for bib in placegroup.split(u'-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is not None:
                        cs = r[COL_CAT].decode(u'utf-8')
                        rcat = self.ridercat(riderdb.primary_cat(cs))
                        xtra = None
                        if dotime:
                            bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                            if bt is not None:
                                st = self.getstart(r)
                                if st is not None:
                                    bt = bt - st
                                xtra = bt.rawtime(0)
                        lines.append([
                            unicode(curplace) + u'.', bib,
                            r[COL_NAMESTR].decode(u'utf-8'), rcat, None, xtra
                        ])
                    idx += 1
                else:
                    _log.warning(u'Duplicate no. %r in places', bib)
        if len(lines) > 0:
            sec = report.section(u'inter' + acode)
            sec.heading = descr
            sec.lines = lines
            ret.append(sec)
        return ret

    def intsprint(self, acode='', places=''):
        """Display an intermediate sprint 'provisional' result."""

        ## TODO : Fix offset time calcs - too many off by ones
        if acode not in self.intermeds:
            _log.debug(u'Attempt to display non-existent inter: %r', acode)
            return
        descr = acode
        if self.intermap[acode]['descr']:
            descr = self.intermap[acode]['descr']
        self.live_announce = False
        self.meet.cmd_announce(u'clear', u'all')
        self.reannounce_times()
        self.meet.cmd_announce(u'title', descr)
        scrollmsg = descr + u' - '
        placeset = set()
        idx = 0
        for placegroup in places.split():
            curplace = idx + 1
            for bib in placegroup.split('-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is not None:
                        scrollmsg += (u' ' + str(curplace) + u'. ' +
                                      r[COL_NAMESTR].decode('utf-8') + u' ')
                        glib.idle_add(self.meet.rider_announce, [
                            str(curplace) + '.', bib, r[COL_NAMESTR],
                            r[COL_CAT], ''
                        ])
                    idx += 1
                else:
                    _log.warn('Duplicate no. %s in places', bib)
        self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        glib.timeout_add_seconds(15, self.reannounce_lap)

    def todempty(self, val):
        if val is not None:
            return val.rawtime()
        else:
            return u''

    def reannounce_times(self):
        """Re-send the current timing values."""
        # TODO
        self.meet.cmd_announce(u'timerstat', self.timerstat)
        return False

    def reannounce_lap(self):
        """Re-send current lap passing data."""
        self.meet.cmd_announce(u'clear', u'all')
        self.meet.cmd_announce(u'scrollmsg', None)
        self.reannounce_times()
        self.live_announce = False
        if self.timerstat == u'armfinish':
            self.meet.cmd_announce(u'title', u'Finish')
        else:
            self.meet.cmd_announce(u'title', self.title_namestr.get_text())
        for bib in self.scratch_ord:
            r = self.getrider(bib)
            if r is not None:
                glib.idle_add(self.meet.rider_announce, [
                    u'', bib, r[COL_NAMESTR].decode(u'utf-8'),
                    r[COL_CAT].decode(u'utf-8'),
                    self.scratch_map[bib].rawtime()
                ])
        self.live_announce = True
        return False

    def timeout(self):
        """Update elapsed time and recalculate if required."""
        if not self.winopen:
            return False
        if self._dorecalc:
            self.recalculate()
            if self.autoexport:
                glib.idle_add(self.meet.menu_data_results_cb, None)
        if self.running_team is not None:
            # bounce a running time onto the panel
            self.bounceruntime(self.running_team, u'')
        return True

    def set_start(self, start=''):
        """Set the start time."""
        if isinstance(start, tod.tod):
            self.start = start
        else:
            self.start = tod.mktod(start)
        if self.start is not None:
            self.set_running()

    def set_running(self):
        """Update event status to running."""
        self.timerstat = u'running'
        self.meet.cmd_announce(u'timerstat', u'running')
        self.meet.stat_but.buttonchg(uiutil.bg_armstart, u'Running')

    def set_finished(self):
        """Update event status to finished."""
        self.timerstat = u'finished'
        self.meet.cmd_announce(u'timerstat', u'finished')
        self.meet.cmd_announce(u'laplbl', None)
        self.meet.stat_but.buttonchg(uiutil.bg_none, u'Finished')
        self.meet.stat_but.set_sensitive(False)

    def info_time_edit_clicked_cb(self, button, data=None):
        """Run an edit times dialog to update event time."""
        st = u''
        if self.start is not None:
            st = self.start.rawtime(2)
        ft = u'[n/a]'
        ret = uiutil.edit_times_dlg(self.meet.window,
                                    stxt=st,
                                    ftxt=ft,
                                    finish=False)
        if ret[0] == 1:
            wasrunning = self.timerstat in [u'running', u'armfinish']
            self.set_start(ret[1])
            if wasrunning:
                # flag a recalculate
                self._dorecalc = True
            _log.info(u'Adjusted event times')

    def editcol_cb(self, cell, path, new_text, col):
        """Edit column callback."""
        new_text = new_text.decode(u'utf-8').strip()
        self.riders[path][col] = new_text

    def editlap_cb(self, cell, path, new_text, col):
        """Edit the lap field if valid."""
        new_text = new_text.decode(u'utf-8').strip()
        if new_text == u'?':
            self.riders[path][col] = len(self.riders[path][COL_RFSEEN])
        elif new_text.isdigit():
            self.riders[path][col] = int(new_text)
        else:
            _log.error(u'Invalid lap count')

    def resetplaces(self):
        """Clear places off all riders."""
        for r in self.riders:
            r[COL_PLACE] = u''
        self.bonuses = {}  # bonuses are global to stage
        for c in self.tallys:  # points are grouped by tally
            self.points[c] = {}
            self.pointscb[c] = {}

    def sortteams(self, x, y):
        """Roughly sort into team groups - all teams have same start time?"""
        #             0    1   2      3       4     5
        # aux cols: ind, stoft, in, place, rftime, laps

        if x[1] != y[1]:  # same team?
            return cmp(x[1], y[1])
        else:
            if x[2] != y[2]:  # both in?
                if x[2]:
                    return -1
                else:
                    return 1
            else:
                if x[4] != y[4]:  # same time?
                    return cmp(x[4], y[4])
                else:
                    return cmp(x[3], y[3])  # last resort is 'place'
        return 0

    # do final sort on manual places then manual bunch entries
    def sortvbunch(self, x, y):
        # aux cols: ind, bib, in, place, vbunch, comment
        #             0    1   2      3       4        5
        if x[2] != y[2]:  # in the race?
            if x[2]:  # return bool comparison equiv
                return -1
            else:
                return 1
        else:
            if x[2]:  # in the race...
                if x[3] != y[3]:  # places not same?
                    if y[3] == u'':
                        return -1
                    elif x[3] == u'':
                        return 1
                    if int(x[3]) < int(y[3]):
                        return -1
                    else:
                        return 1
                else:
                    if x[4] == y[4]:  # same time?
                        return 0
                    else:
                        if y[4] is None:
                            return -1
                        elif x[4] is None:
                            return 1
                        elif x[4] < y[4]:
                            return -1
                        else:
                            return 1
            else:  # not in the race
                if x[5] != y[5]:
                    return strops.cmp_dnf(x[5], y[5])  # sort by code
                else:
                    return cmp(strops.riderno_key(x[1]),
                               strops.riderno_key(y[1]))  # sort on no
        return 0

    def vbunch(self, cbunch=None, mbunch=None):
        """Switch to return best choice bunch time."""
        ret = None
        if mbunch is not None:
            ret = mbunch
        elif cbunch is not None:
            ret = cbunch
        return ret

    def showstart_cb(self, col, cr, model, iter, data=None):
        """Draw start time offset in rider view."""
        st = model.get_value(iter, COL_STOFT)
        otxt = u''
        if st is not None:
            otxt = st.rawtime(0)
        cr.set_property(u'text', otxt)

    def edit_event_properties(self, window, data=None):
        """Edit event specifics."""
        _log.warning(u'Edit event properties not implemented')

    def getbunch_iter(self, iter):
        """Return a 'bunch' string for the rider."""
        cmt = self.riders.get_value(iter, COL_COMMENT).decode(u'utf-8')
        place = self.riders.get_value(iter, COL_PLACE).decode(u'utf-8')
        lap = self.riders.get_value(iter, COL_LAPS)
        cb = self.riders.get_value(iter, COL_CBUNCH)
        mb = self.riders.get_value(iter, COL_MBUNCH)
        tv = u''
        if mb is not None:
            tv = mb.rawtime(0)
        else:
            if cb is not None:
                tv = cb.rawtime(0)
            else:
                # just show event elapsed in this path
                seen = self.riders.get_value(iter, COL_RFSEEN)
                if len(seen) > 0:
                    et = seen[-1]
                    if self.start:
                        et -= self.start
                    tv = u'[' + et.rawtime(1) + u']'
        rv = []
        if place:
            rv.append(u'{}.'.format(place))
        elif cmt:
            rv.append(cmt)
        if lap > 0:
            rv.append(u'Lap:{}'.format(lap))
        if tv:
            rv.append(tv)
        return u' '.join(rv)

    def showbunch_cb(self, col, cr, model, iter, data=None):
        """Update bunch time on rider view."""
        cb = model.get_value(iter, COL_CBUNCH)
        mb = model.get_value(iter, COL_MBUNCH)
        if mb is not None:
            cr.set_property(u'text', mb.rawtime(1))
            cr.set_property(u'style', pango.STYLE_OBLIQUE)
        else:
            cr.set_property(u'style', pango.STYLE_NORMAL)
            if cb is not None:
                cr.set_property(u'text', cb.rawtime(1))
            else:
                seen = model.get_value(iter, COL_RFSEEN)
                st = model.get_value(iter, COL_STOFT)
                if self.start is not None:
                    if len(seen) > 0:
                        et = seen[-1] - st - self.start
                        cr.set_property(u'text', u'[' + et.rawtime(1) + u']')
                        cr.set_property(u'style', pango.STYLE_OBLIQUE)
                    else:
                        cr.set_property(u'text', u'')
                else:
                    cr.set_property(u'text', u'')

    def editstart_cb(self, cell, path, new_text, col=None):
        """Edit start time on rider view."""
        newst = tod.mktod(new_text)
        if newst:
            newst = newst.truncate(0)
        self.riders[path][COL_STOFT] = newst

    def editbunch_cb(self, cell, path, new_text, col=None):
        """Edit bunch time on rider view."""
        new_text = new_text.strip()
        dorecalc = False
        if new_text == u'':  # user request to clear RFTIME?
            self.riders[path][COL_RFTIME] = None
            self.riders[path][COL_MBUNCH] = None
            self.riders[path][COL_CBUNCH] = None
            dorecalc = True
        else:
            # get 'current bunch time'
            omb = self.vbunch(self.riders[path][COL_CBUNCH],
                              self.riders[path][COL_MBUNCH])

            # assign new bunch time
            # note: ttt does not use + times or cascade
            nmb = tod.mktod(new_text)
            if self.riders[path][COL_MBUNCH] != nmb:
                self.riders[path][COL_MBUNCH] = nmb
                dorecalc = True
        if dorecalc:
            self.recalculate()

    def checkplaces(self, rlist=u'', dnf=True):
        """Check the proposed places against current race model."""
        ret = True
        placeset = set()
        for no in strops.reformat_biblist(rlist).split():
            if no != u'x':
                # repetition? - already in place set?
                if no in placeset:
                    _log.error(u'Duplicate no in places: %r', no)
                    ret = False
                placeset.add(no)
                # rider in the model?
                lr = self.getrider(no)
                if lr is None:
                    _log.error(u'Non-starter in places: %r', no)
                    ret = False
                else:
                    # rider still in the race?
                    if not lr[COL_INRACE]:
                        _log.info(u'DNF/DNS rider in places: %r', no)
                        if dnf:
                            ret = False
            else:
                # placeholder needs to be filled in later or left off
                _log.info(u'Placeholder in places')
        return ret

    def recalculate(self):
        """Recalculator, acquires lock and then continues."""
        if not self.recalclock.acquire(False):
            _log.warn(u'Recalculate already in progress')
            return None  # allow only one entry
        try:
            self._recalc()
        except Exception as e:
            _log.error(u'%s recalculating result: %s', e.__class__.__name__, e)
        finally:
            self._dorecalc = False
            self.recalclock.release()

    def rider_in_cat(self, bib, cat):
        """Return True if rider is in nominated category."""
        ret = False
        r = self.getrider(bib)
        if cat and r is not None:
            cv = r[COL_CAT].decode('utf-8').upper().split()
            ret = cat.upper() in cv
        return ret

    def get_cat_placesr(self, cat):
        """Return a normalised place str for a cat within main places."""
        placestr = self.places
        pgroups = []
        lp = None
        ng = []
        for placegroup in placestr.split():
            cg = []
            for bib in placegroup.split(u'-'):
                if self.rider_in_cat(bib, cat):
                    cg.append(bib)
            if len(cg) > 0:  # >= one cat rider in this group
                pgroups.append(u'-'.join(cg))

        ret = u' '.join(pgroups)
        _log.debug(u'Cat %r finish: %r', cat, ret)
        return ret

    def assign_finish(self):
        """Transfer finish line places into rider model."""
        placestr = self.places
        placeset = set()
        idx = 0
        for placegroup in placestr.split():
            curplace = idx + 1
            for bib in placegroup.split(u'-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is None:
                        self.addrider(bib)
                        _log.warning(u'Added rider %r from finish places', bib)
                        r = self.getrider(bib)
                    if r[COL_INRACE]:
                        idx += 1
                        r[COL_PLACE] = unicode(curplace)
                    else:
                        _log.warning(u'DNF Rider %r in finish places', bib)
                else:
                    _log.warning(u'Duplicate no. %r in finish places', bib)

    def assign_places(self, contest):
        """Transfer points and bonuses into the named contest."""
        # fetch context meta infos
        src = self.contestmap[contest][u'source']
        if src not in RESERVED_SOURCES and src not in self.intermeds:
            _log.info(u'Invalid inter source %r in contest %r', src, contest)
            return
        countbackwinner = False  # for stage finish only track winner in cb
        category = self.contestmap[contest][u'category']
        tally = self.contestmap[contest][u'tally']
        bonuses = self.contestmap[contest][u'bonuses']
        points = self.contestmap[contest][u'points']
        allsrc = self.contestmap[contest][u'all_source']
        allpts = 0
        allbonus = tod.ZERO
        if allsrc:
            if len(points) > 0:
                allpts = points[0]
            if len(bonuses) > 0:
                allbonus = bonuses[0]
        placestr = u''
        if src == u'fin':
            placestr = self.places
            if tally in [u'sprint', u'crit']:  # really only for sprints/crits
                countbackwinner = True
        elif src == u'reg':
            placestr = self.get_startlist()
        elif src == u'start':
            placestr = self.get_starters()
        elif src in self.catplaces:  # ERROR -> cat climb tally needs type?
            placestr = self.get_cat_placesr(self.catplaces[src])
            countbackwinner = True
        else:
            placestr = self.intermap[src][u'places']
        placeset = set()
        idx = 0
        for placegroup in placestr.split():
            curplace = idx + 1
            for bib in placegroup.split(u'-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is None:
                        self.addrider(bib)
                        r = self.getrider(bib)
                    idx += 1
                    if allsrc:  # all listed places get same pts/bonus..
                        if allbonus is not tod.ZERO:
                            if bib in self.bonuses:
                                self.bonuses[bib] += allbonus
                            else:
                                self.bonuses[bib] = allbonus
                        if tally and tally in self.points and allpts != 0:
                            if bib in self.points[tally]:
                                self.points[tally][bib] += allpts
                            else:
                                self.points[tally][bib] = allpts
                                self.pointscb[tally][
                                    bib] = countback.countback()
                            # No countback for all_source entries
                    else:  # points/bonus as per config
                        if len(bonuses) >= curplace:  # bonus is vector
                            if bib in self.bonuses:
                                self.bonuses[bib] += bonuses[curplace - 1]
                            else:
                                self.bonuses[bib] = bonuses[curplace - 1]
                        if tally and tally in self.points:
                            if len(points) >= curplace:  # points vector
                                if bib in self.points[tally]:
                                    self.points[tally][bib] += points[curplace
                                                                      - 1]
                                else:
                                    self.points[tally][bib] = points[curplace -
                                                                     1]
                            if bib not in self.pointscb[tally]:
                                self.pointscb[tally][
                                    bib] = countback.countback()
                            if countbackwinner:  # stage finish
                                if curplace == 1:  # winner only at finish
                                    self.pointscb[tally][bib][0] += 1
                            else:  # intermediate/other
                                if tally == u'climb':  # climbs countback on category winners only
                                    if curplace == 1:
                                        self.pointscb[tally][bib][
                                            category] += 1
                                else:
                                    self.pointscb[tally][bib][curplace] += 1
                else:
                    _log.warning(u'Duplicate no. %r in %r places', bib,
                                 contest)

    def bounceteam(self, team, cat, time):
        """Bounce a teamname and time onto the panel"""
        team = team.upper()
        tname = u''
        tcat = self.ridercat(cat)
        # lookup team name in rdb
        dbr = self.meet.rdb.getrider(team, u'team')
        if dbr is not None:
            tname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
        tstr = time.rawtime(1) + u' '  # hunges blanked
        self.meet.cmd_announce(command=u'teamtime',
                               msg=u'\x1f'.join(
                                   (tcat, team.upper(), tname, u'', tstr)))
        self.elaplbl.set_text(u'%s: %s' % (team.upper(), tstr))
        return False

    def bounceruntime(self, team, cat):
        """Bounce a teamname and running time onto the panel"""
        team = team.upper()
        tname = u''
        tcat = self.ridercat(cat)
        tstr = u''
        # lookup team name in rdb
        dbr = self.meet.rdb.getrider(team, u'team')
        if dbr is not None:
            tname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
        if team in self.teamtimes:
            tstr = self.teamtimes[team].rawtime(1) + u' '
        else:
            tstart = tod.mktod(self.meet.rdb.getvalue(dbr, riderdb.COL_REFID))
            if tstart is not None:
                tstr = (tod.now() - tstart).rawtime(0)
        self.meet.cmd_announce(command=u'teamtime',
                               msg=u'\x1f'.join(
                                   (tcat, team.upper(), tname, u'', tstr)))
        self.elaplbl.set_text(u'%s: %s' % (team.upper(), tstr))
        return False

    def decode_limit(self, limitstr, elap=None):
        """Decode a limit and finish time into raw bunch time."""
        ret = None
        if limitstr:
            limit = None
            down = False
            if u'+' in limitstr:
                down = True
                limitstr = limitstr.replace(u'+', u'')
            if u'%' in limitstr:
                down = True
                if elap is not None:
                    try:
                        frac = 0.01 * float(limitstr.replace(u'%', u''))
                        limit = tod.tod(int(frac * float(elap.as_seconds())))
                    except Exception:
                        pass
            else:  # assume tod without sanity check
                limit = tod.mktod(limitstr)
                if limit is not None:
                    if elap is not None and limit < elap:
                        down = True  # assume a time less than winner is down
                    else:  # assume raw bunch time, ignore elap
                        pass

            # assign limit discovered above, if possible
            if limit is not None:
                if down:
                    if elap is not None:
                        ret = elap + limit  # down time on finish
                        ret = ret.truncate(0)
                else:
                    ret = limit.truncate(0)  # raw bunch time
            if ret is None:
                _log.warning(u'Unable to decode time limit: %r', limitstr)
        return ret

    def _recalc(self):
        """Internal 'protected' recalculate function."""
        # if readonly and calc set - skip recalc
        if self.readonly and self.calcset:
            _log.debug(u'Cached Recalculate')
            return False

        # protect:
        if self.start is None:
            return

        _log.debug('Recalculate model')
        # pass one: clear off old places and bonuses
        self.resetplaces()
        self.teamtimes = {}

        # pass two: assign places
        self.assign_finish()
        for c in self.contests:
            self.assign_places(c)

        # for teams time trial...

        # pass three, order in team groups
        auxtbl = []
        idx = 0
        for r in self.riders:
            # aux cols: ind, stoft(team), in, place, rftime, laps
            auxtbl.append([
                idx, r[COL_STOFT], r[COL_INRACE], r[COL_PLACE], r[COL_RFTIME],
                r[COL_LAPS]
            ])
            idx += 1
        if len(auxtbl) > 1:
            auxtbl.sort(self.sortteams)
            self.riders.reorder([a[0] for a in auxtbl])

        # re-build cached team map
        cteam = None
        self.teammap = {}
        for r in self.riders:
            nteam = r[COL_TEAM]
            if nteam != cteam:
                # only rebuild cat and nth on first load
                if nteam not in self.teamcats:
                    ncat = self.ridercat(r[COL_CAT])
                    nth = self.defaultnth  # overridden by cat
                    if ncat in self.nthwheel:
                        try:
                            nth = int(self.nthwheel[ncat])
                            _log.debug('%s: %r nth wheel = %r', ncat, nteam,
                                       nth)
                        except Exception as e:
                            _log.warn(
                                u'%s: %r invalid nth wheel %r set to: %r',
                                ncat, nteam, self.nthwheel[ncat], nth)
                    self.teamnth[nteam] = nth
                    self.teamcats[nteam] = ncat
                self.teammap[nteam] = []
                cteam = nteam
            # cteam will be valid at this point
            if r[COL_RFTIME] is not None:  # will already be sorted!
                self.teammap[cteam].append(r)
                if r[COL_RFTIME] > self.maxfinish:
                    self.maxfinish = r[COL_RFTIME]

        # scan each team for times
        for t in self.teammap:
            # unless team has n finishers, there is no time
            tlist = self.teammap[t]
            nth_wheel = self.teamnth[t]
            if len(tlist) >= nth_wheel:
                ct = (tlist[nth_wheel - 1][COL_RFTIME] - self.start -
                      tlist[nth_wheel - 1][COL_STOFT])
                thetime = ct.truncate(1)
                self.teamtimes[t] = thetime  # save to times map
                if (t not in self.announced_teams and
                    (self.announce_team is None or self.announce_team == t)):
                    # bounce this time onto the panel? HACK
                    self.announced_teams.add(t)
                    self.running_team = None  # cancel a running time
                    self.bounceteam(t, self.teamcats[t], thetime)
                    self.announce_team = None
                for r in tlist[0:nth_wheel]:
                    r[COL_CBUNCH] = thetime
                for r in tlist[nth_wheel:]:
                    et = r[COL_RFTIME] - self.start - r[COL_STOFT]
                    if self.owntime and (et > ct and (et - ct) > GAPTHRESH):
                        # TIME GAP!
                        thetime = et.truncate(1)
                    r[COL_CBUNCH] = thetime
                    ct = et

        # leave mode sorted by arrival order
        self.reorder_arrivals()  # re-order view by arrivals at finish

        # if final places in view, update text entry
        curact = self.meet.action_model.get_value(
            self.meet.action_combo.get_active_iter(), 0).decode(u'utf-8')
        if curact == u'fin':
            self.meet.action_entry.set_text(self.places)
        #_log.debug(u'Event status: %r', self.racestat)
        self.calcset = True

        return False  # allow idle add

    def new_start_trigger(self, rfid):
        """Collect a timer trigger signal and apply it to the model."""
        if self.newstartdlg is not None and self.newstartent is not None:
            et = tod.mktod(self.newstartent.get_text().decode(u'utf-8'))
            if et is not None:
                dlg = self.newstartdlg
                self.newstartdlg = None
                wasrunning = self.timerstat in [u'running', u'armfinish']
                st = rfid - et
                self.set_start(st)
                if wasrunning:
                    # flag a recalculate
                    self._dorecalc = True
                dlg.response(1)
            else:
                _log.warning(u'Invalid elapsed time: Start not updated')
        return False

    def new_start_trig(self, button, entry=None):
        """Use the current time to update start offset."""
        self.meet._timercb(tod.now())

    def verify_timent(self, entry, data=None):
        et = tod.mktod(entry.get_text())
        if et is not None:
            entry.set_text(et.rawtime())
        else:
            _log.info(u'Invalid elapsed time')

    def elapsed_dlg(self, addriders=''):
        """Run a 'new start' dialog."""
        if self.timerstat == u'armstart':
            _log.error(u'Start is armed, unarm to add new start time')
            return

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'new_start.ui'))
        dlg = b.get_object(u'newstart')
        try:
            dlg.set_transient_for(self.meet.window)
            self.newstartdlg = dlg

            timent = b.get_object(u'time_entry')
            self.newstartent = timent
            timent.connect(u'activate', self.verify_timent)

            self.meet.timercb = self.new_start_trigger
            b.get_object(u'now_button').connect(u'clicked',
                                                self.new_start_trig)

            response = dlg.run()
            self.newstartdlg = None
            if response == 1:  # id 1 set in glade for "Apply"
                _log.info(u'Start time updated: %r', self.start.rawtime(2))
            else:
                _log.info(u'Set elapsed time cancelled')
        except Exception as e:
            _log.debug(u'%s setting elapsed time: %s', e.__class__.__name__, e)
        finally:
            self.meet.timercb = self.timertrig
            dlg.destroy()

    def time_context_menu(self, widget, event, data=None):
        """Popup menu for result list."""
        self.context_menu.popup(None, None, None, event.button, event.time,
                                selpath)

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

    def totlapentry_activate_cb(self, entry, data=None):
        """Transfer total lap entry string into model if possible."""
        try:
            nt = entry.get_text().decode(u'utf-8')
            if nt:  # not empty
                self.totlaps = int(nt)
            else:
                self.totlaps = None
        except Exception:
            _log.warning(u'Ignored invalid total lap count')
        if self.totlaps is not None:
            self.totlapentry.set_text(unicode(self.totlaps))
        else:
            self.totlapentry.set_text(u'')

    def chg_dst_ent(self, entry, data):
        bib = entry.get_text().decode(u'utf-8')
        sbib = data[2]
        nv = u'[Invalid Rider]'
        rv = u''
        if sbib != bib:
            i = self.getiter(bib)
            if i is not None:
                nv = self.riders.get_value(i, COL_NAMESTR).decode(u'utf-8')
                rv = self.getbunch_iter(i)
        data[0].set_text(nv)
        data[1].set_text(rv)

    def placeswap(self, src, dst):
        """Swap the src and dst riders if they appear in places."""
        _log.debug(u'Places before swap: %r', self.places)
        newplaces = []
        for placegroup in self.places.split():
            gv = placegroup.split(u'-')
            sind = None
            try:
                sind = gv.index(src)
            except Exception:
                pass
            dind = None
            try:
                dind = gv.index(dst)
            except Exception:
                pass
            if sind is not None:
                gv[sind] = dst
            if dind is not None:
                gv[dind] = src
            newplaces.append(u'-'.join(gv))
        self.places = u' '.join(newplaces)
        _log.debug(u'Places after swap: %r', self.places)

    def rms_context_swap_activate_cb(self, menuitem, data=None):
        """Swap data to/from another rider."""
        sel = self.view.get_selection().get_selected()
        if sel is None:
            _log.info(u'Unable to swap empty rider selection')
            return
        srcbib = self.riders.get_value(sel[1], COL_BIB).decode(u'utf-8')
        spcat = riderdb.primary_cat(
            self.riders.get_value(sel[1], COL_CAT).decode(u'utf-8'))
        spare = spcat == u'SPARE'
        srcname = self.riders.get_value(sel[1], COL_NAMESTR).decode(u'utf-8')
        srcinfo = self.getbunch_iter(sel[1])
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'swap_rider.ui'))
        dlg = b.get_object(u'swap')
        dlg.set_transient_for(self.meet.window)
        src_ent = b.get_object(u'source_entry')
        src_ent.set_text(srcbib)
        src_lbl = b.get_object(u'source_label')
        src_lbl.set_text(srcname)
        src_res = b.get_object(u'source_result')
        src_res.set_text(srcinfo)
        dst_ent = b.get_object(u'dest_entry')
        dst_lbl = b.get_object(u'dest_label')
        dst_result = b.get_object(u'dest_result')
        dst_ent.connect(u'changed', self.chg_dst_ent,
                        (dst_lbl, dst_result, srcbib))
        ret = dlg.run()
        if ret == 1:
            dstbib = dst_ent.get_text().decode(u'utf-8')
            if dstbib != srcbib:
                dr = self.getrider(dstbib)
                if dr is not None:
                    self.placeswap(dstbib, srcbib)
                    sr = self.getrider(srcbib)
                    for col in [
                            COL_COMMENT, COL_INRACE, COL_PLACE, COL_LAPS,
                            COL_RFTIME, COL_CBUNCH, COL_MBUNCH, COL_RFSEEN
                    ]:
                        tv = dr[col]
                        dr[col] = sr[col]
                        sr[col] = tv
                    _log.info(u'Swap riders %r <=> %r', srcbib, dstbib)
                    # If srcrider was a spare bike, remove the spare and patch
                    if spare:
                        ac = [t for t in sr[COL_RFSEEN]]
                        ac.extend(dr[COL_RFSEEN])
                        dr[COL_RFSEEN] = [t for t in sorted(ac)]
                        dr[COL_LAPS] = len(dr[COL_RFSEEN])
                        self.delrider(srcbib)
                        _log.debug(u'Spare bike %r removed', srcbib)
                    # If dstrider is a spare bike, leave it in place
                    self.recalculate()
                else:
                    _log.error(u'Invalid rider swap %r <=> %r', srcbib, dstbib)
            else:
                _log.info(u'Swap to same rider ignored')
        else:
            _log.info(u'Swap rider cancelled')
        dlg.destroy()

    def rms_context_edit_activate_cb(self, menuitem, data=None):
        """Edit rider start/finish."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            stx = ''
            ftx = ''
            st = self.riders.get_value(sel[1], COL_STOFT)
            if st:
                stx = st.rawtime(0)
            ft = self.riders.get_value(sel[1], COL_RFTIME)
            if ft:
                ftx = ft.rawtime(2)
            tvec = uiutil.edit_times_dlg(self.meet.window, stx, ftx)
            if len(tvec) > 2 and tvec[0] == 1:
                self.riders.set_value(sel[1], COL_STOFT, tod.mktod(tvec[1]))
                self.riders.set_value(sel[1], COL_RFTIME, tod.mktod(tvec[2]))
                bib = self.riders.get_value(sel[1], COL_BIB).decode(u'utf-8')
                nst = u'-'
                st = self.riders.get_value(sel[1], COL_STOFT)
                if st:
                    nst = st.rawtime(0)
                nft = u'-'
                ft = self.riders.get_value(sel[1], COL_RFTIME)
                if ft:
                    nft = ft.rawtime(2)
                _log.info(u'Adjust rider %r start:%s, finish:%s', bib, nst,
                          nft)
                self.recalculate()

    def rms_context_chg_activate_cb(self, menuitem, data=None):
        """Update selected rider from event."""
        change = menuitem.get_label().lower()
        #_log.debug(u'menuitem: %r: %r', menuitem, change)
        sel = self.view.get_selection().get_selected()
        bib = None
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            if change == u'delete':
                _log.info(u'Delete rider: %r', selbib)
                self.delrider(selbib)
            elif change == u'clear':
                _log.info(u'Clear rider %r finish time', selbib)
                self.riders.set_value(i, COL_RFTIME, None)
                self.riders.set_value(i, COL_MBUNCH, None)
                self.recalculate()
            elif change == u'refinish':
                splits = self.riders.get_value(i, COL_RFSEEN)
                if splits is not None and len(splits) > 0:
                    nf = splits[-1]
                    _log.info(
                        u'Set raw finish for rider %r to last passing: %s',
                        selbib, nf.rawtime(2))
                    self.riders.set_value(i, COL_RFTIME, nf)
                    self.recalculate()
            elif change in [u'dns', u'dnf', u'wd', u'otl', u'dsq']:
                self.dnfriders(selbib, change)
            elif change == u'return':
                self.retriders(selbib)
            elif change == u'passing':
                self.manpassing(selbib)
            else:
                _log.info(u'Unknown rider change %r ignored', change)

    def __init__(self, meet, event, ui=True):
        self.meet = meet
        self.event = event
        self.evno = event[u'evid']
        self.series = event[u'seri']
        self.configfile = meet.event_configfile(self.evno)
        self.readonly = not ui
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        _log.debug(u'Init %r event %r', rstr, self.evno)

        self.recalclock = threading.Lock()
        self._dorecalc = False

        self.teamnames = {}
        self.teamtimes = {}
        self.teamnth = {}
        self.teamcats = {}
        self.teamuci = {}
        self.teammap = {}
        self.announced_teams = set()
        self.announce_team = None
        self.running_team = None  # show running time for team

        # event run time attributes
        self.autoexport = False
        self.autofinish = False
        self.showuciids = False
        self.relativestart = False
        self.showriders = True
        self.owntime = True  # dropped riders get own time
        self.start = None
        self.calcset = False
        self.maxfinish = None
        self.minlap = None
        self.winopen = True
        self.timerstat = u'idle'
        self.places = u''
        self.comment = []
        self.ridermark = None
        self.cats = []
        self.targetlaps = False  # true if finish is det by target
        self.catplaces = {}
        self.catlaps = {}  # cache of cat lap counts
        self.defaultnth = NTH_WHEEL
        self.nthwheel = {}
        self.autocats = False
        self.autostartlist = None
        self.bonuses = {}
        self.points = {}
        self.pointscb = {}
        self.totlaps = None

        # intermediates
        self.intermeds = []  # sorted list of intermediate keys
        self.intermap = {}  # map of intermediate keys to results
        self.contests = []  # sorted list of contests
        self.contestmap = {}  # map of contest keys
        self.tallys = []  # sorted list of points tallys
        self.tallymap = {}  # map of tally keys

        # announce cache
        self.scratch_map = {}
        self.scratch_ord = []
        self.live_announce = True

        # new start dialog
        self.newstartent = None
        self.newstartdlg = None

        self.riders = gtk.ListStore(
            gobject.TYPE_STRING,  # BIB = 0
            gobject.TYPE_STRING,  # NAMESTR = 1
            gobject.TYPE_STRING,  # NAMESTR = 1
            gobject.TYPE_STRING,  # CAT = 2
            gobject.TYPE_STRING,  # COMMENT = 3
            gobject.TYPE_BOOLEAN,  # INRACE = 4
            gobject.TYPE_STRING,  # PLACE = 5
            gobject.TYPE_INT,  # LAP COUNT = 6
            gobject.TYPE_PYOBJECT,  # RFTIME = 7
            gobject.TYPE_PYOBJECT,  # CBUNCH = 8
            gobject.TYPE_PYOBJECT,  # MBUNCH = 9
            gobject.TYPE_PYOBJECT,  # STOFT = 10
            gobject.TYPE_PYOBJECT,  # BONUS = 11
            gobject.TYPE_PYOBJECT,  # PENALTY = 12
            gobject.TYPE_PYOBJECT,  # RFSEEN = 13
            gobject.TYPE_STRING)  # TEAM = 14

        uifile = os.path.join(metarace.UI_PATH, u'rms.ui')
        _log.debug(u'Building event interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)

        self.frame = b.get_object('race_vbox')
        self.frame.connect('destroy', self.shutdown)

        # meta info pane
        self.shortname = None
        self.title_namestr = b.get_object(u'title_namestr')
        self.set_titlestr()
        self.elaplbl = b.get_object(u'time_lbl')
        self.lapentry = b.get_object(u'lapentry')
        b.get_object(u'lapsepslash').set_text(u' Total Laps:')
        self.lapentry.hide()
        self.totlapentry = b.get_object(u'totlapentry')

        # Result pane
        t = gtk.TreeView(self.riders)
        self.view = t
        t.set_reorderable(True)
        t.set_rules_hint(True)

        self.context_menu = None
        if ui:
            uiutil.mkviewcoltxt(t, u'No.', COL_BIB, calign=1.0)
            uiutil.mkviewcoltxt(t,
                                u'Rider',
                                COL_NAMESTR,
                                expand=True,
                                maxwidth=500)
            uiutil.mkviewcoltxt(t, u'Cat', COL_CAT)
            uiutil.mkviewcoltxt(t, u'Com', COL_COMMENT, cb=self.editcol_cb)
            # don't show in column for team time trial
            #uiutil.mkviewcolbool(t, u'In', COL_INRACE, width=50)
            uiutil.mkviewcoltxt(t,
                                u'Lap',
                                COL_LAPS,
                                width=40,
                                cb=self.editlap_cb)
            uiutil.mkviewcoltod(t,
                                u'Start',
                                cb=self.showstart_cb,
                                width=50,
                                editcb=self.editstart_cb)
            uiutil.mkviewcoltod(t,
                                u'Time',
                                cb=self.showbunch_cb,
                                editcb=self.editbunch_cb,
                                width=50)
            uiutil.mkviewcoltxt(t, u'Arvl', COL_PLACE, calign=0.5, width=50)
            t.show()
            b.get_object(u'race_result_win').add(t)

            # connect signal handlers
            b.connect_signals(self)
            b = gtk.Builder()
            b.add_from_file(os.path.join(metarace.UI_PATH, u'rms_context.ui'))
            self.context_menu = b.get_object(u'rms_context')
            self.view.connect(u'button_press_event',
                              self.treeview_button_press)
            b.connect_signals(self)
            self.meet.timercb = self.timertrig
            self.meet.alttimercb = self.alttimertrig
