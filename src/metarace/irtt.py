
"""Individual road time trial handler for roadmeet."""

import gtk
import glib
import gobject
import pango
import os
import logging

import metarace
from metarace import tod
from metarace import unt4
from metarace import timy
from metarace import eventdb
from metarace import riderdb
from metarace import strops
from metarace import uiutil
from metarace import timerpane
from metarace import report
from metarace import jsonconfig

# rider commands
RIDER_COMMANDS_ORD = [ 'add', 'del', 'que', 'onc', 'dns', 'hd',
                   'dnf', 'dsq', 'com', '']
RIDER_COMMANDS = {'dns':'Did not start',
                   'dnf':'Did not finish',
                   'add':'Add starters',
                   'del':'Remove starters',
                   'que':'Query riders',
                   'com':'Add comment',
                   'hd':'Outside time limit',
                   'dsq':'Disqualify',
                   'onc':'Riders on course',
                   '':'',
                   }

RESERVED_SOURCES = ['fin',      # finished stage
                    'reg',      # registered to stage
                    'start']    # started stage

DNFCODES = ['hd', 'otl', 'dsq', 'dnf', 'dns']
STARTFUDGE = tod.tod(u'1:00')	# min elapsed	- lower for 2km inter

# startlist model columns
COL_BIB = 0
COL_SERIES = 1
COL_NAMESTR = 2
COL_CAT = 3
COL_COMMENT = 4
COL_WALLSTART = 5
COL_TODSTART = 6
COL_TODFINISH = 7
COL_TODPENALTY = 8
COL_PLACE = 9
COL_SHORTNAME = 10
COL_INTERA = 11
COL_INTERB = 12
COL_INTERC = 13
COL_INTERD = 14
COL_INTERE = 15
COL_LASTSEEN = 16
COL_ETA = 17
COL_PASS = 18

# scb function key mappings
key_startlist = 'F6'                 # clear scratchpad (FIX)
key_results = 'F4'                   # recalc/show results in scratchpad
key_starters = 'F3'                  # show next few starters in scratchpad

# timing function key mappings
key_armsync = 'F1'                   # arm for clock sync start
key_armstart = 'F5'                  # arm for start impulse
key_armfinish = 'F9'                 # arm for finish impulse
key_raceover = 'F10'                 # flag race completion/not provisional

# extended function key mappings
key_reset = 'F5'                     # + ctrl for clear/abort
key_falsestart = 'F6'		     # + ctrl for false start
key_abort_start = 'F7'		     # + ctrl abort A
key_abort_finish = 'F8'		     # + ctrl abort B
key_undo = 'Z'

# config version string
EVENT_ID = u'roadtt-2.0'

def jsob(inmap):
    """Return a json'able map."""
    ret = None
    if inmap is not None:
        ret = {}
        for key in inmap:
            if key in [u'minelap', u'maxelap']:
                ret[key] = inmap[key].rawtime()
            else:
                ret[key] = inmap[key]
    return ret

def unjsob(inmap):
    """Un-jsob the provided map."""
    ret = None
    if inmap is not None:
        ret = {}
        for key in inmap:
            if key in [u'minelap', u'maxelap']:
                ret[key] = tod.mktod(inmap[key])
            else:
                ret[key] = inmap[key]
    return ret

def sort_tally(x, y):
    """Points tally rough sort for stage tally."""
    if x[0] == y[0]:
        return cmp(100*y[3]+10*y[4]+y[5],
                   100*x[3]+10*x[4]+x[5])
    else:
        return cmp(y[0], x[0])

def sort_dnfs(x, y):
    """Sort dnf riders by code and riderno."""
    if x[2] == y[2]:	# same code
        if x[2]:
            return cmp(strops.bibstr_key(x[1]),
                       strops.bibstr_key(y[1]))
        else:
            return 0	# don't alter order on unplaced riders
    else:
        return strops.cmp_dnf(x[2], y[2])

class irtt(object):
    """Data handling for road time trial."""
    def key_event(self, widget, event):
        """Race window key press handler."""
        if event.type == gtk.gdk.KEY_PRESS:
            key = gtk.gdk.keyval_name(event.keyval) or 'None'
            if event.state & gtk.gdk.CONTROL_MASK:
                if key == key_reset:    # override ctrl+f5
                    #self.resetall()	-> too dangerous!!
                    return True
                elif key == key_falsestart:	# false start both lanes
                    #self.falsestart()
                    return True
                elif key == key_abort_start:	# abort start line
                    #self.abortstarter()
                    return True
                elif key == key_abort_finish:	# abort finish line
                    #self.abortfinisher()
                    return True
            if key[0] == 'F':
                if key == key_armstart:
                    self.armstart()
                    return True
                elif key == key_armfinish:
                    self.armfinish()
                    return True
                elif key == key_startlist:
                    self.meet.announce_clear()
                    self.doannounce = True
                    return True
                elif key == key_raceover:
                    self.set_finished()
                    return True
                elif key == key_results:
                    #self.showresults()
                    return True
        return False

    def resetall(self):
        #self.start = None
        #self.lstart = None
        #self.sl.toidle()
        #self.sl.disable()
        self.fl.toidle()
        self.fl.disable()
        #self.timerstat = 'idle'
        #self.meet.alttimer.dearm(0)	# 'unarm'
        #self.meet.alttimer.dearm(1)	# 'unarm'
        #uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Idle')
        #self.log.info('Reset to IDLE')

    def set_finished(self):
        """Update event status to finished."""
        if self.timerstat == 'finished':
            self.timerstat = 'running'
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Running')
        else:
            self.timerstat = 'finished'
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Finished')
            self.meet.stat_but.set_sensitive(False) 

    def armfinish(self):
        if self.timerstat == 'running':
            if self.fl.getstatus() != 'finish' and self.fl.getstatus() != 'armfin':
                self.fl.toarmfin()
                #bib = self.fl.bibent.get_text()
                #series = self.fl.serent.get_text()
                #i = self.getiter(bib, series)
                #if i is not None:
                    #self.announce_rider('', bib,
                                        #self.riders.get_value(i,COL_NAMESTR),
                                        #self.riders.get_value(i,COL_SHORTNAME),
                                        #self.riders.get_value(i,COL_CAT))
            else:
                self.fl.toidle()
                self.announce_rider()

    def armstart(self):
        if self.timerstat == 'idle':
            self.log.info('Armed for timing sync.')
            self.timerstat = 'armstart'
        elif self.timerstat == 'armstart':
            self.resetall()
        elif self.timerstat == 'running':
            if self.sl.getstatus() in ['armstart', 'running']:
                self.sl.toidle()
            elif self.sl.getstatus() != 'running':
                self.sl.toarmstart()

    def delayed_announce(self):
        """Re-announce all riders from the nominated category."""
        self.meet.announce_clear()
        heading = u''
        if self.timerstat == 'finished':	# THIS OVERRIDES RESIDUAL
            heading = u': Result'
        else:
            if self.racestat == u'prerace':
                heading = u''	# anything better?
            else:
                heading = u': Standings'
        #self.meet.scb.clrall()
### standings?
        self.meet.announce_title(self.title_namestr.get_text()+heading)
        #self.meet.scb.set_title(self.title_namestr.get_text())
        self.meet.cmd_announce(u'finstr', self.meet.get_short_name())
        cat = self.ridercat(self.curcat)
        for t in self.results[cat]:
            r = self.getiter(t.refid, t.index)
            if r is not None:
                et = self.getelapsed(r)
                bib = t.refid
                rank = self.riders.get_value(r, COL_PLACE)
                cat = self.riders.get_value(r, COL_CAT)
                namestr = self.riders.get_value(r, COL_NAMESTR)
                self.meet.announce_rider([rank,bib,namestr,cat,et.rawtime(2)])
        arrivalsec = self.arrival_report(0)	# fetch all arrivals
        if len(arrivalsec) > 0:
            arrivals = arrivalsec[0].lines
            for a in arrivals:
                self.meet.scb.add_rider(a, 'arrivalrow')
        return False

    def edit_event_properties(self, window, data=None):
        """Edit race specifics."""
        self.log.info(u'going on prop ' + repr(window))
        pass


    def wallstartstr(self, col, cr, model, iter, data=None):
        """Format start time into text for listview."""
        st = model.get_value(iter, COL_TODSTART)
        if st is not None:
            cr.set_property('text', st.timestr(2)) # time from tapeswitch
            cr.set_property('style', pango.STYLE_NORMAL)
        else:
            cr.set_property('style', pango.STYLE_OBLIQUE)
            wt = model.get_value(iter, COL_WALLSTART)
            if wt is not None:
                cr.set_property('text', wt.timestr(0)) # adv start
            else:
                cr.set_property('text', '')	# no info on start time

    def announce_rider(self, place='', bib='', namestr='', shortname='',
                        cat='', rt=None, et=None):
        """Emit a finishing rider to announce."""
        rts = ''
        if et is not None:
            rts = et.rawtime(2)
        elif rt is not None:
            rts = rt.rawtime(0)
        self.meet.scb.add_rider([place,bib,shortname,cat,rts], 'finpanel')
        self.meet.scb.add_rider([place,bib,namestr,cat,rts], 'finish')

    def geteta(self,iter):
        """Return a best guess rider's ET."""
        ret = self.getelapsed(iter)
        if ret is None:
            # scan each inter from farthest to nearest
            for ipt in [COL_INTERE, COL_INTERD, COL_INTERC, COL_INTERB, COL_INTERA]:
              if ipt in self.ischem and self.ischem[ipt] is not None:
                dist = self.ischem[ipt][u'dist']
                inter = self.riders.get_value(iter, ipt)
                if inter is not None and dist is not None:
                    totdist = 1000.0*self.meet.distance
                    st = self.riders.get_value(iter, COL_TODSTART)
                    if st is None: # defer to start time
                        st = self.riders.get_value(iter, COL_WALLSTART)
                    if st is not None:	# still none is error
                        et = inter - st
                        spd = (1000.0*dist)/float(et.timeval)
                        ret = tod.tod(str(totdist / spd))
                        self.riders.set_value(iter, COL_PASS, int(dist))
                        break
        return ret

    def getelapsed(self, iter, runtime=False):
        """Return a tod elapsed time."""
        ret = None
        ft = self.riders.get_value(iter, COL_TODFINISH)
        if ft is not None:
            st = self.riders.get_value(iter, COL_TODSTART)
            if st is None: # defer to start time
                st = self.riders.get_value(iter, COL_WALLSTART)
            if st is not None:	# still none is error
                pt = self.riders.get_value(iter, COL_TODPENALTY)
		# penalties are added into stage result - for consistency
                ret = (ft - st) + pt
        elif runtime:
            st = self.riders.get_value(iter, COL_TODSTART)
            if st is None: # defer to start time
                st = self.riders.get_value(iter, COL_WALLSTART)
            if st is not None:	# still none is error
                ret = tod.now() - st	# runtime increases!
        return ret

    def stat_but_clicked(self, button=None):
        """Deal with a status button click in the main container."""
        self.log.info('Stat button clicked.')

    def ctrl_change(self, acode='', entry=None):
        """Notify change in action combo."""
        pass
        # TODO?
        if acode == 'fin':
            pass
            #if entry is not None:
                #entry.set_text(self.places)
        #elif acode in self.intermeds:
            #if entry is not None:
                #entry.set_text(self.intermap[acode]['places'])
        else:
            if entry is not None:
                entry.set_text('')

    def race_ctrl(self, acode='', rlist=''):
        """Apply the selected action to the provided bib list."""
        if acode in self.intermeds:
            rlist = strops.reformat_bibserplacelist(rlist)
            if self.checkplaces(rlist, dnf=False):
                self.intermap[acode]['places'] = rlist
                self.placexfer()
                #self.intsprint(acode, rlist)
                self.log.info('Intermediate ' + repr(acode) + ' == '
                               + repr(rlist))
            else:
                self.log.error('Intermediate ' + repr(acode) + ' not updated.')
                return False
        elif acode == 'que':
            self.log.warn('Query rider not implemented - reannounce.')
            self.curcat = self.ridercat(rlist.strip())
            self.doannounce = True
        elif acode == 'del':
            rlist = strops.reformat_bibserlist(rlist)
            for bibstr in rlist.split():
                bib, ser = strops.bibstr2bibser(bibstr)
                self.delrider(bib, ser)
            return True
        elif acode == 'add':
            self.log.info('Add starter deprecated: Use startlist import.')
            rlist = strops.reformat_bibserlist(rlist)
            for bibstr in rlist.split():
                bib, ser = strops.bibstr2bibser(bibstr)
                self.addrider(bib, ser)
            return True
        elif acode == 'onc':
            #rlist = strops.reformat_bibserlist(rlist)
            #for bibstr in rlist.split():
                #self.add_starter(bibstr)
            return True
        elif acode == 'dnf':
            self.dnfriders(strops.reformat_bibserlist(rlist))
            return True
        elif acode == 'dsq':
            self.dnfriders(strops.reformat_bibserlist(rlist), 'dsq')
            return True
        elif acode == 'hd':
            self.dnfriders(strops.reformat_bibserlist(rlist), 'otl')
            return True
        elif acode == 'dns':
            self.dnfriders(strops.reformat_bibserlist(rlist), 'dns')
            return True
        elif acode == 'com':
            self.add_comment(rlist)
            return True
        else:
            self.log.error('Ignoring invalid action.')
        return False

    def add_comment(self, comment=''):
        """Append a race comment."""
        self.comment.append(comment.strip())
        self.log.info('Added race comment: ' + repr(comment))

    def elapstr(self, col, cr, model, iter, data=None):
        """Format elapsed time into text for listview."""
        ft = model.get_value(iter, COL_TODFINISH)
        if ft is not None:
            st = model.get_value(iter, COL_TODSTART)
            if st is None: # defer to wall start time
                st = model.get_value(iter, COL_WALLSTART)
                cr.set_property('style', pango.STYLE_OBLIQUE)
            else:
                cr.set_property('style', pango.STYLE_NORMAL)
            et = self.getelapsed(iter)
            if et is not None:
                cr.set_property('text', et.timestr(2))
            else:
                cr.set_property('text', '[ERR]')
        else:
            cr.set_property('text', '')

    def loadcats(self, cats=u''):
        if cats:
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
            self.cats.append(u'')   # always include one empty cat
            self.log.debug(u'Result category list updated: ' + repr(self.cats))
        self.catlaps = {}
        for c in self.cats: 
            lt = self.finishpass
            dbr = self.meet.rdb.getrider(c,u'cat')
            if dbr is not None:
                lt = strops.confopt_posint(self.meet.rdb.getvalue(dbr, riderdb.COL_CAT))
            self.catlaps[c] = lt
            self.log.debug(u'Set category ' + repr(c) + ' pass count to: ' + repr(lt))

    def loadconfig(self):
        """Load race config from disk."""
        self.riders.clear()
        self.results = {u'':tod.todlist(u'UNCAT')}
        self.cats = []

        cr = jsonconfig.config({u'event':{
                u'startlist':u'',
                u'id':EVENT_ID,
                u'start':u'0',
                u'comment':[],
                                        u'categories':[],
                                        u'arrivalcount':4,
                                        u'lstart':u'0',
                                        u'startgap':u'1:00',
                                        u'precision':1,
                                        u'autoexport':False,
                                        u'intermeds':[],
                                        u'contests':[],
                                        u'minelap':STARTFUDGE,
                                        u'sloppystart':False,
                                        u'startdelay':None,
                                        u'startloop':None,
                                        u'starttrig':None,
                                        u'finishloop':u'thbc',
                                        u'finishpass':None,
                                        u'interloops':{},
                                        u'tallys':[],
                                        u'onestartlist':True,
                                        u'timelimit':None,
                                        u'finished':False,
                                        u'showinter':None,
                                        u'intera':None,
                                        u'interb':None,
                                        u'interc':None,
                                        u'interd':None,
                                        u'intere':None,
                                       }})
        cr.add_section(u'event')
        cr.add_section(u'riders')

        # check for config file
        try:
            with open(self.configpath, 'rb') as f:
                cr.read(f)
        except Exception as e:
            self.log.error(u'Reading event config: ' + unicode(e))

        # load default gap
        self.startgap = tod.mktod(cr.get(u'event', u'startgap'))
        if self.startgap is None:
            self.startgap = tod.tod('1:00')

        # load result precision
        self.precision = strops.confopt_posint(cr.get(u'event', u'precision'))
        if self.precision > 2:	# posint forbids negatives
            self.precision = 2

        # load start delay for wireless impulse
        self.startdelay = tod.mktod(cr.get(u'event', u'startdelay'))
        if self.startdelay is None:
            self.startdelay = tod.ZERO

        # load minimum elapsed time
        self.minelap = tod.mktod(cr.get(u'event', u'minelap'))
        if self.minelap is None:
            self.minelap = STARTFUDGE
        self.timelimit = cr.get(u'event',u'timelimit')  # save as str
        
        # allow auto export
        self.autoexport = strops.confopt_bool(cr.get(u'event',
                                                      u'autoexport'))
        # sloppy start times
        self.sloppystart = strops.confopt_bool(cr.get(u'event',
                                                      u'sloppystart'))
        self.startloop = cr.get(u'event', u'startloop')
        self.starttrig = cr.get(u'event', u'starttrig')
        self.finishloop = cr.get(u'event', u'finishloop')
        self.finishpass = cr.get(u'event', u'finishpass')

        # load intermediate split schema
        self.showinter = strops.confopt_posint(cr.get(u'event',
                                                      u'showinter'),None)
        self.ischem[COL_INTERA] = unjsob(cr.get(u'event', u'intera'))
        self.ischem[COL_INTERB] = unjsob(cr.get(u'event', u'interb'))
        self.ischem[COL_INTERC] = unjsob(cr.get(u'event', u'interc'))
        self.ischem[COL_INTERD] = unjsob(cr.get(u'event', u'interd'))
        self.ischem[COL_INTERE] = unjsob(cr.get(u'event', u'intere'))
        self.interloops = cr.get(u'event', u'interloops')

        # load _result_ categories
        catlist = cr.get(u'event', u'categories')
        if u'AUTO' in catlist:  # ignore any others and re-load from rdb
            self.cats = self.meet.rdb.listcats()
            self.autocats = True
            for cat in self.cats:
                self.results[cat] = tod.todlist(cat)
                self.inters[COL_INTERA][cat] = tod.todlist(cat)
                self.inters[COL_INTERB][cat] = tod.todlist(cat)
                self.inters[COL_INTERC][cat] = tod.todlist(cat)
                self.inters[COL_INTERD][cat] = tod.todlist(cat)
                self.inters[COL_INTERE][cat] = tod.todlist(cat)
        else:
            self.autocats = False
            for cat in catlist:
                if cat != u'':
                    cat = cat.upper()
                    if cat not in self.cats:
                        self.cats.append(cat)
                        self.results[cat] = tod.todlist(cat)
                        self.inters[COL_INTERA][cat] = tod.todlist(cat)
                        self.inters[COL_INTERB][cat] = tod.todlist(cat)
                        self.inters[COL_INTERC][cat] = tod.todlist(cat)
                        self.inters[COL_INTERD][cat] = tod.todlist(cat)
                        self.inters[COL_INTERE][cat] = tod.todlist(cat)
                    else:
                        self.log.info(u'Duplicate Category Ignored: ' + repr(cat))
        self.cats.append(u'')   # always include one empty cat
        self.loadcats()

        # restore intermediates
        for i in cr.get(u'event', u'intermeds'):
            if i in RESERVED_SOURCES:
                self.log.info(u'Ignoring reserved intermediate: ' + repr(i))
            else:
                crkey = u'intermed_' + i
                descr = u''
                places = u''
                if cr.has_option(crkey, u'descr'):
                    descr = cr.get(crkey, u'descr')
                if cr.has_option(crkey, u'places'):
                    places = strops.reformat_placelist(
                                 cr.get(crkey, u'places'))
                if i not in self.intermeds:
                    self.log.debug(u'Adding intermediate: '
                                    + repr(i) + u':' + descr
                                    + u':' + places)
                    self.intermeds.append(i)
                    self.intermap[i] = {u'descr':descr, u'places':places}
                else:
                    self.log.info(u'Ignoring duplicate intermediate: '
                                   + repr(i))

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
                            self.log.info(u'Invalid bonus ' + repr(bstr)
                              + u' in contest ' + repr(i))
                            bt = tod.ZERO
                        bonuses.append(bt)
                self.contestmap[i][u'bonuses'] = bonuses
                points = []
                if cr.has_option(crkey, u'points'):
                    pliststr = cr.get(crkey, u'points').strip()
                    if pliststr and tally == u'': # no tally for these points!
                        self.log.error(u'No tally for points in contest: '
                                        + repr(i))
                        tallyset.add(u'')       # add empty placeholder
                    for pstr in pliststr.split():
                        pt = 0
                        try:
                            pt = int(pstr)
                        except:
                            self.log.info(u'Invalid points ' + repr(pstr)
                              + u' in contest ' + repr(i))
                        points.append(pt)
                self.contestmap[i][u'points'] = points
                allsrc = False          # all riders in source get same pts
                if cr.has_option(crkey, u'all_source'):
                    allsrc = strops.confopt_bool(cr.get(crkey, u'all_source'))
                self.contestmap[i][u'all_source'] = allsrc
            else:
                self.log.info(u'Ignoring duplicate contest: ' + repr(i))

            # check for invalid allsrc
            if self.contestmap[i][u'all_source']:
                if (len(self.contestmap[i][u'points']) > 1
                    or len(self.contestmap[i][u'bonuses']) > 1):
                    self.log.info(u'Ignoring extra points/bonus for '
                                  + u'all source contest ' + repr(i))

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
                self.points[i] = {}      # redundant, but ok
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

        # re-join any existing timer state -> no, just do a start
        self.set_syncstart(tod.mktod(cr.get(u'event', u'start')),
                           tod.mktod(cr.get(u'event', u'lstart')))

        # re-load starters/results
        self.onestart = False
        for rs in cr.get(u'event', u'startlist').split():
            (r, s) = strops.bibstr2bibser(rs)
            self.addrider(r, s)
            wst = None
            tst = None
            ft = None
            pt = None
            ima = None
            imb = None
            imc = None
            imd = None
            ime = None
            lpass = None
            pcnt = 0
            if cr.has_option(u'riders', rs):
                # bbb.sss = comment,wall_start,timy_start,finish,penalty,place
                nr = self.getrider(r, s)
                ril = cr.get(u'riders', rs)	# vec
                lr = len(ril)
                if lr > 0:
                    nr[COL_COMMENT] = ril[0]
                if lr > 1:
                    wst = tod.mktod(ril[1])
                if lr > 2:
                    tst = tod.mktod(ril[2])
                if lr > 3:
                    ft = tod.mktod(ril[3])
                if lr > 4:
                    pt = tod.mktod(ril[4])
                if lr > 6:
                    ima = tod.mktod(ril[6])
                if lr > 7:
                    imb = tod.mktod(ril[7])
                if lr > 8:
                    imc = tod.mktod(ril[8])
                if lr > 9:
                    imd = tod.mktod(ril[9])
                if lr > 10:
                    ime = tod.mktod(ril[10])
                if lr > 11:
                    pcnt = strops.confopt_posint(ril[11])
                if lr > 12:
                    lpass = tod.mktod(ril[12])
            nri = self.getiter(r, s)
            self.settimes(nri, wst, tst, ft, pt, doplaces=False)
            self.setpasses(nri, pcnt)
            self.setinter(nri, ima, COL_INTERA)
            self.setinter(nri, imb, COL_INTERB)
            self.setinter(nri, imc, COL_INTERC)
            self.setinter(nri, imd, COL_INTERD)
            self.setinter(nri, ime, COL_INTERE)
            self.riders.set_value(nri, COL_LASTSEEN, lpass)

        self.placexfer()

        self.comment = cr.get(u'event', u'comment')
        self.arrivalcount = strops.confopt_posint(cr.get(u'event',
                                                       u'arrivalcount'), 4)

        if strops.confopt_bool(cr.get(u'event', u'finished')):
            self.set_finished()
        self.onestartlist = strops.confopt_bool(cr.get(u'event',
                                                     u'onestartlist'))

        # After load complete - check config and report. This ensures
        # an error message is left on top of status stack. This is not
        # always a hard fail and the user should be left to determine
        # an appropriate outcome.
        eid = cr.get(u'event', u'id')
        if eid != EVENT_ID:
            self.log.error(u'Event configuration mismatch: '
                           + repr(eid) + u' != ' + repr(EVENT_ID))

    def saveconfig(self):
        """Save race to disk."""
        if self.readonly:
            self.log.error(u'Attempt to save readonly ob.')
            return
        cw = jsonconfig.config()

        # save basic race properties
        cw.add_section(u'event')
        if self.start is not None:
            cw.set(u'event', u'start', self.start.rawtime())
        if self.lstart is not None:
            cw.set(u'event', u'lstart', self.lstart.rawtime())
        cw.set(u'event', u'comment', self.comment)
        if self.startgap is not None:
            cw.set(u'event', u'startgap', self.startgap.rawtime(0))
        else:
            cw.set(u'event', u'startgap', None)
        if self.startdelay is not None:
            cw.set(u'event', u'startdelay', self.startdelay.rawtime())
        else:
            cw.set(u'event', u'startdelay', None)
        if self.minelap is not None:
            cw.set(u'event', u'minelap', self.minelap.rawtime())
        else:
            cw.set(u'event', u'minelap', None)

        cw.set(u'event', u'arrivalcount', self.arrivalcount)
        cw.set(u'event', u'sloppystart', self.sloppystart)
        cw.set(u'event', u'autoexport', self.autoexport)
        cw.set(u'event', u'startloop', self.startloop)
        cw.set(u'event', u'starttrig', self.starttrig)
        cw.set(u'event', u'finishloop', self.finishloop)
        cw.set(u'event', u'finishpass', self.finishpass)
        cw.set(u'event', u'onestartlist', self.onestartlist)
        cw.set(u'event', u'precision', self.precision)
        cw.set(u'event', u'timelimit', self.timelimit)

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

        # save intermediate split schema
        cw.set(u'event', u'intera', jsob(self.ischem[COL_INTERA]))
        cw.set(u'event', u'interb', jsob(self.ischem[COL_INTERB]))
        cw.set(u'event', u'interc', jsob(self.ischem[COL_INTERC]))
        cw.set(u'event', u'interd', jsob(self.ischem[COL_INTERD]))
        cw.set(u'event', u'intere', jsob(self.ischem[COL_INTERE]))
        cw.set(u'event', u'interloops', self.interloops)
        cw.set(u'event', u'showinter', self.showinter)

        # save riders
        cw.set(u'event', u'startlist', self.get_startlist())
        if self.autocats:
            cw.set(u'event', u'categories', [u'AUTO'])
        else:
            cw.set(u'event', u'categories', self.get_catlist())
        cw.add_section(u'riders')
        for r in self.riders:
            if r[COL_BIB] != '':
                bib = r[COL_BIB].decode('utf-8')
                ser = r[COL_SERIES].decode('utf-8')
                # place is saved for info only
                wst = u''
                if r[COL_WALLSTART] is not None:
                    wst = r[COL_WALLSTART].rawtime()
                tst = u''
                if r[COL_TODSTART] is not None:
                    tst = r[COL_TODSTART].rawtime()
                tft = u''
                if r[COL_TODFINISH] is not None:
                    tft = r[COL_TODFINISH].rawtime()
                tpt = u''
                if r[COL_TODPENALTY] is not None:
                    tpt = r[COL_TODPENALTY].rawtime()
                tima = u''
                if r[COL_INTERA] is not None:
                    tima = r[COL_INTERA].rawtime()
                timb = u''
                if r[COL_INTERB] is not None:
                    timb = r[COL_INTERB].rawtime()
                timc = u''
                if r[COL_INTERC] is not None:
                    timc = r[COL_INTERC].rawtime()
                timd = u''
                if r[COL_INTERD] is not None:
                    timd = r[COL_INTERD].rawtime()
                tine = u''
                if r[COL_INTERE] is not None:
                    tine = r[COL_INTERE].rawtime()
                pcnt = u''
                if r[COL_PASS] is not None:
                    pcnt = str(r[COL_PASS])
                lpass = u''
                if r[COL_LASTSEEN] is not None:
                    lpass = r[COL_LASTSEEN].rawtime()
                slice = [r[COL_COMMENT].decode('utf-8'),
                         wst, tst, tft, tpt, r[COL_PLACE],
                         tima, timb, timc, timd, tine,
                         pcnt, lpass]
                cw.set(u'riders', strops.bibser2bibstr(bib, ser), slice)
        cw.set(u'event', u'finished', self.timerstat == 'finished')
        cw.set(u'event', u'id', EVENT_ID)
        self.log.debug(u'Saving race config to: ' + repr(self.configpath))
        with open(self.configpath, 'wb') as f:
            cw.write(f)

    def get_ridercmdorder(self):
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
        """Return a list of bibs in the rider model as b.s."""
        ret = ''
        for r in self.riders:
            ret += ' ' + strops.bibser2bibstr(r[COL_BIB], r[COL_SERIES])
        return ret.strip()

    def shutdown(self, win=None, msg='Exiting'):
        """Terminate race object."""
        self.log.debug('Race Shutdown: ' + msg)
        #self.meet.timer.dearm()
        #self.meet.menu_race_properties.set_sensitive(False)
        if not self.readonly:
            self.saveconfig()
        self.winopen = False

    def do_properties(self):
        """Properties placeholder."""
        self.log.info('Properties callback.')
        pass

    def key_riderno(self, r):
        if r[1] is not None:
            return strops.riderno_key(r[1])
        else:
            return 86400	# maxval

    def key_starttime(self, r):
        if r[1] is not None:
            return int(r[1].truncate(0).timeval)
        else:
            return 86400	# maxval


    def reorder_signon(self):
        """Reorder riders for a sign on."""
        aux = []
        cnt = 0
        for r in self.riders:
            aux.append([cnt, r[COL_BIB]])
            cnt += 1
        if len(aux) > 1:
            aux.sort(key=self.key_riderno)
            self.riders.reorder([a[0] for a in aux])
        return cnt

    def reorder_startlist(self):
        """Reorder riders for a startlist."""
        aux = []
        cnt = 0
        for r in self.riders:
            aux.append([cnt, r[COL_WALLSTART]])
            cnt += 1
        if len(aux) > 1:
            aux.sort(key=self.key_starttime)
            self.riders.reorder([a[0] for a in aux])
        return cnt

    def signon_report(self):
        """Return a signon report."""
        ret = []
        sec = report.signon_list('signon')
        self.reorder_signon()
        for r in self.riders:
            cmt = r[COL_COMMENT].decode('utf-8')
            sec.lines.append([cmt,r[COL_BIB].decode('utf-8'),
                                   r[COL_NAMESTR].decode('utf-8')])
        ret.append(sec)
        return ret

#hh:mm:ss ______ -no rider------ cat
    def startlist_report(self):
        """Return a startlist report."""
        self.reorder_startlist()
        ret = []
        if len(self.cats) > 1 and not self.onestartlist:
            for c in self.cats:
                if c:
                    ret.extend(self.startlist_report_gen(c))
                    ret.append(report.pagebreak(0.05))
        else:
            ret = self.startlist_report_gen()
        return ret

    def startlist_report_gen(self, cat=None):
        catnamecache = {}
        catname = u''
        subhead = u''
        if cat is not None:
            dbr = self.meet.rdb.getrider(cat,u'cat')
            if dbr is not None:
                catname = self.meet.rdb.getvalue(dbr,
                                          riderdb.COL_FIRST)
                subhead = self.meet.rdb.getvalue(dbr,
                                          riderdb.COL_LAST)
        else:
            cat = u''   # match all riders

        if self.onestartlist:
            for rc in self.get_catlist():
                dbr = self.meet.rdb.getrider(rc,u'cat')
                if dbr is not None:
                    cname = self.meet.rdb.getvalue(dbr,
                                      riderdb.COL_FIRST) # already decode
                    if cname:
                        catnamecache[rc] = cname

        """Return a startlist report (rough style)."""
        ret = []
        sec = report.rttstartlist(u'startlist')
        sec.heading = 'Startlist'
        if catname:
            sec.heading += u': ' + catname
            sec.subheading = subhead
        rcnt = 0
        cat = self.ridercat(cat)
        lt = None
        for r in self.riders:
            rcat = r[COL_CAT].decode('utf-8').upper()
            if cat == u'' or rcat == cat:
                rcnt += 1
                ucicode = None
                ## replace with ridermap
                dbr = self.meet.rdb.getrider(r[COL_BIB],r[COL_SERIES])
                if dbr is not None:
                    ucicode = self.meet.rdb.getvalue(dbr,
                                             riderdb.COL_UCICODE)
                    nat = self.meet.rdb.getvalue(dbr,
                                             riderdb.COL_NOTE)[0:3].upper()
                    if nat:
                        ucicode = nat
                ## to here
                bstr = r[COL_BIB].decode('utf-8').upper()
                stxt = ''
                if r[COL_WALLSTART] is not None:
                    stxt = r[COL_WALLSTART].meridiem()
                    stxt = stxt.replace(u'am', u'\u2009am')
                    stxt = stxt.replace(u'pm', u'\u2009pm')
                    if lt is not None: 
                        if r[COL_WALLSTART] - lt > self.startgap:
                            sec.lines.append([None, None, None])	# GAP!!
                    lt = r[COL_WALLSTART]
                nstr = r[COL_NAMESTR]
                #if r[COL_CAT] == 'U23':
                    #ucicode += ' *'
                cstr = None
                if self.onestartlist and r[COL_CAT] != cat: # show if not
                    cstr = r[COL_CAT].decode('utf-8')
                    if cstr in catnamecache and len(catnamecache[cstr]) < 8:
                        cstr = catnamecache[cstr]
                sec.lines.append([stxt, bstr, nstr, ucicode, '____', cstr])
                if cstr in [u'MB', u'WB']:
                    # lookup pilot - series lookup
                    dbr = self.meet.rdb.getrider(r[COL_BIB],u'pilot')
                    if dbr is not None:
                        puci = self.meet.rdb.getvalue(dbr,
                                                      riderdb.COL_UCICODE)
                        pnam = strops.listname(
                           self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                           self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                           self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB))
                        sec.lines.append([u'', u'', pnam, puci, u'', u'pilot'])
                    
        ret.append(sec)
        if rcnt > 1:
            sec = report.bullet_text(u'ridercnt')
            sec.lines.append([u'', 'Total riders: ' + unicode(rcnt)])
            ret.append(sec)
        return ret

    def sort_arrival(self, model, i1, i2, data=None):
        """Sort model by all arrivals."""
        v1 = model.get_value(i1, COL_ETA)
        v2 = model.get_value(i2, COL_ETA)
        if v1 is not None and v2 is not None:
            return cmp(v1, v2)
        else:
            if v1 is None and v2 is None:
                return 0	# same
            else:	# nones are filtered in list traversal
                if v1 is None:
                    return 1
                else:
                    return -1

    def sort_finishtod(self, model, i1, i2, data=None):
        """Sort model on finish tod, then start time."""
        t1 = model.get_value(i1, COL_TODFINISH)
        t2 = model.get_value(i2, COL_TODFINISH)
        if t1 is not None and t2 is not None:
            return cmp(t1, t2)
        else:
            if t1 is None and t2 is None:
                # go by wall start
                s1 = model.get_value(i1, COL_WALLSTART)
                s2 = model.get_value(i2, COL_WALLSTART)
                if s1 is not None and s2 is not None:
                    return cmp(s1, s2)
                else:
                    if s1 is None and s2 is None:
                        return 0
                    elif s1 is None:
                        return 1
                    else:
                        return -1
            else:
                if t1 is None:
                    return 1
                else:
                    return -1

    def arrival_report(self, limit=0):
        """Return an arrival report."""
        ret = []
        ###return ret
        cmod = gtk.TreeModelSort(self.riders)
        cmod.set_sort_func(COL_TODFINISH, self.sort_arrival)
        cmod.set_sort_column_id(COL_TODFINISH, gtk.SORT_ASCENDING)
        sec = report.section(u'arrivals')
        intlbl = None
        if False:	# CHECK FOR INTERMEDIATE TRACKING
            intlbl = u'Intermediate'
        sec.heading = u'Riders On Course'
        sec.footer = u'* denotes projected finish time.'
        # this is probably not required until intermeds available

        sec.colheader = [None, None, None, u'Inter', u'Finish', u'Avg']
        sec.lines = []
        nowtime = tod.now()
        for r in cmod:
            plstr = r[COL_PLACE]
            bstr = r[COL_BIB].decode('utf-8')
            nstr = r[COL_NAMESTR].decode('utf-8')
            turnstr = u''
            ets = u''
            speedstr = u''
            rankstr = u''
            noshow = False
            cat = self.ridercat(r[COL_CAT].decode('utf-8'))
            i = self.getiter(r[COL_BIB], r[COL_SERIES])
            if plstr.isdigit():	# rider placed at finish
                ## only show for a short while
                until = r[COL_TODFINISH] + tod.tod(u'2:30')
                if nowtime < until:
                    et = self.getelapsed(i)
                    ets = et.rawtime(self.precision)
                    rankstr = u'(' + plstr + u'.)'
                    speedstr = u''
                    # cat distance should override this
                    if self.meet.distance is not None:
                        speedstr = et.speedstr(1000.0*self.meet.distance)
                else:
                    noshow = True
                    speedstr = u''
            elif r[COL_ETA] is not None:
                # append km mark if available
                if r[COL_PASS] > 0:
                    nstr += (u' @ km' + unicode(r[COL_PASS]))
                    self.log.debug(u'Check namestr: ' + repr(r[COL_PASS]) + u' / ' + nstr)
                # projected finish time
                ets = u'*' + r[COL_ETA].rawtime(self.precision)
                
            if self.showinter is not None and self.showinter in self.ischem and self.ischem[self.showinter] is not None:
                    # show time at the turnaround
                    trk = self.inters[self.showinter][cat].rank(r[COL_BIB], r[COL_SERIES])
                    if trk is not None:
                        tet = self.inters[self.showinter][cat][trk]	# assuming it is valid
                        tplstr = unicode(trk+1)
                        trankstr = u' (' + tplstr + u'.)'
                        turnstr = tet.rawtime(self.precision) + trankstr
                        if not speedstr:
                            # override speed from turn
                            speedstr = u''
                            dist = self.ischem[self.showinter][u'dist']
                            if dist is not None:
                                speedstr = tet.speedstr(1000.0*dist)
                    else:
                        pass
                        #self.log.debug(u'Invalid intermediate split: ' + bstr)

            #self.log.debug(repr([rankstr, bstr, nstr, turnstr, ets, speedstr]))
            if not noshow:
              if ets or speedstr:	# only add riders with an estimate
                sec.lines.append([rankstr, bstr, nstr, turnstr, ets, speedstr])

        if len(sec.lines) > 0:
            #if limit > 0 and len(sec.lines) > limit:
                #sec.lines = sec.lines[0:limit]
            ret.append(sec)
        return ret

    def camera_report(self):
        """Return a judges report."""
        ret = []
        cmod = gtk.TreeModelSort(self.riders)
        cmod.set_sort_func(COL_TODFINISH, self.sort_finishtod)
        cmod.set_sort_column_id(COL_TODFINISH, gtk.SORT_ASCENDING)
        lcount = 0
        count = 1
        sec = report.section()
        sec.heading = 'Judges Report'
        sec.colheader = ['Hit', None, None, 'Start', 'Fin', 'Net']

        for r in cmod:
            bstr = r[COL_BIB].decode('utf-8')
            nstr = r[COL_NAMESTR].decode('utf-8')
            plstr = r[COL_PLACE].decode('utf-8')
            rkstr = u''
            if plstr and plstr.isdigit():
                rk = int(plstr)
                if rk < 6:	# annotate top 5 places
                    rkstr = u'('+plstr+u'.)'
            sts = '-'
            if r[COL_TODSTART] is not None:
                sts = r[COL_TODSTART].rawtime(2)
            elif r[COL_WALLSTART] is not None:
                sts = r[COL_WALLSTART].rawtime(0) + '   '
            fts = '-'
            if r[COL_TODFINISH] is not None:
                fts = r[COL_TODFINISH].rawtime(2)
            i = self.getiter(r[COL_BIB], r[COL_SERIES])
            et = self.getelapsed(i)
            ets = '-'
            hits = ''
            if et is not None:
                ets = et.rawtime(self.precision)
                hits = unicode(count)
                if rkstr:
                    hits += u' '+rkstr 
                count += 1
            elif r[COL_COMMENT] != '':
                hits = r[COL_COMMENT].decode('utf-8')
            sec.lines.append([hits, bstr, nstr, sts, fts, ets, rkstr])
            lcount += 1
            if lcount % 10 == 0:
                sec.lines.append([None,None, None])

        ret.append(sec)
        return ret

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
                aux.append([self.points[tally][bib], strops.riderno_key(bib),
                           [None, r[COL_BIB], r[COL_NAMESTR],
                           strops.truncpad(str(self.pointscb[tally][bib]), 10,
                                                   elipsis=False),
                             None, str(self.points[tally][bib])],
                           self.pointscb[tally][bib]
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
                aux.append([self.bonuses[bib], strops.riderno_key(bib),
                       [None,r[COL_BIB],r[COL_NAMESTR],None,None,
                        str(int(self.bonuses[bib].truncate(0).timeval))],
                        0, 0, 0])
            aux.sort(sort_tally)
            for r in aux:
                sec.lines.append(r[2])
            ret.append(sec)
        return ret

    def catresult_report(self):
        """Return a categorised result report."""
        ret = []
        for cat in self.cats:
            if not cat:
                continue        # ignore empty and None cat
            ret.extend(self.single_catresult(cat))
        return ret

    def single_catresult(self, cat=''):
        ret = []
        catname = cat   # fallback emergency, cat is never '' here
        subhead = u''
        distance = self.meet.distance	# fall on meet dist
        dbr = self.meet.rdb.getrider(cat,u'cat')
        if dbr is not None:
            catname = self.meet.rdb.getvalue(dbr,
                                      riderdb.COL_FIRST) # already decode
            subhead = self.meet.rdb.getvalue(dbr,
                                      riderdb.COL_LAST)
            dist = self.meet.rdb.getvalue(dbr,
                                      riderdb.COL_REFID)
            try:
                distance = float(dist)
            except:
                self.log.warn(u'Invalid distance: ' + repr(dist)
                               + u' for cat ' + repr(cat))
        sec = report.section(cat+u'result')
        rsec = sec
        ret.append(sec)
        ct = None
        lt = None
        lpstr = None
        totcount = 0
        dnscount = 0
        dnfcount = 0
        hdcount = 0
        fincount = 0
        for r in self.riders:	# scan whole list even though cat are sorted.
            if cat == u'' or cat == self.ridercat(r[COL_CAT].decode('utf-8')):
                placed = False
                totcount += 1
                i = self.getiter(r[COL_BIB], r[COL_SERIES])
                ft = self.getelapsed(i)
                bstr = r[COL_BIB].decode('utf-8')
                nstr = r[COL_NAMESTR].decode('utf-8')
                cstr = u''
                if cat == u'':	# categorised result does not need cat
                    cstr = r[COL_CAT].decode('utf-8')
                # this should be inserted separately in specific reports
                ucicode = None
                ## replace with rmap lookup
                dbr = self.meet.rdb.getrider(r[COL_BIB],r[COL_SERIES])
                if dbr is not None:
                    ucicode = self.meet.rdb.getvalue(dbr,
                                             riderdb.COL_UCICODE)
                ## to here
                if ucicode:
                    ## overwrite category string
                    cstr = ucicode
                if ct is None:
                    ct = ft
                pstr = None
                if r[COL_PLACE] != '' and r[COL_PLACE].isdigit():
                    pstr = (r[COL_PLACE] + '.')
                    fincount += 1	# only count placed finishers
                    placed = True
                else:
                    pstr = r[COL_COMMENT]
                    # 'special' dnfs
                    if pstr == u'dns':
                        dnscount += 1
                    elif pstr in [u'hd', u'otl']:
                        hdcount += 1
                    else:
                        if pstr:	# commented dnf
                            dnfcount += 1
                    if pstr:
                        placed = True
                        if lpstr != pstr:
                            ## append an empty row
                            sec.lines.append([None, None, None,
                                              None, None, None])
                            lpstr = pstr
                tstr = None
                if ft is not None:
                    tstr = ft.rawtime(self.precision)
                dstr = None
                if ct is not None and ft is not None and ct != ft:
                    dstr = dstr = ('+' + (ft - ct).rawtime(1))
                if placed:
                    sec.lines.append([pstr, bstr, nstr, cstr, tstr, dstr])
                    if cat in [u'WB',u'MB']:  #also look up pilots
                        # lookup pilot - series lookup
                        dbr = self.meet.rdb.getrider(r[COL_BIB],u'pilot')
                        if dbr is not None:
                            puci = self.meet.rdb.getvalue(dbr,
                                                          riderdb.COL_UCICODE)
                            pnam = strops.listname(
                               self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                               self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                               self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB))
                            sec.lines.append([u'', u'pilot', pnam, puci, u'', u''])

        residual = totcount - (fincount + dnfcount + dnscount + hdcount)

        if self.timerstat == 'finished':	# THIS OVERRIDES RESIDUAL
            sec.heading = u'Result'
        else:
            if self.racestat == u'prerace':
                sec.heading = u''	# anything better?
            else:
                if residual > 0 :
                    sec.heading = u'Standings'
                else:
                    sec.heading = u'Provisional Result'

        # Race metadata / UCI comments
        sec = report.bullet_text(u'uci'+cat)
        if ct is not None:
            if distance is not None:
                avgprompt = u'Average speed of the winner: '
                if residual > 0:
                    avgprompt = u'Average speed of the leader: '
                sec.lines.append([None, avgprompt
                                    + ct.speedstr(1000.0*distance)])
        sec.lines.append([None,
                          u'Number of starters: '
                          + unicode(totcount-dnscount)])
        if hdcount > 0:
            sec.lines.append([None,
                          u'Riders finishing out of time limits: '
                          + unicode(hdcount)])
        if dnfcount > 0:
            sec.lines.append([None,
                          u'Riders abandoning the race: '
                          + unicode(dnfcount)])
        ret.append(sec)

        # finish report title manipulation
        if catname:
            rsec.heading += u': ' + catname
            rsec.subheading = subhead
            ret.append(report.pagebreak())
        return ret

    def result_report(self):
        """Return a race result report."""
        ret = []
        # dump results
        self.placexfer()	# ensure all cat places are filled
				# also re-announces!
        if self.timerstat == 'running':
            # until final, show last few
            ret.extend(self.arrival_report(self.arrivalcount))
        if len(self.cats) > 1:
            ret.extend(self.catresult_report())
        else:
            ret.extend(self.single_catresult())
        # dump comms info
        if len(self.comment) > 0:
            s = report.bullet_text('comms')
            s.heading = u'Decisions of the commissaires panel'
            for comment in self.comment:
                s.lines.append([None, comment])
            ret.append(s)
        return ret

    def startlist_gen(self, cat=''):
        """Generator function to export a startlist."""
        mcat = self.ridercat(cat)
        self.reorder_startlist()
        for r in self.riders:
            if mcat == '' or mcat == self.ridercat(r[COL_CAT]):
                start = ''
                if r[COL_WALLSTART] is not None:
                    start = r[COL_WALLSTART].rawtime(0)
                bib = r[COL_BIB]
                series = r[COL_SERIES]
                name = ''
                ## replace with rmap lookup
                dbr = self.meet.rdb.getrider(bib, series)
                if dbr is not None:
                    name = strops.listname(
                          self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                          self.meet.rdb.getvalue(dbr, riderdb.COL_LAST), None)
                ## to here
                cat = r[COL_CAT]
                yield [start, bib, series, name, cat]

    def lifexport(self):
        return []

    def get_elapsed(self):
        return None

    def result_gen(self, cat=''):
        """Generator function to export a final result."""
        self.placexfer()
        mcat = self.ridercat(cat)
        rcount = 0
        lrank = None
        lpl = None
        for r in self.riders:
            if mcat == '' or mcat == self.ridercat(r[COL_CAT]):
                i = self.getiter(r[COL_BIB], r[COL_SERIES])
                ft = self.getelapsed(i)
                if ft is not None:
                    ft = ft.truncate(2)	# RETAIN Hundredths
                bib = r[COL_BIB]
                crank = None
                rank = None
                if r[COL_PLACE].isdigit():
                    rcount += 1
                    rank = int(r[COL_PLACE])
                    if rank != lrank:
                        crank = rcount
                    else:
                        crank = lpl
                    lpl = crank
                    lrank = rank
                else:
                    crank = r[COL_COMMENT]
                extra = None
                if r[COL_WALLSTART] is not None:
                    extra = r[COL_WALLSTART]

                bonus = None
                penalty = None	# should stay none for IRTT - ft contains
				# any stage penalty
                yield [crank, bib, ft, bonus, penalty]

    def main_loop(self, cb):
        """Run callback once in main loop idle handler."""
        cb('')
        return False

    def set_syncstart(self, start=None, lstart=None):
        if start is not None:
            if lstart is None:
                lstart = start
            self.start = start
            self.lstart = lstart
            self.timerstat = 'running'
            uiutil.buttonchg(self.meet.stat_but, uiutil.bg_none, 'Running')
            self.log.info('Timer sync @ ' + start.rawtime(2))
            self.sl.toidle()
            self.fl.toidle()

    def rfidinttrig(self, lr, e):
        """Register Intermediate RFID crossing."""
        st = lr[COL_WALLSTART]
        if lr[COL_TODSTART] is not None:
            st = lr[COL_TODSTART]
        bib = lr[COL_BIB].decode(u'utf-8')
        series = lr[COL_SERIES].decode(u'utf-8')
        bibstr = strops.bibser2bibstr(bib, series)
        if st is not None and e > st and e-st > STARTFUDGE:
            if lr[COL_TODFINISH] is None:
                # Got a rider on course, find out where they _should_ be
                self.doannounce = True
                elap = e-st
                # find first matching split point
                split = None
                for isplit in self.interloops[e.source]:
                    minelap = self.ischem[isplit][u'minelap']
                    maxelap = self.ischem[isplit][u'maxelap']
                    if lr[isplit] is None:
                        if elap > minelap and elap < maxelap:
                            split = isplit
                            break

                if split is not None:
                    # save and announce arrival at intermediate
                    nri = self.getiter(bib, series)
                    rank = self.setinter(nri, e, split)
                    place = u'(' + unicode(rank + 1) + u'.)'
                    namestr = lr[COL_NAMESTR].decode('utf-8')
                    cat = lr[COL_CAT].decode('utf-8')
                    rcat = self.ridercat(cat)
                    # use cat field for split label
                    label = self.ischem[split][u'label']
                    rts = u''
                    rt = self.inters[split][rcat][rank]
                    if rt is not None:
                        rts = rt.rawtime(2)
                    #self.meet.scb.add_rider([place,bib,namestr,label,rts],
                                             #'ttsplit')
                    self.log.info(u'Intermediate ' + label + u': ' + place + u' ' + bibstr + u'@' + e.rawtime(2))
                    lr[COL_ETA] = self.geteta(nri)
                else:
                    self.log.info(u'No match found for intermediate: '
                                  + bibstr + u'@' + e.source + u'/' + e.rawtime(2))
            else:
                self.log.info(u'Intermediate ignoring finished rider: '
                                + bibstr + u'@' + e.source + u'/' + e.rawtime(2))
        else:
            self.log.info(u'Intermediate ignoring rider not on course: '
                           + bibstr + u'@' + e.source + u'/' + e.rawtime(2))
        return False

    def rfidstat(self, e):
        """Handle RFID status message."""
        self.log.info(u'Decoder ' + e.source + u': ' + e.refid)
        return False

    def start_by_rfid(self, lr, e):
        # step 1: if sloppystart is false, check for armed start channel
        if not self.sloppystart:
            if lr[COL_TODSTART] is not None:
                self.log.info(u'Started rider seen on start loop: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
                return False
            # compare wall and actual starts
            if lr[COL_WALLSTART] is not None:
                wv = lr[COL_WALLSTART].timeval
                ev = e.timeval
                if abs(wv - ev) > 5:	# differ by more than 5 secs
                    self.log.info(u'Using advertised start time ' 
                                  + lr[COL_WALLSTART].rawtime(0) + u' for: '
                                  + lr[COL_BIB] + u'@' + e.rawtime(2))
                    return False

        if lr[COL_TODFINISH] is not None:
            self.log.info(u'Finished rider seen on start loop: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
        else:
            self.log.info(u'Set start time: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
            i = self.getiter(lr[COL_BIB], lr[COL_SERIES])
            # self.riders.set_value(i, COL_TODSTART, e)
            # set_value corrupts the result list - perhaps
            # combine with get_value and settimes as below
            # settimes manipulates result list, but loses finish time
            self.settimes(i, tst=e)
        return False

    def setrftime(self, bib, rank, rftime, bonus=None):
        """Override rider result from CSV Data."""
        self.log.info(u'Set finish time from CSV: '
                                       + bib + u'@' + rftime.rawtime(2))
        i = self.getiter(bib, u'')
        self.settimes(i, tft=rftime)

    def setriderval(self, bib, rank, bunch, bonus=None):
        """Hook for CSV import - assume bunch holds elapsed only."""
        self.log.debug(u'Set rider val by elapsed time: '
                                      + bib + u'/' + bunch.rawtime(2))
        i = self.getiter(bib, u'')
        self.settimes(i, tst=tod.ZERO, tft=bunch)

    def finish_by_rfid(self, lr, e):
        if lr[COL_TODFINISH] is not None:
            self.log.info(u'Finished rider seen on finish loop: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
        else:
            if lr[COL_WALLSTART] is None and lr[COL_TODSTART] is None:
                self.log.error(u'No start time for rider at finish: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
            else:
                cat = self.ridercat(lr[COL_CAT])
                finishpass = self.finishpass	# load default
                if cat in self.catlaps:
                    finishpass = self.catlaps[cat]
                    self.log.debug(u'Loaded pass count ' + repr(finishpass) + u' for cat: ' + repr(cat))
                if finishpass is None:
                    st = lr[COL_WALLSTART]
                    if lr[COL_TODSTART] is not None:
                        st = lr[COL_TODSTART] # use tod if avail
                    if e > st + self.minelap:
                        self.log.info(u'Set finish time: '
                                       + lr[COL_BIB] + u'@' + e.rawtime(2))
                        i = self.getiter(lr[COL_BIB], lr[COL_SERIES])
                        self.settimes(i, tst=self.riders.get_value(i,
                                                COL_TODSTART), tft=e)
                    else:
                        self.log.info(u'Ignored early finish: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
                else:
                    lt = lr[COL_WALLSTART]
                    if lr[COL_TODSTART] is not None:
                        lt = lr[COL_TODSTART]
                    if lr[COL_LASTSEEN] is not None and lr[COL_LASTSEEN] > lt:
                        lt = lr[COL_LASTSEEN]
                    if e > lt + self.minelap:
                        lr[COL_PASS] += 1
                        nc = lr[COL_PASS]
                        if nc >= finishpass:
                            self.log.info(u'Set finish lap time: '
                                           + lr[COL_BIB] + u'@' + e.rawtime(2))
                            i = self.getiter(lr[COL_BIB], lr[COL_SERIES])
                            self.settimes(i, tst=self.riders.get_value(i,
                                                    COL_TODSTART), tft=e)
                        else:
                            self.log.info('Lap ' + str(nc) + ' passing: ' + lr[COL_BIB] + u'@' + e.rawtime(2))
                    else:
                        self.log.info(u'Ignored short lap: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
        # save the last seen for lap counting and wateva
        lr[COL_LASTSEEN] = e
        return False

    def rfidtrig(self, e):
        """Register RFID crossing."""
        self.meet.announce_timer(e, self.meet.timer)
        if e.refid in ['', '255']:  # Assume finish/cell trigger from decoder
            if self.starttrig is not None and e.source == self.starttrig:
                # remote start trigger from decoder box
                self.log.debug(u'Start trigger from decoder ' + repr(e.source))
                self.log.info(u'Start Trig: ' + e.rawtime())
                self.start_trig(e)
                return False
            # otherwise flow through
            self.log.debug(u'Finish trigger from decoder.')
            self.log.info(u'Trigger: ' + e.rawtime())
            return self.fin_trig(e)
        elif e.chan == u'STS':  # status message
            return self.rfidstat(e)

        # else this is rfid
        r = self.meet.rdb.getrefid(e.refid)
        if r is not None:
            bib = self.meet.rdb.getvalue(r, riderdb.COL_BIB)
            series = self.meet.rdb.getvalue(r, riderdb.COL_SERIES)
            lr = self.getrider(bib, series)
            if lr is not None:
                # distinguish a shared finish / start loop
                okfin = False
                st = lr[COL_WALLSTART]
                if lr[COL_TODSTART] is not None:
                    st = lr[COL_TODSTART]
                if st is not None and e > st and e-st > self.minelap:
                    okfin = True

                bibstr = strops.bibser2bibstr(lr[COL_BIB], lr[COL_SERIES])

                # switch on loop source mode
                if okfin and self.finishloop and e.source == self.finishloop:
                    return self.finish_by_rfid(lr, e)
                elif self.startloop and e.source == self.startloop:
                    return self.start_by_rfid(lr, e)
                elif e.source in self.interloops:
                    return self.rfidinttrig(lr, e)

                if lr[COL_TODFINISH] is not None:
                    self.log.info(u'Finished rider: '
                          + lr[COL_BIB] + u'@' + e.rawtime(2))
                    return False

                if self.fl.getstatus() not in ['armfin']:
                    st = lr[COL_WALLSTART]
                    if lr[COL_TODSTART] is not None:
                        st = lr[COL_TODSTART]
                    if st is not None and e > st and e-st > self.minelap:
                        self.fl.setrider(lr[COL_BIB], lr[COL_SERIES])
                        self.armfinish()
                        self.log.info(u'Finish armed for: ' + bibstr
                                      + u'@' + e.rawtime(3))
                    else:
                        self.log.info(u'Ignoring rider not on course: '
                                       + bibstr + u'@' + e.rawtime(3))
                else:
                    self.log.info(u'Finish channel blocked for: '
                                 + bib + u'.' + series + u'@' + e.rawtime(3))
            else:
                self.log.info(u'Rider not in race: ' + bib + u'.' + series
                                  + u'@' + e.rawtime(3))
        else:
            self.log.info(u'Unkown tag: ' + e.refid + u'@' + e.rawtime(1))
        return False

    def int_trig(self, t):
        """Register intermediate trigger."""
        # Just log
        self.log.info('Intermediate cell: ' + t.rawtime(2))

    def fin_trig(self, t):
        """Register finish trigger."""
        if self.timerstat == 'running':
            if self.fl.getstatus() == 'armfin':
                bib = self.fl.bibent.get_text()
                series = self.fl.serent.get_text()
                i = self.getiter(bib, series)
                if i is not None:
                    cat = self.ridercat(self.riders.get_value(i,COL_CAT))
                    self.curcat = cat
                    self.settimes(i, tst=self.riders.get_value(i,
                                                COL_TODSTART), tft=t)
                    self.fl.tofinish()
                    ft = self.getelapsed(i)
                    if ft is not None:
                        self.fl.set_time(ft.timestr(2))
                        rank = self.results[cat].rank(bib, series) + 1
                        self.announce_rider(str(rank), bib,
                              self.riders.get_value(i,COL_NAMESTR),
                              self.riders.get_value(i,COL_SHORTNAME),
                              cat,
                              et=ft)	# announce the raw elapsed time
                        # send a flush hint to minimise display lag
                        self.meet.cmd_announce(u'redraw',u'timer')
                    else:
                        self.fl.set_time('[err]')

                else:
                    self.log.error('Missing rider at finish')
                    self.sl.toidle()
            else:
                # log impuse to scratchpad
                pass
                #self.meet.scratch_log('Finish : '
                                    #+ t.index.ljust(6) + t.timestr(4))
        elif self.timerstat == 'armstart':
            self.set_syncstart(t)

    def start_trig(self, t):
        """Register start trigger."""
        if self.timerstat == 'running':
            # check lane to apply pulse.
            if self.sl.getstatus() == 'armstart':
                i = self.getiter(self.sl.bibent.get_text(),
                                 self.sl.serent.get_text())
                if i is not None:
                    nst = t - self.startdelay
                    self.settimes(i, tst=nst, doplaces=False)
                    self.sl.torunning()
                else:
                    self.log.error('Missing rider at start')
                    self.sl.toidle()
            else:
                # log impuse to scratchpad
                pass
                #self.meet.scratch_log('Start :  '
                                    #+ t.index.ljust(6) + t.timestr(4))
        elif self.timerstat == 'armstart':
            self.set_syncstart(t, tod.now())

    def timertrig(self, e):
        """Handle timer callback."""
        self.meet.announce_timer(e, self.meet.alttimer)
        chan = timy.chan2id(e.chan)
        if chan == timy.CHAN_START:
            self.start_trig(e)
        elif chan == timy.CHAN_FINISH:
            self.fin_trig(e)
        return False

    def on_start(self, curoft):
        # use search instead of lookup to avoid the tosw problem
        for r in self.riders:
            ws = r[COL_WALLSTART]
            if ws is not None:
                if curoft + tod.tod('30') == ws:
                    bib = r[COL_BIB].decode('utf-8')
                    ser = r[COL_SERIES].decode('utf-8')
                    self.log.info('pre-load starter: ' + repr(bib))
                    self.sl.setrider(bib, ser)
                    self.meet.cmd_announce(u'startline', bib)
                    break
                if curoft + tod.tod('5') == ws:
                    bib = r[COL_BIB].decode('utf-8')
                    ser = r[COL_SERIES].decode('utf-8')
                    self.log.info('Load starter: ' + repr(bib))
                    self.sl.setrider(bib, ser)
                    self.sl.toarmstart()
                    self.start_unload = ws + tod.tod('5')
                    break

    def timeout(self):
        """Update slow changing aspects of race."""
        if not self.winopen:
            return False
        if self.timerstat == 'running':
            nowoft = (tod.now() - self.lstart).truncate(0)
            if self.sl.getstatus() in ['idle','load']:
                if nowoft.timeval % 5 == tod.tod('0'):	# every five
                    self.on_start(nowoft)
            else:
                if nowoft == self.start_unload:
                    self.sl.toidle()

            # after manips, then re-set start time
            self.sl.set_time(nowoft.timestr(0))

            # if finish lane loaded, set the elapsed time
            if self.fl.getstatus() in ['load', 'running', 'armfin']:
                bib = self.fl.bibent.get_text()
                series = self.fl.serent.get_text()
                i = self.getiter(bib, series)
                if i is not None:
                    et = self.getelapsed(i, runtime=True)
                    self.fl.set_time(et.timestr(0))
                    self.announce_rider('', bib,
                                        self.riders.get_value(i,COL_NAMESTR),
                                        self.riders.get_value(i,COL_SHORTNAME),
                                        self.riders.get_value(i,COL_CAT),
                                        rt=et) # announce running time
        
        if self.doannounce:
            self.doannounce = False
            glib.idle_add(self.delayed_announce)
            if self.autoexport:
                glib.idle_add(self.doautoexport)
        return True

    def doautoexport(self, data=None):
        """Run an export process."""
        self.meet.menu_data_results_cb(None)
        return False

    def clearplaces(self):
        """Clear rider places."""
        aux = []
        count = 0
        for r in self.riders:
            r[COL_PLACE] = r[COL_COMMENT]
            aux.append([count, r[COL_BIB], r[COL_COMMENT]])
            count += 1
        if len(aux) > 1:
            aux.sort(sort_dnfs)
            self.riders.reorder([a[0] for a in aux])

    def getrider(self, bib, series=''):
        """Return temporary reference to model row."""
        ret = None
        for r in self.riders:
            if r[COL_BIB] == bib and r[COL_SERIES] == series:
                ret = r
                break
        return ret

    def starttime(self, start=None, bib='', series=''):
        """Adjust start time for the rider."""
        r = self.getrider(bib, series)
        if r is not None:
            r[COL_WALLSTART] = start
            #self.unstart(bib, series, wst=start)

    def delrider(self, bib='', series=''):
        """Delete the specificed rider from the race model."""
        i = self.getiter(bib, series)
        if i is not None:
            self.riders.remove(i)

    def addrider(self, bib='', series=''):
        """Add specified rider to race model."""
        if bib == '' or self.getrider(bib, series) is None:
            ## could be a rmap lookup here
            nr=[bib, series, '', '', '', None, None, None,
                             tod.ZERO, '', '', None, None,
                            None,None, None,None,None,0]
            dbr = self.meet.rdb.getrider(bib, series)
            if dbr is not None:
                nr[COL_NAMESTR] = strops.listname(
                      self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                      self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                      self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB))
                nr[COL_CAT] = self.meet.rdb.getvalue(dbr, riderdb.COL_CAT)
                nr[COL_SHORTNAME] = strops.fitname(
                      self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                      self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                      12)
            return self.riders.append(nr)
        else:
            return None

    def editcol_cb(self, cell, path, new_text, col):
        """Update value in edited cell."""
        new_text = new_text.strip()
        if col == COL_BIB:
            if new_text.isalnum():
                if self.getrider(new_text,
                                  self.riders[path][COL_SERIES]) is None:
                    self.riders[path][COL_BIB] = new_text
                    dbr = self.meet.rdb.getrider(new_text, self.series)
                    if dbr is not None:
                        nr[COL_NAMESTR] = strops.listname(
                              self.meet.rdb.getvalue(dbr, riderdb.COL_FIRST),
                              self.meet.rdb.getvalue(dbr, riderdb.COL_LAST),
                              self.meet.rdb.getvalue(dbr, riderdb.COL_CLUB))
                        nr[COL_CAT] = self.meet.rdb.getvalue(dbr, 
                                                   riderdb.COL_CAT)
        elif col == COL_PASS:
            if new_text.isdigit():
                self.riders[path][COL_PASS] = int(new_text)
                self.log.debug(u'Adjusted pass count: ' + repr(self.riders[path][COL_BIB]) + u' : ' + repr(self.riders[path][COL_PASS]))
        else:
            self.riders[path][col] = new_text.strip()

    def decode_limit(self, limitstr, elap=None):
        """Decode a limit and finish time into raw bunch time."""
        ret = None
        if limitstr:
            limit = None
            down = False
            if u'+' in limitstr:
                down = True
                limitstr = limitstr.replace(u'+',u'')
            if u'%' in limitstr:
                down = True
                if elap is not None:
                    try:
                        frac = 0.01*float(limitstr.replace(u'%',u''))
                        limit = tod.tod(int(frac * float(elap.as_seconds())))
                    except:
                        pass
            else:       # assume tod without sanity check
                limit = tod.mktod(limitstr)
                if limit is not None:
                    if elap is not None and limit < elap:
                        down = True # assume a time less than winner is down
                    else:    # assume raw bunch time, ignore elap
                        pass

            # assign limit discovered above, if possible
            if limit is not None:
                if down:
                    if elap is not None:
                        ret = elap + limit      # down time on finish
                else:
                    ret = limit
            if ret is None:
                self.log.warn(u'Unable to decode time limit: '
                              + repr(limitstr))
        return ret

    def placexfer(self):
        """Transfer places into model."""
        #note: clearplaces also transfers comments into rank col (dns,dnf)
        self.places = u''
        placelist = []
        self.clearplaces()
        count = 0
        for cat in self.cats:
            ft = None
            if len(self.results[cat]) > 0:
                ft = self.results[cat][0]
            limit = None
            if ft is not None and self.timelimit is not None:
                limit = self.decode_limit(self.timelimit, ft)
                if limit is not None:
                    self.log.info(u'Time limit: ' + self.timelimit
                                   + u' = ' + limit.rawtime(0)
                                   + u', +' + (limit-ft).rawtime(0))
            lt = None
            place = 1
            pcount = 0
            for t in self.results[cat]:
                np = t.refid
                if np in placelist:
                    self.log.error(u'Result for rider already in placelist: ' + repr(np))
                    # this is a bad fail - indicates duplicate category entry
                placelist.append(t.refid)
                i = self.getiter(t.refid, t.index)
                if i is not None:
                    if lt is not None:
                        if lt != t:
                            place = pcount + 1
                    if limit is not None and t > limit:
                        self.riders.set_value(i, COL_PLACE, u'otl')
                        self.riders.set_value(i, COL_COMMENT, u'otl')
                    else:
                        self.riders.set_value(i, COL_PLACE, str(place))
                    j = self.riders.get_iter(count)
                    self.riders.swap(j, i)
                    count += 1
                    pcount += 1
                    lt = t
                else:
                    self.log.error('Extra result for rider' 
                                + strops.bibser2bibstr(t.refid, t.index))

        # check counts for racestat
        self.racestat = u'prerace'
        fullcnt = len(self.riders)
        placed = 0
        for r in self.riders:
            if r[COL_PLACE] and r[COL_PLACE] in [u'dns', u'dnf', u'dsq']:
                r[COL_ETA] = None
            else:
                i = self.getiter(r[COL_BIB], r[COL_SERIES])
                r[COL_ETA] = self.geteta(i)
                #self.log.debug(u'Set ETA: ' + repr(r[COL_ETA]))
                # if start set:
                # transfer out finish time to ETA
                # or use inters to establish ETA
            if r[COL_PLACE]:
                placed += 1
        self.log.debug(u'placed = ' + unicode(placed) + ', total = '
                                    + unicode(fullcnt))
        self.log.debug(u'place list = ' + repr(placelist))
        if placed > 0:
            if placed < fullcnt:
                self.racestat = u'virtual'
            else:
                self.places = u' '.join(placelist)
                if self.timerstat == u'finished':
                    self.racestat = u'final'
                else:
                    self.racestat = u'provisional' 
        self.log.debug(u'Racestat set to: ' + repr(self.racestat))

        # pass two: compute any intermediates
        self.bonuses = {}       # bonuses are global to stage
        for c in self.tallys:   # points are grouped by tally
            self.points[c] = {}
        for c in self.contests:
            self.assign_places(c)

        self.doannounce = True

    def get_placelist(self):
        """Return place list."""
        # assume this follows a place sorting.
        fp = None
        ret = ''
        for r in self.riders:
            if r[COL_PLACE]:
                #bibstr = strops.bibser2bibstr(r[COL_BIB], r[COL_SERIES])
                bibstr = r[COL_BIB]	# bibstr will fail later on
                if r[COL_PLACE] != fp:
                    ret += ' ' + bibstr
                else:
                    ret += '-' + bibstr
        return ret

    def get_starters(self):
        """Return a list of riders that 'started' the race."""
        ret = []
        for r in self.riders:
            if r[COL_COMMENT] != 'dns':
                ret.append(r[COL_BIB])
        return ' '.join(ret)

    def assign_places(self, contest):
        """Transfer points and bonuses into the named contest."""
        ## ERROR bib.ser will fail!
        # fetch context meta infos
        src = self.contestmap[contest]['source']
        if src not in RESERVED_SOURCES and src not in self.intermeds:
            self.log.info('Invalid intermediate source: ' + repr(src)
                           + ' in contest: ' + repr(contest))
            return
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
            placestr = self.get_placelist()
            self.log.info('Using placestr = ' + repr(placestr))
        elif src == 'reg':
            placestr = self.get_startlist()
        elif src == 'start':
            placestr = self.get_starters()
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
                        self.log.error('Invalid rider in place string.')
                        break
                        #self.addrider(bib)
                        #r = self.getrider(bib)
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
                                self.pointscb[tally][bib] = [0, 0, 0]
                            # No countback for all_source entries
                    else:       # points/bonus as per config
                        if len(bonuses) >= curplace:    # bonus is vector
                            if bib in self.bonuses:
                                self.bonuses[bib] += bonuses[curplace-1]
                            else:
                                self.bonuses[bib] = bonuses[curplace-1]
                        if tally and len(points) >= curplace: # points vector
                            if bib in self.points[tally]:
                                self.points[tally][bib] += points[curplace-1]
                            else:
                                self.points[tally][bib] = points[curplace-1]
                                self.pointscb[tally][bib] = [0, 0, 0]
                            if curplace <= 3:    # countback on 1-3
                                self.pointscb[tally][bib][curplace-1] += 1
                else:
                    self.log.warn('Duplicate no. = ' + str(bib) + ' in '
                                    + repr(contest) + ' places.')

    def getiter(self, bib, series=''):
        """Return temporary iterator to model row."""
        i = self.riders.get_iter_first()
        while i is not None:
            if self.riders.get_value(i,
                     COL_BIB) == bib and self.riders.get_value(i,
                     COL_SERIES) == series:
                break
            i = self.riders.iter_next(i)
        return i

    #def unstart(self, bib='', series='', wst=None):
        #"""Register a rider as not yet started."""
        #idx = strops.bibser2bibstr(bib, series)
        #self.unstarters[idx] = wst

    #def oncourse(self, bib='', series=''):
        #"""Remove rider from the not yet started list."""
        #pass
        #idx = strops.bibser2bibstr(bib, series)
        #if idx in self.unstarters:
            #del(self.unstarters[idx])
        
    def dnfriders(self, biblist='', code='dnf'):
        """Remove each rider from the race with supplied code."""
        recalc = False
        for bibstr in biblist.split():
            bib, ser = strops.bibstr2bibser(bibstr)
            r = self.getrider(bib, ser)
            if r is not None:
                r[COL_COMMENT] = code
                nri = self.getiter(bib, ser)
                self.settimes(nri, doplaces=False)
                recalc = True
            else:
                self.log.warn('Unregistered Rider '
                               + str(bibstr) + ' unchanged.')
        if recalc:
            self.placexfer()
        return False

    def setinter(self, iter, imed=None, inter=None):
        """Update the intermediate time for this rider and return rank."""
        bib = self.riders.get_value(iter, COL_BIB)
        series = self.riders.get_value(iter, COL_SERIES)
        cat = self.ridercat(self.riders.get_value(iter, COL_CAT))
        ret = None
        
        # fetch handles
        res = self.inters[inter][cat]

        # clear result for this bib
        res.remove(bib, series)

        # save intermed tod to rider model
        self.riders.set_value(iter, inter, imed)
        tst = self.riders.get_value(iter, COL_TODSTART)
        wst = self.riders.get_value(iter, COL_WALLSTART)

        # determine start time
        if imed is not None:
            if tst is not None:		# got a start trigger
                res.insert(imed - tst, bib, series)
                ret = res.rank(bib, series)
            elif wst is not None:	# start on wall time
                res.insert(imed - wst, bib, series)
                ret = res.rank(bib, series)
            else:
                self.log.error('No start time for intermediate '
                                + strops.bibser2bibstr(bib, series))
        return ret

    def setpasses(self, iter, passes=None):
        """Set rider pass count."""
        self.riders.set_value(iter, COL_PASS, passes)

    def settimes(self, iter, wst=None, tst=None, tft=None, pt=None,
                  doplaces=True):
        """Transfer race times into rider model."""
        bib = self.riders.get_value(iter, COL_BIB)
        series = self.riders.get_value(iter, COL_SERIES)
        cat = self.ridercat(self.riders.get_value(iter, COL_CAT))
        #self.log.debug('Check: ' + repr(bib) + ', ' + repr(series)
                        #+ ', ' + repr(cat))

        # clear result for this bib
        self.results[cat].remove(bib, series)

        # assign tods
        if wst is not None:	# Don't clear a set wall start time!
            self.riders.set_value(iter, COL_WALLSTART, wst)
        else:
            wst = self.riders.get_value(iter, COL_WALLSTART)
        #self.unstart(bib, series, wst)	# reg ignorer
        # but allow others to be cleared no worries
        oft = self.riders.get_value(iter, COL_TODFINISH)
        self.riders.set_value(iter, COL_TODSTART, tst)
        self.riders.set_value(iter, COL_TODFINISH, tft)

        if pt is not None:	# Don't clear penalty either
            self.riders.set_value(iter, COL_TODPENALTY, pt)
        else:
            pt = self.riders.get_value(iter, COL_TODPENALTY)

        # save result
        if tft is not None:
            self.onestart = True
            if tst is not None:		# got a start trigger
                self.results[cat].insert((tft - tst).truncate(self.precision) + pt, bib, series)
            elif wst is not None:	# start on wall time
                self.results[cat].insert((tft - wst).truncate(self.precision) + pt, bib, series)
            else:
                self.log.error('No start time for rider '
                                + strops.bibser2bibstr(bib, series))
        elif tst is not None:
            #self.oncourse(bib, series)	# started but not finished
            pass

        # if reqd, do places
        if doplaces and oft != tft:
            self.placexfer()

    def bibent_cb(self, entry, tp):
        """Bib entry callback."""
        bib = tp.bibent.get_text().strip()
        series = tp.serent.get_text().strip()
        namestr = self.lanelookup(bib, series)
        if namestr is not None:
            tp.biblbl.set_text(self.lanelookup(bib, series))
            tp.toload()
    
    def tment_cb(self, entry, tp):
        """Manually register a finish time."""
        thetime = tod.mktod(entry.get_text())
        if thetime is not None:
            bib = tp.bibent.get_text().strip()
            series = tp.serent.get_text().strip()
            if bib != '':
                self.armfinish()
                self.meet.alttimer.trig(thetime, chan=1, index='MANU')
                entry.set_text('')
                tp.grab_focus()
        else:
            self.log.error('Invalid finish time.')

    def lanelookup(self, bib=None, series=''):
        """Prepare name string for timer lane."""
        rtxt = None
        r = self.getrider(bib, series)
        if r is None:
            self.log.info('Non starter specified: ' + repr(bib))
        else:
            rtxt = strops.truncpad(r[COL_NAMESTR], 35)
        return rtxt
        
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
            self.riders.set_value(sel[1],COL_COMMENT,u'')
            self.settimes(sel[1])	# clear iter to empty vals
            self.log_clear(self.riders.get_value(sel[1],
                                       COL_BIB).decode('utf-8'),
                           self.riders.get_value(sel[1],
                                       COL_SERIES).decode('utf-8'))

    def now_button_clicked_cb(self, button, entry=None):
        """Set specified entry to the current time."""
        if entry is not None:
            entry.set_text(tod.now().timestr())

    def tod_context_edit_activate_cb(self, menuitem, data=None):
        """Run edit time dialog."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]	# grab off row iter and read in cur times
            tst = self.riders.get_value(i, COL_TODSTART)
            tft = self.riders.get_value(i, COL_TODFINISH)
            tpt = self.riders.get_value(i, COL_TODPENALTY)

            # prepare text entry boxes
            st = ''
            if tst is not None:
                st = tst.timestr()
            ft = ''
            if tft is not None:
                ft = tft.timestr()
            bt = ''
            pt = '0'
            if tpt is not None:
                pt = tpt.timestr()

            # run the dialog
            (ret, st, ft, bt, pt) = uiutil.edit_times_dlg(self.meet.window,
                                                      st, ft, bt, pt,
                                           bonus=True, penalty=True)
            if ret == 1:
                stod = tod.mktod(st)
                ftod = tod.mktod(ft)
                ptod = tod.mktod(pt)
                if ptod is None:
                    ptod = tod.ZERO
                bib = self.riders.get_value(i, COL_BIB)
                series = self.riders.get_value(i, COL_SERIES)
                self.settimes(i, tst=stod, tft=ftod, pt=ptod) # update model
                self.log.info('Race times manually adjusted for rider '
                               + strops.bibser2bibstr(bib, series))
            else:
                self.log.info('Edit race times cancelled.')

    def tod_context_del_activate_cb(self, menuitem, data=None):
        """Delete selected row from race model."""
        sel = self.view.get_selection().get_selected()
        if sel is not None:
            i = sel[1]	# grab off row iter
            self.settimes(i) # clear times
            if self.riders.remove(i):
                pass	# re-select?

    def log_clear(self, bib, series):
        """Print clear time log."""
        self.log.info('Time cleared for rider ' + strops.bibser2bibstr(bib, series))

    def title_close_clicked_cb(self, button, entry=None):
        """Close and save the race."""
        self.meet.close_event()

    def set_titlestr(self, titlestr=None):
        """Update the title string label."""
        if titlestr is None or titlestr == '':
            titlestr = 'Individual Road Time Trial'
        self.title_namestr.set_text(titlestr)

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

    def ridercat(self, cat):
        """Return a category from the result for the riders cat."""
        ret = u''	# default is the 'None' category - uncategorised
        checka = cat.upper()
        if checka in self.results:
            ret = checka
        #self.log.debug('ridercat read ' + repr(cat) + '/' + repr(checka)
                          #+ '  Returned: ' + repr(ret))
        return ret

    def get_catlist(self):
        """Return the ordered list of categories."""
        rvec = []
        for cat in self.cats:
            if cat != '':
                rvec.append(cat)
        return rvec

    def __init__(self, meet, event, ui=True):
        """Constructor."""
        self.meet = meet
        self.event = event
        self.evno = event[u'evid']
        self.configpath = meet.event_configfile(self.evno)

        self.log = logging.getLogger('irtt')
        self.log.setLevel(logging.DEBUG)
        self.log.debug(u'opening irtt event: ' + unicode(self.evno))

        # properties
        self.sloppystart = False
        self.autoexport = False
        self.finishloop = None
        self.startloop = None
        self.starttrig = None
        self.precision = 2
        self.finishpass = None

        # race run time attributes
        self.onestart = False
        self.readonly = not ui
        self.winopen = True
        self.timerstat = 'idle'
        self.racestat = u'prerace'
        self.start = None
        self.lstart = None
        self.start_unload = None
        self.startgap = None
        self.startdelay = tod.ZERO
        self.cats = []	# the ordered list of cats for results
        self.autocats = False
        self.results = {u'':tod.todlist(u'UNCAT')}
        self.inters = {}
        self.ischem = {}
        self.showinter = None
        for im in [COL_INTERA, COL_INTERB, COL_INTERC, COL_INTERD, COL_INTERE]:
            self.inters[im] = {u'':tod.todlist(u'UNCAT')}
            self.ischem[im] = None
        self.interloops = {}	# map of loop ids to inter splits
        self.curfintod = None
        self.doannounce = False
        self.onestartlist = False
        self.curcat = u''
        self.catlaps = {}
        self.comment = []
        self.places = u''

        self.bonuses = {}
        self.points = {}
        self.pointscb = {}

        ## these have to go!
        self.intermeds = []     # sorted list of intermediate keys
        self.intermap = {}      # map of intermediate keys to results
        self.contests = []      # sorted list of contests
        self.contestmap = {}    # map of contest keys
        self.tallys = []        # sorted list of points tallys
        self.tallymap = {}      # map of tally keys

        self.riders = gtk.ListStore(gobject.TYPE_STRING,   # 0 bib
                                    gobject.TYPE_STRING,   # 1 series
                                    gobject.TYPE_STRING,   # 2 namestr
                                    gobject.TYPE_STRING,   # 3 cat
                                    gobject.TYPE_STRING,   # 4 comment
                                    gobject.TYPE_PYOBJECT, # 5 wstart
                                    gobject.TYPE_PYOBJECT, # 6 tstart
                                    gobject.TYPE_PYOBJECT, # 7 finish
                                    gobject.TYPE_PYOBJECT, # 8 penalty
                                    gobject.TYPE_STRING,   # 9 place
                                    gobject.TYPE_STRING,   # 10 shortname
                                    gobject.TYPE_PYOBJECT,   # 11 intermediate
                                    gobject.TYPE_PYOBJECT,   # 12 intermediate
                                    gobject.TYPE_PYOBJECT,   # 13 intermediate
                                    gobject.TYPE_PYOBJECT,   # 14 intermediate
                                    gobject.TYPE_PYOBJECT,   # 15 intermediate
                                    gobject.TYPE_PYOBJECT,   # 16 last seen
                                    gobject.TYPE_PYOBJECT,   # 17 eta
                                    gobject.TYPE_INT)   # 18 pass count

        b = gtk.Builder()
        b.add_from_file(os.path.join(metarace.UI_PATH, u'irtt.ui'))

        self.frame = b.get_object('race_vbox')
        self.frame.connect('destroy', self.shutdown)

        # meta info pane
        self.title_namestr = b.get_object('title_namestr')
        self.set_titlestr()

        # Timer Panes
        mf = b.get_object('race_timer_pane')
        self.sl = timerpane.timerpane('Start Line', doser=False)
        self.sl.disable()
        self.sl.bibent.connect('activate', self.bibent_cb, self.sl)
        self.sl.serent.connect('activate', self.bibent_cb, self.sl)
        self.fl = timerpane.mantimerpane('Finish Line', doser=False)
        self.fl.disable()
        self.fl.bibent.connect('activate', self.bibent_cb, self.fl)
        self.fl.serent.connect('activate', self.bibent_cb, self.fl)
        self.fl.tment.connect('activate', self.tment_cb, self.fl)
        mf.pack_start(self.sl.frame)
        mf.pack_start(self.fl.frame)
        mf.set_focus_chain([self.sl.frame, self.fl.frame, self.sl.frame])

        # Result Pane
        t = gtk.TreeView(self.riders)
        self.view = t
        t.set_reorderable(True)
        t.set_rules_hint(True)
        t.connect('button_press_event', self.treeview_button_press)
     
        # TODO: show team name & club but pop up for rider list
        uiutil.mkviewcolbibser(t)
        uiutil.mkviewcoltxt(t, 'Rider', COL_NAMESTR, expand=True)
        uiutil.mkviewcoltxt(t, 'Cat', COL_CAT, self.editcol_cb)
        uiutil.mkviewcoltxt(t, 'Passes', COL_PASS, self.editcol_cb)
# -> Add in start time field with edit!
        uiutil.mkviewcoltod(t, 'Start', cb=self.wallstartstr)
        uiutil.mkviewcoltod(t, 'Time', cb=self.elapstr)
        uiutil.mkviewcoltxt(t, 'Rank', COL_PLACE, halign=0.5, calign=0.5)
        t.show()
        b.get_object('race_result_win').add(t)
        self.context_menu = None

        # show window
        if ui:
            b.connect_signals(self)
            b = gtk.Builder()
            b.add_from_file(os.path.join(metarace.UI_PATH, u'tod_context.ui'))
            self.context_menu = b.get_object('tod_context')
            b.connect_signals(self)
            self.meet.alttimer.armlock()   # lock the arm to capture all hits
            self.meet.alttimer.arm(0)	# start line
            self.meet.alttimer.arm(1)	# finish line (primary)
            self.meet.alttimer.arm(2)	# use for backup trigger
            self.meet.alttimer.arm(3)	# use for backup trigger
            self.meet.alttimer.delaytime('0.01')
            self.meet.timer.setcb(self.rfidtrig)
            self.meet.alttimer.setcb(self.timertrig)
