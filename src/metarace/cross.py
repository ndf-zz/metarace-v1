"""Cyclocross handler for roadmeet."""

import gtk
import glib
import gobject
import os
import logging
import threading

import metarace
from metarace import tod
from metarace import eventdb
from metarace import riderdb
from metarace import strops
from metarace import countback
from metarace import uiutil
from metarace import report
from metarace import jsonconfig

LOG = logging.getLogger(u'metarace.cross')
LOG.setLevel(logging.DEBUG)

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
COL_CBUNCH = 9  # computed bunch time   -> derived from rftime
COL_MBUNCH = 10  # manual bunch time     -> manual overrive
COL_STOFT = 11  # start time 'offset' - only reported in result
COL_BONUS = 12
COL_PENALTY = 13
COL_RFSEEN = 14  # list of tods this rider 'seen' by rfid

# listview column nos (used for hiding)
CATCOLUMN = 2
COMCOLUMN = 3
INCOLUMN = 4
LAPCOLUMN = 5
STARTCOLUMN = 6
BUNCHCOLUMN = 7

# rider commands
RIDER_COMMANDS_ORD = [
    u'add', u'del', u'que', u'dns', u'otl', u'wd', u'dnf', u'dsq', u'com',
    u'ret', u'man', u'', u'fin'
]  # then intermediates...
RIDER_COMMANDS = {
    u'dns': u'Did not start',
    u'otl': u'Outside time limit',
    u'dnf': u'Did not finish',
    u'wd': u'Withdraw',
    u'dsq': u'Disqualify',
    u'add': u'Add starters',
    u'del': u'Remove starters',
    u'que': u'Query riders',
    u'fin': u'Final places',
    u'com': u'Add comment',
    u'ret': u'Return to race',
    u'man': u'Manual passing',
    u'': u'',
}

RESERVED_SOURCES = [
    u'fin',  # finished stage
    u'reg',  # registered to stage
    u'start'
]  # started stage
# additional cat finishes added in loadconfig

DNFCODES = [u'otl', u'wd', u'dsq', u'dnf', u'dns']
GAPTHRESH = tod.tod(u'2.12')  # relaxed 'bunch' criteria for cross
MINPASSSTR = u'20.0'
MINPASSTIME = tod.tod(MINPASSSTR)

# timing keys
key_announce = u'F4'
key_armstart = u'F5'
key_armlap = u'F6'
key_placesto = u'F7'  # fill places to selected rider
key_appendplace = u'F8'  # append sepected rider to places
key_armfinish = u'F9'
key_raceover = u'F10'

# extended fn keys      (ctrl + key)
key_abort = u'F5'
key_clearfrom = u'F7'  # clear places on selected rider and all following
key_clearplace = u'F8'  # clear rider from place list

# config version string
EVENT_ID = u'crossrace-3.0'


def key_bib(x):
    """Sort on bib field of aux row."""
    return strops.riderno_key(x[1])


def sort_starters(x, y):
    """Sort using seed number, then riderno (MTB style)."""
    if x[2] == y[2]:
        return cmp(strops.riderno_key(x[1]), strops.riderno_key(y[1]))
    else:
        if y[2] and not x[2]:
            return cmp(strops.riderno_key(x[1]), strops.riderno_key(y[2]))
        elif x[2] and not y[2]:
            return cmp(strops.riderno_key(x[2]), strops.riderno_key(y[1]))
        else:
            return cmp(strops.riderno_key(x[2]), strops.riderno_key(y[2]))


def sort_tally(x, y):
    """Points tally sort using countback struct."""
    if x[0] == y[0]:
        return cmp(y[3], x[3])
    elif y[0] < x[0]:
        return -1
    else:
        return 1


class cross(object):
    """Cross race handler."""

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
                    if cat not in [u'CAT', u'SPARE', u'TEAM']:
                        self.cats.append(cat)
                    else:
                        LOG.warning(u'Invalid result category: %r', cat)
        self.cats.append(u'')  # always include one empty cat
        LOG.debug(u'Result category list updated: %r', self.cats)

    def downtimes(self, show):
        """Set the downtimes flag"""
        LOG.debug(u'Set showdowntimes to: %r', show)
        self.showdowntimes = show

    def loadconfig(self):
        """Load event config from disk."""
        self.riders.clear()
        self.resettimer()
        self.cats = []

        cr = jsonconfig.config({
            u'event': {
                u'start': None,
                u'id': EVENT_ID,
                u'finish': None,
                u'finished': False,
                u'showdowntimes': True,
                u'showuciids': False,
                u'places': u'',
                u'comment': [],
                u'hidecols': [INCOLUMN],
                u'categories': [],
                u'intermeds': [],
                u'allowspares': False,
                u'contests': [],
                u'tallys': [],
                u'lapstart': None,
                u'laplength': None,
                u'dofastestlap': False,
                u'minlap': MINPASSSTR,
                u'lapfin': None,
                u'curlap': -1,
                u'passlabels': {},
                u'catonlap': {},
                u'gapthresh': None,
                u'totlaps': None,
                u'passingsource': [],
                u'laptimes': [],
                u'clubmode': False,
                u'startlist': u'',
                u'autoexport': False,
            }
        })
        cr.add_section(u'event')
        cr.add_section(u'riders')
        # sections for commissaire awarded bonus/penalty
        cr.add_section(u'stagebonus')
        cr.add_section(u'stagepenalty')

        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                LOG.error(u'Unable to read config: %s', e)
        else:
            LOG.info(u'%r not found, loading defaults', self.configfile)

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

        # amend reserved sources with any cats
        if len(self.cats) > 1:
            for cat in self.cats:
                if cat:
                    srcid = cat.lower() + u'fin'
                    RESERVED_SOURCES.append(srcid)
                    self.catplaces[srcid] = cat

        self.passlabels = cr.get(u'event', u'passlabels')
        self.catonlap = cr.get(u'event', u'catonlap')
        self.passingsource = []
        for source in cr.get(u'event', u'passingsource'):
            self.passingsource.append(source.lower())  # force lower case

        # fetch time gap threshold of configured
        ngt = tod.mktod(cr.get(u'event', u'gapthresh'))
        if ngt is not None:
            self.gapthresh = ngt
            if self.gapthresh != GAPTHRESH:
                LOG.warning(u'Set time gap threshold %s',
                            self.gapthresh.rawtime(2))

        # restore intermediates
        for i in cr.get(u'event', u'intermeds'):
            if i in RESERVED_SOURCES:
                LOG.info(u'Ignoring reserved inter: %r', i)
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
                    km = strops.confopt_float(cr.get(crkey, u'dist'), None)
                if cr.has_option(crkey, u'abbr'):
                    abbr = cr.get(crkey, u'abbr')
                if cr.has_option(crkey, u'show'):
                    doshow = strops.confopt_bool(cr.get(crkey, u'show'))
                if cr.has_option(crkey, u'places'):
                    places = strops.reformat_placelist(cr.get(
                        crkey, u'places'))
                if i not in self.intermeds:
                    LOG.debug(u'Adding inter %r: %r %r', i, descr, places)
                    self.intermeds.append(i)
                    self.intermap[i] = {
                        u'descr': descr,
                        u'places': places,
                        u'abbr': abbr,
                        u'dist': km,
                        u'show': doshow
                    }
                else:
                    LOG.info(u'Ignoring duplicate inter: %r', i)

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
                            LOG.info(u'Invalid bonus %r in contest %r', bstr,
                                     i)
                            bt = tod.ZERO
                        bonuses.append(bt)
                self.contestmap[i][u'bonuses'] = bonuses
                points = []
                if cr.has_option(crkey, u'points'):
                    pliststr = cr.get(crkey, u'points').strip()
                    if pliststr and tally == u'':
                        LOG.error(u'No tally for points in contest %r', i)
                        tallyset.add(u'')  # add empty placeholder
                    for pstr in pliststr.split():
                        pt = 0
                        try:
                            pt = int(pstr)
                        except Exception:
                            LOG.info(u'Invalid points %r in contest %r', pstr,
                                     i)
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
                LOG.info(u'Ignoring duplicate contest %r', i)

            # check for invalid allsrc
            if self.contestmap[i][u'all_source']:
                if (len(self.contestmap[i][u'points']) > 1
                        or len(self.contestmap[i][u'bonuses']) > 1):
                    LOG.info(u'Ignoring extra points/bonus for allsrc %r', i)

        # load points tally meta data
        tallylist = cr.get(u'event', u'tallys')
        # append any 'missing' tallys from points data errors
        for i in tallyset:
            if i not in tallylist:
                LOG.debug(u'Adding missing tally to config %r', i)
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
                    keepdnf = strops.confopt_bool(cr.get(crkey, u'keepdnf'))
                self.tallymap[i][u'keepdnf'] = keepdnf
            else:
                LOG.info(u'Ignoring duplicate points tally %r', i)

        starters = cr.get(u'event', u'startlist').split()
        if len(starters) == 1 and starters[0] == u'all':
            starters = strops.riderlist_split(u'all', self.meet.rdb)
        self.allowspares = strops.confopt_bool(cr.get(u'event',
                                                      u'allowspares'))
        onestoft = False
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
                    if nr[COL_STOFT] is not None:
                        onestoft = True
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

        self.laptimes = []
        ltin = cr.get(u'event', u'laptimes')
        for ts in ltin:
            nlt = tod.mktod(ts)
            if nlt is not None:
                self.laptimes.append(nlt)

        self.set_start(cr.get(u'event', u'start'))
        self.set_finish(cr.get(u'event', u'finish'))
        self.lapstart = tod.mktod(cr.get(u'event', u'lapstart'))
        self.lapfin = tod.mktod(cr.get(u'event', u'lapstart'))
        self.minlap = tod.mktod(cr.get(u'event', u'minlap'))
        if self.minlap is None:
            self.minlap = MINPASSTIME
        LOG.debug(u'Minimum lap time: %s', self.minlap.rawtime())
        self.curlap = cr.get(u'event', u'curlap')
        self.totlaps = cr.get(u'event', u'totlaps')
        self.places = strops.reformat_placelist(cr.get(u'event', u'places'))
        self.comment = cr.get(u'event', u'comment')
        self.dofastestlap = strops.confopt_bool(
            cr.get(u'event', u'dofastestlap'))
        self.autoexport = strops.confopt_bool(cr.get(u'event', u'autoexport'))
        if strops.confopt_bool(cr.get(u'event', u'finished')):
            self.set_finished()
        self.showdowntimes = strops.confopt_bool(
            cr.get(u'event', u'showdowntimes'))
        self.showuciids = strops.confopt_bool(cr.get(u'event', u'showuciids'))
        self.clubmode = strops.confopt_bool(cr.get(u'event', u'clubmode'))
        self.laplength = strops.confopt_posint(cr.get(u'event', u'laplength'))
        self.recalculate()

        self.hidecols = cr.get(u'event', u'hidecols')
        for col in self.hidecols:
            target = strops.confopt_posint(col)
            if target is not None:
                self.hidecolumn(target)

        # load starts and targets and then handle lap situation
        self.load_cat_data()
        for c in self.catstarts:
            if self.catstarts[c] is not None:
                onestoft = True
        if not onestoft:
            # auto-hide the start column, but don't stick if starts added
            self.hidecolumn(STARTCOLUMN)

        if self.targetlaps:
            self.curlap = -1
            self.lapentry.set_text(u'')
            self.lapentry.set_sensitive(False)
        else:
            if self.curlap is not None and self.curlap >= 0:
                self.lapentry.set_text(unicode(self.curlap))
            else:
                self.lapentry.set_text(u'')
            self.lapentry.set_sensitive(True)

        if self.totlaps is not None:
            self.totlapentry.set_text(unicode(self.totlaps))

        # After load complete - check config and report.
        eid = cr.get(u'event', u'id')
        if eid and eid != EVENT_ID:
            LOG.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)
            self.readonly = True

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
            LOG.error(u'Attempt to save readonly event')
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
        cw.set(u'event', u'showdowntimes', self.showdowntimes)
        cw.set(u'event', u'showuciids', self.showuciids)
        cw.set(u'event', u'minlap', self.minlap.rawtime())
        cw.set(u'event', u'gapthresh', self.gapthresh.rawtime())
        cw.set(u'event', u'finished', self.timerstat == u'finished')
        cw.set(u'event', u'places', self.places)
        cw.set(u'event', u'curlap', self.curlap)
        cw.set(u'event', u'onlap', self.onlap)
        cw.set(u'event', u'totlaps', self.totlaps)
        cw.set(u'event', u'allowspares', self.allowspares)
        cw.set(u'event', u'passlabels', self.passlabels)
        cw.set(u'event', u'catonlap', self.catonlap)
        cw.set(u'event', u'dofastestlap', self.dofastestlap)
        cw.set(u'event', u'autoexport', self.autoexport)
        cw.set(u'event', u'passingsource', self.passingsource)
        cw.set(u'event', u'clubmode', self.clubmode)
        cw.set(u'event', u'laplength', self.laplength)
        ltout = []
        for lt in self.laptimes:
            ltout.append(lt.rawtime())
        cw.set(u'event', u'laptimes', ltout)

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
        cw.set(u'event', u'intermeds', opinters)

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
        evtriders = self.get_startlist()
        if evtriders:
            cw.set(u'event', u'startlist', self.get_startlist())
        else:
            if self.autostartlist is not None:
                cw.set(u'event', u'startlist', self.autostartlist)
        if self.autocats:
            cw.set(u'event', u'categories', [u'AUTO'])
        else:
            cw.set(u'event', u'categories', self.get_catlist())
        cw.set(u'event', u'comment', self.comment)
        cw.set(u'event', u'hidecols', self.hidecols)

        cw.add_section(u'riders')
        # sections for commissaire awarded bonus/penalty
        cw.add_section(u'stagebonus')
        cw.add_section(u'stagepenalty')
        for r in self.riders:
            rt = u''
            if r[COL_RFTIME] is not None:
                rt = r[COL_RFTIME].rawtime()  # Don't truncate
            mb = u''
            if r[COL_MBUNCH] is not None:
                mb = r[COL_MBUNCH].rawtime(0)  # But bunch is to whole sec
            sto = u''
            if r[COL_STOFT] is not None:
                sto = r[COL_STOFT].rawtime()
            # bib = comment,in,laps,rftod,mbunch,stoft,seen...
            bib = r[COL_BIB].decode('utf-8')
            slice = [
                r[COL_COMMENT].decode('utf-8'), r[COL_INRACE], r[COL_LAPS], rt,
                mb, sto
            ]
            for t in r[COL_RFSEEN]:
                if t is not None:
                    slice.append(t.rawtime())  # retain 'precision' here too
            cw.set(u'riders', bib, slice)
            if r[COL_BONUS] is not None:
                cw.set(u'stagebonus', bib, r[COL_BONUS].rawtime())
            if r[COL_PENALTY] is not None:
                cw.set(u'stagepenalty', bib, r[COL_PENALTY].rawtime())
        cw.set(u'event', u'id', EVENT_ID)
        LOG.debug(u'Saving event config %r', self.configfile)
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
            titlestr = u'Mass Start Road Event'
        self.title_namestr.set_text(titlestr)

    def destroy(self):
        """Emit destroy signal to race handler."""
        if self.context_menu is not None:
            self.context_menu.destroy()
        self.frame.destroy()

    def points_report(self):
        """Return the points tally report."""
        ret = []
        aux = []
        cnt = 0
        for tally in self.tallys:
            sec = report.section(u'points-' + tally)
            sec.heading = tally.upper() + u' ' + self.tallymap[tally][u'descr']
            sec.units = u'pt'
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
                                        ellipsis=False), None,
                        str(self.points[tally][bib])
                    ], self.pointscb[tally][bib]
                ])
            aux.sort(sort_tally)
            for r in aux:
                sec.lines.append(r[2])
            LOG.debug(u'Total points for %r: %r', tally, tallytot)
            ret.append(sec)

        if len(self.bonuses) > 0:
            sec = report.section(u'bonus')
            sec.heading = u'Stage Bonuses'
            sec.units = u'sec'
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
        if len(ret) == 0:
            LOG.warning(u'No data available for points report')
        return ret

    def reorder_startlist(self):
        """Reorder riders for a startlist."""
        self.calcset = False
        aux = []
        cnt = 0
        for r in self.riders:
            aux.append([cnt, r[COL_BIB], r[COL_COMMENT]])
            cnt += 1
        if len(aux) > 1:
            aux.sort(sort_starters)
            self.riders.reorder([a[0] for a in aux])
        return cnt

    def signon_report(self):
        """Return a signon report."""
        ret = []
        self.reorder_startlist()
        if len(self.cats) > 1:
            LOG.debug(u'Preparing categorised signon for %r', self.cats)
            for c in self.cats:
                c = self.ridercat(c)
                LOG.debug(u'Preparing signon cat %r', c)
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
                            LOG.warning(u'%r uncategorised riders',
                                        len(sec.lines))
                        ret.append(sec)
                        ret.append(report.pagebreak(threshold=0.1))
                    else:
                        if c:
                            LOG.warning(u'No starters for category %r', c)
        else:
            LOG.debug(u'Preparing flat signon')
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

    def startlist_report(self):
        """Return a startlist report."""
        ret = []
        self.reorder_startlist()
        if len(self.cats) > 1:
            LOG.debug(u'Preparing categorised startlist for %r', self.cats)
            for c in self.cats:
                LOG.debug(u'Startlist cat %r', c)
                ret.extend(self.startlist_report_gen(c))
        else:
            LOG.debug(u'Preparing flat startlist')
            ret = self.startlist_report_gen()
        return ret

    def load_cat_data(self):
        """Read category start and target data from riderdb."""
        self.catstarts = {}
        self.catlaps = {}
        onetarget = False
        onemissing = False
        for c in self.cats:
            cs = None  # default start offset is None
            ls = None
            dbr = self.meet.rdb.getrider(c, u'cat')
            if dbr is not None:
                ct = tod.mktod(self.meet.rdb.getvalue(dbr,
                                                      riderdb.COL_UCICODE))
                if ct is not None:
                    cs = ct
                lt = strops.confopt_posint(
                    self.meet.rdb.getvalue(dbr, riderdb.COL_CAT))
                if lt:
                    ls = lt
                    onetarget = True
                else:
                    onemissing = True
            self.catstarts[c] = cs
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
                    LOG.warning(u'Categories missing target lap count: %s',
                                u', '.join(missing))

    def startlist_report_gen(self, cat=None):
        catname = u''
        subhead = u''
        footer = u''
        uncat = False
        if cat is not None:
            dbr = self.meet.rdb.getrider(cat, u'cat')
            if dbr is not None:
                catname = self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST)
                subhead = self.meet.rdb.getvalue(dbr, riderdb.COL_LAST)
                footer = self.meet.rdb.getvalue(dbr, riderdb.COL_NOTE)
            if cat == u'':
                catname = u'Uncategorised Riders'
                uncat = True
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
        sec = None
        sec = report.twocol_startlist(u'startlist')
        sec.heading = u'Startlist'
        if catname:
            sec.heading += u': ' + catname
            sec.subheading = subhead
        rcnt = 0
        # fetch result category for this nominated cat
        cat = self.ridercat(cat)
        for r in self.riders:
            # add rider to startlist if primary cat matches
            cs = r[COL_CAT].decode(u'utf-8')
            rcat = self.ridercat(riderdb.primary_cat(cs))
            if cat == rcat:
                ucicode = None
                name = r[COL_NAMESTR].decode(u'utf-8')
                if self.showuciids:
                    dbr = self.meet.rdb.getrider(r[COL_BIB].decode(u'utf-8'),
                                                 self.series)
                    if dbr is not None:
                        ucicode = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_UCICODE)
                if not ucicode and cat == u'':
                    # Rider may have a typo in cat, show the catlist
                    ucicode = cs
                comment = u''
                if not r[COL_INRACE]:
                    cmt = r[COL_COMMENT].decode(u'utf-8')
                    if cmt == u'dns':
                        comment = cmt
                riderno = r[COL_BIB].decode(u'utf-8').translate(
                    strops.INTEGER_UTRANS)
                sec.lines.append([comment, riderno, name, ucicode])
                # Look up pilots
                if cat in [u'MB', u'WB']:
                    # lookup pilot
                    dbr = self.meet.rdb.getrider(r[COL_BIB], u'pilot')
                    if dbr is not None:
                        sec.even = True  # force even first column
                        puci = None
                        if self.showuciids:
                            puci = self.meet.rdb.getvalue(
                                dbr, riderdb.COL_UCICODE)
                        pnam = strops.listname(
                            self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                            self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                            self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB))
                        sec.lines.append([u'', u'', pnam, puci])

                rcnt += 1
        fvc = []
        if footer:
            fvc.append(footer)
        if rcnt > 1:
            fvc.append(u'Total riders: ' + unicode(rcnt))
        if fvc:
            sec.footer = u'\t'.join(fvc)
        if len(sec.lines) > 0:
            ret.append(sec)
            if uncat:
                LOG.warning(u'%r uncategorised riders', len(sec.lines))
        else:
            if cat:
                LOG.warning(u'No starters for category %r', cat)

        return ret

    def analysis_report(self):
        """Return an analysis report."""
        # TODO: check operation and verify validity
        ret = []
        self.recalculate()  # fill places and bunch info
        self.reorder_startlist()
        pthresh = self.meet.timer.photothresh()
        totcount = 0
        dnscount = 0
        dnfcount = 0
        fincount = 0
        bl = tod.tod(u'23h59:59.999')
        blr = None
        blt = None
        blrn = None
        llen = self.laplength
        lcomment = u''
        if self.timerstat != u'idle':
            sec = report.judge24rep(u'analysis')
            sec.heading = u'Lap Analysis'
            sec.colheader = [
                u'dist', u'no', u'rider', u'laps', u'best lap', u'', u'laps'
            ]
            if self.start is not None:
                sec.start = self.start
                sec.laptimes = [self.start]
                n = self.start
                for h in range(1, 25):
                    n += tod.tod(u'1h00:00')
                    sec.laptimes.append(n)

                sec.finish = self.finish

            first = True
            ft = None
            lt = None
            lrf = None
            lplaced = None
            ltimed = None
            lastno = None
            lastlap = None
            for r in self.riders:
                rbl = tod.tod(u'23h59:59.999')
                rblt = None
                rbltt = None
                totcount += 1
                marker = u' '
                es = u''
                bs = u''
                pset = False
                placed = False
                timed = False
                catstart = None
                photo = False
                if r[COL_INRACE]:
                    thislap = r[COL_LAPS]
                    rcat = self.get_ridercat(r[COL_BIB])
                    if rcat in self.catstarts:
                        catstart = self.catstarts[rcat]
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if bt is not None:
                        timed = True
                        fincount += 1
                        # this is going to be the total distance covered
                        comment = u'{0:0.1f}km'.format(llen * r[COL_LAPS] *
                                                       0.001)
                        if r[COL_PLACE] != '':
                            placed = True
                            pset = True

                        # find fastest lap @ time
                        for laptim in r[COL_RFSEEN]:
                            if rbltt is not None:
                                thislap = laptim - rbltt
                                if thislap > self.minlap:
                                    if thislap < rbl:  # new fastest
                                        rbl = thislap
                                        rblt = laptim
                                    if thislap < bl:  # new fastest for event
                                        bl = thislap
                                        blr = r[COL_BIB]
                                        blrn = r[COL_NAMESTR]
                                        blt = laptim
                            rbltt = laptim
                        if rbl < tod.tod(u'23h59:59.999'):
                            bs = rbl.rawtime(1)
                            es = u'@' + rblt.meridiem()

                    else:
                        if r[COL_COMMENT].strip() != '':
                            comment = r[COL_COMMENT].strip()
                        else:
                            comment = u'____'

                    ltimed = timed
                    thisno = strops.bibstr_key(r[riderdb.COL_BIB])
                    if thisno > 200 and lastno is not None and thisno - lastno != 1:
                        sec.lines.append([None, None, None])
                    lastno = thisno
                    sec.lines.append([
                        comment, r[riderdb.COL_BIB], r[COL_NAMESTR],
                        str(r[COL_LAPS]), bs, es, r[COL_RFSEEN], placed, photo,
                        catstart
                    ])
                else:
                    comment = r[COL_COMMENT]
                    if comment == u'':
                        comment = u'dnf'
                    if comment != lcomment:
                        pass
                    lcomment = comment
                    if comment == u'dns':
                        dnscount += 1
                    else:
                        dnfcount += 1
                    sec.lines.append([
                        comment, r[riderdb.COL_BIB], r[COL_NAMESTR],
                        str(r[COL_LAPS]), None, None, None, True, False
                    ])
                first = False

            ret.append(sec)
            sec = report.bullet_text(u'analysisstat')
            sec.lines.append([
                None, u'Fastest lap: ' + blr + u' ' + bl.rawtime(1) + u' @' +
                blt.meridiem()
            ])
            #sec.lines.append([None,'Total riders: ' + str(totcount)])
            #sec.lines.append([None,'Did not start: ' + str(dnscount)])
            #sec.lines.append([None,'Did not finish: ' + str(dnfcount)])
            #sec.lines.append([None,'Finishers: ' + str(fincount)])
            if len(sec.lines) > 0:
                ret.append(sec)
        else:
            # nothing to report...
            pass
        return ret

    def camera_report(self):
        """Return the analysis report in place of the camera report."""
        return self.analysis_report()

    def catresult_report(self):
        """Return a categorised race result report."""
        LOG.debug(u'Categorised result report')
        self.recalculate()
        ret = []
        ignorenull = False
        if len(self.cats) > 1:
            ignorenull = True
        for cat in self.cats:
            if not cat and ignorenull:
                continue
            ret.extend(self.single_catresult(cat))

        # show all intermediates here
        for i in self.intermeds:
            im = self.intermap[i]
            if im[u'places'] and im['show']:
                ret.extend(self.int_report(i))

        if len(self.comment) > 0:
            s = report.bullet_text(u'comms')
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
                LOG.warn(u'Invalid distance: ' + repr(dist) + u' for cat ' +
                         repr(cat))
        sec = report.section(u'result-' + cat)

        wt = None
        ft = None
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
        lapleadtime = None
        lapleadcnt = None
        maxlap = None
        lastlap = None
        #LOG.info(u'RESULT FOR CAT: ' + repr(cat))
        for r in self.riders:
            rcat = u''
            rcats = r[COL_CAT].decode('utf-8').upper().split()
            if cat in rcats:
                if cat:
                    rcat = self.ridercat(cat)
                else:
                    rcat = self.ridercat(rcats[0])
                totcount += 1
                bstr = r[COL_BIB].decode('utf-8')
                nstr = r[COL_NAMESTR].decode('utf-8')
                rlap = r[COL_LAPS]
                pstr = u''
                tstr = u''
                dstr = u''
                cstr = u''  # check for ucicode here !
                showdown = False
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
                        if lapleadtime is None:
                            lapleadtime = bt
                        else:
                            ## riders still out on lap get one 'extra' lap
                            if bt < lapleadtime:
                                rlap += 1

                    if rlap != lastlap:
                        if first:
                            maxlap = rlap
                            showdown = True
                        else:
                            #sec.lines.append([None, None, None])
                            if maxlap is not None:
                                tstr = u'-{0:d} lap{1}'.format(
                                    maxlap - rlap,
                                    strops.plural(maxlap - rlap))
                            showdown = False
                            ft = None
                    else:
                        if bt is not None:
                            if bt < lapleadtime:
                                showdown = False
                            else:
                                showdown = True
                        else:
                            showdown = True

                    lastlap = rlap

                    if bt is not None:
                        if r[COL_RFTIME] or r[COL_MBUNCH]:
                            fincount += 1  # for accounting, use bunch time
                        timed = True  # for virtual standing
                        #tstr = bt.rawtime(0)
                        # compute elapsed
                        et = bt
                        sof = None
                        if r[COL_STOFT] is not None:
                            sof = r[COL_STOFT]
                        elif rcat in self.catstarts:
                            sof = self.catstarts[rcat]
                        if sof is not None:
                            # apply a start offset
                            et = bt - sof
                        if ft is None:  # lap finish time
                            ft = et
                        if wt is None:
                            wt = et  # winner's time
                        #if bt != lt:
                        if not first:
                            if showdown:
                                dstr = u'+' + (et - ft).rawtime(0)
                            elif self.laplength:
                                dstr = '{0:0.1f}km'.format(0.001 * rlap *
                                                           self.laplength)
                        else:
                            if self.laplength:
                                dstr = '{0:0.1f}km'.format(0.001 * rlap *
                                                           self.laplength)
                            else:
                                dstr = ft.rawtime(0)
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
            if fincount < 5:
                sec.heading = u'Virtual Standings'
            else:
                sec.heading = u'Provisional Result'
        else:
            sec.heading = u'Provisional Result'
        ret.append(sec)
        rsec = sec
        # Race metadata / UCI comments
        sec = report.bullet_text('uci-' + cat)
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
            LOG.info(u'Unaccounted for: ' + unicode(residual))
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
        return self.catresult_report()

    def stat_but_clicked(self, button=None):
        """Deal with a status button click in the main container."""
        LOG.debug(u'Stat button clicked.')

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

    def race_ctrl(self, acode=u'', rlist=u''):
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
                    LOG.info(u'Intermediate %r == %r', acode, rlist)
                else:
                    LOG.error('Intermediate %r not updated', acode)
            return False
        elif acode == u'fin':
            rlist = strops.reformat_placelist(rlist)
            if self.checkplaces(rlist):
                self.places = rlist
                self.recalculate()
                self.finsprint(rlist)
                return False
            else:
                LOG.error(u'Places not updated')
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
            return True
        elif acode == u'add':
            rlist = strops.riderlist_split(rlist, self.meet.rdb, self.series)
            for bib in rlist:
                self.addrider(bib)
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
        else:
            LOG.error(u'Ignoring invalid action %r', acode)
        return False

    def add_comment(self, comment=u''):
        """Append a commissaires comment."""
        self.comment.append(comment.strip())
        LOG.info(u'Added comment: %r', comment)

    def query_rider(self, bib=None):
        """List info on selected rider in the scratchpad."""
        LOG.info(u'Query rider: %r', bib)
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
            LOG.info(u'%s %s %s %s', bib, ns, bs, ps)
            lt = None
            if len(r[COL_RFSEEN]) > 0:
                for rft in r[COL_RFSEEN]:
                    nt = rft.truncate(0)
                    ns = rft.timestr(1)
                    ls = u''
                    if lt is not None:
                        ls = (nt - lt).timestr(0)
                    LOG.info(u'\t%s %s', ns, ls)
                    lt = nt
            if r[COL_RFTIME] is not None:
                LOG.info(u'Finish: %s', r[COL_RFTIME].timestr(1))
        else:
            LOG.info(u'%r not in startlist', bib)

    def startlist_gen(self, cat=u''):
        """Generator function to export a startlist."""
        mcat = self.ridercat(cat)
        self.reorder_startlist()
        for r in self.riders:
            cs = r[COL_CAT].decode(u'utf-8')
            rcat = self.ridercat(riderdb.primary_cat(cs))
            if mcat == rcat:
                start = u''
                if r[COL_STOFT] is not None and r[COL_STOFT] != tod.ZERO:
                    start = r[COL_STOFT].rawtime(0)
                elif rcat in self.catstarts:
                    if self.catstarts[rcat] is not None:
                        start = self.catstarts[rcat].rawtime(0)
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

    def lifexport(self):
        """Export lif."""
        self.recalculate()
        st = tod.ZERO
        if self.start is not None:
            st = self.start
        sno = u'1'
        if self.meet.mirrorpath:
            sno = self.meet.mirrorfile
        rdx = 1
        odat = [[
            sno, u'1', u'1', self.meet.subtitle_str, u'', u'', u'', u'', u'',
            u'', u'', u''
        ]]

        for r in self.riders:
            bib = r[COL_BIB].decode(u'utf-8')
            if r[COL_INRACE]:
                if r[COL_RFTIME]:
                    last = u''
                    first = u''
                    team = u''
                    ucicode = u''
                    dbr = self.meet.rdb.getrider(bib, self.series)
                    if dbr is not None:
                        first = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_FIRST).capitalize()
                        last = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_LAST).upper()
                        team = self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB)
                        ucicode = self.meet.rdb.getvalue(
                            dbr, riderdb.COL_UCICODE)
                    rftime = u'0'
                    if r[COL_RFTIME] is not None:
                        rftime = (r[COL_RFTIME] - st).rawtime(2, hoursep=u':')
                    bunch = u''
                    if r[COL_CBUNCH] is not None:
                        bunch = r[COL_CBUNCH].rawtime(0, hoursep=u':')
                    # rider with time
                    odat.append([
                        unicode(rdx), bib, bib, last, first, team, rftime,
                        ucicode, bunch, u'', u'', u''
                    ])
                    rdx += 1
        return odat

    def result_gen(self, cat=''):
        """Generator function to export a final result."""
        self.recalculate()  # fix up ordering of rows
        mcat = self.ridercat(cat)
        rcount = 0
        lrank = None
        lcrank = None
        for r in self.riders:
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
                bib = r[COL_BIB].decode(u'utf-8')
                crank = None
                rank = None
                bonus = None
                ft = None
                if r[COL_INRACE]:
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    ft = bt
                    sof = None
                    if r[COL_STOFT] is not None:
                        sof = r[COL_STOFT]
                    elif rcat in self.catstarts:
                        sof = self.catstarts[rcat]
                    if sof is not None and bt is not None:
                        if self.event[u'type'] != u'rhcp':
                            ft = bt - sof
                        else:
                            # for handicap, time is stage time, bonus
                            # carries the start offset, elapsed is:
                            # stage - bonus
                            ft = bt
                            bonus = sof
                plstr = r[COL_PLACE].decode(u'utf-8')
                if plstr.isdigit():
                    rank = int(plstr)
                    if rank != lrank:
                        crank = rcount
                    else:
                        crank = lcrank
                    lcrank = crank
                    lrank = rank
                else:
                    crank = r[COL_COMMENT].decode(u'utf-8')
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
                if ft is not None:
                    ft = ft.truncate(0)  # force whole second for bunch times
                yield [crank, bib, ft, bonus, penalty]

    def clear_results(self):
        """Clear all data from event model."""
        self.resetplaces()
        self.places = u''
        LOG.debug(u'Clear event result')
        # scan riders to clear any race info
        for r in self.riders:
            r[COL_COMMENT] = u''
            r[COL_INRACE] = True
            r[COL_PLACE] = u''
            r[COL_LAPS] = 0
            r[COL_RFSEEN] = []
            r[COL_RFTIME] = None
            r[COL_CBUNCH] = None
            r[COL_MBUNCH] = None
        LOG.debug(u'Clear rider data')

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

    def delrider(self, bib=u'', series=u''):
        """Remove the specified rider from the model."""
        i = self.getiter(bib)
        if i is not None:
            self.riders.remove(i)
        self.clear_place(bib)

    def starttime(self, start=None, bib=u'', series=u''):
        """Adjust start time for the rider."""
        if series == self.series:
            r = self.getrider(bib)
            if r is not None:
                r[COL_STOFT] = start

    def addrider(self, bib=u'', series=None):
        """Add specified rider to event model."""
        if series is not None and series != self.series:
            LOG.debug(u'Ignoring non-series rider: %r',
                      strops.bibser2bibstr(bib, series))
            return None
        if bib == u'' or self.getrider(bib) is None:
            nr = [
                bib, u'', u'', u'', u'', True, u'', 0, None, None, None, None,
                None, None, []
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
            return self.riders.append(nr)
        else:
            return None

    def resettimer(self):
        """Reset event timer."""
        LOG.info(u'Reset event to idle')
        self.meet.alttimer.dearm(1)
        self.set_finish()
        self.set_start()
        self.clear_results()
        self.timerstat = u'idle'
        self.meet.cmd_announce(u'timerstat', u'idle')
        self.meet.stat_but.buttonchg(uiutil.bg_none, u'Idle')
        self.meet.stat_but.set_sensitive(True)
        self.curlap = -1
        self.onlap = 1
        self.lapentry.set_text(u'')
        self.laptimes = []
        self.live_announce = True

    def armstart(self):
        """Process an armstart request."""
        if self.timerstat == u'idle':
            self.timerstat = u'armstart'
            self.meet.cmd_announce(u'timerstat', u'armstart')
            self.meet.stat_but.buttonchg(uiutil.bg_armint, u'Arm Start')
            self.resetcatonlaps()
        elif self.timerstat == u'armstart':
            self.timerstat = u'idle'
            self.meet.cmd_announce(u'timerstat', u'idle')
            self.meet.stat_but.buttonchg(uiutil.bg_none, u'Idle')

    def resetcatonlaps(self):
        onechange = False
        for cat in self.catlaps:  # for each category with a defined target
            target = self.catlaps[cat]
            if target:
                self.catonlap[cat] = 0
                onechange = True
        if onechange:
            self.announcecatlap()

    def armfinish(self):
        """Process an armfinish request."""
        if self.timerstat in [u'running', u'finished']:
            if self.finish is None and self.curlap:
                # No finish passing yet
                self.armlap()
            elif self.totlaps == 0:
                # Unbound lap count
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

    def armlap(self, data=None):
        if self.targetlaps:
            LOG.info(u'Event laps disabled')
            return False

        LOG.info(u'Arming lap')
        if self.curlap is None or self.curlap < 0:
            self.curlap = 0  # manual override lap counts
        self.scratch_map = {}
        self.scratch_ord = []
        titlestr = self.title_namestr.get_text()
        if self.live_announce:
            self.meet.cmd_announce(u'clear', u'all')
        if self.timerstat in [u'idle', u'armstart', u'armfinish']:
            self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
            if self.timerstat in [u'idle', u'armstart']:
                self.reannounce_times()  # otherwise not called
                self.meet.cmd_announce(u'title', titlestr)  # enforce
                return True  # no arm till event underway
        if self.curlap <= 0 or self.lapfin is not None:
            self.curlap += 1  # increment

            if self.totlaps and self.curlap > self.totlaps:
                LOG.info(u'Too many laps')
                self.curlap = self.totlaps

            # sanity check onlap
            # once arm lap is done, curlap and onlap _should_ be same
            if self.onlap != self.curlap:
                LOG.info(u'Cur/On lap mismatch %r/%r fixed', self.curlap,
                         self.onlap)
                self.onlap = self.curlap
                self.meet.cmd_announce(u'onlap', unicode(self.onlap))

        # update curlap entry whatever happened
        self.lapentry.set_text(unicode(self.curlap))

        # Write lap time fields
        lapstr = None
        if self.timerstat not in [u'armfinish', u'finished']:
            self.meet.cmd_announce(u'bunches', u'laps')
            self.meet.cmd_announce(u'finstr', None)
            ## Step 1: lap time handling
            if self.lapfin:
                # roll over to lap start
                self.lapstart = self.lapfin
            elif self.lapstart:  # assume still waiting for same lap
                pass
            else:  # at start?
                self.lapstart = self.start
            if self.totlaps is not None:
                if self.totlaps > 0:
                    lapstr = (u'Lap ' + unicode(self.onlap) + u'/' +
                              unicode(self.totlaps))
                else:  # 0 flags unknown total
                    lapstr = u''
                    passkey = unicode(self.curlap)
                    if passkey in self.passlabels:
                        lapstr = u'At ' + self.passlabels[passkey]
                    else:
                        lapstr = (u'Lap ' + unicode(self.onlap))
                self.totlapentry.set_text(unicode(self.totlaps))
                self.meet.cmd_announce(u'laplbl', lapstr)
                self.meet.cmd_announce(u'laptype', None)
            else:
                # make sure something is displayed in this path
                self.meet.cmd_announce(u'laplbl', None)
                self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
            self.lapfin = None
        else:
            self.meet.cmd_announce(u'bunches', u'final')
        if self.live_announce:
            if lapstr:
                self.meet.cmd_announce(u'title', titlestr + u' ' + lapstr)
            else:
                self.meet.cmd_announce(u'title', titlestr)
        self.reannounce_times()
        # in case of idle/delay
        return False

    def key_event(self, widget, event):
        """Handle global key presses in event."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or u'None'
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
            LOG.info(u'Confirmed next place: %r/%r', selbib, selpath)
            LOG.debug(u'Old places: %r', self.places)
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
            LOG.debug(u'New places: %r', self.places)
            self.recalculate()
            # advance selection
            j = self.riders.iter_next(i)
            if j is not None:
                # note: set by selection doesn't adjust focus
                self.view.set_cursor_on_cell(self.riders.get_path(j))

    def fill_places_to_selected(self):
        """Update places to match ordering up to selected rider."""
        if u'-' in self.places:
            LOG.warning(u'Dead heat in result, places not updated')
            return False
        sel = self.view.get_selection()
        sv = sel.get_selected()
        if sv is not None:
            # fill places and recalculate
            i = sv[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            selpath = self.riders.get_path(i)
            LOG.info(u'Confirm places to: %r/%r', selbib, selpath)
            LOG.debug(u'Old places: %r', self.places)
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
            LOG.debug(u'New places: %r', self.places)
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
            LOG.info(u'Clear places from: %r/%r', selbib, selpath)
            LOG.debug(u'Old places: %r', self.places)
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
            LOG.debug(u'New places: %r', self.places)
            self.recalculate()

    def clear_place(self, bib):
        LOG.debug(u'Old places: %r', self.places)
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
        LOG.debug(u'New places: %r', self.places)

    def clear_selected_place(self):
        """Remove the selected rider from places."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]
            selbib = self.riders.get_value(i, COL_BIB).decode(u'utf-8')
            selpath = self.riders.get_path(i)
            LOG.info(u'Clear rider from places: %r/%r', selbib, selpath)
            self.clear_place(selbib)
            self.recalculate()

    def dnfriders(self, biblist=u'', code=u'dnf'):
        """Remove each rider from the event with supplied code."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                if code != u'wd':
                    r[COL_INRACE] = False
                r[COL_COMMENT] = code
                recalc = True
                LOG.info('Rider %r did not finish with code: %r', bib, code)
            else:
                LOG.warning('Unregistered Rider %r unchanged', bib)
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
            LOG.debug(u'Manual passing: %r', bibstr)

    def retriders(self, biblist=u''):
        """Return all listed riders to the event."""
        recalc = False
        for bib in biblist.split():
            r = self.getrider(bib)
            if r is not None:
                r[COL_INRACE] = True
                r[COL_COMMENT] = u''
                recalc = True
                LOG.info(u'Rider %r returned to event', bib)
            else:
                LOG.warning(u'Unregistered rider %r unchanged', bib)
        if recalc:
            self.recalculate()
        return False

    def shutdown(self, win=None, msg=u'Race Sutdown'):
        """Close event."""
        LOG.debug(u'Event shutdown: %r', msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def starttrig(self, e):
        """Process a 'start' trigger signal."""
        LOG.info(u'Trigger: %s', e.rawtime(2))
        if self.timerstat == u'armstart':
            self.set_start(e)
            self.resetcatonlaps()
            glib.idle_add(self.armlap)  # always arm first lap in cross
        return False

    def set_lap_finish(self, e):
        """Write lap time into model and emit changed state."""
        self.laptimes.append(e)
        self.lapfin = e
        self.onlap += 1
        self.meet.cmd_announce(u'onlap', unicode(self.onlap))
        self.meet.cmd_announce(u'laptype', None)
        self.reannounce_times()

    def timertrig(self, e):
        """Record timer message from alttimer."""
        if self.timerstat == u'armfinish':
            LOG.info(u'Bunch Trigger: %s', e.rawtime(0))

    def catstarted(self, cat):
        """Return true if category is considered started."""
        ret = False
        if self.start is not None:
            stof = tod.ZERO
            if cat in self.catstarts:
                stof = self.catstarts[cat]
            st = self.start + stof
            nt = tod.now()
            if st < nt:
                LOG.debug(u'Cat %r has started', cat)
                ret = True
            else:
                LOG.debug(u'Cat %r has not yet started: %s < %s', cat,
                          nt.rawtime(1), st.rawtime(1))
        return ret

    def announcecatlap(self, acat=None):
        """Emit a category lap scoreboard message."""
        for cat in self.cats:
            if cat == acat or (acat is None and cat):
                if cat in self.catonlap:
                    count = self.catonlap[cat]
                    onlap = count
                    if self.catstarted(cat):
                        onlap += 1
                    target = None
                    togo = None
                    if cat in self.catlaps:
                        target = self.catlaps[cat]
                    if target is not None and count is not None and count < target:
                        prompt = cat.upper()
                        dbr = self.meet.rdb.getrider(cat, u'cat')
                        if dbr is not None:
                            prompt = self.meet.rdb.getvalue(
                                dbr, riderdb.COL_CLUB)
                        self.meet.cmd_announce(
                            u'catlap', u'\x1f'.join([
                                cat, prompt,
                                unicode(onlap),
                                unicode(target),
                                unicode(target - onlap)
                            ]))
                        LOG.debug(u'Cat %r %r: %r/%r, %r to go', cat, prompt,
                                  onlap, target, target - onlap)
                    else:
                        LOG.debug(u'No data for %r cat lap', cat)

    def rfidtrig(self, e):
        """Process rfid event."""
        # bounce message onto announcer: HACK
        self.meet.announce_timer(e, self.meet.timer)

        if e.refid in ['', '255']:  # got a trigger
            return self.starttrig(e)
        elif e.chan == u'STS':  # status message
            return self.rfidstat(e)

        # else assume this is a passing
        r = self.meet.rdb.getrefid(e.refid)
        if r is None:
            LOG.info('Unknown tag: ' + e.refid + '@' + e.rawtime(2))
            return False

        bib = self.meet.rdb.getvalue(r, riderdb.COL_BIB)
        ser = self.meet.rdb.getvalue(r, riderdb.COL_SERIES)
        rcat = self.get_ridercat(bib)
        if ser != self.series:
            LOG.error('Ignored non-series rider: ' + bib + '.' + ser)
            return False

        # at this point should always have a valid source rider vector
        lr = self.getrider(bib)
        if lr is None:
            if self.clubmode and self.timerstat in [u'running', u'armfinish']:
                self.addrider(bib)
                lr = self.getrider(bib)
                LOG.warn(u'Added new starter: ' + bib + u' @ ' + e.rawtime(2))
            else:
                LOG.warn('Ignoring non starter: ' + bib + ' @ ' + e.rawtime(2))
                return False

        if not lr[COL_INRACE]:
            LOG.warn('Withdrawn rider: ' + lr[COL_BIB] + ' @ ' + e.rawtime(2))
            # but continue anyway just in case it was not correct?
        else:
            LOG.info('Saw: ' + bib + ' @ ' + e.rawtime(2))

        # check run state
        if self.timerstat in ['idle', 'ready', 'armstart']:
            return False

        # save RF ToD into 'passing' vector and log
        # check elapsed time against passing
        st = tod.ZERO
        mineelap = tod.tod(u'1:00')
        catstart = tod.ZERO
        if rcat in self.catstarts and self.catstarts[rcat] is not None:
            catstart = self.catstarts[rcat]
        if self.start is not None:
            st = e - self.start - catstart
        if self.minlap is not None:
            mineelap = self.minlap

        # should first test lap time
        # fail this test for now: 24h rollover fail, et will be < mineelap
        if len(lr[COL_RFSEEN]
               ) == 0 and st < mineelap:  # this is the minelap check
            LOG.info(u'Ignored early first pass: ' + bib + ' lt: ' +
                     st.rawtime(2))
            return False
        else:
            if self.minlap is not None:
                if len(lr[COL_RFSEEN]) > 0:
                    # allow for overflow
                    lastlap = e - lr[COL_RFSEEN][-1]
                    if lastlap < self.minlap:
                        LOG.info(u'Ignored short lap: ' + bib + ' ' +
                                 lastlap.rawtime(2))
                        return False

        lr[COL_RFSEEN].append(e)
        catfinish = False
        if rcat in self.catlaps and self.catlaps[rcat]:
            targetlap = self.catlaps[rcat]
            if lr[COL_LAPS] >= targetlap - 1:
                catfinish = True  # arm just this rider

        if self.timerstat == 'armfinish' or catfinish:
            # in finish mode, all passings are considered on the finish 'lap'
            if self.finish is None:
                # Announce the first finish to scoreboard. Will be corrected
                # if necessary later in recalc.
                self.set_finish(e)
                # send a flush command to SCB for snappy output
                self.meet.cmd_announce(u'redraw', u'timer')
            if lr[COL_RFTIME] is None:
                if lr[COL_COMMENT] != 'wd':
                    lr[COL_RFTIME] = e  # saves the 'finish' time
                    self.__dorecalc = True
                    if lr[COL_INRACE]:
                        lr[COL_LAPS] += 1  # increment only in cross
                        if self.lapfin is None:
                            self.set_lap_finish(e)
                        self.announce_rider('', bib, lr[COL_NAMESTR],
                                            lr[COL_CAT], e)
            else:
                # log this case, but is it required in main log?
                LOG.info('Duplicate finish rider ' + bib + ' @ ' +
                         e.rawtime(2))
        elif self.timerstat in ['running']:
            self.__dorecalc = True  # cleared in timeout, from same thread
            if lr[COL_INRACE]:
                onlap = False
                if self.lapfin is None:
                    # lap finish armed, the first rider with laps == curlap-1
                    # will be considered the leader, otherwise they are dropped
                    if lr[COL_LAPS] == self.curlap - 1:
                        self.set_lap_finish(e)
                        # send a flush command to SCB for snappy output
                        self.meet.cmd_announce(u'redraw', u'timer')
                        lr[COL_LAPS] = self.curlap
                        onlap = True
                    else:
                        lr[COL_LAPS] += 1  # increment anyone inconsistent
                else:
                    # all riders after lap leader are incremented
                    if lr[COL_LAPS] < self.curlap:
                        lr[COL_LAPS] += 1
                        if lr[COL_LAPS] == self.curlap:
                            onlap = True
                    else:
                        # this is the lead rider...
                        if lr[COL_LAPS] == self.curlap:
                            self.armlap()
                            self.set_lap_finish(e)
                            # send a flush command to SCB for snappy output
                            self.meet.cmd_announce(u'redraw', u'timer')
                            lr[COL_LAPS] += 1  # update lap to next
                            onlap = True  # leader is always onlap
                        else:
                            # just increment lap count
                            lr[COL_LAPS] += 1
                ## announce only riders on the current lap
                if onlap:
                    self.announce_rider('', bib, lr[COL_NAMESTR], lr[COL_CAT],
                                        e)
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

    def lapentry_activate_cb(self, entry, data=None):
        """Transfer lap entry string into model if possible."""
        try:
            self.curlap = int(entry.get_text().decode('utf-8'))
        except:
            LOG.warn(u'Ignored invalid lap count.')
        if self.curlap >= 0:
            self.lapentry.set_text(unicode(self.curlap))
        else:
            self.lapentry.set_text(u'')

    def totlapentry_activate_cb(self, entry, data=None):
        """Transfer total lap entry string into model if possible."""
        try:
            nt = entry.get_text().decode('utf-8')
            if nt:  # not empty
                self.totlaps = int(entry.get_text().decode('utf-8'))
            else:
                self.totlaps = None
        except:
            LOG.warn(u'Ignored invalid total lap count.')
        if self.totlaps is not None:
            self.totlapentry.set_text(unicode(self.totlaps))
        else:
            self.totlapentry.set_text(u'')

    def finsprint(self, places):
        """Display a final sprint 'provisional' result."""

        self.live_announce = False
        self.meet.announce_clear()
        scrollmsg = u'Provisional - '
        titlestr = self.title_namestr.get_text()
        if self.racestat == u'final':
            scrollmsg = u'Result - '
            self.meet.announce_title(titlestr + ': Final Result')
        else:
            self.meet.announce_title(titlestr + ': Provisional')
        self.meet.cmd_announce(u'bunches', u'final')
        placeset = set()
        idx = 0
        st = tod.tod('0')
        if self.start is not None:
            st = self.start
        # result is sent in weird hybrid units TODO: fix the amb
        wt = None
        lb = None
        scrollcnt = 0
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
                        if scrollcnt < 5:
                            scrollmsg += (u' ' + r[COL_PLACE] + u'. ' +
                                          r[COL_SHORTNAME].decode('utf-8') +
                                          u' ')
                            scrollcnt += 1
                        glib.idle_add(self.meet.announce_rider, [
                            r[COL_PLACE] + '.', bib, r[COL_NAMESTR],
                            r[COL_CAT], fs
                        ])
                    idx += 1
        self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        if wt is not None:
            if self.start:
                self.meet.cmd_announce(u'start', self.start.rawtime())
            if self.finish:
                self.meet.cmd_announce(u'finish', self.finish.rawtime())
            else:
                LOG.info('No valid times available.')
            # set winner's time
            ## TODO: is this valid?
            #self.meet.announce_time(wt.rawtime(0))

    def int_report(self, acode):
        """Return report sections for the named intermed."""
        ret = []
        if acode not in self.intermeds:
            LOG.debug('Attempt to read non-existent intermediate: ' +
                      repr(acode))
            return ret
        descr = acode
        if self.intermap[acode]['descr']:
            descr = self.intermap[acode]['descr']
        places = self.intermap[acode]['places']
        lines = []
        placeset = set()
        idx = 0
        for placegroup in places.split():
            curplace = idx + 1
            for bib in placegroup.split('-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is not None:
                        lines.append([
                            str(curplace) + '.', bib, r[COL_NAMESTR],
                            r[COL_CAT], None, None
                        ])
                    idx += 1
                else:
                    LOG.warn('Duplicate no. = ' + str(bib) + ' in places.')
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
            LOG.debug('Attempt to display non-existent intermediate: ' +
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
                                      r[COL_SHORTNAME].decode('utf-8') + u' ')
                        rank = u''
                        if acode != u'brk':
                            rank = str(curplace) + '.'
                        glib.idle_add(
                            self.meet.announce_rider,
                            [rank, bib, r[COL_NAMESTR], r[COL_CAT], ''])
                    idx += 1
                else:
                    LOG.warn('Duplicate no. = ' + str(bib) + ' in places.')
        if acode != u'brk':
            self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        glib.timeout_add_seconds(25, self.reannounce_lap)

    def todempty(self, val):
        if val is not None:
            return val.rawtime()
        else:
            return u''

    def reannounce_times(self):
        """Re-send the current timing values."""
        self.meet.cmd_announce(u'timerstat', self.timerstat)
        self.meet.cmd_announce(u'start', self.todempty(self.start))
        self.meet.cmd_announce(u'finish', self.todempty(self.finish))
        self.meet.cmd_announce(u'lapstart', self.todempty(self.lapstart))
        self.meet.cmd_announce(u'lapfin', self.todempty(self.lapfin))
        totlaps = None
        if self.totlaps:  #may be zero, but don't show that case
            totlaps = unicode(self.totlaps)


## announce the onlap and curlap?
        curlap = None
        if self.curlap is not None:
            curlap = unicode(self.curlap)
        onlap = None
        if self.onlap is not None:
            onlap = unicode(self.onlap)
        self.meet.cmd_announce(u'onlap', unicode(onlap))
        self.meet.cmd_announce(u'curlap', unicode(curlap))
        self.meet.cmd_announce(u'totlaps', totlaps)
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
            if self.autoexport:
                glib.idle_add(self.meet.menu_data_results_cb, None)
        et = self.get_elapsed()
        if et is not None:
            self.elaplbl.set_text(et.rawtime(0))
        else:
            self.elaplbl.set_text(u'')
        return True

    def set_start(self, start=''):
        """Set the start time."""
        self.lapstart = None
        self.lapfin = None
        self.onestart = False
        if type(start) is tod.tod:
            self.start = start
        else:
            self.start = tod.mktod(start)
        if self.start is not None:
            self.onestart = True
            self.meet.cmd_announce(u'start', self.start.rawtime())
            self.last_scratch = self.start
            self.curlap = -1  # reset lap count at start
            self.onlap = 1  # leaders are 'onlap'
            self.meet.cmd_announce(u'onlap', unicode(self.onlap))
            if self.finish is None:
                self.set_running()

    def set_finish(self, finish=''):
        """Set the finish time."""
        if type(finish) is tod.tod:
            self.finish = finish
        else:
            self.finish = tod.mktod(finish)
        if self.finish is None:
            if self.start is not None:
                self.set_running()
        else:
            self.meet.cmd_announce(u'finish', self.finish.rawtime())
            if self.start is None:
                self.set_start('0')
            else:
                elap = self.finish - self.start
                dval = self.meet.get_distance()
                if dval is not None:
                    self.meet.cmd_announce(u'average',
                                           elap.rawspeed(1000.0 * dval))

    def get_elapsed(self):
        """Hack mode - always returns time from start."""
        ret = None
        if self.start is not None and self.timerstat != 'finished':
            ret = (tod.now() - self.start).truncate(0)
        return ret

    def set_running(self):
        """Update event status to running."""
        self.timerstat = 'running'
        self.meet.cmd_announce(u'timerstat', u'running')
        uiutil.buttonchg(self.meet.stat_but, uiutil.bg_armstart, 'Running')

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
            LOG.warn('Fill places with dead heat not yet implemented.')
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
        self.places = ' '.join(nplaces)
        self.__dorecalc = True

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
            LOG.info('Adjusted race times.')

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
            LOG.error('Invalid lap count.')

    def resetplaces(self):
        """Clear places off all riders."""
        for r in self.riders:
            r[COL_PLACE] = ''
        self.bonuses = {}  # bonuses are global to stage
        for c in self.tallys:  # points are grouped by tally
            self.points[c] = {}
            self.pointscb[c] = {}

    def sortrough(self, x, y):
        # aux cols: ind, bib, in, place, rftime, laps, lastpass
        #             0    1   2      3       4     5         6
        if x[2] != y[2]:  # in the race?
            if x[2]:
                return -1
            else:
                return 1
        else:
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
                if x[5] == y[5]:  # same lap count?
                    if x[4] == y[4]:  # same finish time?
                        return cmp(x[6], y[6])  # fall back to last pass
                    else:
                        if y[4] is None:
                            return -1
                        elif x[4] is None:
                            return 1
                        elif x[4] < y[4]:
                            return -1
                        else:
                            return 1
                else:
                    if x[5] > y[5]:
                        return -1
                    else:
                        return 1
        return 0

    # do final sort on manual places then laps then manual bunch entries
    def sortvbunch(self, x, y):
        # aux cols: ind, bib, in, place, vbunch, comment, laps
        #             0    1   2      3       4        5     6
        if x[2] != y[2]:  # IN compares differently,
            if x[2]:  # return in rider first
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
                    if x[6] == y[6]:  # same laps?
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
                    else:
                        return cmp(y[6], x[6])  # descend
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
        else:
            rcat = self.ridercat(model.get_value(iter, COL_CAT)).upper()
            rcats = rcat.split()
            if len(rcats) > 0:
                rcat = rcats[0]
            else:
                rcat = u''
            if rcat in self.catstarts and self.catstarts[rcat] is not None:
                otxt = self.catstarts[rcat].rawtime(0)
        cr.set_property('text', otxt)

    def edit_event_properties(self, window, data=None):
        """Edit race specifics."""
        ## make edit prop dialog with text entries
        ## populate and call into uiutil
        ## copy changes back out to model :( sadface
        LOG.info(u'going on prop ' + repr(window))

    def showbunch_cb(self, col, cr, model, iter, data=None):
        """Update bunch time on rider view."""
        cb = model.get_value(iter, COL_CBUNCH)
        mb = model.get_value(iter, COL_MBUNCH)
        ft = model.get_value(iter, COL_RFTIME)
        if mb is not None:
            cr.set_property('text', mb.rawtime(0))
            cr.set_property('style', pango.STYLE_OBLIQUE)
        else:
            if cb is not None:
                cr.set_property('text', cb.rawtime(0))
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
            if ft is not None:
                cr.set_property('style', pango.STYLE_NORMAL)
            else:
                cr.set_property('style', pango.STYLE_OBLIQUE)

    def editstart_cb(self, cell, path, new_text, col=None):
        """Edit start time on rider view."""
        newst = tod.mktod(new_text)
        if newst:
            newst = newst.truncate(0)
        self.riders[path][COL_STOFT] = newst

    def editbunch_cb(self, cell, path, new_text, col=None):
        """Edit bunch time on rider view."""
        # NOTE: times don't cascade in cyclocross
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
            nmb = None
            if u'+' in new_text:  # assume a down time
                oft = tod.ZERO
                if self.winbunch is not None:
                    oft = self.winbunch
                nmb = tod.mktod(new_text.replace(u'+', ''))
                if nmb is not None:
                    nmb += oft
            else:
                nmb = tod.mktod(new_text)
            if self.riders[path][COL_MBUNCH] != nmb:
                self.riders[path][COL_MBUNCH] = nmb
                dorecalc = True
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
                    LOG.error('Duplicate no in places: ' + repr(no))
                    ret = False
                placeset.add(no)
                # rider in the model?
                lr = self.getrider(no)
                if lr is None:
                    LOG.error('Non-starter in places: ' + repr(no))
                    ret = False
                else:
                    # rider still in the race?
                    if not lr[COL_INRACE]:
                        LOG.info('DNF/DNS rider in places: ' + repr(no))
                        if dnf:
                            ret = False
            else:
                # placeholder needs to be filled in later or left off
                LOG.info('Placeholder in places.')
        return ret

    def recalculate(self):
        """Recalculator, acquires lock and then continues."""
        if not self.recalclock.acquire(False):
            LOG.warn('Recalculate already in progress.')
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
            rcat = r[COL_CAT].decode('utf-8').upper().split()
            if len(rcat) > 0:
                rcat = rcat[0]
            else:
                rcat = u''
            ret = self.ridercat(rcat)
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

        #for r in self.result_gen(cat):
        #if type(r[0]) is not int:
        #pgroups.append(u'-'.join(ng))
        #break
        #if lp != r[0]:     # new place group
        #if len(ng) > 0:
        #pgroups.append(u'-'.join(ng))
        #ng = []
        #ng.append(r[1]) # same place group
        #lp = r[0]

        ret = u' '.join(pgroups)
        LOG.debug('Cat ' + repr(cat) + ' finish: ' + repr(ret))
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
                        LOG.warn('DNF Rider ' + str(bib) +
                                 ' in finish places.')
                else:
                    LOG.warn('Duplicate no. = ' + str(bib) +
                             ' in finish places.')

    def assign_places(self, contest):
        """Transfer points and bonuses into the named contest."""
        # fetch context meta infos
        src = self.contestmap[contest]['source']
        if src not in RESERVED_SOURCES and src not in self.intermeds:
            LOG.info('Invalid intermediate source: ' + repr(src) +
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
            if tally != u'climb':  # really only for sprints/crits
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
                                if curplace == 1:  # only track winner at finish
                                    self.pointscb[tally][bib][0] += 1
                            else:  # intermediate/other
                                if tally == u'climb':  # climbs countback on category winners only
                                    if curplace == 1:
                                        self.pointscb[tally][bib][
                                            category] += 1
                                else:
                                    self.pointscb[tally][bib][curplace] += 1
                else:
                    LOG.warn('Duplicate no. = ' + str(bib) + ' in ' +
                             repr(contest) + ' places.')

    def __recalc(self):
        """Internal 'protected' recalculate function."""
        LOG.debug('Recalculate model.')
        # pass one: clear off old places and bonuses
        self.resetplaces()

        # pass two: assign places
        self.assign_finish()
        for c in self.contests:
            self.assign_places(c)

        # pass three: do rough sort on in, place, laps, rftime -> existing
        auxtbl = []
        idx = 0
        for r in self.riders:
            lastpass = tod.ZERO  # earliest?
            if len(r[COL_RFSEEN]) > 0:
                lastpass = r[COL_RFSEEN][-1]
            # aux cols: ind, bib, in, place, rftime, laps, last passing
            auxtbl.append([
                idx, r[COL_BIB], r[COL_INRACE], r[COL_PLACE], r[COL_RFTIME],
                r[COL_LAPS], lastpass
            ])
            idx += 1
        if len(auxtbl) > 1:
            auxtbl.sort(self.sortrough)
            self.riders.reorder([a[0] for a in auxtbl])

        # pass four: compute cbunch values on auto time gaps and manual inputs
        #            At this point all riders are assumed to be in finish order
        self.maxfinish = tod.ZERO
        racefinish = None
        ft = None  # the finish or first bunch time
        lt = None  # the rftime of last competitor across line
        bt = None  # the 'current' bunch time
        if self.start is not None:
            for r in self.riders:
                lastpass = r[COL_RFTIME]
                if lastpass is None and len(r[COL_RFSEEN]) > 0:
                    lastpass = r[COL_RFSEEN][-1]
                if r[COL_INRACE]:
                    if r[COL_MBUNCH] is not None:
                        bt = r[COL_MBUNCH]  # override with manual bunch
                        r[COL_CBUNCH] = bt
                    elif lastpass is not None:
                        # establish elapsed, but allow subsequent override
                        if lastpass > self.maxfinish:
                            self.maxfinish = lastpass
                        et = lastpass - self.start

                        # establish bunch time
                        if ft is None and r[COL_RFTIME] is not None:
                            racefinish = r[COL_RFTIME]  # save race finish
                            ft = et.truncate(0)  # compute first time
                            bt = ft
                        else:
                            # bunches are slightly different in 'cross
                            if lt and et >= lt and et - lt < GAPTHRESH:
                                # same time
                                pass
                            else:
                                bt = et.truncate(0)

                        # assign and continue
                        r[COL_CBUNCH] = bt
                        lt = et
                    else:
                        # empty rftime with non-empty rank implies no time gap
                        if r[COL_PLACE] != '':
                            r[COL_CBUNCH] = bt  # use current bunch time
                        else:
                            r[COL_CBUNCH] = None

        # if racefinish defined, call set finish
        if racefinish:
            self.set_finish(racefinish)

        # pass five: resort on in,vbunch (todo -> check if place cmp reqd)
        #            at this point all riders will have valid bunch time
        auxtbl = []
        idx = 0
        for r in self.riders:
            # aux cols: ind, bib, in, place, vbunch
            auxtbl.append([
                idx, r[COL_BIB], r[COL_INRACE], r[COL_PLACE],
                self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH]), r[COL_COMMENT],
                r[COL_LAPS]
            ])
            idx += 1
        if len(auxtbl) > 1:
            auxtbl.sort(self.sortvbunch)
            self.riders.reorder([a[0] for a in auxtbl])

        # HACK: Pass six - Scan model to determine racestat
        if self.timerstat != 'idle':
            tot = 0
            placed = 0
            handled = 0
            ft = None
            for r in self.riders:
                tot += 1
                if r[COL_INRACE]:
                    if ft is None:
                        ft = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if r[COL_PLACE]:
                        placed += 1
                        handled += 1
                else:
                    handled += 1
            if ft is not None:
                self.winbunch = ft
            if self.timerstat == 'finished' or handled == tot:
                self.racestat = u'final'
            else:
                if placed >= 10 or (placed > 0 and tot < 20):
                    self.racestat = u'provisional'
                else:
                    self.racestat = u'virtual'
        else:
            self.racestat = u'prerace'
        LOG.debug(u'Race status: ' + self.racestat)
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
                LOG.warn('Invalid elapsed time: Start not updated.')
        return False

    def new_start_trig(self, button, entry=None):
        """Use the current time to update start offset."""
        self.meet.timer.trig(refid='0')

    def verify_timent(self, entry, data=None):
        et = tod.mktod(entry.get_text())
        if et is not None:
            entry.set_text(et.rawtime())
        else:
            LOG.info('Invalid elapsed time.')

    def elapsed_dlg(self, addriders=''):
        """Run a 'new start' dialog."""
        if self.timerstat == 'armstart':
            LOG.error('Start is armed, unarm to add new start time.')
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
            LOG.info('Start time updated: ' + self.start.rawtime(2))
        else:
            LOG.info('Set elapsed time cancelled.')
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

    def rms_context_edit_activate_cb(self, menuitem, data=None):
        """Edit rider start/finish/etc."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            stx = u''
            ftx = u''
            btx = u''
            ptx = u''
            st = self.riders.get_value(sel[1], COL_STOFT)
            ft = self.riders.get_value(sel[1], COL_RFTIME)
            bt = self.riders.get_value(sel[1], COL_BONUS)
            pt = self.riders.get_value(sel[1], COL_PENALTY)
            if st:
                stx = st.rawtime()
            if ft:
                ftx = ft.rawtime()
            if bt:
                btx = bt.rawtime()
            if pt:
                ptx = pt.rawtime()
            tvec = uiutil.edit_times_dlg(self.meet.window, stx, ftx, btx, ptx,
                                         True, True)  # enable bon+pen
            if len(tvec) > 4 and tvec[0] == 1:
                self.riders.set_value(sel[1], COL_STOFT, tod.mktod(tvec[1]))
                self.riders.set_value(sel[1], COL_RFTIME, tod.mktod(tvec[2]))
                self.riders.set_value(sel[1], COL_BONUS, tod.mktod(tvec[3]))
                self.riders.set_value(sel[1], COL_PENALTY, tod.mktod(tvec[4]))
                bib = self.riders.get_value(sel[1], COL_BIB)
                nst = u'-'
                st = self.riders.get_value(sel[1], COL_STOFT)
                if st:
                    nst = st.rawtime(0)
                nft = u'-'
                ft = self.riders.get_value(sel[1], COL_RFTIME)
                if ft:
                    nft = ft.rawtime(2)
                LOG.info(u'Adjusted rider ' + bib + u' - Start: ' + nst +
                         u' Raw Finish: ' + nft)
                self.recalculate()

    def rms_context_clear_activate_cb(self, menuitem, data=None):
        """Clear finish time for selected rider."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            self.riders.set_value(sel[1], COL_RFTIME, None)
            self.riders.set_value(sel[1], COL_MBUNCH, None)
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
        self.configfile = meet.event_configfile(self.evno)
        self.readonly = not ui
        rstr = u''
        if self.readonly:
            rstr = u'readonly '
        LOG.debug(u'Init %sevent %s', rstr, self.evno)

        self.recalclock = threading.Lock()
        self.__dorecalc = False

        # race run time attributes
        self.start = None
        self.finish = None
        self.maxfinish = None
        self.autoexport = False
        self.winbunch = None  # bunch time of winner (overall race time)
        self.winopen = True
        self.timerstat = 'idle'
        self.racestat = u'prerace'
        self.onestart = False
        self.places = ''
        self.laptimes = []
        self.comment = []
        self.cats = []
        self.catlaps = {}  # cache of cat lap counts
        self.catstarts = {}  # cache of cat start times
        self.catplaces = {}
        self.autocats = False
        self.bonuses = {}
        self.points = {}
        self.pointscb = {}
        self.laplength = None
        self.clubmode = False
        self.gapthresh = GAPTHRESH
        self.passlabels = {}

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
        self.curlap = -1
        self.onlap = 1
        self.totlaps = None
        self.lapstart = None
        self.lapfin = None
        self.lapmap = {}  # temp lap tracker
        self.minlap = None  # minimum lap/elap time if relevant

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
            gobject.TYPE_PYOBJECT)  # RFSEEN = 13

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, 'rms.ui'))

        self.frame = b.get_object('race_vbox')
        self.frame.connect('destroy', self.shutdown)

        # meta info pane
        self.shortname = None
        self.title_namestr = b.get_object('title_namestr')
        self.set_titlestr()
        self.elaplbl = b.get_object('time_lbl')
        #self.elaplbl.modify_font(pango.FontDescription("bold"))
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
        self.context_menu = None

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
