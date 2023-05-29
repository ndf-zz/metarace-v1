# SPDX-License-Identifier: MIT
"""Road mass-start, crit, categorised and handicap handler for roadmeet."""

import gtk
import glib
import gobject
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

_log = logging.getLogger(u'metarace.rms')
_log.setLevel(logging.DEBUG)

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
COL_SEED = 8  # Seeding number (overrides startlist ordering)

# timing infos
COL_RFTIME = 9  # one-off finish time by rfid
COL_CBUNCH = 10  # computed bunch time   -> derived from rftime
COL_MBUNCH = 11  # manual bunch time     -> manual overrive
COL_STOFT = 12  # start time 'offset' - only reported in result
COL_BONUS = 13
COL_PENALTY = 14
COL_RFSEEN = 15  # list of tods this rider 'seen' by rfid

# listview column nos (used for hiding)
CATCOLUMN = 2
COMCOLUMN = 3
INCOLUMN = 4
LAPCOLUMN = 5
SEEDCOLUMN = 6
STARTCOLUMN = 7
BUNCHCOLUMN = 8

ROADRACE_TYPES = {
    u'road': u'Road Race',
    u'circuit': u'Circuit',
    u'criterium': u'Criterium',
    u'handicap': u'Handicap',
    u'cross': u'Cyclocross',
    u'irtt': u'Road Time Trial',
    u'trtt': u'Team Road Time Trial',
}

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
GAPTHRESH = tod.tod(u'1.12')
MINPASSSTR = u'20.0'
MINPASSTIME = tod.tod(MINPASSSTR)
MAXELAPSTR = u'12h00:00'
MAXELAP = tod.tod(MAXELAPSTR)

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
EVENT_ID = u'roadrace-3.1'


class rms(object):
    """Road race handler."""

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

    def downtimes(self, show):
        """Set the downtimes flag"""
        _log.debug(u'Set showdowntimes to: %r', show)
        self.showdowntimes = show

    def loadconfig(self):
        """Load event config from disk."""
        self.riders.clear()
        self.resettimer()
        self.cats = []
        DEFDOWNTIMES = True
        if self.event[u'type'] == u'criterium':
            DEFDOWNTIMES = False
        cr = jsonconfig.config({
            u'rms': {
                u'start': None,
                u'id': EVENT_ID,
                u'finish': None,
                u'finished': False,
                u'showdowntimes': DEFDOWNTIMES,
                u'showuciids': False,
                u'showcats': True,
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
                u'autofinish': DEFDOWNTIMES,
                u'passingsource': [],
                u'laptimes': [],
                u'clubmode': False,
                u'timelimit': None,
                u'startlist': u'',
                u'autoexport': False,
            }
        })
        cr.add_section(u'rms')
        cr.add_section(u'riders')
        cr.add_section(u'stagebonus')
        cr.add_section(u'stagepenalty')
        cr.merge(metarace.sysconf, u'rms')

        if os.path.exists(self.configfile):
            try:
                with open(self.configfile, 'rb') as f:
                    cr.read(f)
            except Exception as e:
                _log.error(u'Unable to read config: %s', e)
        else:
            _log.info(u'%r not found, loading defaults', self.configfile)

        # load result categories
        self.loadcats(cr.get(u'rms', u'categories'))

        # amend reserved sources with any cats
        if len(self.cats) > 1:
            for cat in self.cats:
                if cat:
                    srcid = cat.lower() + u'fin'
                    RESERVED_SOURCES.append(srcid)
                    self.catplaces[srcid] = cat

        self.passlabels = cr.get(u'rms', u'passlabels')
        self.catonlap = cr.get(u'rms', u'catonlap')
        self.passingsource = []
        for source in cr.get(u'rms', u'passingsource'):
            self.passingsource.append(source.lower())  # force lower case

        # fetch time gap threshold
        ngt = tod.mktod(cr.get(u'rms', u'gapthresh'))
        if ngt is not None:
            self.gapthresh = ngt
            if self.gapthresh != GAPTHRESH:
                _log.warning(u'Set time gap threshold %s',
                             self.gapthresh.rawtime(2))

        # restore intermediates
        for i in cr.get(u'rms', u'intermeds'):
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
        for i in cr.get(u'rms', u'contests'):
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
        tallylist = cr.get(u'rms', u'tallys')
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

        starters = cr.get(u'rms', u'startlist').split()
        if len(starters) == 1 and starters[0] == u'all':
            starters = strops.riderlist_split(u'all', self.meet.rdb)
        self.allowspares = cr.get_bool(u'rms', u'allowspares')
        onestoft = False
        oneseed = False
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
                    nr[COL_SEED] = strops.confopt_posint(ril[3], 0)
                    if nr[COL_SEED] == 0:
                        # HACK: try to pull in seed from UCI code
                        if r in self.ucicache:
                            seedno = strops.confopt_posint(self.ucicache[r], 0)
                            if seedno > 0 and seedno < 9999:
                                nr[COL_SEED] = seedno
                                _log.info(
                                    'Set rider %r seed from UCICODE = %r', r,
                                    seedno)
                    if nr[COL_SEED] != 0:
                        oneseed = True
                if lr > 4:
                    nr[COL_RFTIME] = tod.mktod(ril[4])
                if lr > 5:
                    nr[COL_MBUNCH] = tod.mktod(ril[5])
                if lr > 6:
                    nr[COL_STOFT] = tod.mktod(ril[6])
                    if nr[COL_STOFT] is not None:
                        onestoft = True
                if lr > 7:
                    for i in range(7, lr):
                        laptod = tod.mktod(ril[i])
                        if laptod is not None:
                            nr[COL_RFSEEN].append(laptod)
            # record any extra bonus/penalty to rider model
            if cr.has_option(u'stagebonus', r):
                nr[COL_BONUS] = tod.mktod(cr.get(u'stagebonus', r))
            if cr.has_option(u'stagepenalty', r):
                nr[COL_PENALTY] = tod.mktod(cr.get(u'stagepenalty', r))

        self.laptimes = []
        ltin = cr.get(u'rms', u'laptimes')
        for ts in ltin:
            nlt = tod.mktod(ts)
            if nlt is not None:
                self.laptimes.append(nlt)

        self.set_start(cr.get(u'rms', u'start'))
        self.set_finish(cr.get(u'rms', u'finish'))
        self.lapstart = tod.mktod(cr.get(u'rms', u'lapstart'))
        self.lapfin = tod.mktod(cr.get(u'rms', u'lapfin'))
        self.minlap = tod.mktod(cr.get(u'rms', u'minlap'))
        if self.minlap is None:
            self.minlap = MINPASSTIME
        _log.debug(u'Minimum lap time: %s', self.minlap.rawtime())
        self.curlap = cr.get(u'rms', u'curlap')
        self.totlaps = cr.get(u'rms', u'totlaps')
        self.timelimit = cr.get(u'rms', u'timelimit')
        self.places = strops.reformat_placelist(cr.get(u'rms', u'places'))
        self.comment = cr.get(u'rms', u'comment')
        self.dofastestlap = cr.get_bool(u'rms', u'dofastestlap')
        self.autoexport = cr.get_bool(u'rms', u'autoexport')
        if cr.get_bool(u'rms', u'finished'):
            self.set_finished()
        self.showdowntimes = cr.get_bool(u'rms', u'showdowntimes')
        self.showuciids = cr.get_bool(u'rms', u'showuciids')
        self.showcats = cr.get_bool(u'rms', u'showcats')
        self.clubmode = cr.get_bool(u'rms', u'clubmode')
        self.laplength = cr.get_posint(u'rms', u'laplength')
        self.recalculate()

        self.hidecols = cr.get(u'rms', u'hidecols')
        for col in self.hidecols:
            target = strops.confopt_posint(col)
            if target is not None:
                self.hidecolumn(target)

        # load starts and targets and then handle lap situation
        self.load_cat_data()
        for c in self.catstarts:
            if self.catstarts[c] is not None:
                onestoft = True

        # auto-hide the start column
        if not onestoft:
            self.hidecolumn(STARTCOLUMN)

        # auto-hide the seed column
        if not oneseed:
            self.hidecolumn(SEEDCOLUMN)

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

        if cr.get_bool(u'rms', u'autofinish'):
            # then override targetlaps if autofinish was set
            self.targetlaps = True

        # After load complete - check config and report.
        eid = cr.get(u'rms', u'id')
        if eid and eid != EVENT_ID:
            _log.info(u'Event config mismatch: %r != %r', eid, EVENT_ID)
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
            _log.error(u'Attempt to save readonly event')
            return
        cw = jsonconfig.config()
        cw.add_section(u'rms')
        if self.start is not None:
            cw.set(u'rms', u'start', self.start.rawtime())
        if self.finish is not None:
            cw.set(u'rms', u'finish', self.finish.rawtime())
        if self.lapstart is not None:
            cw.set(u'rms', u'lapstart', self.lapstart.rawtime())
        if self.lapfin is not None:
            cw.set(u'rms', u'lapfin', self.lapfin.rawtime())
        cw.set(u'rms', u'showdowntimes', self.showdowntimes)
        cw.set(u'rms', u'showuciids', self.showuciids)
        cw.set(u'rms', u'showcats', self.showcats)
        cw.set(u'rms', u'minlap', self.minlap.rawtime())
        cw.set(u'rms', u'gapthresh', self.gapthresh.rawtime())
        cw.set(u'rms', u'finished', self.timerstat == u'finished')
        cw.set(u'rms', u'places', self.places)
        cw.set(u'rms', u'curlap', self.curlap)
        cw.set(u'rms', u'onlap', self.onlap)
        cw.set(u'rms', u'totlaps', self.totlaps)
        cw.set(u'rms', u'allowspares', self.allowspares)
        cw.set(u'rms', u'timelimit', self.timelimit)
        cw.set(u'rms', u'passlabels', self.passlabels)
        cw.set(u'rms', u'catonlap', self.catonlap)
        cw.set(u'rms', u'dofastestlap', self.dofastestlap)
        cw.set(u'rms', u'autoexport', self.autoexport)
        cw.set(u'rms', u'passingsource', self.passingsource)
        cw.set(u'rms', u'clubmode', self.clubmode)
        cw.set(u'rms', u'laplength', self.laplength)
        cw.set(u'rms', u'autofinish', self.targetlaps)
        ltout = []
        for lt in self.laptimes:
            ltout.append(lt.rawtime())
        cw.set(u'rms', u'laptimes', ltout)

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
        cw.set(u'rms', u'intermeds', opinters)

        # save contest meta data
        cw.set(u'rms', u'contests', self.contests)
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
        cw.set(u'rms', u'tallys', self.tallys)
        for i in self.tallys:
            crkey = u'tally_' + i
            cw.add_section(crkey)
            cw.set(crkey, u'descr', self.tallymap[i][u'descr'])
            cw.set(crkey, u'keepdnf', self.tallymap[i][u'keepdnf'])

        # save riders
        evtriders = self.get_startlist()
        if evtriders:
            cw.set(u'rms', u'startlist', self.get_startlist())
        else:
            if self.autostartlist is not None:
                cw.set(u'rms', u'startlist', self.autostartlist)
        if self.autocats:
            cw.set(u'rms', u'categories', [u'AUTO'])
        else:
            cw.set(u'rms', u'categories', self.get_catlist())
        cw.set(u'rms', u'comment', self.comment)
        cw.set(u'rms', u'hidecols', self.hidecols)

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
                r[COL_COMMENT].decode('utf-8'), r[COL_INRACE], r[COL_LAPS],
                r[COL_SEED], rt, mb, sto
            ]
            for t in r[COL_RFSEEN]:
                if t is not None:
                    slice.append(t.rawtime())  # retain 'precision' here too
            cw.set(u'riders', bib, slice)
            if r[COL_BONUS] is not None:
                cw.set(u'stagebonus', bib, r[COL_BONUS].rawtime())
            if r[COL_PENALTY] is not None:
                cw.set(u'stagepenalty', bib, r[COL_PENALTY].rawtime())
        cw.set(u'rms', u'id', EVENT_ID)
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
            etype = self.event[u'type']
            if etype in ROADRACE_TYPES:
                titlestr = u'[' + ROADRACE_TYPES[etype] + u']'
            else:
                titlestr = u'[Road Event]'
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
                aux.append((self.points[tally][bib], self.pointscb[tally][bib],
                            -strops.riderno_key(bib), [
                                None, r[COL_BIB], r[COL_NAMESTR],
                                strops.truncpad(str(self.pointscb[tally][bib]),
                                                10,
                                                ellipsis=False), None,
                                str(self.points[tally][bib])
                            ]))
            aux.sort(reverse=True)
            for r in aux:
                sec.lines.append(r[3])
            _log.debug(u'Total points for %r: %r', tally, tallytot)
            ret.append(sec)

        if len(self.bonuses) > 0:
            sec = report.section(u'bonus')
            sec.heading = u'Stage Bonuses'
            sec.units = u'sec'
            aux = []
            for bib in self.bonuses:
                r = self.getrider(bib)
                aux.append((self.bonuses[bib], -strops.riderno_key(bib), [
                    None, r[COL_BIB], r[COL_NAMESTR], None, None,
                    str(int(self.bonuses[bib].truncate(0).timeval))
                ]))
            aux.sort(reverse=True)
            for r in aux:
                sec.lines.append(r[2])
            ret.append(sec)
        if len(ret) == 0:
            _log.warning(u'No data available for points report')
        return ret

    def reorder_startlist(self, callup=False):
        """Reorder riders for a startlist."""
        self.calcset = False
        aux = []
        cnt = 0
        if callup:
            for r in self.riders:
                rseed = 9999
                if r[COL_SEED] > 0:
                    rseed = r[COL_SEED]
                riderno = strops.riderno_key(r[COL_BIB])
                aux.append((rseed, riderno, cnt))
                cnt += 1
        else:
            for r in self.riders:
                riderno = strops.riderno_key(r[COL_BIB])
                aux.append((riderno, 0, cnt))
                cnt += 1
        if len(aux) > 1:
            aux.sort()
            self.riders.reorder([a[2] for a in aux])
        return cnt

    def signon_report(self):
        """Return a signon report."""
        ret = []
        self.reorder_startlist()
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
        """Return a callup report."""
        # Note: this is just a startlist with different ordering and ranks
        ret = []
        self.reorder_startlist(callup=True)
        if len(self.cats) > 1:
            _log.debug(u'Preparing categorised callup for %r', self.cats)
            for c in self.cats:
                _log.debug(u'Callup cat %r', c)
                ret.extend(self.startlist_report_gen(c, callup=True))
        else:
            _log.debug(u'Preparing flat callup')
            ret = self.startlist_report_gen(callup=True)
        return ret

    def startlist_report(self):
        """Return a startlist report."""
        ret = []
        self.reorder_startlist()
        if len(self.cats) > 1:
            _log.debug(u'Preparing categorised startlist for %r', self.cats)
            for c in self.cats:
                _log.debug(u'Startlist cat %r', c)
                ret.extend(self.startlist_report_gen(c))
        else:
            _log.debug(u'Preparing flat startlist')
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
            # fetch data on all but the uncat cat
            if c:
                dbr = self.meet.rdb.getrider(c, u'cat')
                if dbr is not None:
                    ct = tod.mktod(
                        self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE))
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
                    _log.warning(u'Categories missing target lap count: %s',
                                 u', '.join(missing))
        _log.debug(u'Loaded cat data: laps=%r, starts=%r', self.catlaps,
                   self.catstarts)

    def startlist_report_gen(self, cat=None, callup=False):
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
        if callup:
            sec.heading = u'Call-up'
        else:
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
                if self.showcats and not ucicode and cat == u'':
                    # Rider may have a typo in cat, show the catlist
                    ucicode = cs
                comment = u''
                if callup:
                    comment = str(rcnt + 1) + '.'
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
                            self.meet.rdb.getvalue(dbr, riderdb.COL_ORG))
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
                _log.warning(u'%r uncategorised riders', len(sec.lines))
        else:
            if cat:
                _log.warning(u'No starters for category %r', cat)

        return ret

    def analysis_report(self):
        """Return an analysis report."""
        # temporary fall through to camera report
        return self.camera_report()

    def camera_report(self):
        """Return the judges (camera) report."""
        # Note: camera report treats all riders as a single blob
        ret = []
        self.recalculate()  # fill places and bunch info
        pthresh = self.meet.timer.photothresh()
        totcount = 0
        dnscount = 0
        dnfcount = 0
        fincount = 0
        lcomment = u''
        insertgap = True
        if self.timerstat != u'idle':
            sec = report.judgerep(u'judging')
            sec.heading = u'Judges Report'
            if self.event[u'type'] == u'cross':
                sec.colheader = [
                    u'', u'no', u'rider', u'lap', u'finish', u'lap avg',
                    u'passings'
                ]
            else:
                sec.colheader = [
                    u'', u'no', u'rider', u'lap', u'finish', u'rftime',
                    u'passings'
                ]
            if self.start is not None:
                sec.start = self.start
            if self.finish is not None:
                sec.finish = self.maxfinish + tod.tod(u'0.1')
            sec.laptimes = self.laptimes
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
                rbib = r[COL_BIB].decode(u'utf-8')
                rcat = r[COL_CAT].decode(u'utf-8')
                ecat = self.ridercat(riderdb.primary_cat(rcat))
                catstart = None
                #catstart = tod.ZERO
                if ecat in self.catstarts and self.event[u'type'] != u'cross':
                    catstart = self.catstarts[ecat]
                laplist = []
                if r[COL_RFTIME] is not None:
                    for lt in r[COL_RFSEEN]:
                        if lt <= r[COL_RFTIME]:
                            laplist.append(lt)
                else:
                    # include all captured laps
                    laplist = r[COL_RFSEEN]
                if r[COL_INRACE]:
                    comment = unicode(totcount)
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if bt is not None:
                        timed = True
                        fincount += 1
                        if r[COL_PLACE] != u'':
                            comment = r[COL_PLACE] + u'.'
                            placed = True
                            pset = True

                        # format 'elapsed' rftime
                        if r[COL_RFTIME] is not None:
                            if not pset and lrf is not None:
                                if r[COL_RFTIME] - lrf < pthresh:
                                    photo = True
                                    if not sec.lines[-1][7]:  # not placed
                                        sec.lines[-1][8] = True
                            if self.start is not None:
                                et = r[COL_RFTIME] - self.start
                                if self.event[u'type'] == u'cross':
                                    if r[COL_LAPS] > 0:
                                        al = tod.mktod(et.timeval /
                                                       r[COL_LAPS])
                                        es = al.rawtime(1)
                                else:
                                    es = et.rawtime(2)
                            else:
                                es = r[COL_RFTIME].rawtime(2)
                            lrf = r[COL_RFTIME]
                        else:
                            lrf = None

                        # format 'finish' time
                        if ft is None:
                            ft = bt
                            bs = ft.rawtime(0)
                        else:
                            if bt > lt:
                                # New bunch
                                sec.lines.append([None, None, None])
                                bs = u'+' + (bt - ft).rawtime(0)
                            else:
                                # Same time
                                pass
                        lt = bt
                        # sep placed and unplaced
                        insertgap = False
                        if lplaced and placed != lplaced:
                            sec.lines.append([None, None, None])
                            sec.lines.append(
                                [None, None, u'Riders not yet placed'])
                            insertgap = True
                        lplaced = placed
                    else:
                        if r[COL_COMMENT].decode(u'utf-8').strip() != u'':
                            comment = r[COL_COMMENT].decode(u'utf-8').strip()
                        else:
                            comment = u'____'

                    # sep timed and untimed
                    if not insertgap and ltimed and ltimed != timed:
                        sec.lines.append([None, None, None])
                        sec.lines.append(
                            [None, None, u'Riders not seen at finish.'])
                        insertgap = True
                    ltimed = timed
                    sec.lines.append([
                        comment, rbib, r[COL_NAMESTR].decode(u'utf-8'),
                        str(r[COL_LAPS]), bs, es, laplist, placed, photo,
                        catstart, rcat
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
                        comment, rbib, r[COL_NAMESTR].decode(u'utf-8'),
                        unicode(r[COL_LAPS]), None, es, laplist, True, False,
                        catstart, rcat
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
        _log.debug(u'Categorised result report')
        self.recalculate()
        ret = []
        for cat in self.cats:
            ret.extend(self.single_catresult(cat))

        # show all intermediates here
        for i in self.intermeds:
            im = self.intermap[i]
            if im[u'places'] and im[u'show']:
                ret.extend(self.int_report(i))

        if len(self.comment) > 0:
            s = report.bullet_text(u'comms')
            s.heading = u'Decisions of the commissaires panel'
            for comment in self.comment:
                s.lines.append([None, comment])
            ret.append(s)
        return ret

    def single_catresult(self, cat):
        _log.debug(u'Cat result for cat=%r', cat)
        ret = []
        allin = False
        catname = cat
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
        if cat in self.catlaps and self.catlaps[cat] is not None:
            laps = self.catlaps[cat]
        doflap = self.dofastestlap
        if self.start is None:
            doflap = False  # don't do laps unless start is set
        flap = None
        fno = None
        fcnt = None
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

        wt = None
        bwt = None
        leadpass = None
        leadlap = None
        leadsplits = None
        lt = None
        first = True
        lcomment = u''
        lp = None
        lsrc = None
        rcnt = 0
        plcnt = 1
        jcnt = 0
        vcnt = 0
        totcount = 0
        dnscount = 0
        dnfcount = 0
        hdcount = 0
        fincount = 0
        for r in self.riders:
            rcat = r[COL_CAT].decode(u'utf-8').upper()
            rcats = [u'']
            if rcat.strip():
                rcats = rcat.split()
            incat = False
            if allin or (cat and cat in rcats):
                incat = True  # rider is in this category
            elif not cat:  # is the rider uncategorised?
                if rcats[0] == u'':
                    incat = True
                else:
                    incat = rcats[0] not in self.cats  # backward logic
            if incat:
                if cat:
                    rcat = cat
                else:
                    rcat = rcats[0]  # (work-around mis-categorised rider)
                totcount += 1
                sof = None  # all riders have a start time offset
                if r[COL_STOFT] is not None:
                    sof = r[COL_STOFT]
                elif rcat in self.catstarts:
                    sof = self.catstarts[rcat]
                bstr = r[COL_BIB].decode(u'utf-8')
                nstr = r[COL_NAMESTR].decode(u'utf-8')
                rlap = r[COL_LAPS]
                pstr = u''
                tstr = u''  # cross laps down
                dstr = u''  # time/gap
                cstr = u''
                rpass = None
                if self.showuciids:
                    dbr = self.meet.rdb.getrider(bstr, self.series)
                    if dbr is not None:
                        cstr = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                placed = False  # placed at finish
                timed = False  # timed at finish
                virtual = False  # oncourse
                comment = None
                if r[COL_INRACE]:
                    psrc = r[COL_PLACE].decode(u'utf-8')
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
                        jcnt += 1
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if self.event[u'type'] == u'cross':
                        ronlap = True
                        risleader = False
                        dtlap = rlap
                        if leadpass is None and rlap > 0:
                            risleader = True
                            leadlap = rlap
                            if len(r[COL_RFSEEN]) > 0:
                                # an untimed leader with manual lap count
                                leadpass = r[COL_RFSEEN][-1]
                                leadsplits = [tv for tv in r[COL_RFSEEN]]
                        if rlap > 0 and leadpass is not None:
                            if len(r[COL_RFSEEN]) > 0:
                                rpass = r[COL_RFSEEN][-1]
                            if bt is None:
                                if rpass is not None and rpass < leadpass:
                                    # rider is still finishing a lap
                                    rlap += 1
                                    ronlap = False
                                virtual = True
                                vcnt += 1
                                dstr = u''
                        if leadlap is not None:
                            if leadlap != rlap and rlap > 0:
                                # show laps down in time column
                                virtual = True
                                tstr = u'-{0:d} lap{1}'.format(
                                    leadlap - rlap,
                                    strops.plural(leadlap - rlap))
                                # invalidate bunch times for this rider
                                bwt = None
                                bt = None
                        if risleader and self.start is not None:
                            if leadpass is not None:
                                et = leadpass - self.start
                                if sof is not None:
                                    et = et - sof
                                dstr = et.rawtime(0)
                        elif bt is None and self.showdowntimes:
                            # synthesise down time if possible
                            if dtlap > 0:
                                rlpass = None
                                if len(r[COL_RFSEEN]) >= dtlap:
                                    rlpass = r[COL_RFSEEN][dtlap - 1]
                                llpass = None
                                if len(leadsplits) >= dtlap:
                                    llpass = leadsplits[dtlap - 1]
                                else:
                                    _log.debug(u'Lap down time not available')
                                if llpass is not None and rlpass is not None:
                                    rdown = rlpass - llpass
                                    if rdown < MAXELAP:
                                        dstr = u'+' + rdown.rawtime(0)
                                        if not ronlap:
                                            dstr = u'[' + dstr + u']'
                                    else:
                                        # probably a change of leader
                                        if rpass is not None:
                                            et = rpass - self.start
                                            if sof is not None:
                                                et = et - sof
                                            dstr = u'[' + et.rawtime(0) + u']'

                    if bt is not None:
                        fincount += 1  # for accounting, use bunch time
                        timed = True
                        # compute elapsed
                        et = bt
                        if sof is not None:
                            # apply a start offset
                            et = bt - sof
                        if wt is None:  # first finish time
                            wt = et
                            if rlap != laps:
                                # assume the distance is invalid
                                distance = None
                        if bwt is not None:
                            if self.showdowntimes:
                                dstr = u'+' + (et - bwt).rawtime(0)
                        else:
                            dstr = et.rawtime(0)
                        first = False
                        if bwt is None:
                            bwt = et
                    lt = bt
                else:
                    # Non-finishers dns, dnf, otl, dsq
                    placed = True  # for purpose of listing
                    comment = r[COL_COMMENT].decode(u'utf-8')
                    if comment == u'':
                        comment = u'dnf'
                    if comment != lcomment:
                        sec.lines.append([None, None, None])  # new bunch
                    lcomment = comment
                    # account for special cases
                    if comment == u'dns':
                        dnscount += 1
                    elif comment == u'otl':
                        # otl special case: also show down time if possible
                        bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                        if bt is not None and self.showdowntimes:
                            if not first and wt is not None:
                                et = bt
                                if sof is not None:
                                    # apply a start offset
                                    et = bt - sof
                                dstr = u'+' + (et - wt).rawtime(0)
                        hdcount += 1
                    else:
                        dnfcount += 1
                    pstr = comment
                if placed or timed or virtual:
                    sec.lines.append([pstr, bstr, nstr, cstr, tstr, dstr])
                    ## and look up pilots?
                    if cat in [u'MB', u'WB']:
                        sec.even = True  # twocol result
                        # lookup pilot
                        dbr = self.meet.rdb.getrider(bstr, u'pilot')
                        if dbr is not None:
                            puci = u''
                            if self.showuciids:
                                puci = self.meet.rdb.getvalue(
                                    dbr, riderdb.COL_UCICODE)
                            pnam = strops.listname(
                                self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                                self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                                self.meet.rdb.getvalue(dbr, riderdb.COL_ORG))
                            sec.lines.append(
                                [u'', u'pilot', pnam, puci, u'', u''])
                if doflap and comment != u'dns':
                    if len(r[COL_RFSEEN]) > 0:
                        # only consider laps between stime and ftime
                        stime = self.start
                        if sof is not None:
                            time += sof
                        ftime = tod.now()
                        if r[COL_RFTIME] is not None:
                            ftime = r[COL_RFTIME]
                        ls = stime
                        lt = None
                        lc = 0
                        for p in r[COL_RFSEEN]:
                            if ls >= ftime:
                                break  # lap starts after end of region
                            if p < ls:
                                continue  # passing before start of region
                            else:
                                lt = p - ls
                                if lt > self.minlap:
                                    lc += 1  # consider this a legit lap
                                    if flap is None or lt < flap:  # new fastest
                                        flap = lt
                                        fno = bstr
                                        fcnt = lc
                                else:
                                    pass
                                    # short lap
                                ls = p
                rcnt += 1
            else:
                # not in this category.
                pass
        if self.timerstat == u'finished':
            sec.heading = u'Result'
        elif self.timerstat in [u'idle', u'armstart']:
            sec.heading = u''
        elif self.timerstat in [u'running', u'armfinish']:
            # set status if number of judged riders greater than jtgt
            jtgt = 10
            javail = totcount - (dnfcount + dnscount + hdcount)
            if javail < 16:
                jtgt = 1
            if javail > 0 and jcnt >= jtgt:
                sec.heading = u'Provisional Result'
            elif vcnt > 0:
                sec.heading = u'Virtual Standing'
            else:
                sec.heading = u'Race In Progress'
        else:
            sec.heading = u'Provisional Result'
        if footer:
            sec.footer = footer

        # Append all result categories and uncat if riders
        if cat or totcount > 0:
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
            if doflap and fno is not None:
                ftr = self.getrider(fno)
                fts = u''
                if ftr is not None:
                    fts = ftr[COL_SHORTNAME].decode(u'utf-8')
                ftstr = flap.rawtime(0)
                if flap < tod.tod(60):
                    ftstr += u' sec'
                fmsg = u'Fastest lap: {} {} {} on lap {:d}'.format(
                    fno, fts, ftstr, fcnt)
                sec.lines.append([None, fmsg])

            sec.lines.append(
                [None, u'Number of starters: ' + unicode(totcount - dnscount)])
            if hdcount > 0:
                sec.lines.append([
                    None,
                    u'Riders finishing out of time limits: ' + unicode(hdcount)
                ])
            if dnfcount > 0:
                sec.lines.append([
                    None, u'Riders abandoning the event: ' + unicode(dnfcount)
                ])
            residual = totcount - (fincount + dnfcount + dnscount + hdcount)
            if residual > 0:
                _log.info(u'%r unaccounted for: %r', cat, residual)
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
        """Return a result report."""

        # check if a categorised report is required
        if (self.event[u'type'] == u'cross' or
            (self.event[u'type'] != u'handicap' and len(self.cats) > 1)):
            return self.catresult_report()

        _log.debug(u'Result report in uncat/handicap path')
        self.recalculate()
        ret = []
        wt = None
        we = None
        dofastest = False  # ftime for handicaps
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
        lcomment = u''
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
        if self.places or self.timerstat != u'idle':
            sec = report.section(u'result')
            if self.event[u'type'] == u'handicap':
                sec.colheader = [
                    None, None, None, None, u'Elapsed', u'Time/Gap'
                ]
            if self.racestat == u'final':
                sec.heading = u'Result'
            elif self.racestat == u'provisional':
                sec.heading = u'Provisional Result'
            else:
                sec.heading = u'Race In Progress'

            first = True
            for r in self.riders:
                totcount += 1
                bstr = r[COL_BIB].decode(u'utf-8')  # 'bib'
                nstr = r[COL_NAMESTR].decode(u'utf-8')  # 'name'
                # in handicap / flat result - only first category is considered
                cs = r[COL_CAT].decode(u'utf-8')
                cstr = riderdb.primary_cat(cs)  # 'cat'
                rcat = self.ridercat(cstr)
                if cstr.upper() in catcache:
                    cstr = catcache[cstr.upper()]
                if self.showuciids:
                    dbr = self.meet.rdb.getrider(bstr, self.series)
                    if dbr is not None:
                        cstr = self.meet.rdb.getvalue(dbr, riderdb.COL_UCICODE)
                elif not self.showcats:
                    cstr = u''
                pstr = u''  # 'place'
                tstr = u''  # 'elap' (hcp only)
                dstr = u''  # 'time/gap'
                placed = False  # placed at finish
                timed = False  # timed at finish
                if r[COL_INRACE]:
                    psrc = r[COL_PLACE].decode(u'utf-8')
                    if psrc != u'':
                        pstr = psrc + u'.'
                        placed = True
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if bt is not None:
                        timed = True
                        fincount += 1  # for accounting, use bunch time
                        if wt is None:  # first finish time
                            wt = bt
                            first = False
                            # for uncat/hcap first time is always race time
                            dstr = wt.rawtime(0)
                        else:
                            # for uncat/handicap, time gap is always
                            # down on winner's uncorrected time
                            if self.showdowntimes:
                                dstr = u'+' + (bt - wt).rawtime(0)

                        # compute elapsed, or corrected time
                        if self.event[u'type'] == u'handicap':
                            # always show elap for hcp, even if no stof*
                            tstr = bt.rawtime(0)
                        et = bt
                        sof = None
                        if r[COL_STOFT] is not None:  # apply a start offset
                            sof = r[COL_STOFT]
                        elif rcat in self.catstarts:
                            sof = self.catstarts[rcat]
                        if sof is not None:
                            dofastest = True  # will need to report!
                            et = bt - sof
                            # *but, adjust tstr if a start offset is present
                            tstr = et.rawtime(0)
                            if we is None:
                                we = et
                        if fastest is None or et < fastest:
                            fastest = et
                            fastestbib = r[COL_BIB]
                    else:  # check virtual finish time
                        sof = None
                        if r[COL_STOFT] is not None:
                            sof = r[COL_STOFT]
                        elif rcat in self.catstarts and self.catstarts[
                                rcat] != tod.ZERO:
                            sof = self.catstarts[rcat]
                        if sof is not None:
                            vt = curelap - sof
                            if vfastest is None or vt < vfastest:
                                vfastest = vt
                    lt = bt
                else:
                    # Non-finishers dns, dnf, otl, dsq
                    placed = True  # for purpose of listing
                    comment = r[COL_COMMENT].decode(u'utf-8')
                    if comment == u'':
                        comment = u'dnf'
                    if comment != lcomment:
                        sec.lines.append([None, None, None])  # new bunch
                    lcomment = comment
                    # account for special cases
                    if comment == u'dns':
                        dnscount += 1
                    elif comment == u'otl':
                        # otl special case: also show down time if possible
                        bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                        if bt is not None:
                            if not first and wt is not None:
                                et = bt
                                sof = None
                                if r[COL_STOFT] is not None:
                                    sof = r[COL_STOFT]
                                elif rcat in self.catstarts:
                                    sof = self.catstarts[rcat]
                                if sof is not None:
                                    # apply a start offset
                                    et = bt - sof
                                dstr = u'+' + (et - wt).rawtime(0)
                        hdcount += 1
                    else:
                        dnfcount += 1
                    pstr = comment
                if placed or timed:
                    sec.lines.append([pstr, bstr, nstr, cstr, tstr, dstr])
            ret.append(sec)

            # Race metadata / UCI comments
            sec = report.bullet_text(u'resultuci')
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
                    _log.info(u'Fastest time not yet available')
                else:
                    ftr = self.getrider(fastestbib)
                    fts = u''
                    if ftr is not None:
                        fts = ftr[COL_SHORTNAME].decode('utf-8')
                    fmsg = (u'Fastest time: ' + fastest.rawtime(0) + u'  ' +
                            fastestbib + u' ' + fts)
                    smsg = (u'Fastest time - ' + fts + u' ' +
                            fastest.rawtime(0))
                    sec.lines.append([None, fmsg])
                    if not self.readonly:  # in a ui window?
                        self.meet.cmd_announce(u'resultmsg', fmsg)
                        self.meet.cmd_announce(u'scrollmsg', smsg)

            sec.lines.append(
                [None, u'Number of starters: ' + unicode(totcount - dnscount)])
            if hdcount > 0:
                sec.lines.append([
                    None,
                    u'Riders finishing out of time limits: ' + unicode(hdcount)
                ])
            if dnfcount > 0:
                sec.lines.append([
                    None, u'Riders abandoning the event: ' + unicode(dnfcount)
                ])
            residual = totcount - (fincount + dnfcount + dnscount + hdcount)
            if residual > 0:
                _log.info(u'%r unaccounted for', residual)
            ret.append(sec)

            # Intermediates
            # show all intermediates here
            for imed in self.intermeds:
                im = self.intermap[imed]
                _log.info(u'intermed : %r', imed)
                if im[u'places'] and im[u'show']:
                    ret.extend(self.int_report(imed))

            # Decisions of commissaires panel
            if len(self.comment) > 0:
                sec = report.bullet_text(u'comms')
                sec.heading = u'Decisions of the Commissaires Panel'
                for cl in self.comment:
                    sec.lines.append([None, cl.strip()])
                ret.append(sec)
        else:
            _log.warning(u'No data available for result report')
        return ret

    def stat_but_clicked(self, button=None):
        """Deal with a status button click in the main container."""
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
            _log.error(u'Ignoring invalid action %r', acode)
        return False

    def add_comment(self, comment=u''):
        """Append a commissaires comment."""
        self.comment.append(comment.strip())
        _log.info(u'Added comment: %r', comment)

    def query_rider(self, bib=None):
        """List info on selected rider in the scratchpad."""
        _log.info(u'Query rider: %r', bib)
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
                    clubxtra = self.meet.rdb.getvalue(dbr, riderdb.COL_ORG)
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
                        team = self.meet.rdb.getvalue(dbr, riderdb.COL_ORG)
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
                        if self.event[u'type'] != u'handicap':
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
                if self.event[u'type'] != u'handicap' and (
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
        _log.debug(u'Clear event result')
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
            _log.debug(u'Ignoring non-series rider: %r',
                       strops.bibser2bibstr(bib, series))
            return None
        if bib == u'' or self.getrider(bib) is None:
            nr = [
                bib, u'', u'', u'', u'', True, u'', 0, 0, None, None, None,
                None, None, None, []
            ]
            dbr = self.meet.rdb.getrider(bib, self.series)
            if dbr is not None:
                nr[COL_NAMESTR] = strops.listname(
                    self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                    self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                    self.meet.rdb.getvalue(dbr, riderdb.COL_ORG))
                nr[COL_SHORTNAME] = strops.listname(
                    self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                    self.meet.rdb.getvalue(dbr, riderdb.COL_LAST))
                nr[COL_CAT] = self.meet.rdb.getvalue(dbr, riderdb.COL_CAT)
                self.ucicache[bib] = self.meet.rdb.getvalue(
                    dbr, riderdb.COL_UCICODE)
            return self.riders.append(nr)
        else:
            return None

    def resettimer(self):
        """Reset event timer."""
        _log.info(u'Reset event to idle')
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
        self.resetcatonlaps()
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
            self.catonlap[cat] = 0
            target = self.catlaps[cat]
            if target:
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
        _log.debug(u'Arm lap')
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
                return False  # no arm till event underway
        if self.curlap <= 0 or self.lapfin is not None:
            self.curlap += 1  # increment

            if self.totlaps and self.curlap > self.totlaps:
                _log.info(u'Too many laps')
                self.curlap = self.totlaps

            # sanity check onlap
            # once arm lap is done, curlap and onlap _should_ be same
            if self.onlap != self.curlap:
                _log.debug(u'Cur/On lap mismatch %r/%r', self.curlap,
                           self.onlap)
                if self.curlap == 1:
                    # assume this is an in-race correction
                    self.curlap = self.onlap
                    _log.debug(u'Curlap set to %r from onlap', self.curlap)
                else:
                    # assume the curlap is set to the desired count
                    self.onlap = self.curlap
                    _log.debug(u'Onlap set to %r from curlap', self.curlap)
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
            if self.totlaps is not None and self.onlap is not None:
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
            else:
                # make sure something is displayed in this path
                self.meet.cmd_announce(u'laplbl', None)
                self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
            self.lapfin = None
        else:
            self.meet.cmd_announce(u'bunches', u'final')
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
                _log.info('Rider %r did not finish with code: %r', bib, code)
            else:
                _log.warning('Unregistered Rider %r unchanged', bib)
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

    def retriders(self, biblist=u''):
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

    def shutdown(self, win=None, msg=u'Race Sutdown'):
        """Close event."""
        _log.debug(u'Event shutdown: %r', msg)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def starttrig(self, e):
        """Process a start trigger signal."""
        # Note: in rms all triggers other than C1 from alttimer
        #       are assumed to be for the start
        if self.timerstat == u'armstart':
            _log.info(u'Start trigger: %s@%s/%s', e.chan, e.rawtime(),
                      e.source)
            self.set_start(e)
            self.resetcatonlaps()
            if self.event[u'type'] in [u'criterium', u'circuit', u'cross']:
                glib.idle_add(self.armlap)
        else:
            _log.info(u'Trigger: %s@%s/%s', e.chan, e.rawtime(), e.source)
        return False

    def set_lap_finish(self, e):
        """Write lap time into model and emit changed state."""
        self.laptimes.append(e)
        self.lapfin = e
        if self.totlaps is not None:
            if self.onlap == self.totlaps:
                self.onlap = None
            else:
                self.onlap += 1
        else:
            self.onlap += 1
        self.reannounce_times()

    def alttimertrig(self, e):
        """Record timer message from alttimer."""
        # note: these impulses are sourced from alttimer device and keyboard
        #       transponder triggers are collected separately in timertrig()
        _log.debug(u'Alt timer: %s@%s/%s', e.chan, e.rawtime(), e.source)
        channo = strops.chan2id(e.chan)
        if channo == 1:
            # this is a finish impulse, treat as bunch time
            if self.timerstat == u'armfinish':
                if self.altfinish is not None:
                    dt = e - self.altfinish
                    _log.info(u'New bunch time: +%s', dt.rawtime(0))
                else:
                    _log.debug(u'Recording first bunch finish: %s',
                               e.rawtime())
                    self.altfinish = e
        else:
            # send through to catch-all trigger handler
            self.starttrig(e)

    def catstarted(self, cat):
        """Return true if category is considered started."""
        ret = False
        if self.start is not None:
            stof = tod.ZERO
            if cat in self.catstarts and self.catstarts[cat] is not None:
                stof = self.catstarts[cat]
            st = self.start + stof
            nt = tod.now()
            if st < nt:
                _log.debug(u'Cat %r has started', cat)
                ret = True
            else:
                _log.debug(u'Cat %r has not yet started: %s < %s', cat,
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
                    target = self.totlaps
                    togo = None
                    if cat in self.catlaps and self.catlaps[cat] is not None:
                        target = self.catlaps[cat]
                    if target is not None and count is not None and count < target:
                        prompt = cat.upper()
                        dbr = self.meet.rdb.getrider(cat, u'cat')
                        if dbr is not None:
                            prompt = self.meet.rdb.getvalue(
                                dbr, riderdb.COL_ORG)
                        self.meet.cmd_announce(
                            u'catlap', u'\x1f'.join([
                                cat, prompt,
                                unicode(onlap),
                                unicode(target),
                                unicode(target - onlap)
                            ]))
                        _log.debug(u'Cat %r %r: %r/%r, %r to go', cat, prompt,
                                   onlap, target, target - onlap)
                    else:
                        _log.debug(u'No data for %r cat lap', cat)

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

        # if there's a source filter set, discard any unknown sources
        # todo: channel should be the primary source identifier
        if len(self.passingsource) > 0:
            if e.source.lower() not in self.passingsource:
                _log.info(u'Invalid passing: %s:%s@%s/%s', bib, e.chan,
                          e.rawtime(2), e.source)
                return False

        # check for a spare bike in riderdb cat, before clubmode additions
        spcat = riderdb.primary_cat(self.meet.rdb.getvalue(r, riderdb.COL_CAT))
        if self.allowspares and spcat == u'SPARE' and self.timerstat in [
                u'running', u'armfinish'
        ]:
            _log.warning(u'Adding spare bike: %r', bib)
            self.addrider(bib)

        # fetch rider info from event
        lr = self.getrider(bib)
        if lr is None:
            if self.clubmode and self.timerstat in [
                    u'armstart', u'running', u'armfinish'
            ]:
                self.addrider(bib)
                lr = self.getrider(bib)
                _log.info(u'Added new starter: %s:%s@%s/%s', bib, e.chan,
                          e.rawtime(2), e.source)
            else:
                _log.info(u'Non-starter: %s:%s@%s/%s', bib, e.chan,
                          e.rawtime(2), e.source)
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
        elif rcat in self.catstarts and self.catstarts[rcat] is not None:
            catstart = self.catstarts[rcat]
        if self.start is not None:
            st = self.start + catstart + self.minlap
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
        doarm = False
        targetlap = None
        if self.targetlaps:
            # category laps override event laps
            if rcat in self.catlaps and self.catlaps[rcat]:
                targetlap = self.catlaps[rcat]
            else:
                targetlap = self.totlaps
            if targetlap and lr[COL_LAPS] >= targetlap - 1:
                lapfinish = True  # arm just this rider
                if self.event[u'type'] == u'cross':
                    doarm = True

        # for cross races when targets apply, armfinish is set automatically
        if doarm and lapfinish and self.timerstat != u'armfinish':
            self.armfinish()

        # finishing rider path
        if self.timerstat == u'armfinish' or lapfinish:
            if self.finish is None:  # implies lr[COL_RFTIME] is None
                # in case finish is being triggered by a lap target,
                # ensure that the event lap is armed and the final lap
                # is recorded
                if lapfinish:
                    self.armlap()
                    self.set_lap_finish(e)

                # Then Announce first finish to scoreboard.
                self.set_finish(e)
                self.meet.cmd_announce(u'redraw', u'timer')
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
                        if rcat in self.catonlap and lr[
                                COL_LAPS] > self.catonlap[rcat]:
                            self.catonlap[rcat] = lr[COL_LAPS]
                            self.announcecatlap(rcat)
                        if self.lapfin is None:
                            self.set_lap_finish(e)
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
                onlap = False

                # category and target lap counting
                if self.targetlaps:
                    catlap = 0
                    if rcat in self.catonlap:
                        catlap = self.catonlap[rcat]
                    else:
                        self.catonlap[rcat] = 0  # init

                    if lr[COL_LAPS] > catlap:
                        self.catonlap[rcat] = lr[COL_LAPS]
                        self.announcecatlap(rcat)
                    else:
                        if lr[COL_LAPS] >= catlap:
                            # rider is on the current event lap
                            onlap = True

                # event lap count handling
                if self.lapfin is None:
                    # lap finish armed, first rider with laps == curlap
                    # will be considered the leader, otherwise they are dropped
                    # NOTE: this overrides lap time guards
                    if lr[COL_LAPS] == self.curlap:
                        self.set_lap_finish(e)
                        self.meet.cmd_announce(u'redraw', u'timer')
                        onlap = True
                else:
                    # check if passing is on this lap
                    if self.lapfin is not None:
                        curlapstart = self.lapfin
                        if e < curlapstart:
                            # passing is for a previous event lap
                            onlap = False
                            _log.info(
                                u'Passing on previous lap: %s:%s@%s/%s < %s',
                                bib, e.chan, e.rawtime(2), e.source,
                                curlapstart.rawtime(2))
                        else:
                            if lr[COL_LAPS] == self.curlap:
                                onlap = True
                            elif lr[COL_LAPS] < self.curlap:
                                if self.event[u'type'] == u'criterium':
                                    # push them on to the current lap
                                    lr[COL_LAPS] = self.curlap
                                    onlap = True
                                else:
                                    # rider is not on current event lap
                                    pass
                            else:
                                if e < curlapstart + self.minlap:
                                    # passing cannot be for a new lap yet
                                    if self.event[u'type'] == u'criterium':
                                        # push them back to the current lap
                                        lr[COL_LAPS] = self.curlap
                                        onlap = True
                                    else:
                                        _log.warning(
                                            u'Invalid laps %r: %r / %r', bib,
                                            lr[COL_LAPS], self.curlap)
                                else:
                                    # otherwise this is the lap leader
                                    self.armlap()
                                    self.set_lap_finish(e)
                                    self.meet.cmd_announce(u'redraw', u'timer')
                                    onlap = True
                    else:
                        # event laps are not being tracked, no one is onlap
                        pass

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

    def lapentry_activate_cb(self, entry, data=None):
        """Transfer lap entry string into model if possible."""
        reqlap = entry.get_text().decode(u'utf-8')
        newlap = None
        try:
            newlap = int(reqlap)
        except Exception:
            _log.debug(u'Invalid lap count %r', reqlap)

        if newlap is not None and newlap > 0:
            if self.event[u'type'] == u'criterium':
                # force all in riders onto the desired lap
                for r in self.riders:
                    if r[COL_INRACE]:
                        r[COL_LAPS] = newlap - 1
            else:
                # correct all rider lap counts, saturated at desired lap
                for r in self.riders:
                    # TODO: olap should only include laps recorded after
                    #       catstart - which may have changed
                    olap = len(r[COL_RFSEEN])
                    if r[COL_INRACE]:
                        if olap > newlap - 1:
                            olap = newlap - 1
                    r[COL_LAPS] = olap
            if self.lapfin is not None:
                self.curlap = newlap - 1
            else:
                self.curlap = newlap
            self.armlap()
        else:
            self.curlap = -1
            self.lapstart = None
            self.lapfin = None
            maxlap = 1
            entry.set_text(u'')
            if self.timerstat not in [u'idle', u'armstart', u'finished']:
                maxlap = 0
                for r in self.riders:
                    r[COL_LAPS] = len(r[COL_RFSEEN])
                    maxlap = max(r[COL_LAPS] + 1, maxlap)
            self.onlap = maxlap
            if self.event[u'type'] in [u'criterium', u'circuit', u'cross']:
                self.armlap()

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

    def finsprint(self, places):
        """Display a final sprint 'provisional' result."""
        self.live_announce = False
        self.meet.cmd_announce(u'clear', u'all')
        scrollmsg = u'Provisional - '
        titlestr = self.title_namestr.get_text()
        if self.racestat == u'final':
            scrollmsg = u'Result - '
            self.meet.cmd_announce(u'title', titlestr + ': Final Result')
        else:
            self.meet.cmd_announce(u'title', titlestr + ': Provisional')
        self.meet.cmd_announce(u'bunches', u'final')
        placeset = set()
        idx = 0
        st = tod.ZERO
        if self.start is not None:
            st = self.start
        # result is sent in weird hybrid units TODO: fix the amb
        wt = None
        lb = None
        scrollcnt = 0
        for placegroup in places.split():
            curplace = idx + 1
            for bib in placegroup.split(u'-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is not None:
                        ft = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                        fs = u''
                        if ft is not None:
                            # temp -> just use the no-blob style to correct
                            fs = ft.rawtime()
                            if wt is None:
                                wt = ft
                            lb = ft
                        if scrollcnt < 5:
                            scrollmsg += (u' ' +
                                          r[COL_PLACE].decode(u'utf-8') +
                                          u'. ' +
                                          r[COL_SHORTNAME].decode(u'utf-8') +
                                          u' ')
                            scrollcnt += 1
                        glib.idle_add(self.meet.rider_announce, [
                            r[COL_PLACE].decode(u'utf-8') + u'.', bib,
                            r[COL_NAMESTR].decode(u'utf-8'),
                            r[COL_CAT].decode(u'utf-8'), fs
                        ])
                    idx += 1
        self.meet.cmd_announce(u'scrollmsg', scrollmsg)
        if wt is not None:
            if self.start:
                self.meet.cmd_announce(u'start', self.start.rawtime())
            if self.finish:
                self.meet.cmd_announce(u'finish', self.finish.rawtime())
            else:
                _log.info(u'No valid times available')

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
        points = None
        # for 1-1 intermed/contest entries, copy points to inter report
        if acode in self.contestmap:
            if len(self.contestmap[acode][u'points']) > 1:
                points = self.contestmap[acode][u'points']
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
                        elif points is not None:
                            if curplace <= len(points):
                                xtra = unicode(points[curplace - 1])
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
            if points is not None:
                sec.units = u'pt'
            ret.append(sec)
        return ret

    def intsprint(self, acode=u'', places=u''):
        """Display an intermediate sprint 'provisional' result."""

        ## TODO : Fix offset time calcs - too many off by ones
        if acode not in self.intermeds:
            _log.debug(u'Attempt to display non-existent inter: %r', acode)
            return
        descr = acode
        if self.intermap[acode][u'descr']:
            descr = self.intermap[acode][u'descr']
        self.live_announce = False
        self.meet.cmd_announce(u'clear', u'all')
        self.reannounce_times()
        self.meet.cmd_announce(u'title', descr)
        scrollmsg = descr + u' - '
        placeset = set()
        idx = 0
        for placegroup in places.split():
            curplace = idx + 1
            for bib in placegroup.split(u'-'):
                if bib not in placeset:
                    placeset.add(bib)
                    r = self.getrider(bib)
                    if r is not None:
                        scrollmsg += (u' ' + unicode(curplace) + u'. ' +
                                      r[COL_SHORTNAME].decode(u'utf-8') + u' ')
                        rank = u''
                        if acode != u'brk':
                            rank = unicode(curplace) + u'.'
                        glib.idle_add(self.meet.rider_announce, [
                            rank, bib, r[COL_NAMESTR].decode(u'utf-8'),
                            r[COL_CAT].decode(u'utf-8'), u''
                        ])
                    idx += 1
                else:
                    _log.warning(u'Duplicate no. % in places', bib)
        glib.timeout_add_seconds(25, self.reannounce_lap)

    def todempty(self, val):
        if val is not None:
            return val.rawtime()
        else:
            return u''

    def reannounce_times(self):
        """Re-send the current timing values."""
        self.meet.cmd_announce(u'gapthresh', self.gapthresh.rawtime(2))
        self.meet.cmd_announce(u'timerstat', self.timerstat)
        self.meet.cmd_announce(u'start', self.todempty(self.start))
        self.meet.cmd_announce(u'finish', self.todempty(self.finish))
        self.meet.cmd_announce(u'lapstart', self.todempty(self.lapstart))
        self.meet.cmd_announce(u'lapfin', self.todempty(self.lapfin))
        totlaps = None
        if self.totlaps:  #may be zero, but don't show that case
            totlaps = unicode(self.totlaps)
        curlap = None
        if self.curlap is not None:
            curlap = unicode(self.curlap)
        onlap = None
        if self.onlap is not None:
            onlap = unicode(self.onlap)
        self.meet.cmd_announce(u'onlap', onlap)
        self.meet.cmd_announce(u'curlap', curlap)
        self.meet.cmd_announce(u'totlaps', totlaps)
        # Write lap time fields
        lapstr = None
        if self.timerstat not in [u'armfinish', u'finished']:
            self.meet.cmd_announce(u'finstr', None)
            if self.totlaps is not None and self.onlap is not None:
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
            else:
                # make sure something is displayed in this path
                self.meet.cmd_announce(u'laplbl', None)
                self.meet.cmd_announce(u'finstr', self.meet.get_short_name())

        if self.timerstat == u'idle':
            self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
        return False

    def reannounce_lap(self):
        """Re-send current lap passing data."""
        self.meet.cmd_announce(u'clear', u'all')
        self.meet.cmd_announce(u'scrollmsg', None)
        self.meet.cmd_announce(u'bunches', u'laps')
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
        et = None
        nt = None
        if self.start is not None and self.timerstat != u'finished':
            nt = tod.now()
            et = nt - self.start
        if et is not None:
            evec = []
            if self.finish is not None:
                # event down time is on first finisher
                ft = (self.finish - self.start).truncate(0)
                evec.append(ft.rawtime(0))
                evec.append(u'+' + (et - ft).rawtime(0))

                # time limit is applied to total event time/first finisher
                limit = self.decode_limit(self.timelimit, ft)
                if limit is not None:
                    evec.append('Limit:')
                    evec.append(u'+' + (limit - ft).rawtime(0))
            else:
                evec.append(et.rawtime(0))
                if self.lapfin is not None:
                    # prev lap time
                    if self.lapstart is not None:
                        evec.append('Lap:')
                        #evec.append((self.lapfin - self.start).rawtime(0))
                        #evec.append('/')
                        evec.append((self.lapfin - self.lapstart).rawtime(0))
                    # lap down time
                    dt = nt - self.lapfin
                    if dt < MAXELAP:
                        evec.append(u'+' + (dt).rawtime(0))
            elapmsg = u' '.join(evec)
            self.elaplbl.set_text(elapmsg)
            self.meet.cmd_announce(u'elapmsg', elapmsg)
        else:
            self.elaplbl.set_text(u'')
            self.meet.cmd_announce(u'elapmsg', u'')
        return True

    def set_start(self, start=u''):
        """Set the start time."""
        wasidle = self.start is None
        self.start = tod.mktod(start)
        if self.start is not None:
            if wasidle:
                self.lapstart = None
                self.lapfin = None
                self.curlap = -1  # reset lap count at start
                self.onlap = 1  # leaders are 'onlap'
                self.meet.cmd_announce(u'onlap', unicode(self.onlap))
            if self.finish is None:
                self.set_running()
            self.meet.cmd_announce(u'start', self.start.rawtime())
        else:
            self.meet.cmd_announce(u'start', None)

    def set_finish(self, finish=u''):
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
                self.set_start(u'0')
            else:
                elap = self.finish - self.start
                dval = self.meet.get_distance()
                if dval is not None:
                    self.meet.cmd_announce(u'average',
                                           elap.rawspeed(1000.0 * dval))

    def get_elapsed(self):
        """Return time from start."""
        ret = None
        if self.start is not None and self.timerstat != u'finished':
            ret = (tod.now() - self.start).truncate(0)
        return ret

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
        if self.finish is None:
            self.set_finish(tod.now())
            _log.info(u'Finish time forced: %s', self.finish.rawtime())

    def info_time_edit_clicked_cb(self, button, data=None):
        """Run an edit times dialog to update event time."""
        st = u''
        if self.start is not None:
            st = self.start.rawtime(2)
        ft = u''
        if self.finish is not None:
            ft = self.finish.rawtime(2)
        ret = uiutil.edit_times_dlg(self.meet.window, stxt=st, ftxt=ft)
        if ret[0] == 1:
            wasrunning = self.timerstat in [u'running', u'armfinish']
            self.set_finish(ret[2])
            self.set_start(ret[1])
            if wasrunning:
                # flag a recalculate
                self._dorecalc = True
            else:
                self.resetcatonlaps()
                if self.event[u'type'] in [u'criterium', u'circuit', u'cross']:
                    glib.idle_add(self.armlap)
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

    def editseed_cb(self, cell, path, new_text, col):
        """Edit the lap field if valid."""
        new_text = new_text.decode(u'utf-8').strip()
        if new_text.isdigit():
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

    def vbunch(self, cbunch=None, mbunch=None):
        """Switch to return best choice bunch time."""
        ret = None
        if mbunch is not None:
            ret = mbunch
        elif cbunch is not None:
            ret = cbunch
        return ret

    def getstart(self, r):
        ret = None
        if r[COL_STOFT] is not None:
            ret = r[COL_STOFT]
        else:
            # Check primary category for start time
            cs = r[COL_CAT].decode(u'utf-8')
            rcat = self.ridercat(riderdb.primary_cat(cs))
            if rcat in self.catstarts and self.catstarts[rcat] is not None:
                ret = self.catstarts[rcat]
        return ret

    def showstart_cb(self, col, cr, model, iter, data=None):
        """Draw start time offset in rider view."""
        st = model.get_value(iter, COL_STOFT)
        otxt = u''
        if st is not None:
            otxt = st.rawtime(0)
        else:
            # cat start comes from first category only
            cs = model.get_value(iter, COL_CAT).decode(u'utf-8')
            rcat = self.ridercat(riderdb.primary_cat(cs))
            if rcat in self.catstarts and self.catstarts[rcat] is not None:
                otxt = self.catstarts[rcat].rawtime(0)
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
            cr.set_property(u'text', mb.rawtime(0))
            cr.set_property(u'style', uiutil.STYLE_OBLIQUE)
        else:
            cr.set_property(u'style', uiutil.STYLE_NORMAL)
            if cb is not None:
                cr.set_property(u'text', cb.rawtime(0))
            else:
                # display last lap time
                seen = model.get_value(iter, COL_RFSEEN)
                if len(seen) > 0:
                    if self.start:
                        # show elapsed on the rider!
                        # except perhaps for cx
                        #if len(seen) > 1:
                        # show last lap time
                        #et = seen[-1] - seen[-2]
                        #else:
                        # show elapsed
                        et = seen[-1] - self.start
                    else:
                        et = seen[-1]
                    cr.set_property(u'text', u'[' + et.rawtime(1) + u']')
                    cr.set_property(u'style', uiutil.STYLE_OBLIQUE)
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
        if not self.showdowntimes:
            self.showdowntimes = True  # assume edit implies required on result
        if new_text == u'':  # user request to clear also clears RFTIME
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
                nmb = tod.mktod(new_text.replace(u'+', u''))
                if nmb is not None:
                    nmb += oft
            elif new_text.startswith(u's'):
                # assume same time as previous rider
                i = int(path) - 1
                if i >= 0:
                    nmb = self.vbunch(self.riders[i][COL_CBUNCH],
                                      self.riders[i][COL_MBUNCH])
                else:
                    _log.info(u'Ignored same time on first rider')
            else:
                nmb = tod.mktod(new_text)
            if self.riders[path][COL_MBUNCH] != nmb:
                self.riders[path][COL_MBUNCH] = nmb
                dorecalc = True
        if dorecalc:
            self.recalculate()

    def checkplaces(self, rlist=u'', dnf=True):
        """Check the proposed places against current event model."""
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
            _log.warning(u'Recalculate already in progress')
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
        """Internal recalculate function."""
        # if readonly and calc set - skip recalc
        if self.readonly and self.calcset:
            _log.debug(u'Cached Recalculate')
            return False

        _log.debug(u'Recalculate model')
        # clear off old places and bonuses
        self.resetplaces()

        # assign places
        self.assign_finish()
        for c in self.contests:
            self.assign_places(c)

        # do rough sort on in, place, laps, rftime, lastpass
        auxtbl = []
        idx = 0
        for r in self.riders:
            rbib = r[COL_BIB].decode(u'utf-8')
            rplace = r[COL_PLACE].decode(u'utf-8')
            rftime = r[COL_RFTIME]
            rlaps = r[COL_LAPS]
            lastpass = tod.ZERO
            if len(r[COL_RFSEEN]) > 0:
                lastpass = r[COL_RFSEEN][-1]
                # in cross scoring, rftime is same as last passing
                if self.event[u'type'] == u'cross':
                    rftime = lastpass
            if not rplace or not r[COL_INRACE]:
                rplace = r[COL_COMMENT].decode(u'utf-8')
            if not r[COL_INRACE]:
                rlaps = 0
                rftime = tod.MAX
                lastpass = tod.MAX
            auxtbl.append(
                (not r[COL_INRACE], strops.dnfcode_key(rplace), -rlaps, rftime,
                 lastpass, strops.riderno_key(rbib), idx))
            idx += 1
        if len(auxtbl) > 1:
            auxtbl.sort()
            self.riders.reorder([a[6] for a in auxtbl])

        # compute cbunch values on auto time gaps and manual inputs
        # At this point all riders are assumed to be in finish order
        self.maxfinish = tod.ZERO
        racefinish = None
        ft = None  # the finish or first bunch time
        lt = None  # the rftime of last competitor across line
        ll = None  # lap count of previous competitor for cross scoring
        bt = None  # the 'current' bunch time
        if self.start is not None:
            for r in self.riders:
                rcomment = r[COL_COMMENT].decode(u'utf-8')
                if r[COL_INRACE] or rcomment == u'otl':
                    rtime = r[COL_RFTIME]
                    if self.event[u'type'] == u'cross':
                        if ll is None or ll != r[COL_LAPS]:
                            # invalidate last passing since on a different lap
                            lt = None
                            bt = None
                            ll = r[COL_LAPS]
                    if r[COL_MBUNCH] is not None:
                        bt = r[COL_MBUNCH]  # override with manual bunch
                        r[COL_CBUNCH] = bt
                        if ft is None:
                            ft = bt
                        lt = bt
                    elif rtime is not None:
                        # establish elapsed, but allow subsequent override
                        if rtime > self.maxfinish:
                            self.maxfinish = rtime
                        et = rtime - self.start

                        # establish bunch time
                        if ft is None and r[COL_RFTIME] is not None:
                            racefinish = r[COL_RFTIME]  # save race finish
                            ft = et.truncate(0)  # compute first time
                            bt = ft
                        else:
                            if lt is not None and (et < lt or
                                                   et - lt < self.gapthresh):
                                # same time
                                pass
                            else:
                                bt = et.truncate(0)

                        # assign and continue
                        r[COL_CBUNCH] = bt
                        lt = et
                    else:
                        # empty rftime with non-empty rank implies no time gap
                        if r[COL_PLACE]:
                            r[COL_CBUNCH] = bt  # use current bunch time
                        else:
                            r[COL_CBUNCH] = None

                        # for riders still lapping, extend maxfinish too
                        if len(r[COL_RFSEEN]) > 1:
                            lpass = r[COL_RFSEEN][-1]
                            if lpass is not None and lpass > self.maxfinish:
                                self.maxfinish = lpass

        # if racefinish defined, call set finish
        if racefinish:
            self.set_finish(racefinish)

        # re-sort on in,vbunch (not valid for cross scoring)
        # at this point all riders will have valid bunch time
        if self.event[u'type'] != u'cross':
            auxtbl = []
            idx = 0
            for r in self.riders:
                # aux cols: ind, bib, in, place, vbunch
                rbib = r[COL_BIB].decode(u'utf-8')
                rplace = r[COL_PLACE].decode(u'utf-8')
                rlaps = r[COL_LAPS]
                rbunch = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                if not rplace or not r[COL_INRACE]:
                    rplace = r[COL_COMMENT].decode(u'utf-8')
                if not r[COL_INRACE]:
                    rlaps = 0
                    rbunch = tod.MAX
                auxtbl.append((not r[COL_INRACE], strops.dnfcode_key(rplace),
                               -rlaps, rbunch, idx))
                idx += 1
            if len(auxtbl) > 1:
                auxtbl.sort()
                self.riders.reorder([a[4] for a in auxtbl])

        # Scan model to determine racestat and time limits
        if self.timerstat != u'idle':
            limit = None
            if ft is not None and self.timelimit is not None:
                limit = self.decode_limit(self.timelimit, ft)
                if limit is not None:
                    _log.debug(u'Time limit: %r = %s, +%s', self.timelimit,
                               limit.rawtime(0), (limit - ft).rawtime(0))
                    # and export to announce
                    self.meet.cmd_announce(u'timelimit', limit.rawtime(0))
            tot = 0
            placed = 0
            handled = 0
            ft = None
            for r in self.riders:
                tot += 1
                if r[COL_INRACE]:
                    bt = self.vbunch(r[COL_CBUNCH], r[COL_MBUNCH])
                    if ft is None:
                        ft = bt
                    if r[COL_PLACE]:
                        placed += 1
                        handled += 1
                    else:
                        if limit is not None:
                            if bt > limit:
                                r[COL_COMMENT] = u'otl'
                                handled += 1
                            else:  # and clear if not
                                if r[COL_COMMENT] == u'otl':
                                    r[COL_COMMENT] = u''
                else:
                    handled += 1
            if ft is not None:
                self.winbunch = ft
            if self.timerstat == u'finished' or handled == tot:
                self.racestat = u'final'
            else:
                if placed >= 10 or (placed > 0 and tot < 16):
                    self.racestat = u'provisional'
                else:
                    self.racestat = u'virtual'
        else:
            self.racestat = u'prerace'

        # if final places in view, update text entry
        curact = self.meet.action_model.get_value(
            self.meet.action_combo.get_active_iter(), 0).decode(u'utf-8')
        if curact == u'fin':
            self.meet.action_entry.set_text(self.places)
        _log.debug(u'Event status: %r', self.racestat)
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
                else:
                    self.resetcatonlaps()
                    if self.event[u'type'] in [
                            u'criterium', u'circuit', u'cross'
                    ]:
                        glib.idle_add(self.armlap)
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

    def elapsed_dlg(self, addriders=u''):
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

    def treerow_selected(self, treeview, path, view_column, data=None):
        """Select row, confirm only selected place"""
        # filter on running/armfinish
        if self.timerstat not in [u'idle', u'armstart', u'finished']:
            if self.finish is not None:
                rbib = self.riders[path][COL_BIB].decode(u'utf-8')
                _log.info(u'Confirmed next place by tree selection: %r/%r',
                          rbib, path)
                self.append_selected_place()

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
        _log.debug(u'menuitem: %r: %r', menuitem, change)
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

        # event run time attributes
        self.calcset = False
        self.start = None
        self.finish = None
        self.altfinish = None
        self.maxfinish = None
        self.showdowntimes = True
        self.showuciids = False
        self.showcats = False
        self.winbunch = None  # bunch time of winner (overall race time)
        self.winopen = True
        self.timerstat = u'idle'
        self.racestat = u'prerace'
        self.places = u''
        self.laptimes = []
        self.comment = []
        self.hidecols = []
        self.cats = []
        self.passingsource = []  # list of decoders we accept passings from
        self.targetlaps = False  # true if finish is det by target
        self.catlaps = {}  # cache of cat lap counts
        self.catstarts = {}  # cache of cat start times
        self.catplaces = {}
        self.autocats = False
        self.autostartlist = None
        self.bonuses = {}
        self.points = {}
        self.pointscb = {}
        self.ucicache = {}
        self.dofastestlap = False
        self.autoexport = False
        self.timelimit = None
        self.passlabels = {}  # sector labels for mult passings
        self.catonlap = {}  # onlap per category
        self.laplength = None
        self.clubmode = False
        self.gapthresh = GAPTHRESH  # time gap to set new time
        # NOTE: .12 usually added to account
        # for front wheel measurements
        self.curlap = -1
        self.onlap = 1
        self.totlaps = None
        self.lapstart = None
        self.lapfin = None
        self.minlap = MINPASSTIME  # minimum lap/elap time if relevant

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
            gobject.TYPE_STRING,  # SHORTNAME = 2
            gobject.TYPE_STRING,  # CAT = 3
            gobject.TYPE_STRING,  # COMMENT = 4
            gobject.TYPE_BOOLEAN,  # INRACE = 5
            gobject.TYPE_STRING,  # PLACE = 6
            gobject.TYPE_INT,  # LAP COUNT = 7
            gobject.TYPE_INT,  # SEED = 8
            gobject.TYPE_PYOBJECT,  # RFTIME = 9
            gobject.TYPE_PYOBJECT,  # CBUNCH = 10
            gobject.TYPE_PYOBJECT,  # MBUNCH =11 
            gobject.TYPE_PYOBJECT,  # STOFT = 12
            gobject.TYPE_PYOBJECT,  # BONUS = 13
            gobject.TYPE_PYOBJECT,  # PENALTY = 14
            gobject.TYPE_PYOBJECT)  # RFSEEN = 15

        uifile = os.path.join(metarace.UI_PATH, u'rms.ui')
        _log.debug(u'Building event interface from %r', uifile)
        b = gtk.Builder()
        b.add_from_file(uifile)

        self.frame = b.get_object(u'race_vbox')
        self.frame.connect(u'destroy', self.shutdown)

        # meta info pane
        self.shortname = None
        self.title_namestr = b.get_object(u'title_namestr')
        self.set_titlestr()
        self.elaplbl = b.get_object(u'time_lbl')
        self.lapentry = b.get_object(u'lapentry')
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
            uiutil.mkviewcolbool(t, u'In', COL_INRACE, width=50)
            uiutil.mkviewcoltxt(t,
                                u'Lap',
                                COL_LAPS,
                                width=40,
                                cb=self.editlap_cb)
            uiutil.mkviewcoltxt(t,
                                u'Seed',
                                COL_SEED,
                                width=40,
                                cb=self.editseed_cb)
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
            uiutil.mkviewcoltxt(t, u'Place', COL_PLACE, calign=0.5, width=50)
            t.show()
            b.get_object(u'race_result_win').add(t)

            b.connect_signals(self)
            b = gtk.Builder()
            b.add_from_file(os.path.join(metarace.UI_PATH, u'rms_context.ui'))
            self.context_menu = b.get_object(u'rms_context')
            self.view.connect(u'button_press_event',
                              self.treeview_button_press)
            self.view.connect(u'row-activated', self.treerow_selected)
            b.connect_signals(self)
            self.meet.timercb = self.timertrig
            self.meet.alttimercb = self.alttimertrig
