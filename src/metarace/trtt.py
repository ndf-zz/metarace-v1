"""Road team time trial handler for roadmeet."""

import gtk
import glib
import gobject
import pango
import os
import logging
import threading

import metarace
from metarace import tod
from metarace import eventdb
from metarace import riderdb
from metarace import strops
from metarace import uiutil
from metarace import report
from metarace import jsonconfig

# Model columns

# basic infos
COL_BIB = 0
COL_NAMESTR = 1
COL_SHORTNAME = 2
COL_CAT = 3
COL_COMMENT = 4
COL_INRACE = 5  # boolean in the race
COL_PLACE = 6  # Place assigned in result
COL_LAPS = 7  # Incremented if inrace and not finished

# timing infos
COL_RFTIME = 8  # one-off finish time by rfid
COL_CBUNCH = 9  # computed bunch time	-> derived from rftime
COL_MBUNCH = 10  # manual bunch time	-> manual overrive
COL_STOFT = 11  # start time 'offset' - only reported in result
COL_BONUS = 12
COL_PENALTY = 13
COL_RFSEEN = 14  # list of tods this rider 'seen' by rfid
COL_TEAM = 15  # stored team ref for quick refs

# Nth wheel decides whose time is counted to the team
NTH_WHEEL = 4  # 4th wheel for NRS/ToT (default)
STARTFUDGE = tod.tod(u'2:00')  # min elapsed time

# listview column nos
CATCOLUMN = 2
COMCOLUMN = 3
INCOLUMN = 4
LAPCOLUMN = 5
STARTCOLUMN = 6

# rider commands
RIDER_COMMANDS_ORD = [
    'add', 'del', 'que', 'dns', 'hd', 'dnf', 'dsq', 'com', 'ret', 'run', 'man',
    '', 'fin'
]  # then intermediates...
RIDER_COMMANDS = {
    'dns': 'Did not start',
    'hd': 'Outside time limit',
    'dnf': 'Did not finish',
    'dsq': 'Disqualify',
    'add': 'Add starters',
    'del': 'Remove starters',
    'que': 'Query riders',
    'fin': 'Final places',
    'com': 'Add comment',
    'ret': 'Return to race',
    'man': 'Manual passing',
    '': '',
    'run': 'Show team time'
}

RESERVED_SOURCES = [
    'fin',  # finished stage
    'reg',  # registered to stage
    'start'
]  # started stage
# additional cat finishes added in loadconfig

DNFCODES = ['hd', 'dsq', 'dnf', 'dns']

BREAKTHRESH = 0.2  # aim for <20% break to rider thresh

# timing keys
key_announce = 'F4'
key_armstart = 'F5'
key_armlap = 'F6'
key_placesto = 'F7'  # fill places to selected rider
key_appendplace = 'F8'  # append sepected rider to places
key_armfinish = 'F9'
key_raceover = 'F10'

# extended fn keys	(ctrl + key)
key_abort = 'F5'
key_clearfrom = 'F7'  # clear places on selected rider and all following
key_clearplace = 'F8'  # clear rider from place list
key_undo = 'Z'

# config version string
EVENT_ID = 'roadrace-2.0'


def key_bib(x):
    """Sort on bib field of aux row."""
    return strops.riderno_key(x[1])


def sort_bib(x, y):
    """Rider bib sorter."""
    return cmp(strops.riderno_key(x[1]), strops.riderno_key(y[1]))


def sort_starters(x, y):
    """Start order sorter."""
    if x[1] == y[1]:
        return cmp(strops.riderno_key(x[2]), strops.riderno_key(y[2]))
    else:
        return cmp(x[1], y[1])


def sort_tally(x, y):
    """Points tally sort using countback struct."""
    if x[0] == y[0]:
        return cmp(y[3], x[3])
    else:
        return cmp(y[0], x[0])


class trtt(object):
    """Road time trial handler."""

    def hidecolumn(self, target, visible=False):
        tc = self.view.get_column(target)
        if tc:
            tc.set_visible(visible)

    def loadcats(self, cats=u''):
        self.cats = []  # clear old cat list
        catlist = cats.split()
        if u'AUTO' in catlist:  # ignore any others and re-load from rdb
            self.cats = self.meet.rdb.listcats()
            self.autocats = True
        else:
            self.autocats = False
            for cat in catlist:
                if cat != u'':
                    cat = cat.upper()
                    self.cats.append(cat)
        self.cats.append(u'')  # always include one empty cat
        self.log.debug(u'Result category list updated: ' + repr(self.cats))

    def team_start_times(self):
        """Scan riders and patch start times from team entry."""
        teamstarts = {}
        # pass 1: extract team times
        for r in self.riders:
            nt = r[COL_TEAM]
            if nt not in teamstarts:
                dbr = self.meet.rdb.getrider(nt, u'team')
                if dbr is not None:
                    st = tod.mktod(
                        self.meet.rdb.getvalue(dbr, riderdb.COL_REFID))
                    teamstarts[nt] = st

        # pass 2: patch start times if present
        cnt = 0
        for r in self.riders:
            nt = r[COL_TEAM]
            if nt in teamstarts and teamstarts[nt]:
                r[COL_STOFT] = teamstarts[nt]
                cnt += 1
        self.log.info(u'Patched ' + repr(cnt) + u' start times.')

    def loadconfig(self):
        """Load event config from disk."""
        self.riders.clear()
        self.resettimer()
        self.cats = []

        cr = jsonconfig.config({
            u'event': {
                u'start': u'',
                u'id': EVENT_ID,
                u'finish': u'',
                u'finished': u'No',
                u'places': u'',
                u'comment': [],
                u'hidecols': [],  # TODO: default hides
                u'categories': [],
                u'intermeds': [],
                u'contests': [],
                u'tallys': [],
                u'lapstart': u'',
                u'lapfin': u'',
                u'owntime': True,
                u'curlap': 0,
                u'totlaps': None,
                u'sprintlaps': [],
                u'nolaps': u'False',
                u'defaultnth': NTH_WHEEL,
                u'minelap': STARTFUDGE,
                u'minlap': None,
                u'nthwheel': {},
                u'gapfinish': False,
                u'startlist': u'',
                u'resultcats': u'False'
            }
        })
        cr.add_section(u'event')
        cr.add_section(u'riders')
        # sections for commissaire awarded bonus/penalty
        cr.add_section(u'stagebonus')
        cr.add_section(u'stagepenalty')

        # check for config file
        try:
            with open(self.configpath, 'rb') as f:
                cr.read(f)
        except Exception as e:
            self.log.error(u'Reading event config: ' + unicode(e))

        # load _result_ categories
        catlist = cr.get(u'event', u'categories')
        if u'AUTO' in catlist:  # ignore any others and re-load from rdb
            self.cats = self.meet.rdb.listcats()
            self.autocats = True
        else:
            self.autocats = False
            for cat in catlist:
                if cat != u'':
                    cat = cat.upper()
                    self.cats.append(cat)
        self.cats.append(u'')  # always include one empty cat

        # read in category nth wheel values
        self.defaultnth = cr.get(u'event', u'defaultnth')
        self.nthwheel = cr.get(u'event', u'nthwheel')

        # amend reserved sources with any cats
        if len(self.cats) > 1:
            for cat in self.cats:
                if cat:
                    srcid = cat.lower() + 'fin'
                    RESERVED_SOURCES.append(srcid)
                    self.catplaces[srcid] = cat

        # restore intermediates
        for i in cr.get(u'event', u'intermeds'):
            if i in RESERVED_SOURCES:
                self.log.info(u'Ignoring reserved intermediate: ' + repr(i))
            else:
                crkey = u'intermed_' + i
                descr = u''
                places = u''
                km = None
                abbr = u''
                if cr.has_option(crkey, u'descr'):
                    descr = cr.get(crkey, u'descr')
                if cr.has_option(crkey, u'dist'):
                    km = strops.confopt_float(cr.get(crkey, u'dist'), None)
                if cr.has_option(crkey, u'abbr'):
                    abbr = cr.get(crkey, u'abbr')
                if cr.has_option(crkey, u'places'):
                    places = strops.reformat_placelist(cr.get(
                        crkey, u'places'))
                if i not in self.intermeds:
                    self.log.debug(u'Adding intermediate: ' + repr(i) + u':' +
                                   descr + u':' + places)
                    self.intermeds.append(i)
                    self.intermap[i] = {
                        u'descr': descr,
                        u'places': places,
                        u'dist': km,
                        u'abbr': abbr
                    }
                else:
                    self.log.info(u'Ignoring duplicate intermediate: ' +
                                  repr(i))

        # load contest meta data
        tallyset = set()
        for i in cr.get(u'event', u'contests'):
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
                            self.log.info(u'Invalid bonus ' + repr(bstr) +
                                          u' in contest ' + repr(i))
                            bt = tod.ZERO
                        bonuses.append(bt)
                self.contestmap[i][u'bonuses'] = bonuses
                points = []
                if cr.has_option(crkey, u'points'):
                    pliststr = cr.get(crkey, u'points').strip()
                    if pliststr and tally == u'':  # no tally for these points!
                        self.log.error(u'No tally for points in contest: ' +
                                       repr(i))
                        tallyset.add(u'')  # add empty placeholder
                    for pstr in pliststr.split():
                        pt = 0
                        try:
                            pt = int(pstr)
                        except:
                            self.log.info(u'Invalid points ' + repr(pstr) +
                                          u' in contest ' + repr(i))
                        points.append(pt)
                self.contestmap[i][u'points'] = points
                allsrc = False  # all riders in source get same pts
                if cr.has_option(crkey, u'all_source'):
                    allsrc = strops.confopt_bool(cr.get(crkey, u'all_source'))
                self.contestmap[i][u'all_source'] = allsrc
                self.contestmap[i][u'category'] = 0
                if cr.has_option(crkey, u'category'):  # for climbs
                    self.contestmap[i][u'category'] = strops.confopt_posint(
                        cr.get(crkey, u'category'))
            else:
                self.log.info(u'Ignoring duplicate contest: ' + repr(i))

            # check for invalid allsrc
            if self.contestmap[i][u'all_source']:
                if (len(self.contestmap[i][u'points']) > 1
                        or len(self.contestmap[i][u'bonuses']) > 1):
                    self.log.info(u'Ignoring extra points/bonus for ' +
                                  u'all source contest ' + repr(i))

        # load points tally meta data
        tallylist = cr.get('event', 'tallys')
        # append any 'missing' tallys from points data errors
        for i in tallyset:
            if i not in tallylist:
                self.log.debug(u'Adding missing tally to config: ' + repr(i))
                tallylist.append(i)
        # then scan for meta data
        for i in tallylist:
            if i not in self.tallys:
                self.tallys.append(i)
                self.tallymap[i] = {}
                self.points[i] = {}  # redundant, but ok
                self.pointscb[i] = {}
                crkey = u'tally_' + i
                descr = u''
                if cr.has_option(crkey, u'descr'):
                    descr = cr.get(crkey, u'descr')
                self.tallymap[i][u'descr'] = descr
                keepdnf = False
                if cr.has_option(crkey, u'keepdnf'):
                    keepdnf = strops.confopt_bool(cr.get(crkey, u'keepdnf'))
                self.tallymap[i][u'keepdnf'] = keepdnf
            else:
                self.log.info(u'Ignoring duplicate points tally: ' + repr(i))

        starters = cr.get(u'event', u'startlist').split()
        dolaps = not strops.confopt_bool(cr.get(u'event', u'nolaps'))
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
                if dolaps and lr > 6:
                    for i in range(6, lr):
                        laptod = tod.mktod(ril[i])
                        if laptod is not None:
                            nr[COL_RFSEEN].append(laptod)
            # record any extra bonus/penalty to rider model
            if cr.has_option(u'stagebonus', r):
                nr[COL_BONUS] = tod.mktod(cr.get(u'stagebonus', r))
            if cr.has_option(u'stagepenalty', r):
                nr[COL_PENALTY] = tod.mktod(cr.get(u'stagepenalty', r))

        self.owntime = strops.confopt_bool(cr.get(u'event', u'owntime'))
        # load minimum elapsed time
        self.minelap = tod.mktod(cr.get(u'event', u'minelap'))
        if self.minelap is None:
            self.minelap = STARTFUDGE
        self.minlap = tod.mktod(cr.get(u'event', u'minlap'))

        self.set_start(cr.get(u'event', u'start'))
        self.set_finish(cr.get(u'event', u'finish'))
        self.lapstart = tod.mktod(cr.get(u'event', u'lapstart'))
        self.lapfin = tod.mktod(cr.get(u'event', u'lapstart'))
        self.curlap = cr.get(u'event', u'curlap')
        self.totlaps = cr.get(u'event', u'totlaps')
        self.sprintlaps = cr.get(u'event', u'sprintlaps')
        self.places = strops.reformat_placelist(cr.get(u'event', u'places'))
        self.comment = cr.get(u'event', u'comment')  # comments are vec
        self.gapfinish = strops.confopt_bool(cr.get(u'event', u'gapfinish'))
        if strops.confopt_bool(cr.get(u'event', u'finished')):
            self.set_finished()
        self.recalculate()

        for col in cr.get(u'event', u'hidecols'):
            target = strops.confopt_posint(col)
            if target is not None:
                self.hidecolumn(target)
        #if self.totlaps is None:	# NO should be config'ble
        #self.hidecolumn(LAPCOLUMN)
        self.load_cat_starts()

        if self.totlaps is not None:
            self.totlapentry.set_text(unicode(self.totlaps))

        # hack: patch start times from riderdb if present in refid
        #       column of team name
        self.team_start_times()

        # After load complete - check config and report. This ensures
        # an error message is left on top of status stack. This is not
        # always a hard fail and the user should be left to determine
        # an appropriate outcome.
        eid = cr.get(u'event', u'id')
        if eid and eid != EVENT_ID:
            self.log.error(u'Event configuration mismatch: ' + repr(eid) +
                           u' != ' + repr(EVENT_ID))
            self.readonly = True

    def load_cat_starts(self):
        self.catlaps = {}
        for c in self.cats:
            ls = self.totlaps  # default to meet config
            dbr = self.meet.rdb.getrider(c, u'cat')
            if dbr is not None:
                lt = strops.confopt_posint(
                    self.meet.rdb.getvalue(dbr, riderdb.COL_CAT))
                if lt:
                    ls = lt
            self.catlaps[c] = ls
            self.log.debug(u'Set cat total laps to ' + repr(ls) +
                           u' for cat ' + repr(c))

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
                descr = self.intermap[k]['descr']
            ret[k] = descr
        return ret

    def get_startlist(self):
        """Return a list of all rider numbers 'registered' to event."""
        ret = []
        for r in self.riders:
            ret.append(r[COL_BIB])
        return ' '.join(ret)

    def get_starters(self):
        """Return a list of riders that 'started' the race."""
        ret = []
        for r in self.riders:
            if r[COL_COMMENT] != 'dns' or r[COL_INRACE]:
                ret.append(r[COL_BIB])
        return ' '.join(ret)

    def checkpoint_model(self):
        """Write the current rider model to an undo buffer."""
        self.undomod.clear()
        self.placeundo = self.places
        for r in self.riders:
            self.undomod.append(r)
        self.canundo = True

    def undo_riders(self):
        """Roll back rider model to last checkpoint."""
        if self.canundo:
            self.riders.clear()
            for r in self.undomod:
                self.riders.append(r)
            self.places = self.placeundo
            self.canundo = False

    def get_catlist(self):
        """Return the ordered list of categories."""
        rvec = []
        for cat in self.cats:
            if cat != '':
                rvec.append(cat)
        return rvec

    def ridercat(self, cat):
        """Return a category from the result for the riders cat."""
        ret = ''  # default is the 'None' category - uncategorised
        checka = cat.upper()
        if checka in self.cats:
            ret = checka
        return ret

    def saveconfig(self):
        """Save event config to disk."""
        if self.readonly:
            self.log.error(u'Attempt to save readonly ob.')
            return
        cw = jsonconfig.config()
        cw.add_section(u'event')
        if self.start is not None:
            cw.set(u'event', u'start', self.start.rawtime())
        if self.finish is not None:
            cw.set(u'event', u'finish', self.finish.rawtime())
        if self.lapstart is not None:
            cw.set(u'event', u'lapstart', self.lapstart.rawtime())
        if self.lapfin is not None:
            cw.set(u'event', u'lapfin', self.lapfin.rawtime())
        if self.minelap is not None:
            cw.set(u'event', u'minelap', self.minelap.rawtime())
        else:
            cw.set(u'event', u'minelap', None)
        if self.minlap is not None:
            cw.set(u'event', u'minlap', self.minlap.rawtime())
        else:
            cw.set(u'event', u'minlap', None)
        cw.set(u'event', u'finished', self.timerstat == 'finished')
        cw.set(u'event', u'places', self.places)
        cw.set(u'event', u'curlap', self.curlap)
        cw.set(u'event', u'totlaps', self.totlaps)
        cw.set(u'event', u'sprintlaps', self.sprintlaps)
        cw.set(u'event', u'gapfinish', self.gapfinish)
        cw.set(u'event', u'defaultnth', self.defaultnth)
        cw.set(u'event', u'owntime', self.owntime)
        cw.set(u'event', u'nthwheel', self.nthwheel)  # dict of cat keys

        # save intermediate data
        cw.set(u'event', u'intermeds', self.intermeds)
        for i in self.intermeds:
            crkey = u'intermed_' + i
            cw.add_section(crkey)
            cw.set(crkey, u'descr', self.intermap[i][u'descr'])
            cw.set(crkey, u'places', self.intermap[i][u'places'])

        # save contest meta data
        cw.set(u'event', u'contests', self.contests)
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
        cw.set(u'event', u'tallys', self.tallys)
        for i in self.tallys:
            crkey = u'tally_' + i
            cw.add_section(crkey)
            cw.set(crkey, u'descr', self.tallymap[i][u'descr'])
            cw.set(crkey, u'keepdnf', self.tallymap[i][u'keepdnf'])

        # save riders
        cw.set(u'event', u'startlist', self.get_startlist())
        if self.autocats:
            cw.set(u'event', u'categories', [u'AUTO'])
        else:
            cw.set(u'event', u'categories', self.get_catlist())
        cw.set(u'event', u'comment', self.comment)
        hides = []
        idx = 0
        for c in self.view.get_columns():
            if not c.get_visible():
                hides.append(str(idx))
            idx += 1
        if len(hides) > 0:
            cw.set(u'event', u'hidecols', hides)

        cw.add_section(u'riders')
        # sections for commissaire awarded bonus/penalty
        cw.add_section(u'stagebonus')
        cw.add_section(u'stagepenalty')
        for r in self.riders:
            rt = u''
            if r[COL_RFTIME] is not None:
                rt = r[COL_RFTIME].rawtime(2)
            mb = u''
            if r[COL_MBUNCH] is not None:
                mb = r[COL_MBUNCH].rawtime(1)
            sto = u''
            if r[COL_STOFT] is not None:
                sto = r[COL_STOFT].rawtime(2)
            # bib = comment,in,laps,rftod,mbunch,rfseen...
            slice = [
                r[COL_COMMENT].decode('utf-8'), r[COL_INRACE], r[COL_LAPS], rt,
                mb, sto
            ]
            for t in r[COL_RFSEEN]:
                if t is not None:
                    slice.append(t.rawtime(2))
            cw.set(u'riders', r[COL_BIB].decode('utf-8'), slice)
            if r[COL_BONUS] is not None:
                cw.set(u'stagebonus', r[COL_BIB], r[COL_BONUS].rawtime(0))
            if r[COL_PENALTY] is not None:
                cw.set(u'stagepenalty', r[COL_BIB], r[COL_PENALTY].rawtime(0))
        cw.set(u'event', u'id', EVENT_ID)
        self.log.debug(u'Saving config to: ' + repr(self.configpath))
        with open(self.configpath, 'wb') as f:
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
        if titlestr is None or titlestr == '':
            titlestr = 'Mass Start Road Race'
        self.title_namestr.set_text(titlestr)

    def destroy(self):
        """Emit destroy signal to race handler."""
        self.context_menu.destroy()
        self.frame.destroy()

    def get_results(self):
        """Extract results in flat mode (not yet implemented)."""
        return []

    def points_report(self):
        """Return the points tally report."""
        ret = []
        aux = []
        cnt = 0
        for tally in self.tallys:
            sec = report.section()
            sec.heading = self.tallymap[tally]['descr']
            sec.units = 'pt'
            tallytot = 0
            aux = []
            for bib in self.points[tally]:
                r = self.getrider(bib)
                tallytot += self.points[tally][bib]
                aux.append([
                    self.points[tally][bib],
                    strops.riderno_key(bib),
                    [
                        None, r[COL_BIB], r[COL_NAMESTR],
                        strops.truncpad(str(self.pointscb[tally][bib]),
                                        10,
                                        elipsis=False), None,
                        str(self.points[tally][bib])
                    ], self.pointscb[tally][bib]
                ])
            aux.sort(sort_tally)
            for r in aux:
                sec.lines.append(r[2])
            sec.lines.append([None, None, None])
            sec.lines.append([None, None, 'Total Points: ' + str(tallytot)])
            ret.append(sec)

        if len(self.bonuses) > 0:
            sec = report.section()
            sec.heading = 'Stage Bonuses'
            sec.units = 'sec'
            aux = []
            for bib in self.bonuses:
                r = self.getrider(bib)
                aux.append([
                    self.bonuses[bib],
                    strops.riderno_key(bib),
                    [
                        None, r[COL_BIB], r[COL_NAMESTR], None, None,
                        str(int(self.bonuses[bib].truncate(0).timeval))
                    ], 0, 0, 0
                ])
            aux.sort(sort_tally)
            for r in aux:
                sec.lines.append(r[2])
            ret.append(sec)
        return ret

    def reorder_startlist(self):
        """Reorder riders for a startlist."""
        aux = []
        cnt = 0
        for r in self.riders:
            aux.append([cnt, r[COL_STOFT], r[COL_BIB]])
            cnt += 1
        if len(aux) > 1:
            aux.sort(sort_starters)
            self.riders.reorder([a[0] for a in aux])
        return cnt

    def startlist_report(self):
        """Return a startlist report."""
        # This is time trial - so generate a time specific startlist
        ret = []
        cnt = self.reorder_startlist()
        tcount = 0
        rcount = 0

        ##residual = set(get_startlist().split())	- required?
        sec = None  # TEST FOR ERROR

        lcat = None
        ltod = None
        lteam = None
        for r in self.riders:
            rcount += 1
            rno = r[COL_BIB].decode('utf-8')
            rname = r[COL_SHORTNAME].decode('utf-8')
            rteam = r[COL_TEAM]
            rstart = r[COL_STOFT]
            if rteam != lteam:  # issue team time
                ltod = None
                tcat = self.ridercat(r[COL_CAT].decode('utf-8'))
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
                lcat = tcat

                tname = rteam  # use key and only replace if avail
                dbr = self.meet.rdb.getrider(rteam, u'team')
                if dbr is not None:
                    tname = self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB)
                    #tname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
                if ltod is not None and rstart - ltod > tod.tod(u'2:00'):
                    sec.lines.append([])
                ltod = rstart
                cstr = u''
                ustr = u''
                tcount += 1
                ## NO: for now just break over pages automatically
                #if tcount >= 7:
                ## new page
                #ret.append(sec)
                #ret.append(report.pagebreak())
                #sec = report.rttstartlist(u'startlist')
                #sec.heading = u'Startlist (Continued)'
                #tcount = 1
                #rcount = 0
                tcodestr = rteam.upper()
                if rteam.isdigit():
                    tcodestr = None
                sec.lines.append(
                    [rstart.meridiem(), tcodestr, tname, ustr, u'____', cstr])
                lteam = rteam
            sec.lines.append([None, rno, rname, None, None, None, None])
        ret.append(sec)
        #if len(self.cats) > 1:
        #for c in self.cats:
        #if c:
        #ret.extend(self.startlist_report_gen(c))
        #ret.append(report.pagebreak())
        #else:
        #ret = self.startlist_report_gen()
        return ret

    def startlist_report_gen(self, cat=None):
        catname = u''
        subhead = u''
        if cat is not None:
            dbr = self.meet.rdb.getrider(cat, u'cat')
            if dbr is not None:
                catname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
                subhead = self.meet.rdb.getvalue(dbr, riderdb.COL_LAST)
        else:
            cat = u''  # match all riders

        catcache = {u'': None}
        if cat == u'':
            for c in self.meet.rdb.listcats(self.series):
                if c != u'':
                    catnm = c
                    dbr = self.meet.rdb.getrider(c, u'cat')
                    if dbr is not None:
                        catnm = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
                    catcache[c] = catnm
        ret = []
        sec = report.section()
        sec.heading = 'Startlist'
        if catname:
            sec.heading += u': ' + catname
            sec.subheading = subhead
        cnt = self.reorder_startlist()
        rcnt = 0
        cat = self.ridercat(cat)
        for r in self.riders:
            rcat = r[COL_CAT].decode('utf-8').upper()
            if cat == u'' or rcat == cat:
                ucicode = None
                dbr = self.meet.rdb.getrider(r[COL_BIB].decode('utf-8'),
                                             self.series)
                if dbr is not None:
                    ucicode = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                if not ucicode and cat == u'':
                    ucicode = catcache[rcat]  # try and fill cat
                comment = None
                #if r[COL_CAT] == 'U23': # hack -> should be config'ble
                #comment = '*'
                if not r[COL_INRACE]:
                    cmt = r[COL_COMMENT].decode('utf-8')
                    if cmt == u'dns':
                        comment = cmt
                sec.lines.append([
                    comment, r[COL_BIB].decode('utf-8'),
                    r[COL_NAMESTR].decode('utf-8'), ucicode
                ])
                rcnt += 1
        ret.append(sec)
        if rcnt > 1:
            sec = report.bullet_text()
            sec.lines.append([u'', 'Total riders: ' + unicode(rcnt)])
            ret.append(sec)
        return ret

    def camera_report(self):
        """Return the judges (camera) report."""
        ret = []
        self.recalculate()  # fill places and bunch info
        pthresh = self.meet.timer.photothresh()
        totcount = 0
        dnscount = 0
        dnfcount = 0
        fincount = 0
        lcomment = ''
        if self.timerstat != 'idle':
            sec = report.section()
            sec.colheader = ['', 'no', 'rider', 'lap', 'finish', 'rftime']
            first = True
            ft = None
            lt = None
            lrf = None
            for r in self.riders:
                totcount += 1
                marker = ' '
                es = ''
                bs = ''
                pset = False
                if r[COL_INRACE]:
                    comment = '___'
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if bt is not None:
                        fincount += 1
                        if r[COL_PLACE] != '':
                            comment = r[COL_PLACE] + '.'
                            pset = True

                        # format 'elapsed' rftime
                        if r[COL_RFTIME] is not None:
                            if not pset and lrf is not None:
                                if r[COL_RFTIME] - lrf < pthresh:
                                    comment = '[pf]__'
                                    prcom = sec.lines[-1][0]
                                    if prcom in ['___', '[pf]__']:
                                        sec.lines[-1][0] = '[pf]__'
                            if self.start is not None:
                                es = (r[COL_RFTIME] - self.start).rawtime(2)
                            else:
                                es = r[COL_RFTIME].rawtime(2)
                            lrf = r[COL_RFTIME]
                        else:
                            lrf = None

                        # format 'finish' time
                        if ft is None:
                            ft = bt
                            bs = ft.rawtime(1)
                        else:
                            if bt > lt:
                                # New bunch
                                sec.lines.append([None, None, None])
                                bs = "+" + (bt - ft).rawtime(1)
                            else:
                                # Same time
                                pass
                        lt = bt
                    else:
                        if r[COL_COMMENT].strip() != '':
                            comment = r[COL_COMMENT].strip()

                    sec.lines.append([
                        comment, r[riderdb.COL_BIB], r[COL_NAMESTR],
                        str(r[COL_LAPS]), bs, es
                    ])
                else:
                    comment = r[COL_COMMENT]
                    if comment == '':
                        comment = 'dnf'
                    if comment != lcomment:
                        sec.lines.append([None, None, None])
                    lcomment = comment
                    if comment == 'dns':
                        dnscount += 1
                    else:
                        dnfcount += 1
                    sec.lines.append([
                        comment, r[riderdb.COL_BIB], r[COL_NAMESTR],
                        str(r[COL_LAPS]), None, None
                    ])
                first = False

            ret.append(sec)
            sec = report.section()
            sec.lines.append([None, None, 'Total riders: ' + str(totcount)])
            sec.lines.append([None, None, 'Did not start: ' + str(dnscount)])
            sec.lines.append([None, None, 'Did not finish: ' + str(dnfcount)])
            sec.lines.append([None, None, 'Finishers: ' + str(fincount)])
            residual = totcount - (fincount + dnfcount + dnscount)
            if residual > 0:
                sec.lines.append(
                    [None, None, 'Unaccounted for: ' + str(residual)])
            if len(sec.lines) > 0:
                ret.append(sec)
        else:
            # nothing to report...
            pass
        return ret

    def catresult_report(self):
        """Return a categorised race result report."""
        self.recalculate()
        ret = []
        for cat in self.cats:
            #if not cat:
            #continue	# ignore empty and None cat
            ret.extend(self.single_catresult(cat))

        if len(self.comment) > 0:
            s = report.bullet_text()
            s.heading = u'Decisions of the commissaires panel'
            for comment in self.comment:
                s.lines.append([None, comment])
            ret.append(s)

        return ret

    def single_catresult(self, cat):
        ret = []
        catname = cat  # fallback emergency
        subhead = u''
        distance = None
        dbr = self.meet.rdb.getrider(cat, u'cat')
        if dbr is not None:
            catname = self.meet.rdb.getvalue(
                dbr, riderdb.COL_FIRST)  # already decode
            subhead = self.meet.rdb.getvalue(dbr, riderdb.COL_LAST)
            dist = self.meet.rdb.getvalue(dbr, riderdb.COL_REFID)
            try:
                distance = float(dist)
            except:
                self.log.warn(u'Invalid distance: ' + repr(dist) +
                              u' for cat ' + repr(cat))

        # scan result and collect team lists
        tcnt = 0
        rcnt = 0
        fcnt = 0
        wt = None
        teamlist = []
        teamset = set()
        teammap = {}
        for r in self.riders:
            rcat = self.ridercat(r[COL_CAT].decode('utf-8'))
            if cat == rcat:  # rider in cat
                rcnt += 1
                bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                team = r[COL_TEAM]
                teamset.add(team)
                ## NOTE: bunch time in ttt overrides INRACE flag
                if bt is not None:  # team/rider has a result
                    if team not in teammap:
                        tcnt += 1
                        # first rider for the team
                        trank = unicode(len(teamlist) + 1) + u'.'
                        tname = team.upper()  # fallback
                        tcode = tname
                        if tcode.isdigit():
                            tcode = None
                        tucicode = None
                        # lookup team name in rdb
                        dbr = self.meet.rdb.getrider(team, u'team')
                        if dbr is not None:
                            tname = self.meet.rdb.getvalue(
                                dbr, riderdb.COL_CLUB)
                            tucicode = self.meet.rdb.getvalue(
                                dbr, riderdb.COL_UCICODE)
                        teamlist.append(team)  # add onto team result
                        teammap[team] = {}
                        dstr = u''
                        estr = bt.rawtime(1)
                        ## if first team set wt
                        if not wt:
                            wt = bt  # and 'down' time will be bunch time
                        else:
                            dstr = u'+' + (bt - wt).rawtime(1)

                        ## Initialise the team record:
                        teammap[team][u'tline'] = [
                            trank, tcode, tname, tucicode, estr, dstr
                        ]
                        teammap[team][u'ttime'] = bt
                        teammap[team][u'rlines'] = []
                    rtime = None
                    if bt != teammap[team][u'ttime']:
                        dt = bt.truncate(0) - teammap[team][u'ttime'].truncate(
                            0)
                        rtime = u'[+' + dt.rawtime(0) + u']'  # down is stage
                    rno = r[COL_BIB].decode('utf-8')
                    rname = r[COL_SHORTNAME].decode('utf-8')
                    ruci = None  ## TODO - ucicode per rider
                    teammap[team][u'rlines'].append(
                        [None, rno, rname, ruci, rtime, None])
                    fcnt += 1
                elif not r[COL_INRACE] and team in teammap:  # dnf etc
                    rtime = u'[dnf]'
                    if r[COL_COMMENT]:  # overwrite
                        rtime = u'[' + r[COL_COMMENT].decode('utf-8') + u']'
                    rno = r[COL_BIB].decode('utf-8')
                    rname = r[COL_SHORTNAME].decode('utf-8')
                    ruci = None  ## TODO - ucicode per rider
                    teammap[team][u'rlines'].append(
                        [None, rno, rname, ruci, rtime, None])
                    fcnt += 1
                # otherwise rider is not yet finished or abandon
        if len(teamset) == 0:
            return []  # nothing to report
        # scan team result and output to section
        sec = report.section()
        rsec = sec  # remember first section
        first = True
        for team in teamlist:
            if not first:
                sec.lines.append([])
            first = False
            sec.lines.append(teammap[team][u'tline'])
            sec.lines.extend(teammap[team][u'rlines'])

        if self.timerstat == 'finished':
            sec.heading = u'Result'
        else:
            if len(teamset) == len(teamlist):  # all teams in
                sec.heading = u'Provisional Result'
            else:  # not all teams in cat finsihed yet
                if len(teamlist) > 0:
                    sec.heading = u'Virtual Standings'
                else:
                    sec.heading = u'Race in Progress'
        ret.append(sec)

        # Race metadata / UCI comments
        sec = report.bullet_text()
        if wt is not None:
            if distance is not None:
                sec.lines.append([
                    None, u'Average speed of the winning team: ' +
                    wt.speedstr(1000.0 * distance)
                ])
        sec.lines.append([None, u'Number of teams: ' + unicode(len(teamset))])
        #if hdcount > 0:
        #sec.lines.append([None,
        #u'Riders finishing out of time limits: '
        #+ unicode(hdcount)])
        #if dnfcount > 0:
        #sec.lines.append([None,
        #u'Riders abandoning the race: '
        #+ unicode(dnfcount)])
        #residual = totcount - (fincount + dnfcount + dnscount + hdcount)
        #if residual > 0:
        #self.log.info(u'Unaccounted for: ' + unicode(residual))
        #else:	 # everyone accounted for, change stat if prov
        #if self.timerstat in ['running', 'ready', 'armfinish']:
        #rsec.heading = u'Provisional Result'
        ret.append(sec)
        # finish report title manipulation
        if catname:
            rsec.heading += u': ' + catname
            rsec.subheading = subhead
        ret.append(report.pagebreak())
        return ret

    def junx(self):

        wt = None
        lt = None
        first = True
        lcomment = u''
        lp = None
        lsrc = None
        rcnt = 0
        plcnt = 1
        totcount = 0
        dnscount = 0
        dnfcount = 0
        hdcount = 0
        fincount = 0
        for r in self.riders:
            rcat = self.ridercat(r[COL_CAT].decode('utf-8'))
            if cat == rcat:  # rider in cat
                totcount += 1
                bstr = r[COL_BIB].decode('utf-8')
                nstr = r[COL_NAMESTR].decode('utf-8')
                pstr = u''
                tstr = u''
                dstr = u''
                cstr = u''  # check for ucicode here !
                dbr = self.meet.rdb.getrider(bstr, self.series)
                if dbr is not None:
                    cstr = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                placed = False  # placed at finish
                timed = False  # timed at finish
                if r[COL_INRACE]:
                    psrc = r[COL_PLACE].decode('utf-8')
                    if psrc != u'':
                        placed = True
                        if lsrc != psrc:  # previous total place differs
                            lp = unicode(plcnt)
                        else:
                            pass  # dead heat in cat
                        lsrc = psrc
                    else:
                        lp = u''
                    plcnt += 1
                    pstr = u''
                    if lp is not None and lp != u'':
                        pstr = lp + u'.'
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if bt is not None:
                        fincount += 1  # for accounting, use bunch time
                        timed = True
                        #tstr = bt.rawtime(0)
                        # compute elapsed
                        et = bt
                        # stoft in recalc
                        #if r[COL_STOFT] is not None: # apply a start offset
                        #et = bt - r[COL_STOFT]
                        #tstr = et.rawtime(0)
                        if wt is None:  # first finish time
                            wt = et
                        if bt != lt:
                            if not first:
                                dstr = u'+' + (et - wt).rawtime(1)
                            else:
                                dstr = wt.rawtime(1)
                        first = False
                    lt = bt
                else:
                    # Non-finishers dns, dnf. hd, dsq
                    placed = True  # for purpose of listing
                    comment = r[COL_COMMENT].decode('utf-8')
                    if comment == u'':
                        comment = u'dnf'
                    if comment != lcomment:
                        sec.lines.append([None, None, None])  # new bunch
                    lcomment = comment
                    # account for special cases
                    if comment == u'dns':
                        dnscount += 1
                    elif comment == u'hd':
                        hdcount += 1
                    else:
                        dnfcount += 1
                    pstr = comment
                if placed or timed:
                    sec.lines.append([pstr, bstr, nstr, cstr, tstr, dstr])
                rcnt += 1
            else:
                # not in this category.
                pass
        if self.timerstat == 'finished':
            sec.heading = u'Result'
        elif self.timerstat in ['running', 'ready', 'armfinish']:
            sec.heading = u'Race in Progress'
        else:
            sec.heading = u'Provisional Result'
        ret.append(sec)
        rsec = sec
        # Race metadata / UCI comments
        sec = report.bullet_text()
        if wt is not None:
            #sec.lines.append([None, u"Winner's time: " + wt.rawtime(0)])
            if distance is not None:
                sec.lines.append([
                    None, u'Average speed of the winner: ' +
                    wt.speedstr(1000.0 * distance)
                ])
        sec.lines.append(
            [None, u'Number of starters: ' + unicode(totcount - dnscount)])
        if hdcount > 0:
            sec.lines.append([
                None,
                u'Riders finishing out of time limits: ' + unicode(hdcount)
            ])
        if dnfcount > 0:
            sec.lines.append(
                [None, u'Riders abandoning the race: ' + unicode(dnfcount)])
        residual = totcount - (fincount + dnfcount + dnscount + hdcount)
        if residual > 0:
            self.log.info(u'Unaccounted for: ' + unicode(residual))
        else:  # everyone accounted for, change stat if prov
            if self.timerstat in ['running', 'ready', 'armfinish']:
                rsec.heading = u'Provisional Result'
        ret.append(sec)

        # finish report title manipulation
        if catname:
            rsec.heading += u': ' + catname
            rsec.subheading = subhead
        ret.append(report.pagebreak())
        return ret

    def result_report(self):
        """Return a race result report."""

        # check if a categorised report is required
        if True:
            #self.event[u'type'] != u'rhcp' and len(self.cats) > 1:
            return self.catresult_report()

        self.recalculate()
        ret = []
        wt = None
        we = None
        dofastest = False  # ftime for handicap races
        fastest = None
        vfastest = None
        curelap = None
        if self.start is not None:  # virtual bunch time
            curelap = (tod.now() - self.start).truncate(0)
        fastestbib = None
        totcount = 0
        dnscount = 0
        dnfcount = 0
        hdcount = 0
        fincount = 0
        lcomment = ''
        gapcount = 0
        catcache = {u'': None}
        for c in self.meet.rdb.listcats(self.series):
            if c != u'':
                catnm = c
                dbr = self.meet.rdb.getrider(c, u'cat')
                if dbr is not None:
                    catnm = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
                catcache[c] = catnm
        lt = None
        if self.timerstat != 'idle':
            sec = report.section()
            if self.timerstat == 'finished':
                sec.heading = u'Result'
            elif self.timerstat in ['running', 'ready']:
                sec.heading = u'Race in Progress'
            else:
                sec.heading = u'Provisional Result'

            first = True
            for r in self.riders:
                totcount += 1
                bstr = r[COL_BIB].decode('utf-8')  # 'bib'
                nstr = r[COL_NAMESTR].decode('utf-8')  # 'name'
                cstr = r[COL_CAT].decode('utf-8')  # 'cat'
                if cstr.upper() in catcache:
                    cstr = catcache[cstr.upper()]
                pstr = u''  # 'place'
                tstr = u''  # 'time'
                dstr = u''  # 'gap/down'
                placed = False  # placed at finish
                timed = False  # timed at finish
                if r[COL_INRACE]:
                    psrc = r[COL_PLACE].decode('utf-8')
                    if psrc != u'':
                        pstr = psrc + u'.'
                        placed = True
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if bt is not None:
                        timed = True
                        fincount += 1  # for accounting, use bunch time
                        tstr = bt.rawtime(1)
                        if bt != lt:
                            if not first:
                                gapcount += 1
                                if not self.gapfinish:
                                    sec.lines.append([None, None, None])
                                dstr = '+' + (bt - wt).rawtime(1)
                        if wt is None:  # first finish time
                            wt = bt
                            first = False
                            if self.event[u'type'] == u'rhcp':
                                # winner usually not from scratch
                                dstr = wt.rawtime(0)
                        # compute elapsed
                        et = bt
                        # elapsed is written into bunch time for trtt
                        #if r[COL_STOFT] is not None:	# apply a start offset
                        #dofastest = True	# will need to report!
                        #et = bt - r[COL_STOFT]
                        #tstr = et.rawtime(1)
                        #if we is None:
                        #we = et
                        if fastest is None or et < fastest:
                            fastest = et
                            fastestbib = r[COL_BIB]
                    else:  # check virtual finish time
                        pass
                        #if r[COL_STOFT] is not None:
                        #vt = curelap - r[COL_STOFT]
                        #if vfastest is None or vt < vfastest:
                        #vfastest = vt
                    lt = bt
                else:
                    # Non-finishers dns, dnf. hd, dsq
                    placed = True  # for purpose of listing
                    comment = r[COL_COMMENT].decode('utf-8')
                    if comment == u'':
                        comment = u'dnf'
                    if comment != lcomment:
                        sec.lines.append([None, None, None])  # new bunch
                    lcomment = comment
                    # account for special cases
                    if comment == u'dns':
                        dnscount += 1
                    elif comment == u'hd':
                        hdcount += 1
                    else:
                        dnfcount += 1
                    pstr = comment
                if placed or timed:
                    sec.lines.append([pstr, bstr, nstr, cstr, tstr, dstr])
            ret.append(sec)
            # check bunchthresh
            if fincount > 6 and float(gapcount) / float(fincount) > BREAKTHRESH:
                self.gapfinish = True
            else:
                self.gapfinish = False

            # Race metadata / UCI comments
            sec = report.bullet_text()
            if wt is not None:
                sec.lines.append([None, u'Race time: ' + wt.rawtime(0)])
                if we is None:
                    we = wt
                dval = self.meet.get_distance()
                if dval is not None:
                    sec.lines.append([
                        None, u'Average speed of the winner: ' +
                        we.speedstr(1000.0 * dval)
                    ])
            if dofastest:
                if vfastest and vfastest < fastest:
                    self.log.info(u'Fastest time not yet available.')
                else:
                    ftr = self.getrider(fastestbib)
                    fts = u''
                    if ftr is not None:
                        ftcat = ftr[COL_CAT].decode('utf-8')
                        if ftcat.upper() in catcache:
                            ftcat = catcache[ftcat.upper()]
                        fts = ftr[COL_NAMESTR].decode('utf-8') + u' ' + ftcat
                    fmsg = (u'Fastest time: ' + fastest.rawtime(0) + u'  ' +
                            fastestbib + u' ' + fts)
                    sec.lines.append([None, fmsg])
                    if not self.readonly:  # in a ui window?
                        self.meet.cmd_announce(u'resultmsg', fmsg)

            sec.lines.append(
                [None, u'Number of starters: ' + unicode(totcount - dnscount)])
            if hdcount > 0:
                sec.lines.append([
                    None,
                    u'Riders finishing out of time limits: ' + unicode(hdcount)
                ])
            if dnfcount > 0:
                sec.lines.append([
                    None, u'Riders abandoning the race: ' + unicode(dnfcount)
                ])
            residual = totcount - (fincount + dnfcount + dnscount + hdcount)
            if residual > 0:
                self.log.info(u'Unaccounted for: ' + unicode(residual))
            ret.append(sec)

            # Decisions of commissaires panel
            if len(self.comment) > 0:
                sec = report.bullet_text()
                sec.heading = u'Decisions of the Commissaires Panel'
                for cl in self.comment:
                    sec.lines.append([None, cl.strip()])
                ret.append(sec)
        else:
            self.log.info(u'Result report skipped - not started yet.')
        return ret

    def stat_but_clicked(self, button=None):
        """Deal with a status button click in the main container."""
        self.log.info('Stat button clicked.')

    def ctrl_change(self, acode='', entry=None):
        """Notify change in action combo."""
        if acode == 'fin':
            if entry is not None:
                entry.set_text(self.places)
        elif acode in self.intermeds:
            if entry is not None:
                entry.set_text(self.intermap[acode]['places'])
        else:
            if entry is not None:
                entry.set_text('')

    def race_ctrl(self, acode='', rlist=''):
        """Apply the selected action to the provided bib list."""
        self.checkpoint_model()
        if acode in self.intermeds:
            rlist = strops.reformat_placelist(rlist)
            if self.checkplaces(rlist, dnf=False):
                self.intermap[acode]['places'] = rlist
                self.recalculate()
                self.intsprint(acode, rlist)
                self.log.info('Intermediate ' + repr(acode) + ' == ' +
                              repr(rlist))
            else:
                self.log.error('Intermediate ' + repr(acode) + ' not updated.')
                return False
        elif acode == 'fin':
            rlist = strops.reformat_placelist(rlist)
            if self.checkplaces(rlist):
                self.places = rlist
                self.recalculate()
                self.finsprint(rlist)
                return False
            else:
                self.log.error('Places not updated.')
                return False
        elif acode == 'dnf':
            self.dnfriders(strops.reformat_biblist(rlist))
            return True
        elif acode == 'dsq':
            self.dnfriders(strops.reformat_biblist(rlist), 'dsq')
            return True
        elif acode == 'hd':
            self.dnfriders(strops.reformat_biblist(rlist), 'hd')
            return True
        elif acode == 'dns':
            self.dnfriders(strops.reformat_biblist(rlist), 'dns')
            return True
        elif acode == 'ret':
            self.retriders(strops.reformat_biblist(rlist))
            return True
        elif acode == 'del':
            rlist = strops.reformat_riderlist(rlist, self.meet.rdb,
                                              self.series)
            for bib in rlist.split():
                self.delrider(bib)
            return True
        elif acode == 'add':
            rlist = strops.reformat_riderlist(rlist, self.meet.rdb,
                                              self.series)
            for bib in rlist.split():
                self.addrider(bib)
            return True
        elif acode == 'que':
            rlist = strops.reformat_biblist(rlist)
            if rlist != '':
                for bib in rlist.split():
                    self.query_rider(bib)
            return True
        elif acode == 'com':
            self.add_comment(rlist)
            return True
        elif acode == 'man':
            # crude hack tool for now
            self.manpassing(strops.reformat_bibserlist(rlist))
            return True
        elif acode == 'run':
            team = rlist.strip()
            if team:
                self.running_team = team
            else:
                self.running_team = None
                self.meet.scb.add_rider([u'', u'', u'', u'', u''], 'finpanel')
        else:
            self.log.error('Ignoring invalid action.')
        return False

    def add_comment(self, comment=''):
        """Append a race comment."""
        self.comment.append(comment.strip())
        self.log.info('Added race comment: ' + repr(comment))

    def manpassing(self, biblist=''):
        """Register a manual passing, preserving order."""
        for bib in biblist.split():
            # NOTE: All bibs are sent into trigger interface so that
            #       timing master will propagate to slaves, race module
            #       will process as if timing came from attached decoder.
            rno, rser = strops.bibstr2bibser(bib)
            if not rser:  # allow series manual override
                rser = self.series
            bibstr = strops.bibser2bibstr(rno, rser)
            self.meet.timer.trig(refid=u'riderno:' + bibstr)
            self.log.debug(u'Manual Passing: ' + bibstr)

    def query_rider(self, bib=None):
        """List info on selected rider in the scratchpad."""
        self.log.info('Query rider: ' + repr(bib))
        r = self.getrider(bib)
        if r is not None:
            ns = strops.truncpad(r[COL_NAMESTR] + ' ' + r[COL_CAT], 30)
            bs = ''
            bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
            if bt is not None:
                bs = bt.timestr(1)
            ps = r[COL_COMMENT]
            if r[COL_PLACE] != '':
                ps = strops.rank2ord(r[COL_PLACE])
            self.log.info(' '.join([bib, ns, bs, ps]))
            lt = None
            if len(r[COL_RFSEEN]) > 0:
                for rft in r[COL_RFSEEN]:
                    nt = rft.truncate(0)
                    ns = rft.timestr(1)
                    ls = ''
                    if lt is not None:
                        ls = (nt - lt).timestr(0)
                    self.log.info(' '.join(['\t', ns, ls]))
                    lt = nt
            if r[COL_RFTIME] is not None:
                self.log.info(' '.join([' Finish:', r[COL_RFTIME].timestr(1)]))
        else:
            self.log.info(bib.ljust(4) + ' ' + 'Not in startlist.')

    def startlist_gen(self, cat=''):
        """Generator function to export a startlist."""
        mcat = self.ridercat(cat)
        self.reorder_startlist()
        for r in self.riders:
            if mcat == '' or mcat == self.ridercat(r[COL_CAT]):
                start = ''
                if r[COL_STOFT] is not None and r[COL_STOFT] != tod.ZERO:
                    start = r[COL_STOFT].rawtime(0)
                bib = r[COL_BIB]
                series = self.series
                name = r[COL_NAMESTR]
                cat = r[COL_CAT]
                firstxtra = ''
                lastxtra = ''
                clubxtra = ''
                dbr = self.meet.rdb.getrider(r[COL_BIB], self.series)
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

    def result_gen(self, cat=''):
        """Generator function to export a final result."""
        self.recalculate()  # fix up ordering of rows
        mcat = self.ridercat(cat)
        rcount = 0
        lrank = None
        lcrank = None
        for r in self.riders:
            if mcat == '' or mcat == self.ridercat(r[COL_CAT]):
                rcount += 1
                bib = r[COL_BIB]
                crank = None
                rank = None
                bonus = None
                ft = None
                if r[COL_INRACE]:
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    ft = bt
                    #if r[COL_STOFT] is not None and bt is not None:
                    #if self.event[u'type'] != u'rhcp':
                    #ft = bt - r[COL_STOFT]
                    #else:
                    ## for handicap, time is stage time, bonus
                    ## carries the start offset, elapsed is:
                    ## stage - bonus
                    #ft = bt
                    #bonus = r[COL_STOFT]

                if r[COL_PLACE].isdigit():
                    rank = int(r[COL_PLACE])
                    if rank != lrank:
                        crank = rcount
                    else:
                        crank = lcrank
                    lcrank = crank
                    lrank = rank
                else:
                    crank = r[COL_COMMENT]
                if self.event[u'type'] != u'rhcp' and (
                        bib in self.bonuses or r[COL_BONUS] is not None):
                    bonus = tod.ZERO
                    if bib in self.bonuses:
                        bonus += self.bonuses[bib]
                    if r[COL_BONUS] is not None:
                        bonus += r[COL_BONUS]
                penalty = None
                if r[COL_PENALTY] is not None:
                    penalty = r[COL_PENALTY]
                yield [crank, bib, ft, bonus, penalty]

    def clear_results(self):
        """Clear all data from event model."""
        self.resetplaces()
        self.places = ''
        self.log.debug('Cleared event result.')

    def getrider(self, bib):
        """Return reference to selected rider no."""
        ret = None
        for r in self.riders:
            if r[COL_BIB] == bib:
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

    def starttime(self, start=None, bib='', series=''):
        """Adjust start time for the rider."""
        if series == self.series:
            r = self.getrider(bib)
            if r is not None:
                r[COL_STOFT] = start

    def addrider(self, bib='', series=None):
        """Add specified rider to race model."""
        if series is not None and series != self.series:
            self.log.debug('Ignoring non-series starter: ' +
                           repr(strops.bibser2bibstr(bib, series)))
            return None
        if bib == '' or self.getrider(bib) is None:
            nr = [
                bib, '', '', '', '', True, '', 0, None, None, None, None, None,
                None, [], ''
            ]  # add team key
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
                nr[COL_TEAM] = self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB)
            return self.riders.append(nr)
        else:
            return None

    def resettimer(self):
        """Reset race timer."""
        self.meet.alttimer.dearm(1)
        self.set_finish()
        self.set_start()
        self.clear_results()
        self.timerstat = 'idle'
        self.meet.cmd_announce(u'timerstat', u'idle')
        uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Idle')
        self.meet.stat_but.set_sensitive(True)
        self.curlap = 0
        self.live_announce = True

    def armstart(self):
        """Process an armstart request."""
        if self.timerstat == 'idle':
            self.timerstat = 'armstart'
            self.meet.cmd_announce(u'timerstat', u'armstart')
            self.meet.cmd_announce(u'timerstat', u'armstart')
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_armstart,
                             'Arm Start')
        elif self.timerstat == 'armstart':
            self.timerstat = 'idle'
            self.meet.cmd_announce(u'timerstat', u'idle')
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Idle')
        elif self.timerstat == 'ready':
            self.set_running()

    def armfinish(self):
        """Process an armfinish request."""
        if self.timerstat in ['ready', 'running', 'finished']:
            if self.totlaps and self.curlap < self.totlaps:
                # assume lap was not armed
                self.armlap()
            self.timerstat = 'armfinish'
            self.meet.cmd_announce(u'timerstat', u'armfinish')
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_armfin,
                             'Arm Finish')
            self.meet.stat_but.set_sensitive(True)
            self.meet.alttimer.armlock(True)
            self.meet.alttimer.arm(1)
        elif self.timerstat == 'armfinish':
            self.meet.alttimer.dearm(1)
            self.timerstat = 'running'
            self.meet.cmd_announce(u'timerstat', u'running')
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Running')

    def last_rftime(self):
        """Find the last rider with a RFID finish time set."""
        ret = None
        for r in self.riders:
            if r[COL_RFTIME] is not None:
                ret = r[COL_BIB]
        return ret

    def armlap(self):
        ## announce text handling...
        self.scratch_tot = 0
        self.scratch_map = {}
        self.scratch_ord = []
        if self.live_announce:
            self.meet.announce_clear()
            self.meet.announce_title(self.title_namestr.get_text())

        self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
        self.running_team = None
        self.meet.scb.add_rider([u'', u'', u'', u'', u''], 'finpanel')

    def key_event(self, widget, event):
        """Handle global key presses in event."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_abort:  # override ctrl+f5
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
                elif key.upper() == key_undo:  # Undo model change if possible
                    self.undo_riders()
                    return True
            if key[0] == 'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_announce:
                    if self.places:
                        self.finsprint(self.places)
                    else:
                        self.reannounce_lap()
                    return True
                elif key == key_armfinish:
                    self.armfinish()
                    return True
                elif key == key_raceover:
                    self.set_finished()
                    return True
                elif key == key_armlap:
                    ## lap timing vars
                    self.armlap()
                    return True
                elif key == key_placesto:
                    self.fill_places_to_selected()
                    return True
                elif key == key_appendplace:
                    self.append_selected_place()
                    return True
        return False

    def append_selected_place(self):
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB)
            oplaces = self.places.split()
            if selbib in oplaces:
                oplaces.remove(selbib)
            oplaces.append(selbib)
            self.checkpoint_model()
            self.places = ' '.join(oplaces)
            self.__dorecalc = True  # flag recalculate

    def fill_places_to_selected(self):
        """Update places to match ordering up to selected rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB)
            self.fill_places_to(selbib)

    def clear_places_from_selection(self):
        """Clear all places from riders following the current selection."""
        if self.places.find('-') > 0:
            self.log.warn('Clear place with dead heat not implemented.')
            return
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB)
            oplaces = self.places.split()
            nplaces = []
            for p in oplaces:
                if p == selbib:
                    break
                nplaces.append(p)
            self.checkpoint_model()
            self.places = ' '.join(nplaces)
            self.__dorecalc = True  # flag recalculate

    def clear_selected_place(self):
        """Remove the selected rider from places."""
        if self.places.find('-') > 0:
            self.log.warn('Clear place with dead heat not implemented.')
            return

        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB)
            oplaces = self.places.split()
            if selbib in oplaces:
                oplaces.remove(selbib)
            self.checkpoint_model()
            self.places = ' '.join(oplaces)
            self.__dorecalc = True  # flag recalculate

    def dnfriders(self, biblist='', code='dnf'):
        """Remove each rider from the race with supplied code."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[COL_INRACE] = False
                r[COL_COMMENT] = code
                recalc = True
                self.log.info('Rider ' + str(bib) +
                              ' did not finish with code: ' + code)
            else:
                self.log.warn('Unregistered Rider ' + str(bib) + ' unchanged.')
        if recalc:
            self.recalculate()
        return False

    def retriders(self, biblist=''):
        """Return all listed riders to the race."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[COL_INRACE] = True
                r[COL_COMMENT] = ''
                recalc = True
                self.log.info('Rider ' + str(bib) + ' returned to race.')
            else:
                self.log.warn('Unregistered Rider ' + str(bib) + ' unchanged.')
        if recalc:
            self.recalculate()
        return False

    def shutdown(self, win=None, msg='Race Sutdown'):
        """Close event."""
        self.log.debug('Event shutdown: ' + msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def starttrig(self, e):
        """Process a 'start' trigger signal."""
        self.log.info(u'Trigger: ' + e.rawtime(2))
        if self.timerstat == 'armstart':
            self.set_start(e)
        return False

    def set_lap_finish(self, e):
        """Write lap time into model and emit changed state."""
        self.lapfin = e
        self.meet.cmd_announce(u'laptype', None)
        self.reannounce_times()

    def timertrig(self, e):
        """Record timer message from alttimer."""
        self.meet.announce_timer(e, self.meet.alttimer)
        if self.timerstat == 'armfinish':
            self.log.info(u'Finish Trigger: ' + e.rawtime(2))

    def rfidstat(self, e):
        """Handle RFID status message."""
        self.log.info(u'Decoder ' + e.source + u': ' + e.refid)
        return False

    def rfidtrig(self, e):
        """Process rfid event."""
        # bounce message onto announcer: HACK
        self.meet.announce_timer(e)

        if e.refid in ['', '255']:  # got a trigger
            return self.starttrig(e)
        elif e.chan == u'STS':  # status message
            return self.rfidstat(e)

        # else assume this is a passing
        r = self.meet.rdb.getrefid(e.refid)
        if r is None:
            self.log.info('Unknown tag: ' + e.refid + '@' + e.rawtime(2))
            return False

        bib = self.meet.rdb.getvalue(r, riderdb.COL_BIB)
        ser = self.meet.rdb.getvalue(r, riderdb.COL_SERIES)
        rcat = self.get_ridercat(bib)
        if rcat is None:
            rcat = u''
        if ser != self.series:
            self.log.error('Ignored non-series rider: ' + bib + '.' + ser)
            return False

        # at this point should always have a valid source rider vector
        lr = self.getrider(bib)
        if lr is None:
            self.log.warn('Ignoring non starter: ' + bib + ' @ ' +
                          e.rawtime(2))
            return False

        if not lr[COL_INRACE]:
            self.log.warn('Withdrawn rider: ' + lr[COL_BIB] + ' @ ' +
                          e.rawtime(2))
            # but continue anyway just in case it was not correct?
        else:
            self.log.info('Saw: ' + bib + ' @ ' + e.rawtime(2))

        # check run state
        if self.timerstat in ['idle', 'ready', 'armstart']:
            return False

        # save RF ToD into 'passing' vector and log
        # check elapsed time against passing
        st = tod.ZERO
        if lr[COL_STOFT] is not None:
            st = lr[COL_STOFT]
        if self.start is not None:
            if self.minlap is not None:
                st += self.start + self.minlap
            else:
                st = self.start + tod.tod(u'30')  # 30sec fudge pad

        if e <= st:
            self.log.info(u'Ignored early passing: ' + bib + ' < ' +
                          st.rawtime(2))
            return False
        else:
            if self.minlap is not None:
                if len(lr[COL_RFSEEN]) > 0:
                    lastlap = lr[COL_RFSEEN][-1]
                    nl = lastlap + self.minlap
                    if e <= nl:
                        self.log.info(u'Ignored short lap: ' + bib + ' < ' +
                                      nl.rawtime(2))
                        return False

        lr[COL_RFSEEN].append(e)
        catfinish = False
        if rcat in self.catlaps and self.catlaps[rcat]:
            targetlap = self.catlaps[rcat]
            self.log.debug(u'Target lap for this rider is: ' + repr(targetlap))
            if lr[COL_LAPS] >= targetlap - 1:
                catfinish = True  # arm just this rider

        if self.timerstat == 'armfinish' or catfinish:
            st = tod.ZERO
            if lr[COL_STOFT] is not None:
                st = lr[COL_STOFT]
            st += self.minelap  # check agains minelap even if lap count arms
            if e < st:
                self.log.info(u'Ignored early finish: ' + bib + ' < ' +
                              st.rawtime(2))
                return False

            if self.finish is None:
                if self.live_announce:
                    self.meet.announce_title('Finish')
                self.set_finish(e)
            if lr[COL_RFTIME] is None:
                lr[COL_LAPS] += 1
                lr[COL_RFTIME] = e
                self.__dorecalc = True  # cleared in timeout, from same thread
                if self.announce_team is None:
                    # announce this rider's team in recalc(dubious)
                    theteam = lr[COL_TEAM]
                    if theteam not in self.announced_teams:
                        self.announce_team = lr[COL_TEAM]
                if lr[COL_INRACE]:
                    if self.lapfin is None:
                        if lr[COL_LAPS] == self.curlap:
                            self.set_lap_finish(e)
                        else:
                            pass
                            #self.log.info(u'Lap finish ignored: ' +
                            #lr[COL_BIB] + u'@' + e.rawtime(3)
                            #+ u' on lap ' + unicode(lr[COL_LAPS]))
                    #if self.scratch_start is None:
                    #self.scratch_start = e
                    self.announce_rider('', bib, lr[COL_NAMESTR], lr[COL_CAT],
                                        e)
            else:
                self.log.error('Duplicate finish rider = ' + bib + ' @ ' +
                               e.rawtime())
        elif self.timerstat in ['running']:
            lr[COL_LAPS] += 1
            if lr[COL_INRACE]:
                self.announce_rider('', bib, lr[COL_NAMESTR], lr[COL_CAT], e)
        return False  # called from glib_idle_add

    def announce_rider(self, place, bib, namestr, cat, rftime):
        """Log a rider in the lap and emit to announce."""
        if bib not in self.scratch_map:
            self.scratch_map[bib] = rftime
            self.scratch_ord.append(bib)
        if self.live_announce:
            glib.idle_add(self.meet.announce_rider,
                          [place, bib, namestr, cat,
                           rftime.rawtime()])

    def finsprint(self, places):
        """Display a final sprint 'provisional' result."""

        self.live_announce = False
        self.meet.announce_clear()
        scrollmsg = u'Provisional - '
        self.meet.announce_title('Provisional Result')
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
                        glib.idle_add(self.meet.announce_rider, [
                            r[COL_PLACE] + '.', bib, r[COL_NAMESTR],
                            r[COL_CAT], fs
                        ])
                    idx += 1
        self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        if wt is not None:
            pass

    def intsprint(self, acode='', places=''):
        """Display an intermediate sprint 'provisional' result."""

        ## TODO : Fix offset time calcs - too many off by ones
        if acode not in self.intermeds:
            self.log.debug('Attempt to display non-existent intermediate: ' +
                           repr(acode))
            return
        descr = acode
        if self.intermap[acode]['descr']:
            descr = self.intermap[acode]['descr']
        self.live_announce = False
        self.meet.announce_clear()
        self.reannounce_times()
        self.meet.announce_title(descr)
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
                        glib.idle_add(self.meet.announce_rider, [
                            str(curplace) + '.', bib, r[COL_NAMESTR],
                            r[COL_CAT], ''
                        ])
                    idx += 1
                else:
                    self.log.warn('Duplicate no. = ' + str(bib) +
                                  ' in places.')
        self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        glib.timeout_add_seconds(15, self.reannounce_lap)

    def todempty(self, val):
        if val is not None:
            return val.rawtime()
        else:
            return u''

    def reannounce_times(self):
        """Re-send the current timing values."""
        self.meet.cmd_announce(u'timerstat', self.timerstat)
        return False

    def reannounce_lap(self):
        """Re-send current lap passing data."""
        self.meet.announce_clear()
        self.meet.cmd_announce(u'scrollmsg', None)
        self.reannounce_times()
        self.live_announce = False
        if self.timerstat == 'armfinish':
            self.meet.announce_title('Finish')
        else:
            self.meet.announce_title(self.title_namestr.get_text())
        for bib in self.scratch_ord:
            r = self.getrider(bib)
            if r is not None:
                glib.idle_add(self.meet.announce_rider, [
                    '', bib, r[COL_NAMESTR], r[COL_CAT],
                    self.scratch_map[bib].rawtime()
                ])
        self.live_announce = True
        return False

    def timeout(self):
        """Update elapsed time and recalculate if required."""
        if not self.winopen:
            return False
        if self.__dorecalc:
            self.__dorecalc = False
            self.recalculate()
        if self.running_team is not None:
            # bounce a running time onto the panel - HACK
            self.bounceruntime(self.running_team, u'')
        return True

    def set_start(self, start=''):
        """Set the start time."""
        self.lapstart = None
        self.lapfin = None
        if isinstance(start, tod.tod):
            self.start = start
        else:
            self.start = tod.mktod(start)
        if self.start is not None:
            self.last_scratch = self.start
            if self.finish is None:
                self.set_running()

    def set_finish(self, finish=''):
        """Set the finish time."""
        if isinstance(finish, tod.tod):
            self.finish = finish
        else:
            self.finish = tod.mktod(finish)
        if self.finish is None:
            if self.start is not None:
                self.set_running()
        else:
            if self.start is None:
                self.set_start('0')

    def get_elapsed(self):
        """Hack mode - always returns time from start."""
        ret = None
        if self.start is not None and self.timerstat != 'finished':
            ret = (tod.now() - self.start).truncate(0)
        return ret

    def set_ready(self):
        """Update event status to ready to go."""
        self.timerstat = 'ready'
        self.meet.cmd_announce(u'timerstat', u'ready')
        uiutil.buttonchg(self.meet.stat_but, uiutil.bg_armint, 'Ready')

    def set_running(self):
        """Update event status to running."""
        self.timerstat = 'running'
        self.meet.cmd_announce(u'timerstat', u'running')
        uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Running')

    def set_finished(self):
        """Update event status to finished."""
        self.timerstat = 'finished'
        self.meet.cmd_announce(u'timerstat', u'finished')
        self.meet.cmd_announce(u'laplbl', None)
        self.meet.cmd_announce(u'laptype', None)
        uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Finished')
        self.meet.stat_but.set_sensitive(False)
        if self.finish is None:
            self.set_finish(tod.now())

    def title_place_xfer_clicked_cb(self, button, data=None):
        """Transfer current rider list order to place entry."""
        nplaces = ''
        lplace = None
        for r in self.riders:
            if r[COL_INRACE] and r[COL_PLACE] != '':
                if lplace == r[COL_PLACE] and r[COL_PLACE] != '':
                    nplaces += '-' + r[COL_BIB]  # dead heat riders
                else:
                    nplaces += ' ' + r[COL_BIB]
                    lplace = r[COL_PLACE]
        self.places = strops.reformat_placelist(nplaces)
        self.meet.action_combo.set_active(10)  # 'fin'
        self.meet.action_entry.set_text(self.places)

    def fill_places_to(self, bib=None):
        """Fill in finish places up to the nominated bib."""
        if self.places.find('-') > 0:
            self.log.warn('Fill places with dead heat not yet implemented.')
            return
        oplaces = self.places.split()  # only patch if no dead heats
        nplaces = []
        for r in self.riders:
            if r[COL_INRACE]:
                if r[COL_BIB] in oplaces:
                    oplaces.remove(r[COL_BIB])  # remove from old list
                nplaces.append(r[COL_BIB])  # add to new list
            else:
                if r[COL_BIB] in oplaces:
                    oplaces.remove(r[COL_BIB])  # strip out DNFed riders
            if r[COL_BIB] == bib:  # break after to get sel rider
                break
        nplaces.extend(oplaces)
        self.checkpoint_model()
        self.places = ' '.join(nplaces)
        self.recalculate()

    def info_time_edit_clicked_cb(self, button, data=None):
        """Run an edit times dialog to update race time."""
        st = ''
        if self.start is not None:
            st = self.start.rawtime(2)
        ft = ''
        if self.finish is not None:
            ft = self.finish.rawtime(2)
        ret = uiutil.edit_times_dlg(self.meet.window, stxt=st, ftxt=ft)
        if ret[0] == 1:
            self.set_start(ret[1])
            self.set_finish(ret[2])
            self.log.info('Adjusted race times.')

    def editcol_cb(self, cell, path, new_text, col):
        """Edit column callback."""
        new_text = new_text.strip()
        self.riders[path][col] = new_text

    def editlap_cb(self, cell, path, new_text, col):
        """Edit the lap field if valid."""
        new_text = new_text.strip()
        if new_text.isdigit():
            self.riders[path][col] = int(new_text)
        else:
            self.log.error('Invalid lap count.')

    def resetplaces(self):
        """Clear places off all riders."""
        for r in self.riders:
            r[COL_PLACE] = ''
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
                    if y[3] == '':
                        return -1
                    elif x[3] == '':
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
        otxt = ''
        if st is not None:
            otxt = st.rawtime(0)
        cr.set_property('text', otxt)

    def showbunch_cb(self, col, cr, model, iter, data=None):
        """Update bunch time on rider view."""
        cb = model.get_value(iter, COL_CBUNCH)
        mb = model.get_value(iter, COL_MBUNCH)
        if mb is not None:
            cr.set_property('text', mb.rawtime(1))
            cr.set_property('style', pango.STYLE_OBLIQUE)
        else:
            cr.set_property('style', pango.STYLE_NORMAL)
            if cb is not None:
                cr.set_property('text', cb.rawtime(1))
            else:
                seen = model.get_value(iter, COL_RFSEEN)
                if len(seen) > 0:
                    if self.start:
                        et = seen[-1] - self.start
                    else:
                        et = seen[-1]
                    cr.set_property('text', u'[' + et.rawtime(1) + u']')
                    cr.set_property('style', pango.STYLE_OBLIQUE)
                else:
                    cr.set_property('text', '')

    def editstart_cb(self, cell, path, new_text, col=None):
        """Edit start time on rider view."""
        newst = tod.mktod(new_text)
        if newst:
            newst = newst.truncate(0)
        self.riders[path][COL_STOFT] = newst

    def editbunch_cb(self, cell, path, new_text, col=None):
        """Edit bunch time on rider view."""
        # NOTE: This is the cascading bunch time editor,
        new_text = new_text.strip()
        dorecalc = False
        if new_text == '':  # user request to clear RFTIME?
            self.riders[path][COL_RFTIME] = None
            self.riders[path][COL_MBUNCH] = None
            self.riders[path][COL_CBUNCH] = None
            dorecalc = True
        else:
            # get 'current bunch time'
            omb = self.vbunch(self.riders[path][COL_CBUNCH],
                              self.riders[path][COL_MBUNCH])

            # assign new bunch time
            nmb = tod.mktod(new_text)
            if self.riders[path][COL_MBUNCH] != nmb:
                self.riders[path][COL_MBUNCH] = nmb
                dorecalc = True
            # bunch times don't cascade for trtt
            #if nmb is not None:
            #i = int(path)+1
            #tl = len (self.riders)
            ## until next rider has mbunch set OR place clear assign new bt
            #while i < tl:
            #ivb = self.vbunch(self.riders[i][COL_CBUNCH],
            #self.riders[i][COL_MBUNCH])
            #if (self.riders[i][COL_PLACE] != ''
            #and (ivb is None
            #or ivb == omb)):
            #self.riders[i][COL_MBUNCH] = nmb
            #dorecalc = True
            #else:
            #break
            #i += 1
        if dorecalc:
            self.recalculate()

    def checkplaces(self, rlist='', dnf=True):
        """Check the proposed places against current race model."""
        ret = True
        placeset = set()
        for no in strops.reformat_biblist(rlist).split():
            if no != 'x':
                # repetition? - already in place set?
                if no in placeset:
                    self.log.error('Duplicate no in places: ' + repr(no))
                    ret = False
                placeset.add(no)
                # rider in the model?
                lr = self.getrider(no)
                if lr is None:
                    self.log.error('Non-starter in places: ' + repr(no))
                    ret = False
                else:
                    # rider still in the race?
                    if not lr[COL_INRACE]:
                        self.log.info('DNF/DNS rider in places: ' + repr(no))
                        if dnf:
                            ret = False
            else:
                # placeholder needs to be filled in later or left off
                self.log.info('Placeholder in places.')
        return ret

    def recalculate(self):
        """Recalculator, acquires lock and then continues."""
        if not self.recalclock.acquire(False):
            self.log.warn('Recalculate already in progress.')
            return None  # allow only one entry
        try:
            self.__recalc()
        finally:
            self.recalclock.release()

    def get_ridercat(self, bib):
        """Return the primary category for the specified rider."""
        ret = None  # unknown
        r = self.getrider(bib)
        if r is not None:
            ret = self.ridercat(r[COL_CAT].decode('utf-8').upper())
        return ret

    def get_cat_placesr(self, cat):
        """Return a normalised place str for a cat within main places."""
        placestr = self.places
        pgroups = []
        lp = None
        ng = []
        for placegroup in placestr.split():
            cg = []
            for bib in placegroup.split('-'):
                if self.get_ridercat(bib) == cat:
                    cg.append(bib)
            if len(cg) > 0:  # >= one cat rider in this group
                pgroups.append(u'-'.join(cg))

        ret = u' '.join(pgroups)
        self.log.debug('Cat ' + repr(cat) + ' finish: ' + repr(ret))
        return ret

    def assign_finish(self):
        """Transfer finish line places into rider model."""
        placestr = self.places
        placeset = set()
        idx = 0
        for placegroup in placestr.split():
            curplace = idx + 1
            for bib in placegroup.split('-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is None:
                        self.addrider(bib)
                        r = self.getrider(bib)
                    if r[COL_INRACE]:
                        idx += 1
                        r[COL_PLACE] = str(curplace)
                    else:
                        self.log.warn('DNF Rider ' + str(bib) +
                                      ' in finish places.')
                else:
                    self.log.warn('Duplicate no. = ' + str(bib) +
                                  ' in finish places.')

    def assign_places(self, contest):
        """Transfer points and bonuses into the named contest."""
        # fetch context meta infos
        src = self.contestmap[contest]['source']
        if src not in RESERVED_SOURCES and src not in self.intermeds:
            self.log.info('Invalid intermediate source: ' + repr(src) +
                          ' in contest: ' + repr(contest))
            return
        countbackwinner = False  # for stage finish only track winner in cb
        category = self.contestmap[contest]['category']
        tally = self.contestmap[contest]['tally']
        bonuses = self.contestmap[contest]['bonuses']
        points = self.contestmap[contest]['points']
        allsrc = self.contestmap[contest]['all_source']
        allpts = 0
        allbonus = tod.ZERO
        if allsrc:
            if len(points) > 0:
                allpts = points[0]
            if len(bonuses) > 0:
                allbonus = bonuses[0]
        placestr = ''
        if src == 'fin':
            placestr = self.places
            if tally != u'climb':  # really only for sprints
                countbackwinner = True
        elif src == 'reg':
            placestr = self.get_startlist()
        elif src == 'start':
            placestr = self.get_starters()
        elif src in self.catplaces:  # ERROR -> cat climb tally needs type?
            placestr = self.get_cat_placesr(self.catplaces[src])
            countbackwinner = True
        else:
            placestr = self.intermap[src]['places']
        placeset = set()
        idx = 0
        for placegroup in placestr.split():
            curplace = idx + 1
            for bib in placegroup.split('-'):
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
                        if allpts != 0:
                            if bib in self.points[tally]:
                                self.points[tally][bib] += allpts
                            else:
                                self.points[tally][bib] = allpts
                                self.pointscb[tally][bib] = strops.countback()
                            # No countback for all_source entries
                    else:  # points/bonus as per config
                        if len(bonuses) >= curplace:  # bonus is vector
                            if bib in self.bonuses:
                                self.bonuses[bib] += bonuses[curplace - 1]
                            else:
                                self.bonuses[bib] = bonuses[curplace - 1]
                        if tally:
                            if len(points) >= curplace:  # points vector
                                if bib in self.points[tally]:
                                    self.points[tally][bib] += points[curplace
                                                                      - 1]
                                else:
                                    self.points[tally][bib] = points[curplace -
                                                                     1]
                            if bib not in self.pointscb[tally]:
                                self.pointscb[tally][bib] = strops.countback()
                        if countbackwinner:  # stage finish
                            if curplace == 1:  # only track winner at finish
                                self.pointscb[tally][bib][0] += 1
                        else:  # intermediate/other
                            if tally == u'climb':  # climbs countback on category winners only
                                if curplace == 1:
                                    self.pointscb[tally][bib][category] += 1
                            else:
                                self.pointscb[tally][bib][curplace] += 1
                else:
                    self.log.warn('Duplicate no. = ' + str(bib) + ' in ' +
                                  repr(contest) + ' places.')

    def bounceteam(self, team, cat, time):
        ## HACK: bounce a teamname and time onto the panel
        tname = u''
        # lookup team name in rdb
        dbr = self.meet.rdb.getrider(team, u'team')
        if dbr is not None:
            #tname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
            tname = self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB)
        #tcat = self.ridercat(cat)
        tstr = time.rawtime(1) + u' '  # hunges blanked
        #self.meet.scb.add_rider([cat,u'',tname,u'',tstr], 'finpanel')
        self.meet.scb.add_rider([u'', team, tname, u'', tstr], 'finpanel')
        return False

    def bounceruntime(self, team, cat):
        ## HACK: bounce a teamname and running time onto the panel
        tname = u''
        # lookup team name in rdb
        dbr = self.meet.rdb.getrider(team, u'team')
        if dbr is not None:
            tname = self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB)
            #tname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
            tstr = u''
        if team in self.teamtimes:
            tstr = self.teamtimes[team].rawtime(1) + u' '
        else:
            tstart = tod.mktod(self.meet.rdb.getvalue(dbr, riderdb.COL_REFID))
            if tstart is not None:
                tstr = (tod.now() - tstart).rawtime(0)
        #self.meet.scb.add_rider([cat,u'',tname,u'',tstr], 'finpanel')
        self.meet.scb.add_rider([u'', team, tname, u'', tstr], 'finpanel')
        return False

    def __recalc(self):
        """Internal 'protected' recalculate function."""
        self.log.debug('Recalculate model.')

        # protect:
        if self.start is None:
            return

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

        # build temp team maps (DANGEROUS!)
        cteam = None
        teamcats = {}
        teammap = {}
        teamnth = {}
        self.log.debug(u'Default Nth Wheel: ' + repr(self.defaultnth))
        for r in self.riders:
            nteam = r[COL_TEAM]
            if nteam != cteam:
                ncat = self.ridercat(r[COL_CAT])
                nth = self.defaultnth  # overridden by cat
                if ncat in self.nthwheel:
                    try:
                        nth = int(self.nthwheel[ncat])
                        self.log.debug(
                            repr(ncat) + u' Nth Wheel: ' + repr(NTH_WHEEL))
                    except Exception as e:
                        self.log.warn(u'Invalid nth wheel for ' + repr(ncat) +
                                      u', default used: ' + repr(nth))
                teammap[nteam] = []
                teamnth[nteam] = nth
                teamcats[nteam] = ncat
                cteam = nteam
            # cteam will be valid at this point
            if r[COL_RFTIME] is not None:  # will already be sorted!
                teammap[cteam].append(r)
                #cteamlist.append(r)

        # scan each team for times
        for t in teammap:
            # unless team has n finishers, there is no time
            tlist = teammap[t]
            nth_wheel = teamnth[t]
            if len(tlist) >= nth_wheel:
                ct = (tlist[nth_wheel - 1][COL_RFTIME] - self.start -
                      tlist[nth_wheel - 1][COL_STOFT])
                thetime = ct.truncate(1)
                self.teamtimes[t] = thetime  # save to times map
                if (t not in self.announced_teams and self.announce_team
                        and self.announce_team == t):
                    # bounce this time onto the panel? HACK
                    self.announced_teams.add(t)
                    self.running_team = None  # cancel a running time
                    self.bounceteam(t, teamcats[t], thetime)
                    self.announce_team = None
                for r in tlist[0:nth_wheel]:
                    r[COL_CBUNCH] = thetime
                for r in tlist[nth_wheel:]:
                    et = r[COL_RFTIME] - self.start - r[COL_STOFT]
                    if self.owntime and (et > ct and
                                         (et - ct) > tod.tod('1.12')):
                        # TIME GAP!
                        thetime = et.truncate(1)
                    r[COL_CBUNCH] = thetime
                    ct = et

        ## ? extra step -> award individuals with their own time (faux itt)

        # pass five: resort on in,vbunch (todo -> check if place cmp reqd)
        #            at this point all riders will have valid bunch time
        auxtbl = []
        idx = 0
        for r in self.riders:
            # aux cols: ind, bib, in, place, vbunch
            auxtbl.append([
                idx, r[COL_BIB], r[COL_INRACE], r[COL_PLACE],
                self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH]), r[COL_COMMENT]
            ])
            idx += 1
        if len(auxtbl) > 1:
            auxtbl.sort(self.sortvbunch)
            self.riders.reorder([a[0] for a in auxtbl])
        return False  # allow idle add

    def new_start_trigger(self, rfid):
        """Collect a RFID trigger signal and apply it to the model."""
        if self.newstartdlg is not None and self.newstartent is not None:
            et = tod.mktod(self.newstartent.get_text())
            if et is not None:
                st = rfid - et
                self.set_start(st)
                self.newstartdlg.response(1)
                self.newstartdlg = None  # try to ignore the 'up' impulse
            else:
                self.log.warn('Invalid elapsed time: Start not updated.')
        return False

    def new_start_trig(self, button, entry=None):
        """Use the current time to update start offset."""
        self.meet.timer.trig(refid='0')

    def verify_timent(self, entry, data=None):
        et = tod.mktod(entry.get_text())
        if et is not None:
            entry.set_text(et.rawtime())
        else:
            self.log.info('Invalid elapsed time.')

    def elapsed_dlg(self, addriders=''):
        """Run a 'new start' dialog."""
        if self.timerstat == 'armstart':
            self.log.error('Start is armed, unarm to add new start time.')
            return

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'new_start.ui'))
        dlg = b.get_object('newstart')
        dlg.set_transient_for(self.meet.window)
        self.newstartdlg = dlg

        timent = b.get_object('time_entry')
        self.newstartent = timent
        timent.connect('activate', self.verify_timent)

        self.meet.timer.setcb(self.new_start_trigger)
        b.get_object('now_button').connect('clicked', self.new_start_trig)

        response = dlg.run()
        self.newstartdlg = None
        if response == 1:  # id 1 set in glade for "Apply"
            self.log.info('Start time updated: ' + self.start.rawtime(2))
        else:
            self.log.info('Set elapsed time cancelled.')
        self.meet.timer.setcb(self.rfidtrig)
        dlg.destroy()

    def time_context_menu(self, widget, event, data=None):
        """Popup menu for result list."""
        self.context_menu.popup(None, None, None, event.button, event.time,
                                selpath)

    def treerow_selected(self, treeview, path, view_column, data=None):
        """Select row, use to confirm places to."""
        if self.timerstat not in ['idle', 'armstart', 'armfinish']:
            self.fill_places_to(self.riders[path][COL_BIB])

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
            nt = entry.get_text().decode('utf-8')
            if nt:  # not empty
                self.totlaps = int(entry.get_text().decode('utf-8'))
            else:
                self.totlaps = None
        except:
            self.log.warn(u'Ignored invalid total lap count.')
        if self.totlaps is not None:
            self.totlapentry.set_text(unicode(self.totlaps))
        else:
            self.totlapentry.set_text(u'')

    def lapentry_activate_cb(self, entry, data=None):
        pass

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
                bib = self.riders.get_value(sel[1], COL_BIB)
                nst = u'-'
                st = self.riders.get_value(sel[1], COL_STOFT)
                if st:
                    nst = st.rawtime(0)
                nft = u'-'
                ft = self.riders.get_value(sel[1], COL_RFTIME)
                if ft:
                    nft = ft.rawtime(2)
                self.log.info(u'Adjusted rider ' + bib + u' - Start: ' + nst +
                              u' Raw Finish: ' + nft)
                self.recalculate()

    def rms_context_clear_activate_cb(self, menuitem, data=None):
        """Clear finish time for selected rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.riders.set_value(sel[1], COL_RFTIME, None)
            self.riders.set_value(sel[1], COL_MBUNCH, None)
            self.riders.set_value(sel[1], COL_CBUNCH, None)
            self.recalculate()

    def rms_context_del_activate_cb(self, menuitem, data=None):
        """Remove selected rider from event."""
        sel = self.view.get_selection().get_selected()
        bib = None
        if sel is not None:
            bib = self.riders.get_value(sel[1], COL_BIB)
            self.delrider(bib)

    def rms_context_refinish_activate_cb(self, menuitem, data=None):
        """Try to automatically re-finish rider from last passing."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            splits = self.riders.get_value(sel[1], COL_RFSEEN)
            if splits is not None and len(splits) > 0:
                self.riders.set_value(sel[1], COL_RFTIME, splits[-1])
                self.recalculate()

    def __init__(self, meet, event, ui=True):
        self.meet = meet
        self.event = event
        self.evno = event[u'evid']
        self.series = event[u'seri']
        self.configpath = meet.event_configfile(self.evno)

        self.log = logging.getLogger('trtt')
        self.log.setLevel(logging.DEBUG)
        self.log.debug(u'opening event: ' + unicode(self.evno))

        self.recalclock = threading.Lock()
        self.__dorecalc = False
        self.teamtimes = {}
        self.announced_teams = set()
        self.announce_team = None
        self.running_team = None  # show running time for team

        # race property attributes

        # race run time attributes
        self.owntime = True  # dropped riders get own time
        self.readonly = not ui
        self.start = None
        self.finish = None
        self.minelap = STARTFUDGE
        self.minlap = None
        self.winopen = True
        self.timerstat = 'idle'
        self.places = ''
        self.comment = []
        self.ridermark = None
        self.cats = []
        self.catplaces = {}
        self.catlaps = {}  # cache of cat lap counts
        self.defaultnth = NTH_WHEEL
        self.nthwheel = {}
        self.autocats = False
        self.bonuses = {}
        self.points = {}
        self.pointscb = {}
        self.gapfinish = False  # set automatically if count>breakthresh

        # intermediates
        self.intermeds = []  # sorted list of intermediate keys
        self.intermap = {}  # map of intermediate keys to results
        self.contests = []  # sorted list of contests
        self.contestmap = {}  # map of contest keys
        self.tallys = []  # sorted list of points tallys
        self.tallymap = {}  # map of tally keys

        # Scratch pad status variables - check if needed?
        self.last_scratch = None
        self.scratch_start = None
        self.scratch_last = None
        self.scratch_count = 0
        self.scratch_tot = 0
        self.curlap = 0
        self.sprintlaps = []
        self.totlaps = None
        self.lapstart = None
        self.lapfin = None

        # lap tracking
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
        self.undomod = gtk.ListStore(
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
        self.canundo = False
        self.placeundo = None

        # !! does this need a builder? perhaps make directly...
        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'rms.ui'))

        # !! destroy??
        self.frame = b.get_object('race_vbox')
        self.frame.connect('destroy', self.shutdown)

        # meta info pane
        self.shortname = None
        self.title_namestr = b.get_object('title_namestr')
        self.set_titlestr()
        self.lapentry = b.get_object('lapentry')
        self.totlapentry = b.get_object('totlapentry')

        # results pane
        t = gtk.TreeView(self.riders)
        t.set_reorderable(True)
        t.set_rules_hint(True)
        t.show()
        self.view = t
        uiutil.mkviewcoltxt(t, 'No.', COL_BIB, calign=1.0)
        uiutil.mkviewcoltxt(t, 'Rider', COL_NAMESTR, expand=True, maxwidth=500)
        uiutil.mkviewcoltxt(t, 'Cat', COL_CAT)
        uiutil.mkviewcoltxt(t, 'Com', COL_COMMENT, cb=self.editcol_cb)
        uiutil.mkviewcolbool(t, 'In', COL_INRACE, width=50)
        # too dangerous!
        #cb=self.cr_inrace_toggled, width=50)
        uiutil.mkviewcoltxt(t, 'Lap', COL_LAPS, width=40, cb=self.editlap_cb)
        uiutil.mkviewcoltod(t,
                            'Start',
                            cb=self.showstart_cb,
                            width=50,
                            editcb=self.editstart_cb)
        uiutil.mkviewcoltod(t,
                            'Bunch',
                            cb=self.showbunch_cb,
                            editcb=self.editbunch_cb,
                            width=50)
        uiutil.mkviewcoltxt(t, 'Place', COL_PLACE, calign=0.5, width=50)
        b.get_object('race_result_win').add(t)

        if ui:
            # connect signal handlers
            b.connect_signals(self)
            b = gtk.Builder()
            b.add_from_file(os.path.join(metarace.UI_PATH, 'rms_context.ui'))
            self.context_menu = b.get_object('rms_context')
            self.view.connect('button_press_event', self.treeview_button_press)
            #self.view.connect('row-activated', self.treerow_selected)
            b.connect_signals(self)
            self.meet.timer.setcb(self.rfidtrig)
            self.meet.alttimer.setcb(self.timertrig)
